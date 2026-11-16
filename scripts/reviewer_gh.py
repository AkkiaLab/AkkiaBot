#!/usr/bin/env python3
"""Run ``gh`` as the ``akkiabot-reviewer`` GitHub App.

The Reviewer agent needs an identity distinct from the implementers'
(``AkkiaS7``) so it can approve and merge their pull requests. This script
mints a short-lived *installation token* for the GitHub App and hands it to
``gh`` through ``GH_TOKEN``:

1. sign an RS256 JWT with the App private key (via ``openssl``; the key is
   never loaded into Python and never leaves its file);
2. look up the App installation on the target repository;
3. exchange the JWT for an installation token restricted to that repository;
4. cache the token (0600) and reuse it until it is close to expiry, then mint
   a new one automatically.

Only the standard library and ``openssl`` are required, so the installed copy
runs with a bare ``python3`` outside the uv workspace.

Usage::

    reviewer-gh check                 # verify install scope + permissions, no secrets printed
    reviewer-gh gh pr review 12 -R AkkiaLab/AkkiaBot --approve
    reviewer-gh revoke                # revoke the cached token and delete the cache

There is deliberately no command that prints the token: agent runtimes capture
stdout into run logs, so the token only ever travels through ``GH_TOKEN`` into
the ``gh`` process.

Files (override with environment variables):

* ``$AKKIABOT_REVIEWER_APP_DIR`` (default ``~/.config/akkiabot/reviewer-app``):
  ``app.json`` (``{"app_id": ..., "client_id": ...}``) and ``private-key.pem``.
* ``$AKKIABOT_REVIEWER_CACHE_DIR`` (default ``~/.cache/akkiabot/reviewer-app``):
  ``token.json``.
* ``$AKKIABOT_REVIEWER_REPO`` (default ``AkkiaLab/AkkiaBot``).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API = "https://api.github.com"
DEFAULT_REPO = "AkkiaLab/AkkiaBot"
#: Repositories the App may be installed on (BOT-40): the product repository and
#: the public release-flow drill repository. A token is always restricted to the
#: single repository picked with ``AKKIABOT_REVIEWER_REPO``.
ALLOWED_REPOS: tuple[str, ...] = (DEFAULT_REPO, "AkkiaLab/akkiabot-release-drill")
# Reuse a cached token only while it has at least this many seconds left, so
# a long review never runs with a token that expires half way through.
MIN_REMAINING = 15 * 60
# Permissions the App is expected to hold on the installation (BOT-3).
EXPECTED_PERMISSIONS: Mapping[str, str] = {
    "actions": "read",
    "checks": "read",
    "contents": "write",
    "metadata": "read",
    "pull_requests": "write",
    "statuses": "read",
}

# (method, url, bearer, json body) -> parsed JSON response
Http = Callable[[str, str, str, dict[str, Any] | None], Any]


class ReviewerGhError(Exception):
    """A user-facing failure. Messages never contain key material or tokens."""


@dataclass(frozen=True)
class Paths:
    app_dir: Path
    cache_dir: Path

    @property
    def app_json(self) -> Path:
        return self.app_dir / "app.json"

    @property
    def private_key(self) -> Path:
        return self.app_dir / "private-key.pem"

    @property
    def token_cache(self) -> Path:
        return self.cache_dir / "token.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Paths:
        home = Path(env.get("HOME") or Path.home())
        return cls(
            app_dir=Path(
                env.get("AKKIABOT_REVIEWER_APP_DIR") or home / ".config/akkiabot/reviewer-app"
            ),
            cache_dir=Path(
                env.get("AKKIABOT_REVIEWER_CACHE_DIR") or home / ".cache/akkiabot/reviewer-app"
            ),
        )


# ---------------------------------------------------------------- JWT


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _ensure_private(path: Path) -> None:
    """Refuse key files readable by group/others, like ssh does."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise ReviewerGhError(
            f"找不到 App 私钥：{path}（见 docs/reviewer-github-app.md）"
        ) from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ReviewerGhError(f"{path} 权限过宽（{stat.filemode(mode)}），请执行 chmod 600")


def load_app_issuer(paths: Paths) -> str:
    """Return the JWT ``iss`` claim: the client ID, falling back to the App ID."""
    try:
        data = json.loads(paths.app_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReviewerGhError(
            f"找不到 {paths.app_json}（见 docs/reviewer-github-app.md）"
        ) from None
    issuer = data.get("client_id") or data.get("app_id")
    if not issuer:
        raise ReviewerGhError(f"{paths.app_json} 中缺少 client_id / app_id")
    return str(issuer)


def make_jwt(issuer: str, key_path: Path, now: int) -> str:
    """Build an RS256 app JWT. ``openssl`` reads the key; Python never does."""
    _ensure_private(key_path)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    # iat is backdated to tolerate clock drift; GitHub caps exp at 10 minutes.
    claims = {"iat": now - 60, "exp": now + 9 * 60, "iss": issuer}
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    openssl = shutil.which("openssl")
    if openssl is None:
        raise ReviewerGhError("需要 openssl 来签名 JWT，但未在 PATH 中找到")
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [openssl, "dgst", "-sha256", "-sign", str(key_path)],
        input=signing_input,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ReviewerGhError("openssl 签名 JWT 失败，请检查私钥文件是否完整")
    return f"{header}.{payload}.{_b64url(proc.stdout)}"


# ---------------------------------------------------------------- HTTP


def urllib_http(method: str, url: str, bearer: str, body: dict[str, Any] | None) -> Any:
    if not url.startswith(API + "/"):
        raise ReviewerGhError(f"拒绝请求非 GitHub API 地址：{url}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - https only, checked above
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "akkiabot-reviewer-gh")
    req.add_header("Authorization", f"Bearer {bearer}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("message", "")
        except ValueError:
            message = ""
        path = url.removeprefix(API)
        raise ReviewerGhError(f"GitHub API {method} {path} 返回 {exc.code}：{message}") from None
    except urllib.error.URLError as exc:
        raise ReviewerGhError(f"无法连接 GitHub API：{exc.reason}") from None


# ---------------------------------------------------------------- token


def _parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp()


def read_cache(path: Path, repo: str, now: float) -> dict[str, Any] | None:
    """Return the cached token record if it is for ``repo`` and still fresh."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("repo") != repo or not record.get("token"):
            return None
        if _parse_ts(record["expires_at"]) - now < MIN_REMAINING:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return record


def write_cache(path: Path, record: dict[str, Any]) -> None:
    """Atomically write the cache with 0700 dir / 0600 file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def mint_token(paths: Paths, repo: str, http: Http, now: float) -> dict[str, Any]:
    jwt = make_jwt(load_app_issuer(paths), paths.private_key, int(now))
    installation = http("GET", f"{API}/repos/{repo}/installation", jwt, None)
    installation_id = installation["id"]
    resp = http(
        "POST",
        f"{API}/app/installations/{installation_id}/access_tokens",
        jwt,
        {"repositories": [repo.split("/", 1)[1]]},
    )
    return {
        "repo": repo,
        "installation_id": installation_id,
        "token": resp["token"],
        "expires_at": resp["expires_at"],
        "permissions": resp.get("permissions", {}),
        "repository_selection": resp.get("repository_selection"),
    }


def get_token(
    paths: Paths,
    repo: str,
    http: Http,
    *,
    refresh: bool = False,
    now: Callable[[], float] = time.time,
) -> tuple[dict[str, Any], bool]:
    """Return ``(record, from_cache)``; mint and cache a new token when needed."""
    if not refresh:
        cached = read_cache(paths.token_cache, repo, now())
        if cached is not None:
            return cached, True
    record = mint_token(paths, repo, http, now())
    try:
        write_cache(paths.token_cache, record)
    except OSError as exc:
        # e.g. a sandboxed runtime that cannot write outside its workdir: the
        # token still works, we just mint a fresh one next time.
        print(f"reviewer-gh: 无法写入 token 缓存（{exc.strerror}），本次不缓存", file=sys.stderr)
    return record, False


# ---------------------------------------------------------------- commands


def check_installation(paths: Paths, repo: str, http: Http, *, refresh: bool) -> list[str]:
    """Print a secret-free report and return the list of problems found."""
    record, from_cache = get_token(paths, repo, http, refresh=refresh)
    jwt = make_jwt(load_app_issuer(paths), paths.private_key, int(time.time()))
    app = http("GET", f"{API}/app", jwt, None)
    installation = http("GET", f"{API}/app/installations/{record['installation_id']}", jwt, None)
    # The cached token is restricted to ``repo``; listing with it would always
    # show just ``repo``. Mint a throwaway metadata-only token, unrestricted by
    # repository, to see what the installation can really reach.
    probe = http(
        "POST",
        f"{API}/app/installations/{record['installation_id']}/access_tokens",
        jwt,
        {"permissions": {"metadata": "read"}},
    )
    try:
        repos = http("GET", f"{API}/installation/repositories", probe["token"], None)
    finally:
        http("DELETE", f"{API}/installation/token", probe["token"], None)

    repo_names = sorted(r["full_name"] for r in repos.get("repositories", []))
    permissions: dict[str, str] = installation.get("permissions", {})
    print(f"App:              {app.get('slug')} (bot 身份 {app.get('slug')}[bot])")
    print(f"Installation:     {record['installation_id']} @ {installation['account']['login']}")
    print(
        f"安装范围:         {installation.get('repository_selection')} -> {', '.join(repo_names)}"
    )
    print(f"权限:             {json.dumps(permissions, sort_keys=True)}")
    print(f"token 过期时间:   {record['expires_at']}（{'复用缓存' if from_cache else '新签发'}）")

    problems: list[str] = []
    if (
        installation.get("repository_selection") != "selected"
        or repo not in repo_names
        or not set(repo_names) <= set(ALLOWED_REPOS)
    ):
        problems.append(
            f"安装范围应为 selected、包含 {repo} 且只在 {list(ALLOWED_REPOS)} 之内，"
            f"实际为 {installation.get('repository_selection')} {repo_names}"
        )
    for name, level in EXPECTED_PERMISSIONS.items():
        if permissions.get(name) != level:
            problems.append(f"权限 {name} 应为 {level}，实际为 {permissions.get(name)}")
    extra = sorted(set(permissions) - set(EXPECTED_PERMISSIONS))
    if extra:
        problems.append(f"存在多余权限：{extra}")
    if app.get("events"):
        problems.append(f"App 不应订阅 webhook 事件：{app.get('events')}")
    return problems


def revoke_cached(paths: Paths, http: Http) -> bool:
    """Revoke the cached token (if any) and delete the cache file."""
    try:
        record = json.loads(paths.token_cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    try:
        if record.get("token"):
            http("DELETE", f"{API}/installation/token", record["token"], None)
    finally:
        paths.token_cache.unlink(missing_ok=True)
    return True


def find_gh(env: Mapping[str, str]) -> str:
    candidates = [
        env.get("REVIEWER_GH_BIN"),
        shutil.which("gh"),
        str(Path.home() / ".local/bin/gh"),
    ]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise ReviewerGhError("找不到 gh 可执行文件（可用 REVIEWER_GH_BIN 指定）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reviewer-gh", description="以 akkiabot-reviewer GitHub App 身份运行 gh"
    )
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，强制签发新 token")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="验证安装范围、权限并取得 token（不输出任何凭据）")
    sub.add_parser("revoke", help="吊销缓存中的 token 并删除缓存")
    gh = sub.add_parser("gh", help="以 App 身份执行 gh，其余参数原样传给 gh")
    gh.add_argument("gh_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    paths = Paths.from_env(env)
    repo = env.get("AKKIABOT_REVIEWER_REPO") or DEFAULT_REPO
    try:
        if repo not in ALLOWED_REPOS:
            raise ReviewerGhError(
                f"AKKIABOT_REVIEWER_REPO 只能是 {', '.join(ALLOWED_REPOS)}，实际为 {repo}"
            )
        if args.command == "check":
            problems = check_installation(paths, repo, urllib_http, refresh=args.refresh)
            for problem in problems:
                print(f"问题：{problem}", file=sys.stderr)
            print("检查通过" if not problems else "检查未通过")
            return 1 if problems else 0
        if args.command == "revoke":
            print("已吊销缓存的 token" if revoke_cached(paths, urllib_http) else "没有缓存的 token")
            return 0
        gh_args = list(args.gh_args)
        if gh_args[:1] == ["--"]:
            gh_args = gh_args[1:]
        if not gh_args:
            raise ReviewerGhError("用法：reviewer-gh gh <gh 参数...>")
        record, _ = get_token(paths, repo, urllib_http, refresh=args.refresh)
        gh_bin = find_gh(env)
        env["GH_TOKEN"] = record["token"]
        env.pop("GITHUB_TOKEN", None)
        os.execve(gh_bin, [gh_bin, *gh_args], env)  # noqa: S606 - replaces this process with gh
    except ReviewerGhError as exc:
        print(f"reviewer-gh: {exc}", file=sys.stderr)
        return 2
    return 0  # pragma: no cover - execve does not return


if __name__ == "__main__":
    sys.exit(main())
