import base64
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from scripts import reviewer_gh
from scripts.reviewer_gh import (
    API,
    EXPECTED_PERMISSIONS,
    Paths,
    ReviewerGhError,
    check_installation,
    get_token,
    main,
    make_jwt,
    revoke_cached,
    urllib_http,
)

REPO = "AkkiaLab/AkkiaBot"
MOCK_GH_TOKEN = "ghs_fake-token-value"  # mock: 人工构造的不可用测试值，不是凭据

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl")


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    app_dir = tmp_path / "app"
    app_dir.mkdir(mode=0o700)
    key = app_dir / "private-key.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    (app_dir / "app.json").write_text(json.dumps({"app_id": 42, "client_id": "Iv-test"}))
    return Paths(app_dir=app_dir, cache_dir=tmp_path / "cache")


class FakeGitHub:
    """Records calls and answers like the GitHub API does for a healthy install."""

    def __init__(self, *, selection: str = "selected", repos: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.minted = 0
        self.permissions = dict(EXPECTED_PERMISSIONS)
        self.selection = selection
        self.repos = repos if repos is not None else [REPO]
        self.events: list[str] = []

    def __call__(self, method: str, url: str, bearer: str, body: dict[str, Any] | None) -> Any:
        self.calls.append((method, url, bearer, body))
        path = url.removeprefix(API)
        if path == f"/repos/{REPO}/installation":
            return {"id": 7}
        if path == "/app/installations/7/access_tokens":
            self.minted += 1
            return {
                "token": f"{MOCK_GH_TOKEN}-{self.minted}",
                "expires_at": _iso(time.time() + 3600),
                "permissions": self.permissions,
                "repository_selection": "selected",
            }
        if path == "/app":
            return {"slug": "akkiabot-reviewer", "events": self.events}
        if path == "/app/installations/7":
            return {
                "account": {"login": "AkkiaLab"},
                "repository_selection": self.selection,
                "permissions": self.permissions,
            }
        if path == "/installation/repositories":
            return {"repositories": [{"full_name": r} for r in self.repos]}
        if path == "/installation/token" and method == "DELETE":
            return None
        raise AssertionError(f"unexpected call {method} {path}")


# ---------------------------------------------------------------- JWT


def test_jwt_is_signed_by_the_app_key(paths: Paths, tmp_path: Path) -> None:
    now = int(time.time())
    jwt = make_jwt("Iv-test", paths.private_key, now)
    header, payload, signature = jwt.split(".")
    assert json.loads(_b64decode(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64decode(payload))
    assert claims == {"iat": now - 60, "exp": now + 540, "iss": "Iv-test"}

    pub = tmp_path / "pub.pem"
    sig = tmp_path / "sig.bin"
    subprocess.run(
        ["openssl", "pkey", "-in", str(paths.private_key), "-pubout", "-out", str(pub)],
        check=True,
        capture_output=True,
    )
    sig.write_bytes(_b64decode(signature))
    verify = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig)],
        input=f"{header}.{payload}".encode(),
        capture_output=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr


def test_rejects_world_readable_key(paths: Paths) -> None:
    paths.private_key.chmod(0o644)
    with pytest.raises(ReviewerGhError, match="chmod 600"):
        make_jwt("Iv-test", paths.private_key, int(time.time()))


def test_missing_key_is_reported_without_traceback(paths: Paths) -> None:
    paths.private_key.unlink()
    with pytest.raises(ReviewerGhError, match="找不到 App 私钥"):
        make_jwt("Iv-test", paths.private_key, int(time.time()))


def test_issuer_falls_back_to_app_id(paths: Paths) -> None:
    paths.app_json.write_text(json.dumps({"app_id": 42}))
    assert reviewer_gh.load_app_issuer(paths) == "42"


# ---------------------------------------------------------------- token cache


def test_mints_token_scoped_to_repo_and_caches_privately(paths: Paths) -> None:
    gh = FakeGitHub()
    record, from_cache = get_token(paths, REPO, gh)
    assert not from_cache
    assert record["token"] == f"{MOCK_GH_TOKEN}-1"
    mint = next(c for c in gh.calls if c[0] == "POST")
    assert mint[3] == {"repositories": ["AkkiaBot"]}
    # The JWT, not a token, authenticates the installation lookup and mint.
    assert all(c[2].count(".") == 2 for c in gh.calls)
    assert stat.S_IMODE(paths.token_cache.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.cache_dir.stat().st_mode) == 0o700


def test_reuses_fresh_cached_token(paths: Paths) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    record, from_cache = get_token(paths, REPO, gh)
    assert from_cache
    assert record["token"] == f"{MOCK_GH_TOKEN}-1"
    assert gh.minted == 1


@pytest.mark.parametrize("remaining", [-10, 0, reviewer_gh.MIN_REMAINING - 1])
def test_refreshes_token_close_to_expiry(paths: Paths, remaining: int) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    cached = json.loads(paths.token_cache.read_text())
    cached["expires_at"] = _iso(time.time() + remaining)
    paths.token_cache.write_text(json.dumps(cached))

    record, from_cache = get_token(paths, REPO, gh)
    assert not from_cache
    assert record["token"] == f"{MOCK_GH_TOKEN}-2"


def test_refresh_flag_and_corrupt_or_foreign_cache_mint_new_token(paths: Paths) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    assert not get_token(paths, REPO, gh, refresh=True)[1]
    paths.token_cache.write_text("{not json")
    assert not get_token(paths, REPO, gh)[1]
    cached = json.loads(paths.token_cache.read_text())
    cached["repo"] = "someone/else"
    paths.token_cache.write_text(json.dumps(cached))
    assert not get_token(paths, REPO, gh)[1]
    assert gh.minted == 4


def test_revoke_deletes_token_and_cache(paths: Paths) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    assert revoke_cached(paths, gh)
    assert gh.calls[-1][:3] == ("DELETE", f"{API}/installation/token", f"{MOCK_GH_TOKEN}-1")
    assert not paths.token_cache.exists()
    assert not revoke_cached(paths, gh)


# ---------------------------------------------------------------- check


def test_check_passes_and_never_prints_tokens(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert check_installation(paths, REPO, gh, refresh=False) == []
    out = capsys.readouterr()
    assert "akkiabot-reviewer[bot]" in out.out
    assert MOCK_GH_TOKEN not in out.out + out.err
    # The unrestricted probe token is revoked right after use.
    assert gh.calls[-1][:3] == ("DELETE", f"{API}/installation/token", f"{MOCK_GH_TOKEN}-2")


@pytest.mark.parametrize(
    ("tweak", "fragment"),
    [
        (lambda g: setattr(g, "selection", "all"), "安装范围"),
        (lambda g: g.repos.append("AkkiaLab/other"), "安装范围"),
        (lambda g: g.permissions.update(contents="read"), "contents"),
        (lambda g: g.permissions.update(administration="write"), "多余权限"),
        (lambda g: g.events.append("push"), "webhook"),
    ],
)
def test_check_reports_misconfiguration(paths: Paths, tweak: Any, fragment: str) -> None:
    gh = FakeGitHub()
    tweak(gh)
    problems = check_installation(paths, REPO, gh, refresh=False)
    assert any(fragment in p for p in problems), problems


# ---------------------------------------------------------------- CLI


def test_http_refuses_non_github_urls() -> None:
    with pytest.raises(ReviewerGhError, match="拒绝"):
        urllib_http("GET", "https://example.com/x", "jwt", None)


def test_cli_has_no_command_that_prints_the_token(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["token"])
    assert "invalid choice" in capsys.readouterr().err


def test_cli_gh_requires_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gh"]) == 2
    assert "用法" in capsys.readouterr().err


def test_cli_gh_execs_gh_with_token_in_env(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = FakeGitHub()
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/bin/sh\n")
    fake_gh.chmod(0o700)
    monkeypatch.setenv("AKKIABOT_REVIEWER_APP_DIR", str(paths.app_dir))
    monkeypatch.setenv("AKKIABOT_REVIEWER_CACHE_DIR", str(paths.cache_dir))
    monkeypatch.setenv("REVIEWER_GH_BIN", str(fake_gh))
    monkeypatch.setenv("GITHUB_TOKEN", "should-be-dropped")
    monkeypatch.setattr(reviewer_gh, "urllib_http", gh)
    seen: dict[str, Any] = {}

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        seen.update(path=path, argv=argv, env=env)

    monkeypatch.setattr(os, "execve", fake_execve)
    assert main(["gh", "pr", "review", "1", "--approve"]) == 0
    assert seen["argv"] == [str(fake_gh), "pr", "review", "1", "--approve"]
    assert seen["env"]["GH_TOKEN"] == f"{MOCK_GH_TOKEN}-1"
    assert "GITHUB_TOKEN" not in seen["env"]


def test_unwritable_cache_degrades_to_minting_each_time(
    paths: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    readonly = Paths(app_dir=paths.app_dir, cache_dir=blocker / "cache")
    gh = FakeGitHub()
    assert get_token(readonly, REPO, gh)[0]["token"] == f"{MOCK_GH_TOKEN}-1"
    assert get_token(readonly, REPO, gh)[0]["token"] == f"{MOCK_GH_TOKEN}-2"
    err = capsys.readouterr().err
    assert "无法写入 token 缓存" in err
    assert MOCK_GH_TOKEN not in err
