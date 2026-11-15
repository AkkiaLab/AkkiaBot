"""Staging release acceptance record (docs/webapp.md, "发布流程").

An acceptance run against the deployed staging environment can record its
outcome as the GitHub commit status ``staging-acceptance`` on the commit that
staging runs. The production promotion workflow (webapp-promote.yml) only
accepts commits whose latest ``staging-acceptance`` status is ``success``.

A run is either full (every scenario) or a diff run (docs/e2e-acceptance.md
§6.2): only the scenarios the product changes since ``baseline`` touch, where
``baseline`` is an earlier master commit that already has
``staging-acceptance=success`` and is an ancestor of the target.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.report import Report
from akkiabot_e2e.scenarios import WebProbe

STATUS_CONTEXT = "staging-acceptance"
DEFAULT_REPO = "AkkiaLab/AkkiaBot"
_SHA = re.compile(r"^[0-9a-f]{40}$")

Runner = Callable[..., Any]


def check_sha(sha: str) -> str:
    if not _SHA.fullmatch(sha):
        raise E2EError("--record-status 需要完整的 40 位小写 commit sha")
    return sha


def _gh_json(argv: Sequence[str], run: Runner, what: str) -> Any:
    result = run(list(argv), text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise E2EError(f"{what}失败（gh 退出码 {result.returncode}）")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise E2EError(f"{what}失败：gh 输出不是 JSON") from exc


def check_baseline(
    baseline: str,
    sha: str,
    *,
    repo: str = DEFAULT_REPO,
    run: Runner = subprocess.run,
    gh: Sequence[str] = ("gh",),
) -> None:
    """A diff run's baseline must be an accepted ancestor of ``sha``.

    Checked before anything is sent to Telegram: the latest
    ``staging-acceptance`` status of ``baseline`` is ``success`` and ``sha``
    is strictly ahead of it, so the accepted versions form one chain.
    """
    check_sha(baseline)
    check_sha(sha)
    if baseline == sha:
        raise E2EError("--baseline 不能等于要验收的 commit")
    combined = _gh_json(
        [*gh, "api", f"repos/{repo}/commits/{baseline}/status"], run, "读取基线 commit status "
    )
    statuses = combined.get("statuses", []) if isinstance(combined, dict) else []
    state = next(
        (
            s.get("state")
            for s in statuses
            if isinstance(s, dict) and s.get("context") == STATUS_CONTEXT
        ),
        None,
    )
    if state != "success":
        raise E2EError(
            f"基线 {baseline} 的 {STATUS_CONTEXT} 是 {state or '未写入'}，"
            "不是 success；基线必须是已通过 staging 验收的 commit"
        )
    compare = _gh_json(
        [*gh, "api", f"repos/{repo}/compare/{baseline}...{sha}"], run, "比较基线与目标 commit "
    )
    status = compare.get("status") if isinstance(compare, dict) else None
    if status != "ahead":
        raise E2EError(
            f"{sha} 相对基线 {baseline} 的关系是 {status or '未知'}，不是 ahead；"
            "基线必须是目标 commit 的祖先"
        )


async def deployed_webapp_sha(web: WebProbe, webapp_url: str) -> str | None:
    """The commit the deployed Mini App was built from (``/version.json``)."""
    result = await web.get(f"{webapp_url}/version.json")
    data = result.json() if result.status == 200 else None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) else None


async def guarded_run(
    sha: str,
    webapp_url: str,
    web: WebProbe,
    run: Callable[[], Awaitable[Report]],
    baseline: str | None = None,
) -> Report:
    """Run the acceptance only against ``sha`` and prove it stayed ``sha`` throughout.

    Call it while holding the run lock. The fixed staging alias is overwritten
    by every master push, so the deployed version is read right before the run
    (mismatch: nothing is sent) and again after it; if it changed or is unknown
    at the end, the report fails and can never be recorded as a success.
    """
    check_sha(sha)
    before = await deployed_webapp_sha(web, webapp_url)
    if before != sha:
        raise E2EError(
            f"{webapp_url} 当前部署的是 {before or '未知版本'}，不是要验收的 {sha}；"
            "先把该 commit 发布到 staging"
        )
    report = await run()
    after = await deployed_webapp_sha(web, webapp_url)
    report.release = {
        "target_sha": sha,
        "baseline_sha": baseline,
        "webapp_sha_at_start": before,
        "webapp_sha_at_end": after,
    }
    if after != sha:
        note = f"验收期间 staging Mini App 由 {sha} 变为 {after or '未知版本'}，结果不能作为 {sha} 的验收"
        report.detail = f"{report.detail}；{note}" if report.detail else note
        report.status = "failed"
    return report


def release_consistent(report: Report, sha: str) -> bool:
    release = report.release or {}
    return (
        release.get("target_sha") == sha
        and release.get("webapp_sha_at_start") == sha
        and release.get("webapp_sha_at_end") == sha
    )


def describe(report: Report, sha: str | None = None) -> tuple[str, str]:
    """GitHub status ``state`` and a short description (max 140 chars).

    With ``sha``, success additionally requires that the run verified that exact
    version at its start and end (:func:`guarded_run`). A full run must execute
    every scenario; a diff run (``baseline_sha`` in the release record) must
    execute every selected scenario, and only unselected ones may be skipped
    (``Report.finish``). Skipped never counts as passed.
    """
    total = len(report.results)
    passed = sum(r.status == "passed" for r in report.results)
    skipped = sum(r.status == "skipped" for r in report.results)
    baseline = (report.release or {}).get("baseline_sha") if sha is not None else None
    ran = [r for r in report.results if r.skip_reason != "not_selected"]
    if baseline:
        ok = report.status == "passed"
        parts = [
            f"差异验收（基线 {baseline[:7]}）：{passed}/{len(ran)} 个选中场景通过"
            f"（运行 {report.run_id}）",
            f"{total - len(ran)} 个未涉及差异、未执行",
        ]
        if skipped > total - len(ran):
            parts.append(f"{skipped - (total - len(ran))} 个选中场景跳过（不计为通过）")
    else:
        ok = report.status == "passed" and report.fully_executed()
        parts = [f"{passed}/{total} 个场景通过（运行 {report.run_id}）"]
        if skipped:
            parts.append(f"{skipped} 个跳过（不计为通过）")
    ok = ok and (sha is None or release_consistent(report, sha))
    if sha is not None and not release_consistent(report, sha):
        parts.append("验收期间版本未保持一致")
    if report.not_automated:
        parts.append(f"{len(report.not_automated)} 项未自动化，见报告")
    if baseline:
        # Last: the only part that may be cut off; the full list is in the report.
        parts.append("场景 " + "、".join(r.name for r in ran))
    text = "；".join(parts)
    if len(text) > 140:
        text = text[:139] + "…"
    return ("success" if ok else "failure"), text


def record_status(
    sha: str,
    report: Report,
    *,
    repo: str = DEFAULT_REPO,
    run: Runner = subprocess.run,
    gh: Sequence[str] = ("gh",),
) -> str:
    """Write the commit status with the GitHub CLI; returns the state written."""
    state, description = describe(report, check_sha(sha))
    payload = json.dumps({"state": state, "context": STATUS_CONTEXT, "description": description})
    result = run(
        [*gh, "api", "-X", "POST", f"repos/{repo}/statuses/{check_sha(sha)}", "--input", "-"],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise E2EError(f"写入 commit status 失败（gh 退出码 {result.returncode}）")
    return state
