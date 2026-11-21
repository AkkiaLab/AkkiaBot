"""``python -m scripts.release <command>`` — entry point of the release flow (docs/release.md).

Read-only commands: status, baseline, guard, check-rc, verify-formal.
Writing commands need a token that may write to the repository: ``gh``'s
login on the runtime machine, or the release App token in Actions
(``GH_TOKEN``; ``git`` pushes through the checkout's credentials).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.release import branches, cascade, deploy, rc, seal, window
from scripts.release.git import Git
from scripts.release.github import GhCli, GitHub
from scripts.release.schedule import Phase, current_regular, window_at
from scripts.release.versions import ReleaseError, Version, next_regular


def _summary(lines: list[str]) -> None:
    for line in lines:
        print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _now(args: argparse.Namespace) -> datetime:
    return datetime.fromisoformat(args.now) if getattr(args, "now", None) else datetime.now(UTC)


def cmd_status(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    state = git.state()
    sealed = gh.sealed_branches() if state.branches else set()
    now = _now(args)
    lines = [f"master: {state.master}", f"当前窗口阶段：{window_at(now).phase}"]
    current = current_regular(state, sealed)
    lines.append(f"当前常规 release：{current.branch if current else '无（未引导）'}")
    for version, sha in sorted(state.branches.items()):
        flags = []
        if version.branch in sealed:
            flags.append("已封版")
        if version in state.tags:
            flags.append(f"正式 tag {state.tags[version][:12]}")
        latest = state.latest_rc(version)
        if latest:
            flags.append(
                f"最新 {version.rc_tag(latest[0])}{'' if latest[1] == sha else '（已过期）'}"
            )
        lines.append(f"- {version.branch} {sha[:12]} {' '.join(flags)}")
    orphan_tags = sorted(set(state.tags) - set(state.branches))
    lines += [f"- 正式 tag {v}（无分支）" for v in orphan_tags]
    if state.known_versions:
        lines.append(f"下一个常规版本：{next_regular(state.known_versions)}")
    _summary(lines)
    return 0


def cmd_create(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    if args.version == "next":
        version = branches.ensure_next_regular(git, gh, dry_run=args.dry_run)
        if version is None:
            raise ReleaseError("还没有任何 release：首个版本需显式给出版本号（docs/release.md §6）")
        _summary([f"下一个常规 release：{version.branch}"])
        return 0
    version = Version.parse(args.version)
    source, sha = branches.create_release(git, version, dry_run=args.dry_run)
    _summary([f"{version.branch} ← {source}（{sha}）{'（dry-run）' if args.dry_run else ''}"])
    return 0


def cmd_cascade(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    report = cascade.run_cascade(git, gh, trigger=args.trigger, dry_run=args.dry_run)
    _summary(["### 逐级上合", *report.lines])
    return 1 if report.paused and args.fail_on_pause else 0


def cmd_land(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    head = cascade.land_forward_pr(git, gh, args.pr)
    _summary([f"已以快进方式落地 PR #{args.pr}（{head}），继续逐级上合"])
    report = cascade.run_cascade(git, gh, trigger=f"PR #{args.pr} 冲突解决")
    _summary(report.lines)
    return 0


def cmd_land_approved(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    """Reconcile entry: land every approved, green resolution PR (scheduled run)."""
    lines = []
    for pr in gh.open_prs():
        if cascade.FORWARD_LABEL not in pr.labels:
            continue
        try:
            head = cascade.land_forward_pr(git, gh, pr.number)
            lines.append(f"- 已落地 PR #{pr.number}（{head[:12]}）")
        except ReleaseError as exc:
            lines.append(f"- PR #{pr.number} 暂不落地：{exc}")
    _summary(["### 冲突解决 PR", *(lines or ["- 无"])])
    return 0


def cmd_guard(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    pr = gh.get_pr(args.pr)
    result = branches.guard_pr(git, gh, pr, _now(args))
    _summary(
        [f"### release-guard：PR #{pr.number} → {pr.base}", *(f"- {m}" for m in result.messages)]
    )
    return 0 if result.ok else 1


def cmd_rc(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    candidate, created = rc.create_rc(git, gh, Version.parse(args.version), dry_run=args.dry_run)
    _summary([f"{'创建' if created else '沿用'} {candidate.tag} → {candidate.sha}"])
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"tag={candidate.tag}\nsha={candidate.sha}\n")
    return 0


def cmd_check_rc(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    candidate = rc.check_rc(git.state(), args.tag, gh.sealed_branches())
    baseline = rc.select_baseline(git, gh, candidate.sha)
    _summary(
        [
            f"{candidate.tag} 有效：{candidate.sha}",
            f"差异验收基线：{baseline or '无已验收祖先（执行全量验收）'}",
        ]
    )
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(
                f"sha={candidate.sha}\nversion={candidate.version}\nbaseline={baseline or ''}\n"
            )
    return 0


def cmd_baseline(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    baseline = rc.select_baseline(git, gh, args.sha)
    print(baseline or "")
    return 0 if baseline else 1


def cmd_seal(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    result = seal.seal(git, gh, Version.parse(args.version))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(
                f"tag={result.version.tag}\nsha={result.sha}\ncreated={str(result.created).lower()}\n"
            )
    _summary(
        [
            f"{result.version} → {result.sha}（{result.rc_tag}）{'已创建' if result.created else '已存在'}"
        ]
    )
    return 0


def cmd_verify_formal(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    version, sha = seal.verify_formal(git, gh, args.tag)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"sha={sha}\nversion={version}\n")
    _summary([f"{args.tag} 可发布：{sha}（分支已封版、HEAD 一致、staging 验收通过）"])
    return 0


def cmd_window(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    report = window.run_window(
        git,
        gh,
        _now(args),
        phase=Phase(args.phase) if args.phase else None,
        version=Version.parse(args.version) if args.version else None,
        dry_run=args.dry_run,
    )
    _summary(
        [
            f"### 发布窗口（{report.phase}）{report.version or ''}",
            *(f"- {line}" for line in report.lines),
        ]
    )
    return 1 if report.failed else 0


def cmd_deploy(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    target = deploy.resolve_target(args.env, ssh=args.ssh, directory=args.dir)
    with deploy.environment_lock(target.name):
        if target.name == "staging":
            staged = deploy.release_staging(
                git,
                gh,
                target,
                args.tag,
                scenarios=args.scenarios,
                accept=args.accept,
                allow_migration=args.allow_migration,
            )
            _summary([f"### staging {args.tag}", *(f"- {line}" for line in staged.lines)])
            return staged.code
        result = deploy.deploy(git, gh, target, args.tag, allow_migration=args.allow_migration)
        _summary([f"### {target.name} 部署 {args.tag}", *(f"- {line}" for line in result.lines)])
        if result.code != 0:
            _summary(["- 已停止：未触发前端提升"])
            return result.code
        gh.dispatch("webapp-promote.yml", {"sha": result.sha})
        _summary(["- 已触发 webapp-promote.yml（前端需负责人审批 webapp-production）"])
        return 0


def cmd_lease(git: Git, gh: GitHub, args: argparse.Namespace) -> int:
    from scripts.release import lease

    if args.action in {"check", "acquire"} and not args.sha:
        raise ReleaseError(f"lease {args.action} 需要完整的 commit sha")
    if args.action == "show":
        current = lease.read_lease(git)
        _summary([f"staging 租约：{current}" if current else "staging 没有租约"])
    elif args.action == "check":
        held = lease.check(git, args.sha)
        _summary([f"staging 租约有效：{held.label}（{held.sha}）到 {held.expires.isoformat()}"])
    elif args.action == "acquire":
        held = lease.acquire(git, args.sha, args.label or args.sha[:12])
        _summary(
            [
                f"已取得 staging 租约 {held.commit}（{args.sha}），用完执行 `just release lease release`"
            ]
        )
    else:
        current = lease.read_lease(git)
        if current is None:
            _summary(["staging 没有租约"])
        elif args.sha and current.sha != args.sha:
            raise ReleaseError(
                f"租约属于 {current.label}（{current.sha[:12]}），不是 {args.sha[:12]}，未释放"
            )
        elif not lease.release(git, current):
            raise ReleaseError("释放失败：租约刚被更新")
        else:
            _summary([f"已释放 {current.label} 的 staging 租约"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release", description="AkkiaBot 版本开发与发布流程")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "AkkiaLab/AkkiaBot"))
    parser.add_argument("--workdir", default=".", help="带 origin 的仓库检出")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        return p

    p = add("status", cmd_status, "列出版本分支、tag、RC 与封版状态")
    p.add_argument("--now")
    p = add("create", cmd_create, "创建 release 分支（vX.Y.Z 或 next）")
    p.add_argument("version")
    p.add_argument("--dry-run", action="store_true")
    p = add("cascade", cmd_cascade, "逐级上合对账（幂等）")
    p.add_argument("--trigger", default="手动对账")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--fail-on-pause", action="store_true")
    p = add("land", cmd_land, "落地已批准的冲突解决 PR")
    p.add_argument("pr", type=int)
    add("land-approved", cmd_land_approved, "落地所有已批准、检查通过的冲突解决 PR")
    p = add("guard", cmd_guard, "PR 的 release-guard 检查")
    p.add_argument("pr", type=int)
    p.add_argument("--now")
    p = add("rc", cmd_rc, "在版本分支 HEAD 上创建下一个 RC（幂等）")
    p.add_argument("version")
    p.add_argument("--dry-run", action="store_true")
    p = add("check-rc", cmd_check_rc, "确认 RC 仍是最新且等于分支 HEAD，并给出差异基线")
    p.add_argument("tag")
    p = add("baseline", cmd_baseline, "给出 sha 的已验收祖先基线")
    p.add_argument("sha")
    p = add("seal", cmd_seal, "封版：锁分支、回读、在 RC 上创建正式 tag")
    p.add_argument("version")
    p = add("verify-formal", cmd_verify_formal, "生产流水线的正式 tag 门禁")
    p.add_argument("tag")
    p = add("window", cmd_window, "发布窗口（定时或手动加车）")
    p.add_argument("--phase", choices=[ph.value for ph in Phase if ph != Phase.NONE])
    p.add_argument("--version", help="手动加车 / 小版本：指定版本")
    p.add_argument("--now")
    p.add_argument("--dry-run", action="store_true")
    p = add("lease", cmd_lease, "staging 租约：show / check <sha> / acquire <sha> / release [sha]")
    p.add_argument("action", choices=["show", "check", "acquire", "release"])
    p.add_argument("sha", nargs="?", default="")
    p.add_argument("--label")
    p = add("deploy", cmd_deploy, "在运行机上部署 RC（staging）或正式 tag（production）")
    p.add_argument("env", choices=sorted(deploy.ENVIRONMENTS))
    p.add_argument("tag")
    p.add_argument("--allow-migration", action="store_true", help="负责人已确认本次数据库迁移")
    p.add_argument("--ssh", help="覆盖目标 ssh 主机（默认取 AKKIABOT_<ENV>_SSH；空字符串表示本机）")
    p.add_argument("--dir", help="覆盖目标目录（默认取 AKKIABOT_<ENV>_DIR，未配置时报错）")
    p.add_argument("--scenarios", help="staging：差异验收场景（逗号分隔）")
    p.add_argument("--accept", action="store_true", help="staging：部署后在同一把锁内执行验收")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    git = Git(Path(args.workdir))
    gh = GhCli(args.repo)
    try:
        return args.func(git, gh, args)
    except ReleaseError as exc:
        print(
            f"::error::{exc}" if os.environ.get("GITHUB_ACTIONS") else f"错误：{exc}",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
