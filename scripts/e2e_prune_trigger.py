"""Weekly check: was there a staging release this week? If so, trigger the group clean-up.

Used by ``.github/workflows/e2e-prune-trigger.yml`` (BOT-30). Every staging
release acceptance (``just e2e-staging --record-status <sha>``) creates a test
group and writes the commit status ``staging-acceptance``. This script asks the
GitHub API whether any such status was written in the last ``WINDOW_DAYS`` days.

* Yes: POST to the Multica autopilot webhook (URL from the
  ``MULTICA_PRUNE_WEBHOOK_URL`` secret), which starts an Engineer task that runs
  ``just e2e-prune-groups`` on the machine holding the test account sessions.
* No: exit without calling anything, so a quiet week costs no Multica run.

CI never touches Telegram sessions. The webhook URL is a secret: it is never
printed, and errors only report the HTTP status.

Environment: ``GITHUB_TOKEN``, ``GITHUB_REPOSITORY``, ``MULTICA_PRUNE_WEBHOOK_URL``
(only needed when there was a release), optional ``GITHUB_API_URL``,
``GITHUB_RUN_ID``, ``GITHUB_SERVER_URL``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

STATUS_CONTEXT = "staging-acceptance"
WINDOW_DAYS = 7
#: Release acceptance records the deployed master commit, which may be older
#: than the window; look this far back for candidate commits.
COMMIT_LOOKBACK_DAYS = 60
MAX_COMMITS = 100

#: GET returning parsed JSON (url -> data).
Fetch = Callable[[str], Any]
#: POST a JSON body with headers; returns the HTTP status.
Post = Callable[[str, bytes, Mapping[str, str]], int]


@dataclass(frozen=True, slots=True)
class Release:
    sha: str
    state: str
    created_at: datetime


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def recent_releases(
    fetch: Fetch, api: str, repo: str, now: datetime, *, window_days: int = WINDOW_DAYS
) -> list[Release]:
    """``staging-acceptance`` statuses written within the window, newest first."""
    since = (now - timedelta(days=COMMIT_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = fetch(f"{api}/repos/{repo}/commits?sha=master&since={since}&per_page={MAX_COMMITS}")
    cutoff = now - timedelta(days=window_days)
    found: list[Release] = []
    for commit in commits:
        sha = commit["sha"]
        statuses = fetch(f"{api}/repos/{repo}/commits/{sha}/statuses?per_page=100")
        for status in statuses:
            if status.get("context") != STATUS_CONTEXT:
                continue
            created = _parse_time(status["created_at"])
            if created >= cutoff:
                found.append(Release(sha, status.get("state", ""), created))
    found.sort(key=lambda r: r.created_at, reverse=True)
    return found


def iso_week(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def webhook_payload(releases: list[Release], repo: str, now: datetime, run_url: str) -> bytes:
    return json.dumps(
        {
            "event": "staging-acceptance-groups-prune",
            "repository": repo,
            "week": iso_week(now),
            "releases": [
                {"sha": r.sha, "state": r.state, "created_at": r.created_at.isoformat()}
                for r in releases
            ],
            "workflow_run": run_url,
        },
        ensure_ascii=False,
    ).encode()


def run(
    env: Mapping[str, str],
    fetch: Fetch,
    post: Post,
    now: datetime,
    out: Callable[[str], None] = print,
) -> int:
    api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = env["GITHUB_REPOSITORY"]
    releases = recent_releases(fetch, api, repo, now)
    if not releases:
        out(f"过去 {WINDOW_DAYS} 天没有 {STATUS_CONTEXT} 状态（没有 staging 发版），不触发清理。")
        return 0
    out(f"过去 {WINDOW_DAYS} 天有 {len(releases)} 次 staging 发版验收：")
    for r in releases:
        out(f"- {r.sha[:12]} {r.state} {r.created_at.isoformat()}")
    url = env.get("MULTICA_PRUNE_WEBHOOK_URL", "").strip()
    if not url:
        out("::error::缺少 Actions secret MULTICA_PRUNE_WEBHOOK_URL，无法触发清理任务")
        return 1
    run_url = ""
    if env.get("GITHUB_RUN_ID"):
        server = env.get("GITHUB_SERVER_URL", "https://github.com")
        run_url = f"{server}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}"
    headers = {
        "Content-Type": "application/json",
        # One clean-up per ISO week even if the workflow is re-run or retried.
        "Idempotency-Key": f"akkiabot-e2e-prune-{iso_week(now)}",
    }
    status = post(url, webhook_payload(releases, repo, now, run_url), headers)
    if not 200 <= status < 300:
        out(f"::error::调用 Multica webhook 失败：HTTP {status}")
        return 1
    out(f"已触发 Multica 清理任务（HTTP {status}）。")
    return 0


def _github_fetch(token: str) -> Fetch:
    def fetch(url: str) -> Any:
        request = urllib.request.Request(  # noqa: S310 - fixed https API base
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)

    return fetch


def _post(url: str, body: bytes, headers: Mapping[str, str]) -> int:
    if not url.startswith("https://"):
        return 0  # refuse anything but https; reported as a failed call
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        # The reason may echo the host; the URL is a secret, so report nothing else.
        return 0


def main() -> int:
    env = os.environ
    return run(env, _github_fetch(env["GITHUB_TOKEN"]), _post, datetime.now(UTC))


if __name__ == "__main__":
    sys.exit(main())
