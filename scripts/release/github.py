"""GitHub (and Multica webhook) access for the release flow.

``GitHub`` is the interface the logic depends on; ``GhCli`` implements it with
``gh api`` so the token stays in ``gh``'s environment (``GH_TOKEN`` in Actions,
the logged-in ``gh`` on the runtime machine) and never passes through Python.
Tests use an in-memory fake (tests/release/conftest.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from scripts.release.versions import ReleaseError

ACCEPTANCE_CONTEXT = "staging-acceptance"
#: Ruleset whose ``include`` list holds every sealed release branch (docs/release.md §4).
SEALED_RULESET = "releases 封版"
#: Rule types the sealed ruleset must enforce on a branch before a formal tag is created.
SEALED_RULE_TYPES = frozenset({"update", "deletion", "non_fast_forward"})
CONFLICT_LABEL = "release-forward-conflict"
FORWARD_LABEL = "release-forward"
FIX_LABEL = "release:fix"


@dataclass
class PullRequest:
    number: int
    title: str
    base: str
    head_ref: str
    head_sha: str
    author: str
    labels: set[str] = field(default_factory=set)
    state: str = "open"
    merged: bool = False


@dataclass
class Issue:
    number: int
    body: str
    url: str = ""


class GitHub(Protocol):
    def acceptance(self, sha: str) -> str | None: ...
    def sealed_branches(self) -> set[str]: ...
    def set_sealed(self, branch: str, *, sealed: bool) -> None: ...
    def branch_rule_types(self, branch: str) -> set[str]: ...
    def find_issue(self, marker: str) -> Issue | None: ...
    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue: ...
    def comment_issue(self, number: int, body: str) -> None: ...
    def close_issue(self, number: int, comment: str) -> None: ...
    def prs_for_commit(self, sha: str) -> list[PullRequest]: ...
    def open_prs(self, base: str | None = None) -> list[PullRequest]: ...
    def get_pr(self, number: int) -> PullRequest: ...
    def retarget_pr(self, number: int, base: str) -> None: ...
    def comment_pr(self, number: int, body: str) -> None: ...
    def pr_approved(self, number: int) -> bool: ...
    def checks_passed(self, sha: str, contexts: tuple[str, ...]) -> bool: ...
    def approved_run(self, workflow: str, title: str, job: str, environment: str) -> bool: ...
    def dispatch(self, workflow: str, inputs: dict[str, str], ref: str = "master") -> None: ...
    def notify(self, payload: dict[str, Any]) -> str: ...


class GhError(ReleaseError):
    pass


class GhCli:
    """``GitHub`` over ``gh api``; ``repo`` is ``owner/name``."""

    def __init__(self, repo: str, *, webhook_env: str = "MULTICA_RELEASE_WEBHOOK_URL") -> None:
        self.repo = repo
        self.webhook_env = webhook_env

    # ----------------------------------------------------------- plumbing
    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        paginate: bool = False,
        allow_404: bool = False,
    ) -> Any:
        args = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json"]
        if paginate:
            args += ["--paginate", "--slurp"]
        args.append(path)
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        proc = subprocess.run(  # noqa: S603 - fixed executable; token stays inside gh
            args, input=stdin, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            if allow_404 and "HTTP 404" in proc.stderr:
                return None
            raise GhError(f"gh api {method} {path} 失败：{proc.stderr.strip()[:300]}")
        if not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        if paginate:
            merged: list[Any] = []
            for page in data:
                merged.extend(page if isinstance(page, list) else [page])
            return merged
        return data

    def _r(self, path: str) -> str:
        return f"repos/{self.repo}/{path}"

    # ----------------------------------------------------------- statuses
    def acceptance(self, sha: str) -> str | None:
        statuses = self._api(self._r(f"commits/{sha}/statuses?per_page=100"), paginate=True)
        for status in statuses or []:  # newest first
            if status.get("context") == ACCEPTANCE_CONTEXT:
                return status.get("state")
        return None

    def checks_passed(self, sha: str, contexts: tuple[str, ...]) -> bool:
        pages = self._api(self._r(f"commits/{sha}/check-runs?per_page=100"), paginate=True) or []
        conclusions: dict[str, str | None] = {}
        for page in pages:
            for run in page.get("check_runs", []) if isinstance(page, dict) else []:
                conclusions.setdefault(run["name"], run.get("conclusion"))
        statuses = self._api(self._r(f"commits/{sha}/status")) or {}
        for status in statuses.get("statuses", []):
            conclusions.setdefault(status["context"], status.get("state"))
        return all(conclusions.get(c) == "success" for c in contexts)

    # ------------------------------------------------------------ rulesets
    def _sealed_ruleset(self) -> dict[str, Any] | None:
        rulesets = self._api(self._r("rulesets?per_page=100"), paginate=True) or []
        for ruleset in rulesets:
            if ruleset.get("name") == SEALED_RULESET:
                return self._api(self._r(f"rulesets/{ruleset['id']}"))
        return None

    def sealed_branches(self) -> set[str]:
        """Branches listed in the sealed ruleset; none while it does not exist yet."""
        ruleset = self._sealed_ruleset()
        if ruleset is None:
            return set()
        include = ruleset["conditions"]["ref_name"]["include"]
        return {ref.removeprefix("refs/heads/") for ref in include}

    def set_sealed(self, branch: str, *, sealed: bool) -> None:
        ruleset = self._sealed_ruleset()
        if ruleset is None:
            raise GhError(
                f"仓库中没有名为 {SEALED_RULESET!r} 的规则集，无法锁定（接入步骤见 docs/release.md §7）"
            )
        ref = f"refs/heads/{branch}"
        include = list(ruleset["conditions"]["ref_name"]["include"])
        if sealed and ref not in include:
            include.append(ref)
        elif not sealed and ref in include:
            include.remove(ref)
        else:
            return
        conditions = ruleset["conditions"]
        conditions["ref_name"]["include"] = include
        body = {
            "name": ruleset["name"],
            "target": ruleset["target"],
            "enforcement": ruleset["enforcement"],
            "conditions": conditions,
            "rules": ruleset["rules"],
            "bypass_actors": ruleset.get("bypass_actors", []),
        }
        self._api(self._r(f"rulesets/{ruleset['id']}"), method="PUT", body=body)

    def branch_rule_types(self, branch: str) -> set[str]:
        quoted = urllib.parse.quote(branch, safe="")
        rules = self._api(self._r(f"rules/branches/{quoted}?per_page=100"), paginate=True) or []
        return {rule["type"] for rule in rules}

    # -------------------------------------------------------------- issues
    def find_issue(self, marker: str) -> Issue | None:
        issues = self._api(
            self._r(f"issues?state=open&labels={CONFLICT_LABEL}&per_page=100"), paginate=True
        )
        for issue in issues or []:
            if marker in (issue.get("body") or ""):
                return Issue(issue["number"], issue["body"], issue.get("html_url", ""))
        return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        data = self._api(
            self._r("issues"), method="POST", body={"title": title, "body": body, "labels": labels}
        )
        return Issue(data["number"], data["body"], data.get("html_url", ""))

    def comment_issue(self, number: int, body: str) -> None:
        self._api(self._r(f"issues/{number}/comments"), method="POST", body={"body": body})

    def close_issue(self, number: int, comment: str) -> None:
        self.comment_issue(number, comment)
        self._api(self._r(f"issues/{number}"), method="PATCH", body={"state": "closed"})

    # ----------------------------------------------------------- pull requests
    @staticmethod
    def _pr(data: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=data["number"],
            title=data["title"],
            base=data["base"]["ref"],
            head_ref=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            author=data["user"]["login"],
            labels={label["name"] for label in data.get("labels", [])},
            state=data.get("state", "open"),
            merged=bool(data.get("merged_at")),
        )

    def prs_for_commit(self, sha: str) -> list[PullRequest]:
        data = self._api(self._r(f"commits/{sha}/pulls"), allow_404=True) or []
        return [self._pr(pr) for pr in data]

    def open_prs(self, base: str | None = None) -> list[PullRequest]:
        query = "state=open&per_page=100" + (f"&base={urllib.parse.quote(base)}" if base else "")
        return [self._pr(pr) for pr in self._api(self._r(f"pulls?{query}"), paginate=True) or []]

    def get_pr(self, number: int) -> PullRequest:
        return self._pr(self._api(self._r(f"pulls/{number}")))

    def retarget_pr(self, number: int, base: str) -> None:
        self._api(self._r(f"pulls/{number}"), method="PATCH", body={"base": base})

    def comment_pr(self, number: int, body: str) -> None:
        self.comment_issue(number, body)

    def pr_approved(self, number: int) -> bool:
        """Approved by someone other than the author *on the current head*, and no
        outstanding Request changes (newest review per reviewer wins)."""
        pr = self.get_pr(number)
        reviews = self._api(self._r(f"pulls/{number}/reviews?per_page=100"), paginate=True) or []
        latest: dict[str, tuple[str, str]] = {}
        for review in reviews:  # oldest first
            login = review["user"]["login"]
            if login != pr.author and review["state"] in {
                "APPROVED",
                "CHANGES_REQUESTED",
                "DISMISSED",
            }:
                latest[login] = (review["state"], review.get("commit_id", ""))
        states = latest.values()
        approved_head = any(st == "APPROVED" and sha == pr.head_sha for st, sha in states)
        return approved_head and all(st != "CHANGES_REQUESTED" for st, _ in states)

    # ------------------------------------------------------------ workflows
    def approved_run(self, workflow: str, title: str, job: str, environment: str) -> bool:
        """The newest master run of ``workflow`` named ``title`` had ``job`` succeed after an
        approval of ``environment`` (the run's review history, not just a green job).

        Only the newest run counts, so a rollback or retry of an old tag needs a
        fresh approval instead of reusing the one given at its first release."""
        runs = self._api(
            self._r(
                f"actions/workflows/{workflow}/runs?branch=master&event=workflow_dispatch&per_page=100"
            )
        )
        for run in (runs or {}).get("workflow_runs", []):  # newest first
            if run.get("display_title") != title or run.get("head_branch") != "master":
                continue
            jobs = self._api(self._r(f"actions/runs/{run['id']}/jobs?per_page=100")) or {}
            if not any(
                j["name"] == job and j.get("conclusion") == "success" for j in jobs.get("jobs", [])
            ):
                return False
            approvals = self._api(self._r(f"actions/runs/{run['id']}/approvals")) or []
            return any(
                approval.get("state") == "approved"
                and environment in {env.get("name") for env in approval.get("environments", [])}
                for approval in approvals
            )
        return False

    def dispatch(self, workflow: str, inputs: dict[str, str], ref: str = "master") -> None:
        self._api(
            self._r(f"actions/workflows/{workflow}/dispatches"),
            method="POST",
            body={"ref": ref, "inputs": inputs},
        )

    # -------------------------------------------------------------- webhook
    def notify(self, payload: dict[str, Any]) -> str:
        """POST to the Multica autopilot webhook. The URL is a secret: never printed."""
        url = os.environ.get(self.webhook_env, "")
        if not url:
            return f"未配置 {self.webhook_env}，未通知 Multica"
        if not url.startswith("https://"):
            return f"{self.webhook_env} 不是 https 地址，未通知 Multica"
        request = urllib.request.Request(  # noqa: S310 - https enforced above
            url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return f"已通知 Multica（HTTP {response.status}）"
        except urllib.error.HTTPError as exc:
            return f"通知 Multica 失败（HTTP {exc.code}）"
        except (urllib.error.URLError, TimeoutError) as exc:
            return f"通知 Multica 失败（{type(exc).__name__}）"
