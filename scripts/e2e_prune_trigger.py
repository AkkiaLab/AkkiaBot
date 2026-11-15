"""Weekly check: was there a staging release this week? If so, trigger the group clean-up.

Used by ``.github/workflows/e2e-prune-trigger.yml`` (BOT-30). Every staging
release acceptance (``just e2e-staging --record-status <sha>``) creates a test
group and writes the commit status ``staging-acceptance``. This script asks the
GitHub GraphQL API whether any such status was written in the last
``WINDOW_DAYS`` days, scanning the whole master history page by page; an
incomplete scan fails the job instead of reporting "no release".

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
PAGE_SIZE = 100
#: Upper bound on history pages (100 commits each). Exceeding it fails the job
#: instead of reporting "no release" from an incomplete scan.
MAX_PAGES = 100

#: GraphQL call (query, variables) -> response JSON (``data`` / ``errors``).
Graphql = Callable[[str, Mapping[str, Any]], Any]
#: POST a JSON body with headers; returns the HTTP status.
Post = Callable[[str, bytes, Mapping[str, str]], int]

# Release acceptance records the deployed master commit (docs/e2e-acceptance.md
# §6.2), which may be arbitrarily old, so every master commit is checked. A
# commit's `status.contexts` holds the newest status of each context (like the
# REST combined status): if the newest `staging-acceptance` status is older
# than the window, no status of that context was written within it.
HISTORY_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: "refs/heads/master") {
      target {
        ... on Commit {
          history(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes { oid status { contexts { context state createdAt } } }
          }
        }
      }
    }
  }
}
"""


class IncompleteScan(Exception):  # noqa: N818 - reads better in the job log
    """The history could not be scanned completely; never reported as "no release"."""


@dataclass(frozen=True, slots=True)
class Release:
    sha: str
    state: str
    created_at: datetime


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def recent_releases(
    graphql: Graphql,
    repo: str,
    now: datetime,
    *,
    window_days: int = WINDOW_DAYS,
    max_pages: int = MAX_PAGES,
) -> list[Release]:
    """Master commits whose newest ``staging-acceptance`` status is within the window."""
    owner, name = repo.split("/", 1)
    cutoff = now - timedelta(days=window_days)
    found: list[Release] = []
    after: str | None = None
    for _ in range(max_pages):
        variables = {"owner": owner, "name": name, "first": PAGE_SIZE, "after": after}
        response = graphql(HISTORY_QUERY, variables)
        if response.get("errors"):
            messages = "; ".join(str(e.get("message", e)) for e in response["errors"])
            raise IncompleteScan(f"GitHub GraphQL 返回错误：{messages}")
        ref = (((response.get("data") or {}).get("repository") or {}).get("ref")) or {}
        history = (ref.get("target") or {}).get("history")
        if history is None:
            raise IncompleteScan("没有读到 master 的提交历史")
        for commit in history["nodes"]:
            for context in (commit.get("status") or {}).get("contexts") or ():
                if context.get("context") != STATUS_CONTEXT:
                    continue
                created = _parse_time(context["createdAt"])
                if created >= cutoff:
                    found.append(Release(commit["oid"], str(context["state"]).lower(), created))
        page = history["pageInfo"]
        if not page["hasNextPage"]:
            found.sort(key=lambda r: r.created_at, reverse=True)
            return found
        after = page["endCursor"]
    raise IncompleteScan(
        f"master 提交超过 {max_pages * PAGE_SIZE} 个，未能检查完整，请调大 MAX_PAGES"
    )


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
    graphql: Graphql,
    post: Post,
    now: datetime,
    out: Callable[[str], None] = print,
) -> int:
    repo = env["GITHUB_REPOSITORY"]
    try:
        releases = recent_releases(graphql, repo, now)
    except IncompleteScan as exc:
        out(f"::error::无法完整判断过去 {WINDOW_DAYS} 天是否有 staging 发版：{exc}")
        return 1
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


def _github_graphql(token: str, api: str) -> Graphql:
    def graphql(query: str, variables: Mapping[str, Any]) -> Any:
        request = urllib.request.Request(  # noqa: S310 - fixed https API base
            f"{api}/graphql",
            data=json.dumps({"query": query, "variables": dict(variables)}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return json.load(response)
        except urllib.error.URLError as exc:
            raise IncompleteScan(f"调用 GitHub GraphQL 失败：{exc}") from None

    return graphql


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
    api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    return run(env, _github_graphql(env["GITHUB_TOKEN"], api), _post, datetime.now(UTC))


if __name__ == "__main__":
    sys.exit(main())
