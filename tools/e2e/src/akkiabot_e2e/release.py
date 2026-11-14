"""Staging release acceptance record (docs/webapp.md, "发布流程").

A full acceptance run against the deployed staging environment can record its
outcome as the GitHub commit status ``staging-acceptance`` on the commit that
staging runs. The production promotion workflow (webapp-promote.yml) only
accepts commits whose latest ``staging-acceptance`` status is ``success``.
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


async def deployed_webapp_sha(web: WebProbe, webapp_url: str) -> str | None:
    """The commit the deployed Mini App was built from (``/version.json``)."""
    result = await web.get(f"{webapp_url}/version.json")
    data = result.json() if result.status == 200 else None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) else None


async def guarded_run(
    sha: str, webapp_url: str, web: WebProbe, run: Callable[[], Awaitable[Report]]
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
    report.release = {"target_sha": sha, "webapp_sha_at_start": before, "webapp_sha_at_end": after}
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
    version at its start and end (:func:`guarded_run`).
    """
    total = len(report.results)
    passed = sum(r.status == "passed" for r in report.results)
    ok = report.status == "passed" and (sha is None or release_consistent(report, sha))
    state = "success" if ok else "failure"
    text = f"{passed}/{total} 个场景通过（运行 {report.run_id}）"
    if sha is not None and not release_consistent(report, sha):
        text += "；验收期间版本未保持一致"
    if report.not_automated:
        text += f"；{len(report.not_automated)} 项未自动化，见报告"
    return state, text[:140]


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
