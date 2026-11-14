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
from collections.abc import Callable, Sequence
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


def describe(report: Report) -> tuple[str, str]:
    """GitHub status ``state`` and a short description (max 140 chars)."""
    total = len(report.results)
    passed = sum(r.status == "passed" for r in report.results)
    state = "success" if report.status == "passed" else "failure"
    text = f"{passed}/{total} 个场景通过（运行 {report.run_id}）"
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
    state, description = describe(report)
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
