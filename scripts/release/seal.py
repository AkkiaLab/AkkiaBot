"""Sealing a release: lock the branch, verify, then create the formal tag.

Order (docs/release.md §4), every step idempotent so a failed run is resumed
by running it again:

1. the newest RC of the version is accepted (``staging-acceptance=success``)
   and still equals the branch head;
2. the branch is added to the ``releases 封版`` ruleset (no updates, no
   deletion, no force push — PR merges and direct pushes are refused by
   GitHub itself, not reported afterwards by CI);
3. read back: the ruleset lists the branch, GitHub reports the rules as
   effective on it, and the head is still the RC sha. A commit that slipped in
   between steps 1 and 2 makes the head differ: the lock is undone again and
   no tag is created (a new RC is needed);
4. create the formal tag ``vX.Y.Z`` on the RC sha and read it back.

A failure before step 4 never creates a tag. An existing formal tag on a
different commit is a hard error: tags are never moved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scripts.release.git import Git
from scripts.release.github import ACCEPTANCE_CONTEXT, SEALED_RULE_TYPES, GitHub
from scripts.release.versions import ReleaseError, Version


@dataclass
class SealResult:
    version: Version
    sha: str
    rc_tag: str
    created: bool


def seal(
    git: Git, gh: GitHub, version: Version, *, log: Callable[[str], None] = print
) -> SealResult:
    state = git.state()
    latest = state.latest_rc(version)
    if latest is None:
        raise ReleaseError(f"{version} 没有 RC，不能封版")
    number, rc_sha = latest
    rc_tag = version.rc_tag(number)
    existing = state.tags.get(version)
    if existing is not None:
        if existing != rc_sha:
            raise ReleaseError(
                f"正式 tag {version} 已存在且指向 {existing[:12]}，与最新 RC {rc_tag}（{rc_sha[:12]}）不同；"
                "tag 不会被移动，请人工核查"
            )
        _lock(gh, version, log)  # resume: make sure the lock is in place
        log(f"{version} 已封版并有正式 tag（{existing}），无需重复操作")
        return SealResult(version, existing, rc_tag, created=False)
    state_accept = gh.acceptance(rc_sha)
    if state_accept != "success":
        raise ReleaseError(
            f"{rc_tag}（{rc_sha[:12]}）的 {ACCEPTANCE_CONTEXT} 是 {state_accept or '未写入'}，不能封版"
        )
    head = state.branches.get(version)
    if head != rc_sha:
        raise ReleaseError(
            f"{version.branch} 已前进到 {str(head)[:12]}，{rc_tag} 已过期，需要新 RC 并重新验收"
        )

    _lock(gh, version, log)

    head_after = git.state().branches.get(version)
    if head_after != rc_sha:
        gh.set_sealed(version.branch, sealed=False)
        raise ReleaseError(
            f"锁定期间 {version.branch} 前进到 {str(head_after)[:12]}，已解除锁定且未创建正式 tag；"
            "请出新 RC 并验收后重新封版"
        )
    if not git.push_ref(rc_sha, f"refs/tags/{version.tag}"):
        raise ReleaseError(f"创建正式 tag {version} 失败；分支保持锁定，修复后重新运行封版即可继续")
    created = git.state().tags.get(version)
    if created != rc_sha:
        raise ReleaseError(f"回读正式 tag {version} 得到 {created}，不是 {rc_sha}；请人工核查")
    log(f"已封版 {version.branch} 并在 {rc_sha} 创建正式 tag {version}（来自 {rc_tag}）")
    return SealResult(version, rc_sha, rc_tag, created=True)


def _lock(gh: GitHub, version: Version, log: Callable[[str], None]) -> None:
    gh.set_sealed(version.branch, sealed=True)
    if version.branch not in gh.sealed_branches():
        raise ReleaseError(f"回读封版规则集时没有 {version.branch}，锁定未生效，不创建正式 tag")
    missing = SEALED_RULE_TYPES - gh.branch_rule_types(version.branch)
    if missing:
        raise ReleaseError(
            f"{version.branch} 上未生效的封版规则：{', '.join(sorted(missing))}；锁定未完成，不创建正式 tag"
        )
    log(f"{version.branch} 已锁定（规则 {', '.join(sorted(SEALED_RULE_TYPES))} 已生效）")


def verify_formal(git: Git, gh: GitHub, tag: str) -> tuple[Version, str]:
    """Gate of the production pipeline for a formal tag."""
    version = Version.parse(tag)
    state = git.state()
    sha = state.tags.get(version)
    if sha is None:
        raise ReleaseError(f"正式 tag {tag} 不存在")
    if version.branch not in gh.sealed_branches():
        raise ReleaseError(f"{version.branch} 未封版：正式 tag 只能由封版流程创建")
    head = state.branches.get(version)
    if head != sha:
        raise ReleaseError(f"{version.branch} 的 HEAD {str(head)[:12]} 不等于正式 tag {sha[:12]}")
    latest = state.latest_rc(version)
    if latest is None or latest[1] != sha:
        raise ReleaseError(f"正式 tag {tag} 不在最新 RC 上")
    if gh.acceptance(sha) != "success":
        raise ReleaseError(f"{sha[:12]} 没有 {ACCEPTANCE_CONTEXT}=success")
    return version, sha
