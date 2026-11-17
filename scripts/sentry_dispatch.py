"""Sentry issue → Multica squad task dispatcher (BOT-55).

Runs on the Multica runtime host, called by the "Sentry 报错分派" autopilot:

* ``dispatch --issue-id N --reason webhook:<action>``: one webhook from the
  Sentry relay (``deploy/sentry-relay``). Only the numeric issue id is taken
  from the webhook; everything else is read back from the Sentry API, so a
  forged or out-of-order webhook converges to Sentry's *current* state.
* ``reconcile``: scheduled catch-up for webhooks lost while the runtime was
  offline. It keeps a success cursor in the state directory (with an overlap
  window) and only advances it when every issue and page was processed.
* ``init-cursor``: start reconciliation from now, without backfilling.

De-duplication, in order:

1. A host-local ``flock`` serialises lookup → create → link, so two webhooks for
   the same issue cannot both see "no task" on this host.
2. Lookup by issue metadata ``sentry_issue=<key>`` and by the fixed marker
   ``sentry-issue-key=<key>`` written into the description *in the create
   request itself*. A task whose metadata write was interrupted is still found
   by its marker, and the metadata is filled in then.
3. Multica refuses to create an active issue whose title duplicates another one
   in the same project (server-side lock); the title embeds the Sentry short id.
   After a failed or timed-out create, the dispatcher looks the task up again
   before retrying, never blindly.

Sentry "resolved" only syncs status and evidence; tasks are never closed here.
A regression reopens a done/cancelled task to ``todo``; a task still being
worked on only gets a comment.

Secrets: the Sentry read-only token comes from ``SENTRY_READONLY_TOKEN`` or from
1Password (``op read``) and is never printed. Output contains no free text from
Sentry beyond a redacted, truncated title and culprit.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

SENTRY_API = "https://sentry.io/api/0"
OP_TOKEN_REF = "op://AkkiaBot/sentry-webhook/sentry_readonly_token"  # noqa: S105 (reference, not a value)
DEFAULT_OP_TOKEN_FILE = "~/.config/akkiabot/op-service-account-token"  # noqa: S105 (path)
DEFAULT_STATE_DIR = "~/.config/akkiabot/sentry-dispatch"
META_KEY = "sentry_issue"
META_SOURCE = "sentry_source"
META_STATE = "sentry_state"
SOURCE_VALUE = "bot-55-sentry"
MARKER_PREFIX = "sentry-issue-key="
TEST_MARKER = "[BOT-55-WEBHOOK-TEST]"
REGRESSION_SUBSTATUSES = frozenset({"regressed", "escalating"})
CLOSED_TASK_STATUSES = frozenset({"done", "cancelled"})
RECONCILE_OVERLAP = timedelta(hours=1)
MAX_PAGES = 50
CLI_TIMEOUT_SECONDS = 60

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Authorization headers: the scheme (Bearer, Basic, ...) and the whole value.
    (
        re.compile(
            r"(?i)\b(proxy-authorization|authorization)\b[\"']?\s*[:=]\s*[\"']?"
            r"(?:[a-z][\w-]*\s+)?[^\s,;\"']+"
        ),
        r"\1=[redacted]",
    ),
    # Bare auth schemes, e.g. "Bearer <value>" without a header name.
    (re.compile(r"(?i)\b(bearer|basic|digest|negotiate|token)\s+[^\s,;\"']{4,}"), r"\1 [redacted]"),
    # key=value / key: value where the key names a credential.
    (
        re.compile(
            r"(?i)\b([\w-]*(?:token|secret|passw(?:or)?d|pwd|key|bearer|basic|credential"
            r"|cookie|session|signature|dsn|auth)[\w-]*)\b[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+"
        ),
        r"\1=[redacted]",
    ),
    # URLs: keep scheme and host only (drops userinfo, path, query and fragment).
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://(?:[^\s/@]*@)?([^\s/?#:@]+)[^\s]*"), r"\1://\2/…"),
    # Scheme-less userinfo such as user:pass@host.
    (re.compile(r"[^\s:/@]+:[^\s@/]+@"), "[credential]@"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?"), "[jwt]"),
    (re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b"), "[token]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "[ip]"),
    (re.compile(r"-100\d{6,}"), "[chat]"),
    (re.compile(r"\b[A-Za-z0-9_\-+/=]{32,}"), "[redacted]"),
]


def redact(text: str | None, limit: int = 120) -> str:
    """Strip known secret/personal shapes from Sentry free text, then truncate."""
    value = " ".join((text or "").split()).replace("`", "'")
    for pattern, repl in _REDACTIONS:
        value = pattern.sub(repl, value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def issue_key(org: str, project_slug: str, issue_id: str) -> str:
    return f"{org}/{project_slug}/{issue_id}".lower()


def marker(key: str) -> str:
    return f"{MARKER_PREFIX}{key}"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DispatchError(RuntimeError):
    """A step failed; the message never contains secrets or Sentry free text."""


# ----------------------------------------------------------------- Sentry API


class SentryApi(Protocol):
    def get_issue(self, issue_id: str) -> dict[str, Any] | None: ...
    def tag_values(self, issue_id: str, key: str) -> list[tuple[str, int]]: ...
    def project_id(self, slug: str) -> str: ...
    def issues_seen_since(self, project_id: str, since: datetime) -> Iterator[dict[str, Any]]: ...


Http = Callable[[str, Mapping[str, str]], tuple[int, Mapping[str, str], bytes]]


def _urllib_get(url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
    req = urllib.request.Request(url, headers=dict(headers))  # noqa: S310 (fixed https base)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), b""


_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next";\s*results="true"')


@dataclass
class SentryClient:
    token: str
    org: str
    http: Http = _urllib_get
    base: str = SENTRY_API

    def _get(self, path_or_url: str) -> tuple[Any, Mapping[str, str]]:
        url = path_or_url if path_or_url.startswith("https://") else self.base + path_or_url
        status, headers, body = self.http(url, {"Authorization": f"Bearer {self.token}"})
        if status == 404:
            return None, headers
        if status != 200:
            raise DispatchError(f"Sentry API HTTP {status}")
        return json.loads(body), headers

    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        data, _ = self._get(f"/organizations/{self.org}/issues/{issue_id}/")
        return data

    def tag_values(self, issue_id: str, key: str) -> list[tuple[str, int]]:
        data, _ = self._get(f"/organizations/{self.org}/issues/{issue_id}/tags/{key}/")
        if not data:
            return []
        return [(str(v.get("value")), int(v.get("count") or 0)) for v in data.get("topValues", [])]

    def project_id(self, slug: str) -> str:
        data, _ = self._get(f"/projects/{self.org}/{slug}/")
        if not data:
            raise DispatchError("Sentry project not found")
        return str(data["id"])

    def issues_seen_since(self, project_id: str, since: datetime) -> Iterator[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "project": project_id,
                "query": f"lastSeen:>={since.strftime('%Y-%m-%dT%H:%M:%S')}",
                "sort": "date",
                "limit": 100,
            }
        )
        url = f"{self.base}/organizations/{self.org}/issues/?{query}"
        for _ in range(MAX_PAGES):
            data, headers = self._get(url)
            yield from data or []
            match = _NEXT_LINK.search(headers.get("Link") or headers.get("link") or "")
            if not match:
                return
            url = match.group(1)
        raise DispatchError("Sentry pagination exceeded MAX_PAGES; cursor not advanced")


# ---------------------------------------------------------------- Multica CLI


class Tasks(Protocol):
    def find_by_metadata(self, key: str) -> list[dict[str, Any]]: ...
    def find_by_marker(self, key: str) -> list[dict[str, Any]]: ...
    def list_linked(self) -> list[dict[str, Any]]: ...
    def create(self, title: str, description: str, status: str, priority: str) -> None: ...
    def set_metadata(self, task_id: str, key: str, value: str) -> None: ...
    def comment(self, task_id: str, body: str) -> None: ...
    def set_status(self, task_id: str, status: str) -> None: ...


TASK_FIELDS = "id,identifier,number,status,metadata,description"


@dataclass
class MulticaCli:
    cli: str
    project_id: str
    squad_id: str
    workdir: Path

    def _run(self, *args: str) -> Any:
        try:
            proc = subprocess.run(  # noqa: S603 (fixed CLI path, argv list)
                [self.cli, *args],
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as err:
            raise DispatchError(f"multica {args[0]} {args[1]} timed out") from err
        if proc.returncode != 0:
            raise DispatchError(f"multica {args[0]} {args[1]} failed (exit {proc.returncode})")
        out = proc.stdout.strip()
        return json.loads(out) if out.startswith(("{", "[")) else None

    def _list(self, *filters: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._run(
                "issue",
                "list",
                *filters,
                "--project",
                self.project_id,
                "--fields",
                TASK_FIELDS,
                "--limit",
                "100",
                "--offset",
                str(offset),
                "--output",
                "json",
            )
            issues = page.get("issues", []) if isinstance(page, dict) else []
            found.extend(issues)
            if not (isinstance(page, dict) and page.get("has_more")) or not issues:
                return found
            offset += len(issues)

    def find_by_metadata(self, key: str) -> list[dict[str, Any]]:
        return self._list("--metadata", f"{META_KEY}={key}")

    def find_by_marker(self, key: str) -> list[dict[str, Any]]:
        page = self._run(
            "issue",
            "search",
            marker(key),
            "--include-closed",
            "--project",
            self.project_id,
            "--limit",
            "50",
            "--output",
            "json",
        )
        issues = page.get("issues", []) if isinstance(page, dict) else []
        # Search also matches comments; only the description marker counts.
        return [i for i in issues if marker(key) in (i.get("description") or "")]

    def list_linked(self) -> list[dict[str, Any]]:
        return self._list("--metadata", f"{META_SOURCE}={SOURCE_VALUE}")

    def create(self, title: str, description: str, status: str, priority: str) -> None:
        path = self.workdir / "sentry-dispatch-description.md"
        path.write_text(description, encoding="utf-8")
        try:
            self._run(
                "issue",
                "create",
                "--title",
                title,
                "--description-file",
                str(path),
                "--project",
                self.project_id,
                "--assignee-id",
                self.squad_id,
                "--status",
                status,
                "--priority",
                priority,
                "--output",
                "json",
            )
        finally:
            path.unlink(missing_ok=True)

    def set_metadata(self, task_id: str, key: str, value: str) -> None:
        self._run(
            "issue",
            "metadata",
            "set",
            task_id,
            "--key",
            key,
            "--value",
            value,
            "--type",
            "string",
            "--output",
            "json",
        )

    def comment(self, task_id: str, body: str) -> None:
        path = self.workdir / "sentry-dispatch-comment.md"
        path.write_text(body, encoding="utf-8")
        try:
            self._run(
                "issue", "comment", "add", task_id, "--content-file", str(path), "--output", "json"
            )
        finally:
            path.unlink(missing_ok=True)

    def set_status(self, task_id: str, status: str) -> None:
        self._run("issue", "status", task_id, status)


# -------------------------------------------------------------------- sync


@dataclass
class Snapshot:
    """What the dispatcher knows about one Sentry issue, from the API only."""

    issue_id: str
    short_id: str
    project_slug: str
    status: str
    substatus: str
    level: str
    title: str
    culprit: str
    permalink: str
    first_seen: str
    last_seen: str
    count: str
    user_count: int
    environments: list[tuple[str, int]] = field(default_factory=list)
    releases: list[tuple[str, int]] = field(default_factory=list)

    @property
    def state(self) -> str:
        return f"{self.status}/{self.substatus or '-'}"

    @property
    def is_test(self) -> bool:
        return TEST_MARKER in self.title

    def env_text(self) -> str:
        return "、".join(f"{redact(name, 40)}（{n}）" for name, n in self.environments) or "未知"


def load_snapshot(sentry: SentryApi, issue_id: str) -> Snapshot | None:
    raw = sentry.get_issue(issue_id)
    if not raw:
        return None
    return Snapshot(
        issue_id=str(raw["id"]),
        short_id=str(raw.get("shortId") or raw["id"]),
        project_slug=str((raw.get("project") or {}).get("slug") or ""),
        status=str(raw.get("status") or ""),
        substatus=str(raw.get("substatus") or ""),
        level=str(raw.get("level") or ""),
        title=str(raw.get("title") or ""),
        culprit=str(raw.get("culprit") or ""),
        permalink=str(raw.get("permalink") or ""),
        first_seen=str(raw.get("firstSeen") or ""),
        last_seen=str(raw.get("lastSeen") or ""),
        count=str(raw.get("count") or "0"),
        user_count=int(raw.get("userCount") or 0),
        environments=sentry.tag_values(issue_id, "environment"),
        releases=sentry.tag_values(issue_id, "release"),
    )


def task_title(snap: Snapshot) -> str:
    prefix = "[测试] " if snap.is_test else ""
    return f"{prefix}Sentry · {snap.short_id} · {redact(snap.title, 60)}"


def task_priority(snap: Snapshot) -> str:
    in_prod = any(name == "production" for name, _ in snap.environments)
    if snap.level in {"error", "fatal"}:
        return "high" if in_prod else "medium"
    return "low"


def task_description(snap: Snapshot, key: str) -> str:
    releases = "、".join(f"{redact(r, 60)}（{n}）" for r, n in snap.releases) or "未知"
    return f"""## Sentry 报错

- Sentry：[{snap.short_id}]({snap.permalink})（项目 `{snap.project_slug}`，issue id `{snap.issue_id}`）
- 环境：{snap.env_text()}
- 版本：{releases}
- 级别 / 状态：{snap.level} / {snap.state}
- 首次 / 最近：{snap.first_seen} / {snap.last_seen}
- 次数 / 影响用户：{snap.count} / {snap.user_count}
- 摘要（已脱敏）：`{redact(snap.title)}`
- 位置（已脱敏）：`{redact(snap.culprit)}`

## 处理约定

- 本任务由 Sentry webhook 自动分派创建（BOT-55）。Leader 指定 Engineer 或 Platform 唯一主责，小队归属不变。
- 分析时用 Sentry 只读 API 查看事件；请求体、用户信息、令牌等不写入评论或公开记录。
- Sentry 状态变化（resolved / 回归）会以评论同步到本任务。resolved 不会自动关闭本任务，仍按实现、评审和验收流程收尾。

关联标识（自动去重使用，请勿修改）：`{marker(key)}`
"""


@dataclass
class Dispatcher:
    sentry: SentryApi
    tasks: Tasks
    org: str
    target_project: str
    lock_path: Path
    log: Callable[[str], None] = print

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _find(self, key: str) -> list[dict[str, Any]]:
        # `issue search` returns an empty metadata object, so a task found by both
        # lookups keeps the `issue list` row, which carries the real metadata.
        by_id: dict[str, dict[str, Any]] = {}
        for task in [*self.tasks.find_by_metadata(key), *self.tasks.find_by_marker(key)]:
            by_id.setdefault(task["id"], task)
        return sorted(by_id.values(), key=lambda t: int(t.get("number") or 0))

    def _link(self, task: dict[str, Any], key: str) -> None:
        meta = task.get("metadata") or {}
        if meta.get(META_KEY) != key:
            self.tasks.set_metadata(task["id"], META_KEY, key)
        if meta.get(META_SOURCE) != SOURCE_VALUE:
            self.tasks.set_metadata(task["id"], META_SOURCE, SOURCE_VALUE)

    def _create(self, snap: Snapshot, key: str) -> dict[str, Any]:
        title, description = task_title(snap), task_description(snap, key)
        status = "backlog" if snap.is_test else "todo"
        last_error: DispatchError | None = None
        for _attempt in range(2):
            try:
                self.tasks.create(title, description, status, task_priority(snap))
            except DispatchError as err:
                # Timed out or refused (e.g. active duplicate title): the task may
                # exist anyway, so look again before deciding to retry.
                last_error = err
            found = self._find(key)
            if found:
                return found[0]
        raise DispatchError(f"task creation failed: {last_error}")

    def sync(self, issue_id: str, *, may_create: bool, reason: str) -> str:
        if not re.fullmatch(r"\d{1,20}", issue_id):
            raise DispatchError("issue id must be numeric")
        # The snapshot is read inside the lock: a snapshot taken before waiting
        # could be older than one another notification already applied.
        with self._locked():
            snap = load_snapshot(self.sentry, issue_id)
            if snap is None:
                return "sentry_issue_not_found"
            if snap.project_slug != self.target_project:
                return "project_not_target"
            key = issue_key(self.org, snap.project_slug, snap.issue_id)
            found = self._find(key)
            if not found:
                if not (may_create and snap.status == "unresolved"):
                    return "no_task_needed"
                task = self._create(snap, key)
                self._link(task, key)
                self.tasks.set_metadata(task["id"], META_STATE, snap.state)
                return f"created {task.get('identifier')}"
            task, duplicates = found[0], found[1:]
            self._link(task, key)
            outcome = self._update(task, snap, reason)
            if duplicates:
                ids = ", ".join(str(d.get("identifier")) for d in duplicates)
                outcome += f"; duplicates kept for Leader review: {ids}"
            return outcome

    def _update(self, task: dict[str, Any], snap: Snapshot, reason: str) -> str:
        previous = (task.get("metadata") or {}).get(META_STATE)
        if previous == snap.state:
            return f"unchanged {task.get('identifier')}"
        lines = [
            f"Sentry 状态同步（{reason}）：`{previous or '未记录'}` → `{snap.state}`。",
            f"最近发生 {snap.last_seen}，累计 {snap.count} 次，环境 {snap.env_text()}。"
            f"[{snap.short_id}]({snap.permalink})",
        ]
        reopened = False
        if snap.status == "resolved":
            lines.append(
                "Sentry 已标记 resolved。这里只同步状态，本任务仍按实现、评审和验收流程收尾，不会自动关闭。"
            )
        elif snap.status == "unresolved" and snap.substatus in REGRESSION_SUBSTATUSES:
            if task.get("status") in CLOSED_TASK_STATUSES:
                reopened = True
                lines.append(
                    f"Sentry 判定回归（{snap.substatus}），本任务从 {task.get('status')} 重开为 todo，请 Leader 重新指定主责。"
                )
            else:
                lines.append(
                    f"Sentry 判定回归（{snap.substatus}）。任务仍在处理，不改状态和负责人，请当前主责关注。"
                )
        self.tasks.comment(task["id"], "\n\n".join(lines))
        if reopened:
            self.tasks.set_status(task["id"], "todo")
        self.tasks.set_metadata(task["id"], META_STATE, snap.state)
        return f"{'reopened' if reopened else 'updated'} {task.get('identifier')}"

    def reconcile(self, since: datetime) -> list[str]:
        """Sync everything touched since ``since``; raises if anything failed."""
        results: list[str] = []
        failures = 0
        project_id = self.sentry.project_id(self.target_project)
        seen: set[str] = set()
        for raw in self.sentry.issues_seen_since(project_id, since):
            issue_id = str(raw.get("id"))
            seen.add(issue_id)
            first_seen = parse_time(raw.get("firstSeen"))
            is_new = first_seen is not None and first_seen >= since
            regressed = raw.get("substatus") in REGRESSION_SUBSTATUSES
            try:
                results.append(
                    f"{issue_id}: {self.sync(issue_id, may_create=is_new or regressed, reason='reconcile')}"
                )
            except DispatchError as err:
                failures += 1
                results.append(f"{issue_id}: failed ({err})")
        # Resolved/archived issues are not "seen" again, so re-check open tasks.
        for task in self.tasks.list_linked():
            key = (task.get("metadata") or {}).get(META_KEY, "")
            issue_id = key.rsplit("/", 1)[-1]
            if issue_id in seen or not issue_id.isdigit():
                continue
            if task.get("status") in CLOSED_TASK_STATUSES:
                continue
            try:
                results.append(
                    f"{issue_id}: {self.sync(issue_id, may_create=False, reason='reconcile')}"
                )
            except DispatchError as err:
                failures += 1
                results.append(f"{issue_id}: failed ({err})")
        if failures:
            for line in results:
                self.log(line)
            raise DispatchError(f"{failures} issue(s) failed; cursor not advanced")
        return results


# --------------------------------------------------------------------- CLI


def read_cursor(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return parse_time(json.loads(path.read_text(encoding="utf-8"))["cursor"])


def write_cursor(path: Path, value: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cursor": value.isoformat()}), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def sentry_token() -> str:
    token = os.environ.get("SENTRY_READONLY_TOKEN", "").strip()
    if token:
        return token
    token_file = Path(os.environ.get("AKKIABOT_OP_TOKEN_FILE", DEFAULT_OP_TOKEN_FILE)).expanduser()
    env = dict(os.environ)
    if "OP_SERVICE_ACCOUNT_TOKEN" not in env and token_file.exists():
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token_file.read_text(encoding="utf-8").strip()
    proc = subprocess.run(  # noqa: S603
        ["op", "read", OP_TOKEN_REF],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        raise DispatchError("cannot read the Sentry read-only token from 1Password")
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sentry issue → Multica squad task dispatcher (BOT-55)."
    )
    parser.add_argument("--org", default="akkia")
    parser.add_argument("--target-project", default="akkiabot-staging")
    parser.add_argument("--multica-project", required=True)
    parser.add_argument("--squad-id", required=True)
    parser.add_argument("--expect-host", help="refuse to run elsewhere (the flock is host-local)")
    parser.add_argument(
        "--state-dir", default=os.environ.get("AKKIABOT_SENTRY_STATE_DIR", DEFAULT_STATE_DIR)
    )
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dispatch")
    d.add_argument("--issue-id", required=True)
    d.add_argument("--action", required=True, choices=["created", "unresolved", "resolved"])
    sub.add_parser("reconcile")
    sub.add_parser("init-cursor")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.expect_host and socket.gethostname() != args.expect_host:
        print(
            "refusing to run: host does not match --expect-host (lock is host-local)",
            file=sys.stderr,
        )
        return 2
    state_dir = Path(args.state_dir).expanduser()
    cursor_path = state_dir / "reconcile-cursor.json"
    if args.command == "init-cursor":
        if read_cursor(cursor_path) is None:
            write_cursor(cursor_path, datetime.now(UTC))
        print(f"cursor: {read_cursor(cursor_path).isoformat()}")  # type: ignore[union-attr]
        return 0
    cli = os.environ.get("MULTICA_CLI_PATH") or "multica"
    dispatcher = Dispatcher(
        sentry=SentryClient(token=sentry_token(), org=args.org),
        tasks=MulticaCli(cli, args.multica_project, args.squad_id, Path.cwd()),
        org=args.org,
        target_project=args.target_project,
        lock_path=state_dir / "dispatch.lock",
    )
    try:
        if args.command == "dispatch":
            outcome = dispatcher.sync(
                args.issue_id,
                may_create=args.action in {"created", "unresolved"},
                reason=f"webhook {args.action}",
            )
            print(f"{args.issue_id}: {outcome}")
            return 0
        cursor = read_cursor(cursor_path)
        if cursor is None:
            print("no reconcile cursor; run init-cursor first", file=sys.stderr)
            return 2
        started = datetime.now(UTC)
        for line in dispatcher.reconcile(cursor - RECONCILE_OVERLAP):
            print(line)
        write_cursor(cursor_path, started)
        print(f"cursor advanced to {started.isoformat()}")
        return 0
    except DispatchError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
