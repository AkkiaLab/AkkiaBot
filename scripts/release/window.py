"""Release window orchestration: prepare (16:00), freeze (18:00), release (20:00) UTC+8.

``run_window`` is what the scheduled workflow and the manual 加车 entry call.
It decides from the clock (or an explicit phase) and the repository state, so
a late, repeated or concurrent trigger takes the same decision; every action
it takes is itself idempotent (create_release, create_rc, seal).

* prepare: the current regular release must exist (after bootstrap) and so must
  the next one (the 下车 target); a release without content skips the window.
  "Content" means files: a branch that only gained cascade merge commits and
  has the same tree as the previous formal tag has nothing to ship.
* freeze: move open feature PRs to the next release, then cut an RC from the
  head and hand it to the staging pipeline (``release-rc.yml``). A reused RC
  that was already handed over (a ``release-rc.yml`` run for it is pending or
  finished its handoff) or already has a staging-acceptance status is not
  handed over again, so ``release-rc-ready`` is sent once per RC.
* release: only a current, accepted RC is sealed (``release-seal.yml``, which
  then starts the production pipeline). Anything else stops the window with
  the reason; nothing is sealed or tagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from scripts.release.branches import defer_features, ensure_next_regular
from scripts.release.git import Git, RepoState
from scripts.release.github import GitHub
from scripts.release.rc import Candidate, check_rc, create_rc
from scripts.release.schedule import Phase, current_regular, window_at
from scripts.release.versions import ReleaseError, Version

RC_WORKFLOW = "release-rc.yml"
RC_HANDOFF_JOB = "handoff"
SEAL_WORKFLOW = "release-seal.yml"


@dataclass
class WindowReport:
    phase: Phase
    version: Version | None
    lines: list[str] = field(default_factory=list)
    failed: bool = False


def has_content(git: Git, state: RepoState, version: Version) -> bool:
    """Does the release carry anything not shipped by an earlier formal tag?

    New commits are not enough: the cascade merges every released line into
    the next one, so a branch with no work of its own still gains merge
    commits. It has content only when its files differ from the previous
    formal tag (the highest one below ``version``).
    """
    released = {v: sha for v, sha in state.tags.items() if v < version}
    head = state.branches[version]
    if not git.rev_list(head, *released.values(), limit=1):
        return False
    if not released:
        return True
    return not git.same_tree(head, released[max(released)])


def handed_off(gh: GitHub, candidate: Candidate) -> str | None:
    """Why a reused RC must not be handed to staging again (None: hand it over).

    Each handoff sends ``release-rc-ready`` and the receiver opens an
    acceptance task, so a repeated or late freeze must not resend it.
    """
    acceptance = gh.acceptance(candidate.sha)
    if acceptance:
        return f"已有 staging-acceptance={acceptance}"
    if gh.handed_off(RC_WORKFLOW, f"RC {candidate.tag}", RC_HANDOFF_JOB):
        return f"{RC_WORKFLOW} 已交接过（或正在运行）"
    return None


def run_window(
    git: Git,
    gh: GitHub,
    now: datetime,
    *,
    phase: Phase | None = None,
    version: Version | None = None,
    dry_run: bool = False,
) -> WindowReport:
    """``version`` given = manual entry (加车 / patch release); otherwise the current regular."""
    phase = phase or window_at(now).phase
    state = git.state()
    sealed = gh.sealed_branches()
    target = version or current_regular(state, sealed)
    report = WindowReport(phase, target)
    if phase == Phase.NONE:
        report.lines.append("当前不在发布窗口内（周四、周日 16:00–24:00 UTC+8），无操作")
        return report
    if target is None:
        report.lines.append(
            "还没有可接收的 release 分支：版本流程尚未引导（docs/release.md §6），跳过"
        )
        return report
    if target not in state.branches:
        raise ReleaseError(f"{target.branch} 不存在")
    if target in state.tags:
        report.lines.append(
            f"{target} 已有正式 tag，本窗口无需操作（失败重试请运行 release-production.yml）"
        )
        return report
    if not has_content(git, state, target):
        report.lines.append(
            f"{target.branch} 相对上一个正式版本没有新提交或文件差异（仅上合 merge），跳过本窗口"
        )
        return report

    if phase == Phase.PREPARE:
        upcoming = ensure_next_regular(git, gh, dry_run=dry_run)
        report.lines.append(
            f"{target.branch} 有待发布内容；下车目标：{upcoming.branch if upcoming else '无'}"
        )
        return report

    if phase == Phase.FREEZE:
        if version is None:
            for number, base in defer_features(git, gh, now, dry_run=dry_run):
                report.lines.append(f"PR #{number} 下车到 {base}")
        candidate, created = create_rc(git, gh, target, dry_run=dry_run)
        report.lines.append(
            f"{'dry-run：将' if dry_run and created else ''}"
            f"{'创建' if created else '沿用'} {candidate.tag}（{candidate.sha[:12]}）"
        )
        reason = None if created else handed_off(gh, candidate)
        if reason:
            report.lines.append(
                f"{candidate.tag} {reason}：不再触发 {RC_WORKFLOW}，不重发 rc-ready"
            )
        elif dry_run:
            report.lines.append(f"dry-run：将触发 {RC_WORKFLOW}（tag={candidate.tag}）")
        else:
            gh.dispatch(RC_WORKFLOW, {"tag": candidate.tag})
            report.lines.append(f"已触发 {RC_WORKFLOW}：部署 staging 并等待差异验收")
        return report

    # Phase.RELEASE
    latest = state.latest_rc(target)
    if latest is None:
        report.failed = True
        report.lines.append(f"{target} 没有 RC：不能发布（冻结阶段未执行或失败）")
        return report
    tag = target.rc_tag(latest[0])
    try:
        candidate = check_rc(state, tag, sealed)
    except ReleaseError as exc:
        report.failed = True
        report.lines.append(f"停止：{exc}")
        return report
    acceptance = gh.acceptance(candidate.sha)
    if acceptance != "success":
        report.failed = True
        report.lines.append(
            f"停止：{tag} 的 staging-acceptance 是 {acceptance or '未写入'}，不封版"
        )
        return report
    if dry_run:
        report.lines.append(f"dry-run：{tag} 已通过 staging 验收，将触发 {SEAL_WORKFLOW} 封版")
        return report
    gh.dispatch(SEAL_WORKFLOW, {"version": str(target)})
    report.lines.append(f"{tag} 已通过 staging 验收，已触发 {SEAL_WORKFLOW} 封版并进入生产审批")
    return report
