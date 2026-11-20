"""Grafana alert → Multica dispatcher and post-fix watch lifecycle (AKKIA-28).

Runs on the Multica runtime host. Called by the autopilot "Grafana 告警分派"
(webhooks from ``deploy/grafana-relay`` and a daily self-check) and by the
agent that owns a watch (start / stop / deadline verdict). Only the Python
standard library is used, like ``scripts/sentry_dispatch.py``.

Commands:

* ``alert``: one alert event from the relay. Routing, in order:

  1. ``akkiabot-dispatch-heartbeat`` records the chain heartbeat (self-check).
  2. An alert carrying ``watch=AKKIA-<n>`` (a rule written for one fix) acts
     on that issue: ``watch_kind=exercised`` records a natural event, anything
     else is a regression that wakes the issue owner, or reopens a closed issue.
  3. The generic watch rules (``watch_kind`` without ``watch``) are matched
     against the watch list, i.e. issues whose metadata ``grafana_watch`` is
     ``active``/``extended`` and whose env / handler / command match. Without a
     match nothing happens: these rules exist only for watches.
  4. Any other rule is a general alert: one squad task per alert key
     (rule / env / handler / command), de-duplicated by metadata and a marker
     in the description; ``resolved`` only syncs status, a new firing episode
     comments, or reopens a closed task.

* ``watch-start`` / ``watch-stop``: open or end a watch on a fix issue. Start
  writes the watch metadata and creates exactly one time wakeup at the
  deadline; there is no polling.
* ``watch-conclude``: run by that wakeup. Natural event and no regression →
  observed; regression → regressed (the owner was already woken); nothing seen
  → extend once by the default period (new single wakeup), then end as
  "fix deployed, natural scenario not observed".
* ``selfcheck``: scheduled. If no heartbeat arrived for HEARTBEAT_MAX_AGE, the
  chain is reported as a general alert task; the next heartbeat resolves it.
* ``init``: record a heartbeat baseline before the first self-check.

Inputs from webhooks are untrusted: every field is validated against a strict
pattern and nothing but those fields is written anywhere. A host-local
``flock`` serialises all lookups and writes.
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
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

DEFAULT_STATE_DIR = "~/.config/akkiabot/grafana-dispatch"
DEFAULT_OP_TOKEN_FILE = "~/.config/akkiabot/op-service-account-token"  # noqa: S105 (path)
OP_GRAFANA_URL = "op://AkkiaBot/staging-grafana/grafana_url"
OP_GRAFANA_TOKEN = "op://AkkiaBot/staging-grafana/grafana_sa_token"  # noqa: S105 (reference)
WATCH_RULES = Path(__file__).resolve().parents[1] / "deploy/observability/alerting/watch-rules.json"
GRAFANA_FOLDER_UID = "akkiabot"
CLI_TIMEOUT_SECONDS = 60

HEARTBEAT_RULE = "akkiabot-dispatch-heartbeat"
HEARTBEAT_MISSING_RULE = "akkiabot-dispatch-heartbeat-missing"
HEARTBEAT_MAX_AGE = timedelta(hours=26)
WATCH_REGRESSION_RULE = "akkiabot-watch-regression"
WATCH_EXERCISED_RULE = "akkiabot-watch-exercised"
DEFAULT_WATCH_DAYS = 7

# General alert tasks.
META_ALERT = "grafana_alert"
META_ALERT_STATE = "grafana_alert_state"
META_SOURCE = "grafana_source"
SOURCE_VALUE = "grafana-dispatch"
MARKER_PREFIX = "grafana-alert-key="

# Watch list (metadata on the fix issue).
W_STATUS = "grafana_watch"  # active | extended | ended
W_ENV = "grafana_watch_env"
W_HANDLER = "grafana_watch_handler"
W_COMMAND = "grafana_watch_command"
W_STARTED = "grafana_watch_started"
W_DEADLINE = "grafana_watch_deadline"
W_OWNER = "grafana_watch_owner"
W_CONCLUDER = "grafana_watch_concluder"
W_EXERCISED = "grafana_watch_exercised_at"
W_REGRESSION = "grafana_watch_regression_at"
W_WAKEUP = "grafana_watch_wakeup"
W_RESULT = "grafana_watch_result"
ACTIVE_WATCH = ("active", "extended")

CLOSED = frozenset({"done", "cancelled"})

PATTERNS = {
    "rule": re.compile(r"akkiabot-[a-z0-9-]{1,80}"),
    "env": re.compile(r"[a-z][a-z0-9-]{0,31}"),
    "status": re.compile(r"firing|resolved"),
    "fingerprint": re.compile(r"[0-9a-f]{8,32}"),
    "starts_at": re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"),
    "severity": re.compile(r"[a-z]{1,16}"),
    "handler": re.compile(r"[A-Za-z0-9_.:<>-]{1,100}"),
    "command": re.compile(r"[a-z0-9_]{1,32}"),
    "watch": re.compile(r"AKKIA-\d{1,6}"),
    "watch_kind": re.compile(r"regression|exercised"),
    "uuid": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    "issue": re.compile(
        r"AKKIA-\d{1,6}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    ),
}


class DispatchError(RuntimeError):
    """A step failed; the message never contains secrets or payload text."""


def check(name: str, value: str | None, *, optional: bool = False) -> str | None:
    if value in (None, "") and optional:
        return None
    if not isinstance(value, str) or not PATTERNS[name].fullmatch(value):
        raise DispatchError(f"invalid {name}")
    return value


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Alert:
    rule: str
    env: str
    status: str
    fingerprint: str
    starts_at: str
    severity: str | None = None
    handler: str | None = None
    command: str | None = None
    watch: str | None = None
    watch_kind: str | None = None
    drill: bool = False

    @classmethod
    def validated(cls, **kw: Any) -> Alert:
        return cls(
            rule=check("rule", kw.get("rule")),  # type: ignore[arg-type]
            env=check("env", kw.get("env")),  # type: ignore[arg-type]
            status=check("status", kw.get("status")),  # type: ignore[arg-type]
            fingerprint=check("fingerprint", kw.get("fingerprint")),  # type: ignore[arg-type]
            starts_at=check("starts_at", kw.get("starts_at")),  # type: ignore[arg-type]
            severity=check("severity", kw.get("severity"), optional=True),
            handler=check("handler", kw.get("handler"), optional=True),
            command=check("command", kw.get("command"), optional=True),
            watch=check("watch", kw.get("watch"), optional=True),
            watch_kind=check("watch_kind", kw.get("watch_kind"), optional=True),
            drill=bool(kw.get("drill")),
        )

    @property
    def key(self) -> str:
        parts = [self.rule, self.env]
        if self.handler:
            parts.append(f"handler={self.handler}")
        if self.command:
            parts.append(f"command={self.command}")
        return "/".join(parts)

    @property
    def state(self) -> str:
        return f"{self.status}@{self.starts_at}"

    def describe(self) -> str:
        extra = "".join(
            f"，{name} `{value}`"
            for name, value in (("handler", self.handler), ("command", self.command))
            if value
        )
        return f"规则 `{self.rule}`，环境 `{self.env}`{extra}，本轮开始于 {self.starts_at}"


def marker(key: str) -> str:
    return f"{MARKER_PREFIX}{key}"


WAKE_DELAY = timedelta(minutes=1)


# ---------------------------------------------------------------- Multica CLI


class Tasks(Protocol):
    def get(self, issue: str) -> dict[str, Any]: ...
    def find_by_metadata(self, key: str, value: str) -> list[dict[str, Any]]: ...
    def find_by_marker(self, text: str) -> list[dict[str, Any]]: ...
    def create(self, title: str, description: str, status: str, priority: str) -> None: ...
    def set_metadata(self, task_id: str, key: str, value: str) -> None: ...
    def delete_metadata(self, task_id: str, key: str) -> None: ...
    def comment(self, task_id: str, body: str) -> None: ...
    def set_status(self, task_id: str, status: str) -> None: ...
    def wakeup_at(self, task_id: str, at: str, agent_id: str, instruction: str) -> str: ...
    def wakeup_disable(self, task_id: str, wakeup_id: str) -> None: ...


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
            raise DispatchError(f"multica {' '.join(args[:2])} failed (exit {proc.returncode})")
        out = proc.stdout.strip()
        return json.loads(out) if out.startswith(("{", "[")) else None

    def _file(self, name: str, text: str) -> Path:
        path = self.workdir / name
        path.write_text(text, encoding="utf-8")
        return path

    def get(self, issue: str) -> dict[str, Any]:
        data = self._run("issue", "get", issue, "--output", "json")
        if not isinstance(data, dict) or "id" not in data:
            raise DispatchError("issue not found")
        return data

    def find_by_metadata(self, key: str, value: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._run(
                "issue", "list", "--metadata", f"{key}={value}",
                "--project", self.project_id, "--fields", TASK_FIELDS,
                "--limit", "100", "--offset", str(offset), "--output", "json",
            )  # fmt: skip
            issues = page.get("issues", []) if isinstance(page, dict) else []
            found.extend(issues)
            if not (isinstance(page, dict) and page.get("has_more")) or not issues:
                return found
            offset += len(issues)

    def find_by_marker(self, text: str) -> list[dict[str, Any]]:
        page = self._run(
            "issue", "search", text, "--include-closed", "--project", self.project_id,
            "--limit", "50", "--output", "json",
        )  # fmt: skip
        issues = page.get("issues", []) if isinstance(page, dict) else []
        # Search also matches comments; only the description marker counts.
        return [i for i in issues if text in (i.get("description") or "")]

    def create(self, title: str, description: str, status: str, priority: str) -> None:
        path = self._file("grafana-dispatch-description.md", description)
        try:
            self._run(
                "issue", "create", "--title", title, "--description-file", str(path),
                "--project", self.project_id, "--assignee-id", self.squad_id,
                "--status", status, "--priority", priority, "--output", "json",
            )  # fmt: skip
        finally:
            path.unlink(missing_ok=True)

    def set_metadata(self, task_id: str, key: str, value: str) -> None:
        self._run(
            "issue", "metadata", "set", task_id, "--key", key, "--value", value,
            "--type", "string", "--output", "json",
        )  # fmt: skip

    def delete_metadata(self, task_id: str, key: str) -> None:
        self._run("issue", "metadata", "delete", task_id, "--key", key, "--output", "json")

    def comment(self, task_id: str, body: str) -> None:
        path = self._file("grafana-dispatch-comment.md", body)
        try:
            self._run(
                "issue", "comment", "add", task_id, "--content-file", str(path), "--output", "json"
            )
        finally:
            path.unlink(missing_ok=True)

    def set_status(self, task_id: str, status: str) -> None:
        self._run("issue", "status", task_id, status)

    def wakeup_at(self, task_id: str, at: str, agent_id: str, instruction: str) -> str:
        path = self._file("grafana-dispatch-wakeup.md", instruction)
        try:
            out = self._run(
                "issue", "wakeup", "create", task_id, "--kind", "at", "--at", at,
                "--agent-id", agent_id, "--instruction-file", str(path), "--output", "json",
            )  # fmt: skip
        finally:
            path.unlink(missing_ok=True)
        if isinstance(out, dict):
            wid = out.get("id") or (out.get("wakeup") or {}).get("id")
            if wid:
                return str(wid)
        raise DispatchError("wakeup create returned no id")

    def wakeup_disable(self, task_id: str, wakeup_id: str) -> None:
        self._run("issue", "wakeup", "disable", task_id, wakeup_id, "--output", "json")


# ---------------------------------------------------------------- Grafana


class Grafana(Protocol):
    def datasource_uids(self) -> dict[str, str]: ...
    def put_rule_group(self, payload: dict[str, Any]) -> None: ...
    def delete_rule_group(self, name: str) -> None: ...


@dataclass
class GrafanaApi:
    """Minimal provisioning client; the token never leaves the request header."""

    url: str
    token: str

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 (https URL from 1Password)
            self.url.rstrip("/") + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as err:
            if method == "DELETE" and err.code == 404:
                return None
            raise DispatchError(f"Grafana {method} {path} failed: HTTP {err.code}") from err
        except urllib.error.URLError as err:
            raise DispatchError(f"Grafana {method} {path} failed: {type(err).__name__}") from err
        return json.loads(raw) if raw.strip() else None

    def datasource_uids(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for kind, suffix in (("prometheus", "-prom"), ("loki", "-logs")):
            pool = [
                s
                for s in self._call("GET", "/api/datasources")
                if s.get("type") == kind and str(s.get("name", "")).endswith(suffix)
            ]
            if len(pool) != 1:
                raise DispatchError(f"cannot pick the stack's {kind} data source")
            found[kind] = str(pool[0]["uid"])
        return found

    def put_rule_group(self, payload: dict[str, Any]) -> None:
        path = f"/api/v1/provisioning/folder/{GRAFANA_FOLDER_UID}/rule-groups/{payload['title']}"
        self._call("PUT", path, payload)

    def delete_rule_group(self, name: str) -> None:
        self._call("DELETE", f"/api/v1/provisioning/folder/{GRAFANA_FOLDER_UID}/rule-groups/{name}")


def watch_rule_group(
    watches: list[dict[str, Any]], uids: dict[str, str], template: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Render the generic watch rules for the watched targets only.

    Returns the group name and its payload, or ``None`` when nothing is watched.
    """
    template = template or json.loads(WATCH_RULES.read_text(encoding="utf-8"))
    metas = [t.get("metadata") or {} for t in watches]
    envs = sorted({m[W_ENV] for m in metas if m.get(W_ENV)})
    targets = {
        "regression": ("__HANDLERS__", sorted({m[W_HANDLER] for m in metas if m.get(W_HANDLER)})),
        "exercised": ("__COMMANDS__", sorted({m[W_COMMAND] for m in metas if m.get(W_COMMAND)})),
    }
    rules = []
    for kind, (placeholder, values) in targets.items():
        if not (envs and values):
            continue
        rule = json.loads(json.dumps(template["rules"][kind]))
        for query in rule["data"]:
            if query["datasourceUid"] in uids:
                query["datasourceUid"] = uids[query["datasourceUid"]]
            if "expr" in query["model"]:
                query["model"]["expr"] = (
                    query["model"]["expr"]
                    .replace("__ENVS__", "|".join(envs))
                    .replace(placeholder, "|".join(re.escape(v) for v in values))
                )
        rule.update(
            folderUID=GRAFANA_FOLDER_UID,
            ruleGroup=template["group"],
            orgID=1,
            labels={**rule["labels"], "akkiabot_rule": rule["uid"]},
        )
        rules.append(rule)
    if not rules:
        return template["group"], None
    return template["group"], {
        "title": template["group"],
        "folderUid": GRAFANA_FOLDER_UID,
        "interval": template["interval"],
        "rules": rules,
    }


# ------------------------------------------------------------------ texts


def task_title(alert: Alert) -> str:
    prefix = "[测试] " if alert.drill else ""
    suffix = f" · {alert.handler or alert.command}" if (alert.handler or alert.command) else ""
    return f"{prefix}Grafana 告警 · {alert.rule} · {alert.env}{suffix}"


def task_priority(alert: Alert) -> str:
    if alert.drill or alert.env != "production":
        return "low"
    return "high" if alert.severity == "critical" else "medium"


def task_description(alert: Alert) -> str:
    return f"""## Grafana 告警

- 规则 uid：`{alert.rule}`（Grafana → Alerting → Alert rules → 文件夹 AkkiaBot）
- 环境：`{alert.env}`
- 级别：{alert.severity or "未标注"}
- 分组标签：handler `{alert.handler or "-"}`，command `{alert.command or "-"}`
- 本轮开始：{alert.starts_at}（fingerprint `{alert.fingerprint}`）

## 处理约定

- 本任务由 Grafana 告警分派自动创建（`docs/grafana-alerting.md`）。Leader 指定唯一主责，小队归属不变。
- 告警的取值、注解和查询结果不随 webhook 进入 Multica；排查时在 Grafana 中查看，真实数值只写在受控的内部记录里，不写入公开仓库或 PR。
- 告警恢复（resolved）只以评论同步，不会自动关闭本任务；同一告警再次触发时会评论，已关闭的任务会被重开。

关联标识（自动去重使用，请勿修改）：`{marker(alert.key)}`
"""


def conclude_instruction(issue: str, args_text: str, script_ref: str) -> str:
    return f"""[grafana-watch] 修复后观察到期判定（{issue}）。

只做一次判定，不轮询、不另建唤醒（需要延长时脚本会自行创建唯一的下一次唤醒）：
1. 在 runtime 主机上执行 `"$MULTICA_CLI_PATH" repo checkout https://github.com/AkkiaLab/AkkiaBot.git --ref {script_ref}`，进入检出目录。
2. 执行 `python3 scripts/grafana_dispatch.py {args_text} watch-conclude --issue {issue}`。
3. 脚本已在本 issue 写下结论评论；你的回复只写脚本输出的结果行。出错时如实报告失败步骤，不要手工改观察 metadata。
"""


# ------------------------------------------------------------------- core


@dataclass
class Dispatcher:
    tasks: Tasks
    state_dir: Path
    leader_id: str
    grafana: Grafana | None = None
    conclude_args: str = ""
    script_ref: str = "<DISPATCH_SHA>"
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state_dir / "dispatch.lock").open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------ alerts

    def handle_alert(self, alert: Alert) -> list[str]:
        with self.locked():
            if alert.rule == HEARTBEAT_RULE:
                return [self._heartbeat()]
            if alert.watch:
                return [self._watch_label(alert)]
            if alert.watch_kind:
                return self._watch_generic(alert)
            return [self._general(alert)]

    def _find_alert_task(self, key: str) -> list[dict[str, Any]]:
        # `issue search` returns an empty metadata object; keep the list row.
        by_id: dict[str, dict[str, Any]] = {}
        for task in [
            *self.tasks.find_by_metadata(META_ALERT, key),
            *self.tasks.find_by_marker(marker(key)),
        ]:
            by_id.setdefault(task["id"], task)
        return sorted(by_id.values(), key=lambda t: int(t.get("number") or 0))

    def _create(self, alert: Alert) -> dict[str, Any]:
        last_error: DispatchError | None = None
        for _attempt in range(2):
            try:
                self.tasks.create(
                    task_title(alert),
                    task_description(alert),
                    "backlog" if alert.drill else "todo",
                    task_priority(alert),
                )
            except DispatchError as err:
                # Timed out or refused (active duplicate title): look again first.
                last_error = err
            found = self._find_alert_task(alert.key)
            if found:
                return found[0]
        raise DispatchError(f"task creation failed: {last_error}")

    def _general(self, alert: Alert) -> str:
        found = self._find_alert_task(alert.key)
        if not found:
            if alert.status != "firing":
                return "no_task_needed"
            task = self._create(alert)
            self._link(task, alert.key)
            self.tasks.set_metadata(task["id"], META_ALERT_STATE, alert.state)
            return f"created {task.get('identifier')}"
        task, duplicates = found[0], found[1:]
        self._link(task, alert.key)
        outcome = self._update(task, alert)
        if duplicates:
            ids = ", ".join(str(d.get("identifier")) for d in duplicates)
            outcome += f"; duplicates kept for Leader review: {ids}"
        return outcome

    def _link(self, task: dict[str, Any], key: str) -> None:
        meta = task.get("metadata") or {}
        if meta.get(META_ALERT) != key:
            self.tasks.set_metadata(task["id"], META_ALERT, key)
        if meta.get(META_SOURCE) != SOURCE_VALUE:
            self.tasks.set_metadata(task["id"], META_SOURCE, SOURCE_VALUE)

    def _update(self, task: dict[str, Any], alert: Alert) -> str:
        ident = task.get("identifier")
        previous = (task.get("metadata") or {}).get(META_ALERT_STATE) or ""
        prev_status, _, prev_starts = previous.partition("@")
        if previous == alert.state:
            return f"unchanged {ident}"
        # Out of order: an older episode, or a firing notice after that same
        # episode already resolved. The recorded state is newer; keep it.
        if prev_starts and (
            alert.starts_at < prev_starts
            or (
                alert.starts_at == prev_starts
                and prev_status == "resolved"
                and alert.status == "firing"
            )
        ):
            return f"stale_ignored {ident}"
        reopened = False
        if alert.status == "resolved":
            line = (
                f"Grafana 告警已恢复（resolved）：{alert.describe()}。"
                "这里只同步状态，本任务仍按实现、评审和验收流程收尾，不会自动关闭。"
            )
        elif task.get("status") in CLOSED:
            reopened = True
            line = (
                f"Grafana 告警再次触发：{alert.describe()}。本任务从 {task.get('status')} 重开为 todo，"
                "已唤醒 Leader 重新指定主责。"
            )
        else:
            line = f"Grafana 告警再次触发：{alert.describe()}。任务仍在处理，不改状态和负责人，请当前主责关注。"
        self.tasks.comment(task["id"], line)
        if reopened:
            self.tasks.set_status(task["id"], "todo")
            self._wake(
                task["id"],
                self.leader_id,
                "Grafana 告警再次触发，任务已重开为 todo：请重新指定唯一主责。",
            )
        self.tasks.set_metadata(task["id"], META_ALERT_STATE, alert.state)
        return f"{'reopened' if reopened else 'updated'} {ident}"

    def _wake(self, task_id: str, agent_id: str, instruction: str) -> None:
        """Start one run of ``agent_id`` on the issue (works for the run's own agent too)."""
        self.tasks.wakeup_at(task_id, iso(self.clock() + WAKE_DELAY), agent_id, instruction)

    # ------------------------------------------------------------ watches

    def _active_watches(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for status in ACTIVE_WATCH:
            out.extend(self.tasks.find_by_metadata(W_STATUS, status))
        return sorted(out, key=lambda t: int(t.get("number") or 0))

    def _watch_generic(self, alert: Alert) -> list[str]:
        field, value = (
            (W_HANDLER, alert.handler)
            if alert.watch_kind == "regression"
            else (W_COMMAND, alert.command)
        )
        if not value:
            return ["no_watch (alert has no matching label)"]
        matched = [
            t
            for t in self._active_watches()
            if (t.get("metadata") or {}).get(W_ENV) == alert.env
            and (t.get("metadata") or {}).get(field) == value
        ]
        if not matched:
            return ["no_watch"]
        return [self._apply_watch(t, alert, require_active=True) for t in matched]

    def _watch_label(self, alert: Alert) -> str:
        task = self.tasks.get(alert.watch)  # type: ignore[arg-type]
        # A rule written for one fix: a regression wakes the issue even after
        # the watch ended (the rule's existence is the watch); a natural event
        # only counts while the watch is open.
        return self._apply_watch(task, alert, require_active=alert.watch_kind == "exercised")

    def _apply_watch(self, task: dict[str, Any], alert: Alert, *, require_active: bool) -> str:
        ident = task.get("identifier")
        meta = task.get("metadata") or {}
        active = meta.get(W_STATUS) in ACTIVE_WATCH
        if require_active and not active:
            return f"watch_not_active {ident}"
        started = meta.get(W_STARTED)
        if active and started and alert.starts_at < started:
            return f"before_watch {ident}"
        if alert.watch_kind == "exercised":
            if meta.get(W_EXERCISED):
                return f"exercised_already_recorded {ident}"
            self.tasks.set_metadata(task["id"], W_EXERCISED, alert.starts_at)
            self.tasks.comment(
                task["id"],
                f"修复后观察：已出现自然场景（{alert.describe()}）。到期判定时据此记为“已观测”；"
                "若期间没有复发告警，结论为“已观测，无复发”。",
            )
            return f"exercised {ident}"
        if meta.get(W_REGRESSION) == alert.starts_at:
            return f"regression_already_recorded {ident}"
        self.tasks.set_metadata(task["id"], W_REGRESSION, alert.starts_at)
        if task.get("status") in CLOSED:
            self.tasks.comment(
                task["id"],
                f"修复后观察：出现复发告警（{alert.describe()}）。本任务从 {task.get('status')} "
                "重开为 todo，已唤醒 Leader 重新指定主责。",
            )
            self.tasks.set_status(task["id"], "todo")
            self._wake(
                task["id"],
                self.leader_id,
                "修复后观察出现复发，任务已重开为 todo：请重新指定唯一主责。",
            )
            return f"regression_reopened {ident}"
        owner = meta.get(W_OWNER) or self.leader_id
        self.tasks.comment(
            task["id"],
            f"修复后观察：出现复发告警（{alert.describe()}）。已唤醒主责排查；"
            "排查方法见 docs/grafana-alerting.md「复发排查」。",
        )
        self._wake(
            task["id"],
            owner,
            f"修复后观察出现复发告警（{alert.describe()}）：按 docs/grafana-alerting.md「复发排查」处理，"
            "结论回复在本 issue。",
        )
        return f"regression_woken {ident}"

    def sync_watch_rules(self) -> str:
        """Make the Grafana group akkiabot-watch cover exactly the open watches."""
        if self.grafana is None:
            return "watch rules not synced (no Grafana client)"
        watches = self._active_watches()
        name, payload = watch_rule_group(watches, self.grafana.datasource_uids())
        if payload is None:
            self.grafana.delete_rule_group(name)
            return "watch rules removed (no open watch)"
        self.grafana.put_rule_group(payload)
        return f"watch rules synced for {len(watches)} open watch(es)"

    def watch_start(
        self,
        issue: str,
        *,
        env: str,
        handler: str | None,
        command: str | None,
        owner: str,
        concluder: str,
        deadline: datetime | None = None,
    ) -> str:
        if not (handler or command):
            raise DispatchError("a watch needs --handler and/or --command")
        with self.locked():
            task = self.tasks.get(issue)
            ident = str(task.get("identifier"))
            meta = task.get("metadata") or {}
            if meta.get(W_STATUS) in ACTIVE_WATCH:
                synced = self.sync_watch_rules()
                return f"already_watching {ident} (deadline {meta.get(W_DEADLINE)}); {synced}"
            now = self.clock()
            due = deadline or now + timedelta(days=DEFAULT_WATCH_DAYS)
            values = {
                W_ENV: env,
                W_HANDLER: handler or "",
                W_COMMAND: command or "",
                W_STARTED: iso(now),
                W_DEADLINE: iso(due),
                W_OWNER: owner,
                W_CONCLUDER: concluder,
                W_EXERCISED: "",
                W_REGRESSION: "",
                W_RESULT: "",
            }
            for key, value in values.items():
                if value:
                    self.tasks.set_metadata(task["id"], key, value)
                elif key in meta:
                    # Left over from an earlier watch on the same issue.
                    self.tasks.delete_metadata(task["id"], key)
            wakeup = self.tasks.wakeup_at(
                task["id"],
                iso(due),
                concluder,
                conclude_instruction(ident, self.conclude_args, self.script_ref),
            )
            self.tasks.set_metadata(task["id"], W_WAKEUP, wakeup)
            # Status last: the watch only becomes visible to alerts once complete.
            self.tasks.set_metadata(task["id"], W_STATUS, "active")
            synced = self.sync_watch_rules()
            targets = "，".join(
                f"{name} `{value}`"
                for name, value in (("handler", handler), ("command", command))
                if value
            )
            self.tasks.comment(
                task["id"],
                f"修复后观察已开启：环境 `{env}`，{targets}；截止 {iso(due)}，到期只做一次判定（时间唤醒），不轮询。"
                "复发告警会唤醒主责；未出现自然场景时自动延长一次，再无则以“修复已上线、自然场景未观测”收尾。",
            )
            return f"watch_started {ident} until {iso(due)}; {synced}"

    def watch_stop(self, issue: str, reason: str) -> str:
        with self.locked():
            task = self.tasks.get(issue)
            ident = task.get("identifier")
            meta = task.get("metadata") or {}
            if meta.get(W_STATUS) not in ACTIVE_WATCH:
                return f"watch_not_active {ident}"
            if meta.get(W_WAKEUP):
                with contextlib.suppress(DispatchError):
                    self.tasks.wakeup_disable(task["id"], meta[W_WAKEUP])
            self.tasks.set_metadata(task["id"], W_RESULT, "stopped")
            self.tasks.set_metadata(task["id"], W_STATUS, "ended")
            self.tasks.comment(task["id"], f"修复后观察已手动结束：{reason}")
            return f"watch_stopped {ident}; {self.sync_watch_rules()}"

    def watch_conclude(self, issue: str) -> str:
        with self.locked():
            task = self.tasks.get(issue)
            ident = str(task.get("identifier"))
            meta = task.get("metadata") or {}
            status = meta.get(W_STATUS)
            if status not in ACTIVE_WATCH:
                return f"watch_not_active {ident}"
            now = self.clock()
            deadline = parse_time(meta.get(W_DEADLINE))
            if deadline and now < deadline - timedelta(minutes=1):
                return f"not_due {ident} (deadline {iso(deadline)})"
            exercised, regressed = meta.get(W_EXERCISED), meta.get(W_REGRESSION)
            window = f"{meta.get(W_STARTED)} ~ {meta.get(W_DEADLINE)}"
            if regressed:
                result = "regressed"
                text = (
                    f"观察期 {window} 内出现复发告警（最近一次开始于 {regressed}），已在当时唤醒主责。"
                    f"自然场景：{'已出现（' + exercised + '）' if exercised else '未出现'}。"
                )
            elif exercised:
                result = "observed_no_regression"
                text = f"观察期 {window} 内已出现自然场景（{exercised}），没有复发告警：结论为“已观测，无复发”。"
            elif status == "active":
                new_deadline = (deadline or now) + timedelta(days=DEFAULT_WATCH_DAYS)
                if new_deadline <= now:
                    new_deadline = now + timedelta(days=DEFAULT_WATCH_DAYS)
                wakeup = self.tasks.wakeup_at(
                    task["id"],
                    iso(new_deadline),
                    meta.get(W_CONCLUDER) or self.leader_id,
                    conclude_instruction(ident, self.conclude_args, self.script_ref),
                )
                self.tasks.set_metadata(task["id"], W_DEADLINE, iso(new_deadline))
                self.tasks.set_metadata(task["id"], W_WAKEUP, wakeup)
                self.tasks.set_metadata(task["id"], W_STATUS, "extended")
                self.tasks.comment(
                    task["id"],
                    f"观察期 {window} 内未出现自然场景，也没有复发告警。按约定自动延长一次 "
                    f"{DEFAULT_WATCH_DAYS} 天，新截止 {iso(new_deadline)}（仍只在到期时判定一次）。",
                )
                return f"extended {ident} until {iso(new_deadline)}"
            else:
                result = "unobserved"
                text = (
                    f"观察期（含一次延长）{window} 内仍未出现自然场景，也没有复发告警。"
                    "结论：**修复已上线、自然场景未观测**（未观测不等于已验证）。"
                )
            self.tasks.set_metadata(task["id"], W_RESULT, result)
            self.tasks.set_metadata(task["id"], W_STATUS, "ended")
            still_open = task.get("status") not in CLOSED
            closing = " 已唤醒 Leader 核对收尾。" if still_open else ""
            self.tasks.comment(task["id"], f"修复后观察到期判定：{text}{closing}")
            if still_open:
                self._wake(
                    task["id"], self.leader_id, f"修复后观察已结束（{result}）：请核对验收并收尾。"
                )
            return f"concluded {ident}: {result}; {self.sync_watch_rules()}"

    # -------------------------------------------------------- self-check

    def _heartbeat_path(self) -> Path:
        return self.state_dir / "heartbeat.json"

    def _read_heartbeat(self) -> datetime | None:
        path = self._heartbeat_path()
        if not path.exists():
            return None
        return parse_time(json.loads(path.read_text(encoding="utf-8")).get("at"))

    def _write_heartbeat(self, at: datetime) -> None:
        path = self._heartbeat_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": iso(at)}), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)

    def _missing_alert(self, last: datetime, status: str) -> Alert:
        return Alert(
            rule=HEARTBEAT_MISSING_RULE,
            env="production",
            status=status,
            fingerprint="0" * 16,
            starts_at=iso(last + HEARTBEAT_MAX_AGE),
            severity="critical",
        )

    def _heartbeat(self) -> str:
        now = self.clock()
        last = self._read_heartbeat()
        self._write_heartbeat(now)
        if last and now - last > HEARTBEAT_MAX_AGE:
            # The chain was reported broken by selfcheck; this heartbeat resolves it.
            return f"heartbeat recorded; {self._general(self._missing_alert(last, 'resolved'))}"
        return "heartbeat recorded"

    def selfcheck(self) -> str:
        with self.locked():
            last = self._read_heartbeat()
            if last is None:
                raise DispatchError("no heartbeat baseline; run init first")
            if self.clock() - last <= HEARTBEAT_MAX_AGE:
                outcome = f"ok (last heartbeat {iso(last)})"
            else:
                missing = self._general(self._missing_alert(last, "firing"))
                outcome = f"heartbeat missing since {iso(last)}; {missing}"
            # Daily reconciliation of the watch rule group with the watch list.
            try:
                return f"{outcome}; {self.sync_watch_rules()}"
            except DispatchError as err:
                raise DispatchError(f"{outcome}; watch rules sync failed: {err}") from err

    def watch_sync(self) -> str:
        with self.locked():
            return self.sync_watch_rules()

    def init(self) -> str:
        with self.locked():
            if self._read_heartbeat() is None:
                self._write_heartbeat(self.clock())
            return f"heartbeat baseline {iso(self._read_heartbeat())}"  # type: ignore[arg-type]


# --------------------------------------------------------------------- CLI


def script_ref() -> str:
    proc = subprocess.run(  # noqa: S603
        ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    ref = proc.stdout.strip()
    return ref if re.fullmatch(r"[0-9a-f]{40}", ref) else "<DISPATCH_SHA>"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grafana alert → Multica dispatcher (AKKIA-28).")
    parser.add_argument("--multica-project", required=True)
    parser.add_argument("--squad-id", required=True)
    parser.add_argument("--leader-id", required=True, help="agent woken to (re)assign owners")
    parser.add_argument("--expect-host", help="refuse to run elsewhere (the flock is host-local)")
    parser.add_argument(
        "--state-dir", default=os.environ.get("AKKIABOT_GRAFANA_STATE_DIR", DEFAULT_STATE_DIR)
    )
    parser.add_argument(
        "--now", help="override the clock (RFC3339); drills and tests only", default=None
    )
    sub = parser.add_subparsers(dest="action", required=True)

    a = sub.add_parser("alert", help="handle one alert event from the relay")
    for name in ("rule", "env", "status", "fingerprint", "starts-at"):
        a.add_argument(f"--{name}", required=True)
    for name in ("severity", "handler", "command", "watch", "watch-kind"):
        a.add_argument(f"--{name}")
    a.add_argument("--drill", action="store_true")

    s = sub.add_parser("watch-start", help="open a post-fix watch on an issue")
    s.add_argument("--issue", required=True)
    s.add_argument("--env", required=True)
    s.add_argument("--handler")
    s.add_argument("--command")
    s.add_argument("--owner-agent-id", required=True, help="woken on regression")
    s.add_argument("--concluder-agent-id", required=True, help="runs the deadline verdict")
    s.add_argument("--deadline", help="RFC3339 deadline instead of the default 7 days (drills)")

    t = sub.add_parser("watch-stop", help="end a watch early")
    t.add_argument("--issue", required=True)
    t.add_argument("--reason", required=True)

    c = sub.add_parser("watch-conclude", help="deadline verdict (run by the time wakeup)")
    c.add_argument("--issue", required=True)

    sub.add_parser("watch-sync", help="re-render the watch rule group from the watch list")
    sub.add_parser("selfcheck", help="report the chain broken when heartbeats stop")
    sub.add_parser("init", help="record a heartbeat baseline")
    return parser


def common_args_text(args: argparse.Namespace) -> str:
    parts = [
        f"--multica-project {args.multica_project}",
        f"--squad-id {args.squad_id}",
        f"--leader-id {args.leader_id}",
    ]
    if args.expect_host:
        parts.append(f"--expect-host {args.expect_host}")
    return " ".join(parts)


def op_read(ref: str) -> str:
    token_file = Path(os.environ.get("AKKIABOT_OP_TOKEN_FILE", DEFAULT_OP_TOKEN_FILE)).expanduser()
    env = dict(os.environ)
    if "OP_SERVICE_ACCOUNT_TOKEN" not in env and token_file.exists():
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token_file.read_text(encoding="utf-8").strip()
    proc = subprocess.run(  # noqa: S603
        ["op", "read", ref],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        raise DispatchError("cannot read the Grafana credentials from 1Password")
    return value


def grafana_from_env() -> GrafanaApi:
    """GRAFANA_URL / GRAFANA_SA_TOKEN, else the staging-grafana item (shared stack)."""
    url = os.environ.get("GRAFANA_URL", "").strip() or op_read(OP_GRAFANA_URL)
    token = os.environ.get("GRAFANA_SA_TOKEN", "").strip() or op_read(OP_GRAFANA_TOKEN)
    return GrafanaApi(url=url, token=token)


NEEDS_GRAFANA = frozenset(
    {"watch-start", "watch-stop", "watch-conclude", "watch-sync", "selfcheck"}
)


def run(args: argparse.Namespace, tasks: Tasks, grafana: Grafana | None = None) -> list[str]:
    fixed_now = parse_time(args.now) if args.now else None
    if grafana is None and args.action in NEEDS_GRAFANA:
        grafana = grafana_from_env()
    dispatcher = Dispatcher(
        tasks=tasks,
        grafana=grafana,
        state_dir=Path(args.state_dir).expanduser(),
        leader_id=args.leader_id,
        conclude_args=common_args_text(args),
        script_ref=script_ref(),
        clock=(lambda: fixed_now) if fixed_now else (lambda: datetime.now(UTC)),  # type: ignore[return-value]
    )
    if args.action == "alert":
        alert = Alert.validated(
            rule=args.rule,
            env=args.env,
            status=args.status,
            fingerprint=args.fingerprint,
            starts_at=args.starts_at,
            severity=args.severity,
            handler=args.handler,
            command=args.command,
            watch=args.watch,
            watch_kind=args.watch_kind,
            drill=args.drill,
        )
        return dispatcher.handle_alert(alert)
    if args.action == "watch-start":
        for name in ("owner_agent_id", "concluder_agent_id"):
            check("uuid", getattr(args, name))
        return [
            dispatcher.watch_start(
                check("issue", args.issue),  # type: ignore[arg-type]
                env=check("env", args.env),  # type: ignore[arg-type]
                handler=check("handler", args.handler, optional=True),
                command=check("command", args.command, optional=True),
                owner=args.owner_agent_id,
                concluder=args.concluder_agent_id,
                deadline=parse_time(args.deadline) if args.deadline else None,
            )
        ]
    if args.action == "watch-stop":
        return [dispatcher.watch_stop(check("issue", args.issue), args.reason)]  # type: ignore[arg-type]
    if args.action == "watch-conclude":
        return [dispatcher.watch_conclude(check("issue", args.issue))]  # type: ignore[arg-type]
    if args.action == "selfcheck":
        return [dispatcher.selfcheck()]
    if args.action == "watch-sync":
        return [dispatcher.watch_sync()]
    return [dispatcher.init()]


def main(
    argv: list[str] | None = None, tasks: Tasks | None = None, grafana: Grafana | None = None
) -> int:
    args = build_parser().parse_args(argv)
    if args.expect_host and socket.gethostname() != args.expect_host:
        print(
            "refusing to run: host does not match --expect-host (lock is host-local)",
            file=sys.stderr,
        )
        return 2
    try:
        for name in ("multica_project", "squad_id", "leader_id"):
            check("uuid", getattr(args, name))
        if tasks is None:
            cli = os.environ.get("MULTICA_CLI_PATH") or "multica"
            tasks = MulticaCli(cli, args.multica_project, args.squad_id, Path.cwd())
        for line in run(args, tasks, grafana):
            print(line)
        return 0
    except DispatchError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
