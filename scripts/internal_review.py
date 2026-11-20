"""Private pre-review (预审) tooling for pushes to the public repository.

Workflow (docs/secret-scanning.md, "推送前的内部预审"): implementers push to the
private repository ``AkkiaLab/AkkiaBot-internal`` first and open a private PR;
the reviewer agent reviews it and records the verdict as a Multica issue comment
line ``预审通过：AkkiaLab/AkkiaBot-internal@<40-hex sha>`` (or ``预审撤销：…``). Only
that exact sha may then be pushed to the public repository.

Commands (stdlib only, ``git`` and the ``multica`` CLI on the host):

  check-push      pre-commit ``preapproval-check`` hook. Pushes not aimed at the public
                  repository, and pushes adding no commit the public remote lacks, pass.
                  Otherwise the issue number is parsed from the branch name, the
                  reviewer's latest verdict is read with the multica CLI and the pushed
                  head must equal the approved sha. Also requires the ref guard below.
  ref-guard       Reads the complete git pre-push input (installed as
                  ``hooks/pre-push.legacy``, which pre-commit's hook runs with the full
                  stdin). A push to the public repository that carries new commits must
                  update exactly one ref: pre-commit hands its hooks (gitleaks,
                  preapproval) only one ref, so other refs would go unchecked.
  install-guard   Install that ``pre-push.legacy`` wrapper (``just setup`` runs it).
  sync            Fast-forward every public ``releases/*`` branch into the private
                  repository (public -> private only, never forced).

Host configuration lives in git config, never in this public repository:

  akkia.preapproval.reviewer  (multi-valued) Multica agent ids whose verdicts count
  akkia.internal.remote       name of the private remote (default ``internal``)

Messages never print comment bodies, remote URLs or raw git stderr (a URL may carry
credentials); only issue numbers, shas, ref names and exit codes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PUBLIC_REPO = "AkkiaLab/AkkiaBot"
INTERNAL_REPO = "AkkiaLab/AkkiaBot-internal"
INTERNAL_URL = f"https://github.com/{INTERNAL_REPO}.git"
REVIEWER_KEY = "akkia.preapproval.reviewer"
INTERNAL_REMOTE_KEY = "akkia.internal.remote"
SKIP_HINT = "确需跳过时显式执行：SKIP=preapproval-check git push …（并在 issue 中说明原因）"
GUARD_MARKER = "akkiabot-internal-review-ref-guard v1"
GUARD_SCRIPT = f"""#!/usr/bin/env bash
# {GUARD_MARKER}: installed by `just setup` (scripts/internal_review.py install-guard).
# pre-commit's pre-push hook runs this file with the complete pre-push stdin.
# Checkouts without the script (older releases) have nothing to enforce.
if [[ -f scripts/internal_review.py ]]; then
  exec python3 scripts/internal_review.py ref-guard "$@"
fi
exit 0
"""

ISSUE_RE = re.compile(r"akkia-([1-9][0-9]*)", re.IGNORECASE)
# A verdict is a whole line on its own: no quote marker, no indentation, no inline code.
VERDICT_RE = re.compile(
    r"^预审(?P<verdict>通过|撤销)：" + re.escape(INTERNAL_REPO) + r"@(?P<sha>[0-9a-f]{40})$"
)
# CommonMark fenced code: an opener may carry an info string (no backticks after a
# backtick fence); a closer is only fence characters plus trailing spaces.
FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})[ \t]*$")
ZERO_SHA = re.compile(r"^0+$")


class GitError(RuntimeError):
    """A git command failed; the message holds only the subcommand and exit code."""


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", *args],  # noqa: S607 - git from PATH, like the hooks
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        # Raw args and stderr may contain remote URLs with credentials: never echo them.
        raise GitError(f"git {args[0]} 失败（退出码 {result.returncode}）")
    return result.stdout.strip()


GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "ssh.github.com"})


def split_url(url: str) -> tuple[str, list[str]]:
    """Return (lower-case host without user/port, path segments) of a git URL.

    Handles ``scheme://[user@]host[:port]/path`` and scp-like ``[user@]host:path``;
    local paths give an empty host.
    """
    url = url.strip()
    match = re.match(
        r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]*@)?(?P<host>[^/:]*)(?::\d*)?(?P<path>/.*)?$", url
    )
    if match is None:
        match = re.match(r"^(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>.*)$", url)
    if match is None:
        host, path = "", url
    else:
        host, path = match.group("host") or "", match.group("path") or ""
    path = re.sub(r"\.git/?$", "", path.rstrip("/"))
    return host.lower(), [seg for seg in path.split("/") if seg]


def repo_slug(url: str) -> str | None:
    """Return ``owner/name`` for a GitHub remote URL (incl. SSH over port 443), else None."""
    host, segments = split_url(url)
    if host not in GITHUB_HOSTS or len(segments) != 2:
        return None
    return "/".join(segments)


def is_repo(url: str, repo: str) -> bool:
    return (repo_slug(url) or "").lower() == repo.lower()


def classify(url: str) -> str:
    """``public`` for the public repository, ``unknown`` when the path names it but
    the host is not a recognised GitHub endpoint (SSH host alias, proxy, mirror…),
    ``other`` otherwise. Hooks must treat ``unknown`` as a refusal, not a pass."""
    if is_repo(url, PUBLIC_REPO):
        return "public"
    _, segments = split_url(url)
    if "/".join(segments[-2:]).lower() == PUBLIC_REPO.lower():
        return "unknown"
    return "other"


def is_public(url: str) -> bool:
    return classify(url) == "public"


UNKNOWN_DEST = (
    "目的地路径指向公开仓库，但主机不是可识别的 GitHub 地址，无法确认目的地，拒绝推送；"
    "请改用 https://github.com/、git@github.com: 或 ssh://git@ssh.github.com:443/ 地址"
)


def public_remotes() -> list[str]:
    names = git("remote").splitlines()
    return [n for n in names if is_public(git("remote", "get-url", n, check=False))]


def has_new_commits(sha: str) -> bool:
    """True when ``sha`` reaches a commit no public remote-tracking ref has."""
    excludes = [f"--remotes={name}" for name in public_remotes()]
    return bool(git("rev-list", "-n", "1", sha, "--not", *excludes))


@dataclass(frozen=True)
class Verdict:
    approved: bool
    sha: str
    comment_id: str


def verdict_lines(content: str) -> Iterable[re.Match[str]]:
    """Formal verdict lines of one comment, skipping fenced code and quotes."""
    fence: str | None = None
    for line in content.splitlines():
        if fence is not None:
            closer = FENCE_CLOSE_RE.match(line)
            if closer is not None:
                mark = closer.group("mark")
                if mark[0] == fence[0] and len(mark) >= len(fence):
                    fence = None
            continue
        opener = FENCE_OPEN_RE.match(line)
        if opener is not None and not (
            opener.group("mark")[0] == "`" and "`" in opener.group("info")
        ):
            fence = opener.group("mark")
            continue
        match = VERDICT_RE.match(line.rstrip())
        if match is not None:
            yield match


def latest_verdict(comments: Sequence[Mapping[str, object]], reviewers: set[str]) -> Verdict | None:
    """The newest formal ``预审通过/撤销`` line written by one of ``reviewers``.

    Comments are ordered by ``created_at``; within one comment the last line wins.
    Comments by anyone else (including the implementer quoting the format), quoted
    lines and code examples are ignored.
    """
    found: tuple[str, int, Verdict] | None = None
    for comment in comments:
        if comment.get("author_type") != "agent" or comment.get("author_id") not in reviewers:
            continue
        content = comment.get("content")
        if not isinstance(content, str):
            continue
        created = str(comment.get("created_at", ""))
        for index, match in enumerate(verdict_lines(content)):
            verdict = Verdict(
                approved=match.group("verdict") == "通过",
                sha=match.group("sha"),
                comment_id=str(comment.get("id", "")),
            )
            if found is None or (created, index) >= (found[0], found[1]):
                found = (created, index, verdict)
    return found[2] if found else None


def read_comments(cli: str, issue: str) -> list[Mapping[str, object]]:
    result = subprocess.run(  # noqa: S603 - multica CLI from MULTICA_CLI_PATH, fixed argv
        [cli, "issue", "comment", "list", issue, "--full", "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"multica 读取 {issue} 评论失败（退出码 {result.returncode}）")
    data = json.loads(result.stdout)
    if isinstance(data, dict):
        data = data.get("comments", [])
    if not isinstance(data, list):
        raise RuntimeError(f"multica 返回的 {issue} 评论格式无法识别")
    return [c for c in data if isinstance(c, dict)]


def hooks_dir() -> Path:
    return Path(git("rev-parse", "--path-format=absolute", "--git-path", "hooks"))


def guard_installed() -> bool:
    path = hooks_dir() / "pre-push.legacy"
    try:
        return os.access(path, os.X_OK) and GUARD_MARKER in path.read_text("utf-8")
    except OSError:
        return False


def ref_guard(url: str, lines: Iterable[str]) -> tuple[bool, str]:
    """Check the complete pre-push input (``<local ref> <sha> <remote ref> <sha>`` lines)."""
    kind = classify(url)
    if kind == "unknown":
        return False, f"ref-guard: {UNKNOWN_DEST}"
    if kind != "public":
        return True, "ref-guard: 目标不是公开仓库，跳过"
    updates: list[tuple[str, str]] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or ZERO_SHA.match(parts[1]):
            continue  # malformed or a deletion: nothing is published
        updates.append((parts[2], parts[1]))
    with_content = [ref for ref, sha in updates if has_new_commits(sha)]
    if with_content and len(updates) > 1:
        refs = "、".join(ref for ref, _ in updates)
        return False, (
            f"ref-guard: 推往公开仓库的提交只能一次更新一个 ref（本次 {len(updates)} 个：{refs}），"
            "否则 gitleaks 与预审核对只覆盖其中一个。请逐个分支推送"
        )
    return True, "ref-guard: 通过"


def check_push(env: Mapping[str, str]) -> tuple[bool, str]:
    """Decide one pre-commit pre-push invocation. Returns (allowed, message)."""
    url = env.get("PRE_COMMIT_REMOTE_URL", "")
    kind = classify(url)
    if kind == "unknown":
        return False, f"preapproval: {UNKNOWN_DEST}"
    if kind != "public":
        return True, "preapproval: 目标不是公开仓库，跳过"

    target = env.get("PRE_COMMIT_TO_REF") or env.get("PRE_COMMIT_LOCAL_BRANCH") or "HEAD"
    if ZERO_SHA.match(target):
        return True, "preapproval: 删除分支，跳过"
    head = git("rev-parse", "--verify", f"{target}^{{commit}}")

    if not has_new_commits(head):
        return True, "preapproval: 没有公开仓库尚未包含的提交，放行"

    if not guard_installed():
        return False, (
            "preapproval: 未安装多 ref 推送守卫（hooks/pre-push.legacy），"
            "请先执行 just setup，拒绝推送"
        )

    branch = ""
    for ref in (env.get("PRE_COMMIT_REMOTE_BRANCH", ""), env.get("PRE_COMMIT_LOCAL_BRANCH", "")):
        if ISSUE_RE.search(ref):
            branch = ref
            break
    match = ISSUE_RE.search(branch)
    if match is None:
        return False, "preapproval: 分支名中没有 akkia-<编号>，无法定位预审 issue，拒绝推送"
    issue = f"AKKIA-{match.group(1)}"

    reviewers = set(git("config", "--get-all", REVIEWER_KEY, check=False).split())
    if not reviewers:
        return False, (
            f"preapproval: 本机未配置预审评审者（git config --add {REVIEWER_KEY} <agent id>），"
            "拒绝推送"
        )

    cli = env.get("MULTICA_CLI_PATH") or shutil.which("multica", path=env.get("PATH"))
    if not cli:
        return False, "preapproval: 找不到 multica CLI（MULTICA_CLI_PATH），无法核对预审，拒绝推送"

    try:
        verdict = latest_verdict(read_comments(cli, issue), reviewers)
    except (RuntimeError, ValueError, OSError) as exc:
        return False, f"preapproval: {exc}，拒绝推送"

    if verdict is None:
        return False, f"preapproval: {issue} 中没有评审者的预审结论，拒绝推送 {head}"
    if not verdict.approved:
        return False, f"preapproval: {issue} 最新预审结论为撤销（{verdict.sha}），拒绝推送"
    if verdict.sha != head:
        return False, (
            f"preapproval: 待推送的 {head} 与 {issue} 最新批准的 {verdict.sha} 不一致，拒绝推送"
        )
    return True, f"preapproval: {head} 与 {issue} 的预审批准一致，放行"


def install_guard() -> int:
    path = hooks_dir() / "pre-push.legacy"
    if path.exists() and GUARD_MARKER not in path.read_text("utf-8", errors="replace"):
        print(f"install-guard: {path} 已存在且不是本守卫，未覆盖，请人工处理", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GUARD_SCRIPT, "utf-8")
    path.chmod(0o755)
    print(f"install-guard: 已安装 {path}")
    return 0


def check_sync_remote(remote: str) -> str | None:
    """Return a problem if ``remote`` could push anywhere but the private repository."""
    if git("config", "--get", f"remote.{remote}.mirror", check=False).lower() in ("true", "1"):
        return f"remote {remote} 设置了 mirror，可能强制更新，停止"
    # --push --all: every effective push destination (pushurl entries, insteadOf rewrites).
    urls = git("remote", "get-url", "--push", "--all", remote, check=False).splitlines()
    if not urls:
        return f"无法读取 remote {remote} 的推送地址，停止"
    if not all(is_repo(url, INTERNAL_REPO) for url in urls):
        return f"remote {remote} 的推送地址不全是 {INTERNAL_REPO}，停止"
    return None


def sync(remote: str | None) -> int:
    """Fast-forward public ``releases/*`` into the private repository."""
    remote = remote or git("config", "--get", INTERNAL_REMOTE_KEY, check=False) or "internal"
    if remote not in git("remote").splitlines():
        git("remote", "add", remote, INTERNAL_URL)
        print(f"sync: 已添加 remote {remote} -> {INTERNAL_REPO}")
    problem = check_sync_remote(remote)
    if problem:
        print(f"sync: {problem}", file=sys.stderr)
        return 2

    sources = public_remotes()
    if not sources:
        print(f"sync: 找不到指向 {PUBLIC_REPO} 的 remote，停止", file=sys.stderr)
        return 2
    source = sources[0]
    git("fetch", "--prune", source, f"+refs/heads/releases/*:refs/remotes/{source}/releases/*")
    refs = git(
        "for-each-ref", "--format=%(refname)", f"refs/remotes/{source}/releases/"
    ).splitlines()
    if not refs:
        print("sync: 公开仓库没有 releases/* 分支")
        return 0
    prefix = f"refs/remotes/{source}/"
    # No "+" prefix and no --force: a diverged private branch is rejected, not overwritten.
    specs = [f"{ref}:refs/heads/{ref.removeprefix(prefix)}" for ref in refs]
    result = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", "push", "--porcelain", "--no-force-with-lease", remote, *specs],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    # Porcelain ref lines are "<flag>\t<from>:<to>\t<summary>"; the "To <url>" line and
    # stderr (remote messages, hook output) are dropped because they may carry the URL.
    for line in result.stdout.splitlines():
        if "\t" in line and line[:1] in "*=!+- ":
            print(f"sync: {line}")
    if result.returncode != 0:
        print(f"sync: git push 失败（退出码 {result.returncode}），未强制更新", file=sys.stderr)
    return result.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-push", help="pre-commit 预审核对钩子")
    guard = sub.add_parser("ref-guard", help="git pre-push 多 ref 守卫（读取 stdin）")
    guard.add_argument("remote_name", nargs="?", default="")
    guard.add_argument("remote_url", nargs="?", default="")
    sub.add_parser("install-guard", help="安装 hooks/pre-push.legacy 守卫")
    sync_parser = sub.add_parser("sync", help="同步公开 releases/* 到私有仓库")
    sync_parser.add_argument("--remote", help="私有 remote 名（默认 internal）")
    args = parser.parse_args(argv)

    try:
        if args.command == "sync":
            return sync(args.remote)
        if args.command == "install-guard":
            return install_guard()
        if args.command == "ref-guard":
            allowed, message = ref_guard(args.remote_url, sys.stdin.read().splitlines())
            if not allowed:
                print(message, file=sys.stderr)
            return 0 if allowed else 1
        allowed, message = check_push(os.environ)
    except GitError as exc:
        print(f"internal_review: {exc}", file=sys.stderr)
        return 1
    print(message if allowed else f"{message}\n{SKIP_HINT}")
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
