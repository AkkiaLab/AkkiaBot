"""Creating release branches, the PR guard and moving features off a frozen release."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from scripts.release.git import Git, RepoState
from scripts.release.github import FIX_LABEL, FORWARD_LABEL, GitHub, PullRequest
from scripts.release.schedule import current_regular, feature_frozen, next_regular_open
from scripts.release.versions import (
    MASTER,
    ReleaseError,
    Version,
    check_new_version,
    next_regular,
    parse_branch,
    source_ref,
)


def create_release(git: Git, version: Version, *, dry_run: bool = False) -> tuple[str, str]:
    """Create ``releases/<version>`` at its source; idempotent when it already matches.

    Returns (source ref, sha). A branch that already exists is accepted only
    when it still descends from the right source.
    """
    state = git.state()
    existing = state.branches.get(version)
    if existing is not None:
        # Rerun: a regular release stays as cut (master moves on); a patch
        # release must still descend from its source tag.
        source = source_ref(version, state.tags)
        if version.is_patch and not git.is_ancestor(state.tags[version.previous_patch], existing):
            raise ReleaseError(f"{version.branch} 已存在，但不是从 {source} 创建的")
        return source, existing
    source = source_ref(version, state.tags)
    source_sha = state.master if source == MASTER else state.tags[Version.parse(source)]
    known = state.known_versions
    check_new_version(version, known)
    if not dry_run and not git.push_ref(source_sha, f"refs/heads/{version.branch}"):
        raise ReleaseError(f"创建 {version.branch} 失败（并发创建或规则拒绝），请重试")
    return source, source_sha


def ensure_next_regular(git: Git, gh: GitHub, *, dry_run: bool = False) -> Version | None:
    """Make sure a regular release exists after the current one (the 下车 target).

    Only acts once the flow is bootstrapped (some release branch exists); the
    very first version is chosen by a person (docs/release.md §6).
    """
    state = git.state()
    sealed = gh.sealed_branches()
    if current_regular(state, sealed) is None:
        return None
    existing = next_regular_open(state, sealed)
    if existing is not None:
        return existing
    version = next_regular(state.known_versions)
    create_release(git, version, dry_run=dry_run)
    return version


@dataclass
class GuardResult:
    ok: bool
    messages: list[str]


def guard_pr(
    git: Git, gh: GitHub, pr: PullRequest, now: datetime, *, state: RepoState | None = None
) -> GuardResult:
    """The ``release-guard`` check on a pull request (docs/release.md §2).

    * base ``master``: only cascade resolution PRs once the flow is bootstrapped;
    * base sealed release: rejected (the ruleset blocks the merge as well);
    * base frozen release: only PRs labelled ``release:fix`` (or cascade PRs);
    * the head must be built on the target release: commits it adds must not
      already belong to another release branch or master (wrong base branch).
    """
    state = state or git.state()
    sealed = gh.sealed_branches()
    messages: list[str] = []
    forward = FORWARD_LABEL in pr.labels
    bootstrapped = bool(state.branches)
    if pr.base == MASTER:
        if bootstrapped and not forward:
            current = current_regular(state, sealed)
            hint = f"`{current.branch}`" if current else "目标 `releases/vX.Y.Z`"
            messages.append(f"版本流程已启用：功能与修复 PR 应提向 {hint}，不再直接提向 master")
        return GuardResult(not messages, messages or ["base 为 master：允许"])
    target = parse_branch(pr.base)
    if target is None:
        return GuardResult(True, [f"base `{pr.base}` 不是版本分支，不做版本检查"])
    if target not in state.branches:
        return GuardResult(False, [f"版本分支 `{pr.base}` 不存在"])
    if pr.base in sealed:
        messages.append(f"`{pr.base}` 已正式封版，不再接收 PR；请改提向下一个可接收的版本")
    elif not forward and FIX_LABEL not in pr.labels and feature_frozen(target, now, state, sealed):
        upcoming = next_regular_open(state, sealed)
        messages.append(
            f"`{pr.base}` 已于发布日 18:00（UTC+8）冻结新增功能：修复请加标签 `{FIX_LABEL}`，"
            f"功能请下车到 `{upcoming.branch if upcoming else '下一个 release'}`"
        )
    if not forward:
        git.fetch_pr(pr.number, pr.head_ref)
        messages.extend(_base_mismatch(git, state, target, pr.head_sha))
    return GuardResult(not messages, messages or [f"base `{pr.base}`：允许"])


def _base_mismatch(git: Git, state: RepoState, target: Version, head: str) -> list[str]:
    base_sha = state.branches[target]
    added = git.rev_list(head, base_sha, limit=500)
    if not added:
        return []
    others = {MASTER: state.master} | {
        v.branch: sha for v, sha in state.branches.items() if v != target
    }
    foreign: list[str] = []
    for name, sha in others.items():
        if any(git.is_ancestor(commit, sha) for commit in added):
            foreign.append(name)
    if not foreign:
        return []
    return [
        f"PR 分支包含已属于 {', '.join(f'`{f}`' for f in sorted(foreign))} 的提交："
        f"任务分支应从目标 `{target.branch}` 创建（`git switch -c <分支> origin/{target.branch}`），"
        "不要从 master 或其他版本创建"
    ]


def defer_features(
    git: Git, gh: GitHub, now: datetime, *, dry_run: bool = False
) -> list[tuple[int, str]]:
    """下车: move open feature PRs off the frozen current release to the next one."""
    state = git.state()
    sealed = gh.sealed_branches()
    current = current_regular(state, sealed)
    if current is None or not feature_frozen(current, now, state, sealed):
        return []
    upcoming = ensure_next_regular(git, gh, dry_run=dry_run)
    if upcoming is None:
        return []
    moved: list[tuple[int, str]] = []
    for pr in gh.open_prs(current.branch):
        if FIX_LABEL in pr.labels or FORWARD_LABEL in pr.labels:
            continue
        moved.append((pr.number, upcoming.branch))
        if dry_run:
            continue
        gh.retarget_pr(pr.number, upcoming.branch)
        gh.comment_pr(
            pr.number,
            f"`{current.branch}` 已冻结新增功能，本 PR 已自动下车到 `{upcoming.branch}`。"
            f"请把分支变基到 `origin/{upcoming.branch}`（`git rebase --onto origin/{upcoming.branch} "
            f"origin/{current.branch}`）后推送；如确属修复，加标签 `{FIX_LABEL}` 并由 Leader 确认后改回。",
        )
    return moved
