"""Private pre-review (预审) tooling for pushes to the public repository.

Workflow (docs/secret-scanning.md, "推送前的内部预审"): implementers push to the
private repository ``AkkiaLab/AkkiaBot-internal`` first and open a private PR;
the reviewer agent reviews it and records the verdict as a Multica issue comment
``预审通过：AkkiaLab/AkkiaBot-internal@<40-hex sha>`` (or ``预审撤销：…``). Only that
exact sha may then be pushed to the public repository.

Commands (stdlib only, ``git`` and the ``multica`` CLI on the host):

  check-push   pre-push hook (run by pre-commit). Pushes that are not aimed at the
               public repository, and pushes that add no commit the public remote
               lacks, pass. Otherwise the issue number is parsed from the branch
               name, the reviewer's latest verdict is read with the multica CLI and
               the pushed head must equal the approved sha.
  sync         Fast-forward every public ``releases/*`` branch into the private
               repository (public -> private only, never forced).

Host configuration lives in git config, never in this public repository:

  akkia.preapproval.reviewer  (multi-valued) Multica agent ids whose verdicts count
  akkia.internal.remote       name of the private remote (default ``internal``)

Messages never print comment bodies, only issue numbers, shas and rule names.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

PUBLIC_REPO = "AkkiaLab/AkkiaBot"
INTERNAL_REPO = "AkkiaLab/AkkiaBot-internal"
INTERNAL_URL = f"https://github.com/{INTERNAL_REPO}.git"
REVIEWER_KEY = "akkia.preapproval.reviewer"
INTERNAL_REMOTE_KEY = "akkia.internal.remote"
SKIP_HINT = "确需跳过时显式执行：SKIP=preapproval-check git push …（并在 issue 中说明原因）"

ISSUE_RE = re.compile(r"akkia-([1-9][0-9]*)", re.IGNORECASE)
VERDICT_RE = re.compile(
    r"^[\s>*`-]*预审(?P<verdict>通过|撤销)[:：]\s*"
    + re.escape(INTERNAL_REPO)
    + r"@(?P<sha>[0-9a-f]{40})[\s`*]*$",
    re.MULTILINE,
)
ZERO_SHA = re.compile(r"^0+$")


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", *args],  # noqa: S607 - git from PATH, like the hooks
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


def repo_slug(url: str) -> str | None:
    """Return ``owner/name`` for a GitHub remote URL, or None for anything else."""
    match = re.match(
        r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$",
        url.strip(),
    )
    return match.group("slug") if match else None


def is_public(url: str) -> bool:
    slug = repo_slug(url)
    return slug is not None and slug.lower() == PUBLIC_REPO.lower()


def public_remotes() -> list[str]:
    names = git("remote").splitlines()
    return [n for n in names if is_public(git("remote", "get-url", n, check=False))]


@dataclass(frozen=True)
class Verdict:
    approved: bool
    sha: str
    comment_id: str


def latest_verdict(comments: Sequence[Mapping[str, object]], reviewers: set[str]) -> Verdict | None:
    """The newest ``预审通过/撤销`` line written by one of ``reviewers``.

    Comments are ordered by ``created_at``; within one comment the last line wins.
    Comments by anyone else (including the implementer quoting the format) are ignored.
    """
    found: tuple[str, int, Verdict] | None = None
    for comment in comments:
        if comment.get("author_type") != "agent" or comment.get("author_id") not in reviewers:
            continue
        content = comment.get("content")
        if not isinstance(content, str):
            continue
        created = str(comment.get("created_at", ""))
        for index, match in enumerate(VERDICT_RE.finditer(content)):
            verdict = Verdict(
                approved=match.group("verdict") == "通过",
                sha=match.group("sha"),
                comment_id=str(comment.get("id", "")),
            )
            key = (created, index)
            if found is None or key >= (found[0], found[1]):
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


def check_push(env: Mapping[str, str]) -> tuple[bool, str]:
    """Decide one pre-push invocation. Returns (allowed, message)."""
    url = env.get("PRE_COMMIT_REMOTE_URL", "")
    if not is_public(url):
        return True, "preapproval: 目标不是公开仓库，跳过"

    target = env.get("PRE_COMMIT_TO_REF") or env.get("PRE_COMMIT_LOCAL_BRANCH") or "HEAD"
    if ZERO_SHA.match(target):
        return True, "preapproval: 删除分支，跳过"
    head = git("rev-parse", "--verify", f"{target}^{{commit}}")

    excludes = [f"--remotes={name}" for name in public_remotes()]
    if not git("rev-list", "-n", "1", head, "--not", *excludes):
        return True, "preapproval: 没有公开仓库尚未包含的提交，放行"

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


def sync(remote: str | None) -> int:
    """Fast-forward public ``releases/*`` into the private repository."""
    remote = remote or git("config", "--get", INTERNAL_REMOTE_KEY, check=False) or "internal"
    current = git("remote", "get-url", remote, check=False)
    if not current:
        git("remote", "add", remote, INTERNAL_URL)
        print(f"sync: 已添加 remote {remote} -> {INTERNAL_URL}")
    elif (repo_slug(current) or "").lower() != INTERNAL_REPO.lower():
        print(f"sync: remote {remote} 不指向 {INTERNAL_REPO}，停止", file=sys.stderr)
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
    specs = [f"{ref}:refs/heads/{ref.removeprefix(prefix)}" for ref in refs]
    # Never forced: a diverged private branch is rejected and reported, not overwritten.
    result = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", "push", "--porcelain", remote, *specs],  # noqa: S607
        check=False,
    )
    return result.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-push", help="pre-push hook")
    sync_parser = sub.add_parser("sync", help="同步公开 releases/* 到私有仓库")
    sync_parser.add_argument("--remote", help="私有 remote 名（默认 internal）")
    args = parser.parse_args(argv)

    if args.command == "sync":
        return sync(args.remote)
    allowed, message = check_push(os.environ)
    print(message if allowed else f"{message}\n{SKIP_HINT}")
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
