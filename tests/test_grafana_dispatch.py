"""Tests for scripts/grafana_dispatch.py (AKKIA-28). All alert data here is fictional."""

from __future__ import annotations

import itertools
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import grafana_dispatch as gd

LEADER = "00000000-0000-4000-8000-00000000000a"
OWNER = "00000000-0000-4000-8000-00000000000b"
CONCLUDER = "00000000-0000-4000-8000-00000000000c"
PROJECT = "00000000-0000-4000-8000-00000000000d"
SQUAD = "00000000-0000-4000-8000-00000000000e"
T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


class FakeTasks:
    """In-memory Multica with the server's active-duplicate-title guard."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.wakeups: list[dict[str, str]] = []
        self.disabled: list[str] = []
        self.fail_create_after_insert = False
        self.fail_wakeup_once = False
        self.wakeup_lands_but_fails_once = False
        self.fail_comment_once = False
        self._ids = itertools.count(100)

    def add(self, status: str = "in_progress", **metadata: str) -> dict[str, Any]:
        n = next(self._ids)
        task = {
            "id": f"id-{n}",
            "identifier": f"AKKIA-{n}",
            "number": n,
            "status": status,
            "metadata": dict(metadata),
            "description": "",
            "title": f"fix {n}",
        }
        self.tasks.append(task)
        return task

    def _by(self, ref: str) -> dict[str, Any]:
        for t in self.tasks:
            if ref in (t["id"], t["identifier"]):
                return t
        raise gd.DispatchError("issue not found")

    def get(self, issue: str) -> dict[str, Any]:
        t = self._by(issue)
        return {**t, "metadata": dict(t["metadata"])}

    def find_by_metadata(self, key: str, value: str) -> list[dict[str, Any]]:
        return [
            {**t, "metadata": dict(t["metadata"])}
            for t in self.tasks
            if t["metadata"].get(key) == value
        ]

    def find_by_marker(self, text: str) -> list[dict[str, Any]]:
        return [{**t, "metadata": {}} for t in self.tasks if text in t["description"]]

    def create(self, title: str, description: str, status: str, priority: str) -> None:
        if any(t["title"] == title and t["status"] not in gd.CLOSED for t in self.tasks):
            raise gd.DispatchError("multica issue create failed (exit 1)")
        task = self.add(status)
        task.update(title=title, description=description, priority=priority)
        if self.fail_create_after_insert:
            self.fail_create_after_insert = False
            raise gd.DispatchError("multica issue create timed out")

    def set_metadata(self, task_id: str, key: str, value: str) -> None:
        self._by(task_id)["metadata"][key] = value

    def delete_metadata(self, task_id: str, key: str) -> None:
        self._by(task_id)["metadata"].pop(key, None)

    def find_wakeup(self, task_id: str, tag: str) -> str | None:
        ident = self._by(task_id)["identifier"]
        for w in self.wakeups:
            if w["task"] == ident and tag in w["instruction"]:
                return w["id"]
        return None

    def has_comment(self, task_id: str, tag: str) -> bool:
        ident = self._by(task_id)["identifier"]
        return any(i == ident and tag in body for i, body in self.comments)

    def comment(self, task_id: str, body: str) -> None:
        if self.fail_comment_once:
            self.fail_comment_once = False
            raise gd.DispatchError("multica issue comment failed (exit 1)")
        self.comments.append((self._by(task_id)["identifier"], body))

    def set_status(self, task_id: str, status: str) -> None:
        self._by(task_id)["status"] = status

    def wakeup_at(self, task_id: str, at: str, agent_id: str, instruction: str) -> str:
        if self.fail_wakeup_once:
            self.fail_wakeup_once = False
            raise gd.DispatchError("multica issue wakeup failed (exit 1)")
        if self.wakeup_lands_but_fails_once:
            # Created on the server, but the call timed out: the result is unknown.
            self.wakeup_lands_but_fails_once = False
            self.wakeups.append({"id": "wakeup-lost", "task": self._by(task_id)["identifier"],
                                 "at": at, "agent": agent_id, "instruction": instruction})  # fmt: skip
            raise gd.DispatchError("multica issue wakeup timed out")
        wid = f"wakeup-{len(self.wakeups) + 1}"
        self.wakeups.append(
            {"id": wid, "task": self._by(task_id)["identifier"], "at": at, "agent": agent_id,
             "instruction": instruction}
        )  # fmt: skip
        return wid

    def wakeup_disable(self, task_id: str, wakeup_id: str) -> None:
        self.disabled.append(wakeup_id)


class FakeGrafana:
    def __init__(self) -> None:
        self.groups: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        # Times at which the watched target was active, per watch kind.
        self.events: dict[str, list[datetime]] = {"regression": [], "exercised": []}
        self.queries: list[str] = []

    def window_count(self, kind: str, expr: str, at: datetime) -> float:
        self.queries.append(expr)
        (seconds,) = {int(n) for n in re.findall(r"\[(\d+)s\]|offset (\d+)s", expr)[0] if n}
        start = at - timedelta(seconds=seconds)
        return float(sum(1 for t in self.events[kind] if start < t <= at))

    def datasource_uids(self) -> dict[str, str]:
        return {"prometheus": "grafanacloud-prom", "loki": "grafanacloud-logs"}

    def put_rule_group(self, payload: dict[str, Any]) -> None:
        self.calls.append(f"put {payload['title']}")
        self.groups[payload["title"]] = payload

    def delete_rule_group(self, name: str) -> None:
        self.calls.append(f"delete {name}")
        self.groups.pop(name, None)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


GRAFANA = FakeGrafana()


@pytest.fixture
def env(tmp_path: Path) -> tuple[gd.Dispatcher, FakeTasks, Clock]:
    tasks, clock = FakeTasks(), Clock()
    GRAFANA.__init__()
    dispatcher = gd.Dispatcher(
        tasks=tasks,
        grafana=GRAFANA,
        state_dir=tmp_path / "state",
        leader_id=LEADER,
        conclude_args="--multica-project p --squad-id s --leader-id l",
        script_ref="f" * 40,
        clock=clock,
    )
    return dispatcher, tasks, clock


def alert(**kw: Any) -> gd.Alert:
    base = {
        "rule": "akkiabot-readyz-failing",
        "env": "production",
        "status": "firing",
        "fingerprint": "0123456789abcdef",
        "starts_at": "2026-11-20T07:58:00Z",
        "severity": "critical",
    }
    base.update(kw)
    return gd.Alert.validated(**base)


# ------------------------------------------------------------ general alerts


def test_firing_creates_one_squad_task_and_repeats_are_unchanged(env) -> None:
    d, tasks, _ = env
    assert d.handle_alert(alert()) == ["created AKKIA-100"]
    (task,) = tasks.tasks
    assert task["status"] == "todo"
    assert task["priority"] == "high"
    assert task["title"] == "Grafana 告警 · akkiabot-readyz-failing · production"
    assert gd.marker("akkiabot-readyz-failing/production") in task["description"]
    assert task["metadata"][gd.META_ALERT] == "akkiabot-readyz-failing/production"
    assert task["metadata"][gd.META_ALERT_STATE] == "firing@2026-11-20T07:58:00Z"
    assert d.handle_alert(alert()) == ["unchanged AKKIA-100"]
    assert len(tasks.tasks) == 1
    assert tasks.comments == []


def test_resolved_only_syncs_and_never_closes(env) -> None:
    d, tasks, _ = env
    d.handle_alert(alert())
    assert d.handle_alert(alert(status="resolved")) == ["updated AKKIA-100"]
    assert tasks.tasks[0]["status"] == "todo"
    assert "resolved" in tasks.comments[-1][1]
    assert "不会自动关闭" in tasks.comments[-1][1]
    # A late firing notice of the episode that already resolved is stale.
    assert d.handle_alert(alert()) == ["stale_ignored AKKIA-100"]


def test_resolved_without_task_creates_nothing(env) -> None:
    d, tasks, _ = env
    assert d.handle_alert(alert(status="resolved")) == ["no_task_needed"]
    assert tasks.tasks == []


def test_new_episode_reopens_a_closed_task_and_wakes_the_leader(env) -> None:
    d, tasks, _ = env
    d.handle_alert(alert())
    d.handle_alert(alert(status="resolved"))
    tasks.tasks[0]["status"] = "done"
    assert d.handle_alert(alert(starts_at="2026-11-21T07:00:00Z")) == ["reopened AKKIA-100"]
    assert tasks.tasks[0]["status"] == "todo"
    assert tasks.wakeups[-1]["agent"] == LEADER
    assert tasks.wakeups[-1]["at"] == "2026-11-20T08:01:00Z"
    # An older episode arriving late does not roll the state back.
    assert d.handle_alert(alert(status="resolved")) == ["stale_ignored AKKIA-100"]


def test_new_episode_on_open_task_only_comments(env) -> None:
    d, tasks, _ = env
    d.handle_alert(alert())
    tasks.tasks[0]["status"] = "in_progress"
    assert d.handle_alert(alert(starts_at="2026-11-21T07:00:00Z")) == ["updated AKKIA-100"]
    assert tasks.tasks[0]["status"] == "in_progress"
    assert tasks.wakeups == []


def test_alert_key_includes_handler_and_staging_is_low_priority(env) -> None:
    d, tasks, _ = env
    d.handle_alert(alert(env="staging", rule="akkiabot-handler-error-rate", handler="command:spam"))
    d.handle_alert(alert(env="staging", rule="akkiabot-handler-error-rate", handler="command:ban"))
    assert [t["metadata"][gd.META_ALERT] for t in tasks.tasks] == [
        "akkiabot-handler-error-rate/staging/handler=command:spam",
        "akkiabot-handler-error-rate/staging/handler=command:ban",
    ]
    assert {t["priority"] for t in tasks.tasks} == {"low"}


def test_drill_alerts_become_backlog_test_tasks(env) -> None:
    d, tasks, _ = env
    d.handle_alert(alert(rule="akkiabot-dispatch-drill", env="staging", drill=True))
    assert tasks.tasks[0]["status"] == "backlog"
    assert tasks.tasks[0]["title"].startswith("[测试] ")


def test_task_found_by_marker_after_interrupted_create(env) -> None:
    d, tasks, _ = env
    tasks.fail_create_after_insert = True
    assert d.handle_alert(alert()) == ["created AKKIA-100"]
    assert len(tasks.tasks) == 1
    # Metadata lost: the description marker still finds the task, and relinks it.
    tasks.tasks[0]["metadata"] = {}
    assert d.handle_alert(alert(status="resolved")) == ["updated AKKIA-100"]
    assert tasks.tasks[0]["metadata"][gd.META_ALERT] == "akkiabot-readyz-failing/production"


def test_duplicates_are_reported_not_cancelled(env) -> None:
    d, tasks, _ = env
    key = "akkiabot-readyz-failing/production"
    tasks.add(**{gd.META_ALERT: key})
    tasks.add(**{gd.META_ALERT: key})
    (out,) = d.handle_alert(alert())
    assert out.startswith("updated AKKIA-100")
    assert "AKKIA-101" in out


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rule", "other-rule"),
        ("env", "Prod; rm -rf"),
        ("status", "pending"),
        ("fingerprint", "XYZ"),
        ("starts_at", "yesterday"),
        ("handler", "a b"),
        ("watch", "BOT-1"),
        ("watch_kind", "maybe"),
    ],
)
def test_untrusted_fields_are_validated(field: str, value: str) -> None:
    with pytest.raises(gd.DispatchError, match=field):
        alert(**{field: value})


# ------------------------------------------------------------------ watches


def start_watch(d: gd.Dispatcher, tasks: FakeTasks, status: str = "in_review") -> dict[str, Any]:
    fix = tasks.add(status)
    out = d.watch_start(
        fix["identifier"],
        env="production",
        handler="command:spam",
        command="spam",
        owner=OWNER,
        concluder=CONCLUDER,
    )
    assert out.startswith(f"watch_started {fix['identifier']} until 2026-11-27T08:00:00Z; ")
    return fix


DAY1 = T0 + timedelta(days=1, hours=1)  # 2026-11-21T09:00:00Z


def occur(clock: Clock, kind: str, when: datetime = DAY1) -> None:
    """The watched target was really active at ``when``; the clock moves past it."""
    GRAFANA.events[kind].append(when)
    clock.now = max(clock.now, when + timedelta(hours=1))


def regression(**kw: Any) -> gd.Alert:
    base = {
        "rule": gd.WATCH_REGRESSION_RULE,
        "watch_kind": "regression",
        "handler": "command:spam",
        "starts_at": "2026-11-21T09:00:00Z",
        "severity": "warning",
    }
    base.update(kw)
    return alert(**base)


def exercised(**kw: Any) -> gd.Alert:
    base = {
        "rule": gd.WATCH_EXERCISED_RULE,
        "watch_kind": "exercised",
        "command": "spam",
        "starts_at": "2026-11-21T10:00:00Z",
        "severity": "info",
    }
    base.update(kw)
    return alert(**base)


def test_watch_start_writes_metadata_and_one_time_wakeup(env) -> None:
    d, tasks, _ = env
    fix = start_watch(d, tasks)
    meta = fix["metadata"]
    assert meta[gd.W_STATUS] == "active"
    assert meta[gd.W_DEADLINE] == "2026-11-27T08:00:00Z"
    assert (meta[gd.W_ENV], meta[gd.W_HANDLER], meta[gd.W_COMMAND]) == (
        "production",
        "command:spam",
        "spam",
    )
    (wakeup,) = tasks.wakeups
    assert (wakeup["at"], wakeup["agent"]) == ("2026-11-27T08:00:00Z", CONCLUDER)
    assert "watch-conclude --issue AKKIA-100" in wakeup["instruction"]
    assert "--ref " + "f" * 40 in wakeup["instruction"]
    assert meta[gd.W_WAKEUP] == "wakeup-1"
    # Starting again while active changes nothing.
    assert d.watch_start(
        fix["identifier"],
        env="production",
        handler="x",
        command=None,
        owner=OWNER,
        concluder=CONCLUDER,
    ).startswith("already_watching")
    assert len(tasks.wakeups) == 1


def test_watch_needs_a_target(env) -> None:
    d, tasks, _ = env
    fix = tasks.add()
    with pytest.raises(gd.DispatchError):
        d.watch_start(
            fix["id"],
            env="production",
            handler=None,
            command=None,
            owner=OWNER,
            concluder=CONCLUDER,
        )


def test_generic_regression_wakes_the_owner_once(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    occur(clock, "regression")
    assert d.handle_alert(regression()) == [f"regression_woken {fix['identifier']}"]
    assert tasks.wakeups[-1]["agent"] == OWNER
    assert "复发" in tasks.wakeups[-1]["instruction"]
    assert fix["status"] == "in_review"
    assert d.handle_alert(regression()) == [f"regression_already_recorded {fix['identifier']}"]
    assert tasks.tasks == [fix]  # never a general task for watch rules


def test_generic_regression_reopens_a_closed_issue(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks, status="done")
    occur(clock, "regression")
    assert d.handle_alert(regression()) == [f"regression_reopened {fix['identifier']}"]
    assert fix["status"] == "todo"
    assert tasks.wakeups[-1]["agent"] == LEADER


def test_generic_watch_rules_need_a_matching_watch(env) -> None:
    d, tasks, _ = env
    start_watch(d, tasks)
    assert d.handle_alert(regression(handler="command:ban")) == ["no_watch"]
    assert d.handle_alert(regression(env="staging")) == ["no_watch"]
    assert d.handle_alert(exercised(command="ban")) == ["no_watch"]
    assert d.handle_alert(regression(starts_at="2026-11-20T07:00:00Z")) == [
        "before_watch AKKIA-100"
    ]
    assert len(tasks.tasks) == 1


def test_exercised_is_recorded_once(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    occur(clock, "exercised")
    assert d.handle_alert(exercised()) == [f"exercised {fix['identifier']}"]
    assert fix["metadata"][gd.W_EXERCISED] == "2026-11-21T10:00:00Z"
    n = len(tasks.comments)
    assert d.handle_alert(exercised(starts_at="2026-11-22T10:00:00Z")) == [
        f"exercised_already_recorded {fix['identifier']}"
    ]
    assert len(tasks.comments) == n


def test_watch_label_rule_wakes_its_issue_even_after_the_watch(env) -> None:
    d, tasks, _ = env
    fix = tasks.add("done")
    custom = alert(rule="akkiabot-watch-akkia-100", watch=fix["identifier"], severity="warning")
    assert d.handle_alert(custom) == [f"regression_reopened {fix['identifier']}"]
    # A natural-event rule for one fix only counts while its watch is open.
    natural = alert(
        rule="akkiabot-watch-akkia-100-ok", watch=fix["identifier"], watch_kind="exercised"
    )
    assert d.handle_alert(natural) == [f"watch_not_active {fix['identifier']}"]


def test_conclude_observed_without_regression(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    occur(clock, "exercised")
    d.handle_alert(exercised())
    assert (
        d.watch_conclude(fix["identifier"])
        == f"not_due {fix['identifier']} (deadline 2026-11-27T08:00:00Z)"
    )
    clock.now = T0 + timedelta(days=7)
    assert d.watch_conclude(fix["identifier"]).startswith(
        f"concluded {fix['identifier']}: observed_no_regression;"
    )
    assert fix["metadata"][gd.W_STATUS] == "ended"
    assert "已观测，无复发" in tasks.comments[-1][1]
    assert tasks.wakeups[-1]["agent"] == LEADER
    assert d.watch_conclude(fix["identifier"]) == f"watch_not_active {fix['identifier']}"


def test_conclude_unobserved_extends_once_then_ends(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks, status="done")
    clock.now = T0 + timedelta(days=7)
    assert (
        d.watch_conclude(fix["identifier"])
        == f"extended {fix['identifier']} until 2026-12-04T08:00:00Z"
    )
    assert fix["metadata"][gd.W_STATUS] == "extended"
    assert [w["at"] for w in tasks.wakeups] == ["2026-11-27T08:00:00Z", "2026-12-04T08:00:00Z"]
    assert fix["metadata"][gd.W_WAKEUP] == "wakeup-2"
    clock.now = T0 + timedelta(days=14)
    assert d.watch_conclude(fix["identifier"]).startswith(
        f"concluded {fix['identifier']}: unobserved"
    )
    assert "修复已上线、自然场景未观测" in tasks.comments[-1][1]
    assert tasks.wakeups[-1]["agent"] == CONCLUDER  # issue already closed: Leader not woken
    assert len(tasks.wakeups) == 2


def test_conclude_regressed(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    occur(clock, "regression")
    d.handle_alert(regression())
    clock.now = T0 + timedelta(days=7)
    assert d.watch_conclude(fix["identifier"]).startswith(
        f"concluded {fix['identifier']}: regressed"
    )


def test_watch_stop_disables_the_wakeup_and_restart_clears_old_events(env) -> None:
    d, tasks, _ = env
    fix = start_watch(d, tasks)
    d.handle_alert(exercised())
    assert d.watch_stop(fix["identifier"], "fictional reason").startswith(
        f"watch_stopped {fix['identifier']}"
    )
    assert tasks.disabled == ["wakeup-1"]
    assert d.handle_alert(regression()) == ["no_watch"]
    d.watch_start(fix["identifier"], env="production", handler="command:spam", command=None,
                  owner=OWNER, concluder=CONCLUDER)  # fmt: skip
    assert gd.W_EXERCISED not in fix["metadata"]
    assert gd.W_COMMAND not in fix["metadata"]


# -------------------------------------------------------------- self-check


def test_selfcheck_reports_a_silent_chain_and_heartbeat_resolves_it(env) -> None:
    d, tasks, clock = env
    with pytest.raises(gd.DispatchError, match="init"):
        d.selfcheck()
    assert d.init() == "heartbeat baseline 2026-11-20T08:00:00Z"
    clock.now = T0 + timedelta(hours=20)
    assert d.selfcheck().startswith("ok")
    clock.now = T0 + timedelta(hours=30)
    assert "created AKKIA-100" in d.selfcheck()
    assert tasks.tasks[0]["metadata"][gd.META_ALERT] == f"{gd.HEARTBEAT_MISSING_RULE}/production"
    assert "unchanged AKKIA-100" in d.selfcheck()
    heartbeat = alert(rule=gd.HEARTBEAT_RULE, severity="info")
    assert d.handle_alert(heartbeat) == ["heartbeat recorded; updated AKKIA-100"]
    assert "resolved" in tasks.comments[-1][1]
    assert d.handle_alert(heartbeat) == ["heartbeat recorded"]
    assert d.selfcheck().startswith("ok")


# --------------------------------------------------------------------- CLI


def test_cli_validates_and_prints_only_result_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tasks = FakeTasks()
    common = ["--multica-project", PROJECT, "--squad-id", SQUAD, "--leader-id", LEADER,
              "--state-dir", str(tmp_path)]  # fmt: skip
    argv = [*common, "alert", "--rule", "akkiabot-llm-errors", "--env", "staging", "--status",
            "firing", "--fingerprint", "abcdef0123456789", "--starts-at", "2026-11-20T07:58:00Z"]  # fmt: skip
    assert gd.main(argv, tasks=tasks) == 0
    assert capsys.readouterr().out.strip() == "created AKKIA-100"
    bad = [*argv[:-1], "2026-11-20 07:58"]
    assert gd.main(bad, tasks=tasks) == 1
    assert "invalid starts_at" in capsys.readouterr().err
    assert gd.main([*common[:5], "not-a-uuid", *common[6:], "init"], tasks=tasks) == 1


def test_cli_refuses_other_hosts(tmp_path: Path) -> None:
    argv = ["--multica-project", PROJECT, "--squad-id", SQUAD, "--leader-id", LEADER,
            "--expect-host", "fictional-host-that-does-not-exist", "init"]  # fmt: skip
    assert gd.main(argv, tasks=FakeTasks()) == 2


# -------------------------------------------------------------- watch rules


def test_watch_rules_cover_exactly_the_open_watches(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    group = GRAFANA.groups["akkiabot-watch"]
    rules = {r["uid"]: r for r in group["rules"]}
    assert set(rules) == {gd.WATCH_REGRESSION_RULE, gd.WATCH_EXERCISED_RULE}
    regression_rule = rules[gd.WATCH_REGRESSION_RULE]
    (loki,) = [q for q in regression_rule["data"] if q["refId"] == "A"]
    assert loki["datasourceUid"] == "grafanacloud-logs"
    assert "env=~`production`" in loki["model"]["expr"]
    assert "handler=~`command:spam`" in loki["model"]["expr"]
    assert regression_rule["labels"]["akkiabot_rule"] == gd.WATCH_REGRESSION_RULE
    assert regression_rule["labels"]["dispatch_only"] == "true"
    (prom,) = [q for q in rules[gd.WATCH_EXERCISED_RULE]["data"] if q["refId"] == "A"]
    assert prom["datasourceUid"] == "grafanacloud-prom"
    assert prom["model"]["expr"].count("command=~`spam`") == 3
    for rule in group["rules"]:
        assert "__" not in json.dumps(rule["data"]).replace("__expr__", "")

    # A second watch on staging with only a handler extends the regex sets.
    other = tasks.add()
    d.watch_start(other["id"], env="staging", handler="Spam.handle", command=None,
                  owner=OWNER, concluder=CONCLUDER)  # fmt: skip
    rules = {r["uid"]: r for r in GRAFANA.groups["akkiabot-watch"]["rules"]}
    expr = rules[gd.WATCH_REGRESSION_RULE]["data"][0]["model"]["expr"]
    assert "env=~`production|staging`" in expr
    assert r"handler=~`Spam\.handle|command:spam`" in expr

    # Ending every watch removes the group; conclude also re-syncs.
    d.watch_stop(other["id"], "fictional")
    occur(clock, "exercised")
    d.handle_alert(exercised())
    clock.now = T0 + timedelta(days=7)
    d.watch_conclude(fix["identifier"])
    assert "akkiabot-watch" not in GRAFANA.groups
    assert GRAFANA.calls[-1] == "delete akkiabot-watch"


def test_already_watching_repairs_the_rule_group(env) -> None:
    d, tasks, _ = env
    fix = start_watch(d, tasks)
    GRAFANA.groups.clear()
    out = d.watch_start(fix["id"], env="production", handler="x", command=None,
                        owner=OWNER, concluder=CONCLUDER)  # fmt: skip
    assert out.startswith("already_watching")
    assert "akkiabot-watch" in GRAFANA.groups


def test_watch_rule_template_matches_the_relay_and_dispatcher() -> None:
    template = json.loads(gd.WATCH_RULES.read_text(encoding="utf-8"))
    assert template["group"] == "akkiabot-watch"
    for kind, uid in (
        ("regression", gd.WATCH_REGRESSION_RULE),
        ("exercised", gd.WATCH_EXERCISED_RULE),
    ):
        rule = template["rules"][kind]
        assert rule["uid"] == uid
        assert rule["labels"]["watch_kind"] == kind
        assert rule["labels"]["dispatch_only"] == "true"
        assert rule["noDataState"] == "OK"
        assert "$" not in json.dumps(rule)


def test_cli_metadata_filter_is_passed_raw(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`issue list --metadata` parses its value as CSV: JSON quotes would be rejected."""
    seen: list[list[str]] = []

    class Proc:
        returncode = 0
        stdout = '{"issues": [], "has_more": false}'

    def fake_run(argv: list[str], **_: Any) -> Proc:
        seen.append(argv)
        return Proc()

    monkeypatch.setattr(gd.subprocess, "run", fake_run)
    cli = gd.MulticaCli("multica", PROJECT, SQUAD, tmp_path)
    assert cli.find_by_metadata(gd.W_STATUS, "active") == []
    assert "grafana_watch=active" in seen[0]


# ------------------------------------------- partial failures and the window


@pytest.mark.parametrize(
    ("status", "outcome", "woken"), [("in_review", "woken", OWNER), ("done", "reopened", LEADER)]
)
def test_regression_retry_completes_the_wakeup(env, status: str, outcome: str, woken: str) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks, status=status)
    occur(clock, "regression")
    tasks.fail_wakeup_once = True
    with pytest.raises(gd.DispatchError):
        d.handle_alert(regression())
    # Nothing claims a wakeup that does not exist, and the event is not marked done.
    assert not any("已唤醒" in body for _, body in tasks.comments[1:])
    assert gd.W_REGRESSION not in fix["metadata"]
    # The retry (the issue may already be reopened) finishes the same path.
    assert d.handle_alert(regression()) == [f"regression_{outcome} {fix['identifier']}"]
    assert tasks.wakeups[-1]["agent"] == woken
    assert fix["status"] == ("todo" if status == "done" else status)
    assert gd.W_REGRESSION_PENDING not in fix["metadata"]
    assert d.handle_alert(regression()) == [f"regression_already_recorded {fix['identifier']}"]


def test_general_reopen_retry_still_wakes_the_leader(env) -> None:
    d, tasks, _ = env
    d.handle_alert(alert())
    tasks.tasks[0]["status"] = "done"
    tasks.fail_wakeup_once = True
    with pytest.raises(gd.DispatchError):
        d.handle_alert(alert(starts_at="2026-11-21T07:00:00Z"))
    assert tasks.tasks[0]["status"] == "todo"  # reopened before the wakeup failed
    assert d.handle_alert(alert(starts_at="2026-11-21T07:00:00Z")) == ["reopened AKKIA-100"]
    assert tasks.wakeups[-1]["agent"] == LEADER
    assert gd.META_ALERT_PENDING not in tasks.tasks[0]["metadata"]


def test_activity_before_the_watch_never_counts(env) -> None:
    d, tasks, clock = env
    # Fictional: the command ran and the handler failed shortly before the watch opened.
    GRAFANA.events["exercised"].append(T0 - timedelta(minutes=30))
    GRAFANA.events["regression"].append(T0 - timedelta(minutes=5))
    fix = start_watch(d, tasks)
    # The rules' first evaluation after opening still sees that activity in their lookback.
    clock.now = T0 + timedelta(minutes=2)
    early = {"starts_at": "2026-11-20T08:01:00Z"}
    assert d.handle_alert(exercised(**early)) == [f"outside_window {fix['identifier']}"]
    assert d.handle_alert(regression(**early)) == [f"outside_window {fix['identifier']}"]
    assert len(tasks.wakeups) == 1  # only the deadline wakeup
    clock.now = T0 + timedelta(days=7)
    assert d.watch_conclude(fix["identifier"]).startswith(f"extended {fix['identifier']}")
    clock.now = T0 + timedelta(days=14)
    assert d.watch_conclude(fix["identifier"]).startswith(
        f"concluded {fix['identifier']}: unobserved"
    )
    for query in GRAFANA.queries:
        assert 'env="production"' in query


def test_conclude_counts_events_whose_alert_was_skipped(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    # Inside the window, but no alert was handled (merged into an earlier episode).
    GRAFANA.events["exercised"].append(T0 + timedelta(days=2))
    GRAFANA.events["regression"].append(T0 + timedelta(days=3))
    clock.now = T0 + timedelta(days=7)
    assert d.watch_conclude(fix["identifier"]).startswith(
        f"concluded {fix['identifier']}: regressed"
    )
    assert OWNER in [w["agent"] for w in tasks.wakeups]
    assert gd.W_EXERCISED in fix["metadata"]


def test_window_query_shapes() -> None:
    reg = gd.window_query("regression", "production", "command:spam", 600)
    assert reg == (
        'sum(count_over_time({service="app", env="production"} |= "handler failed" '
        '| handler="command:spam" [600s]))'
    )
    ex = gd.window_query("exercised", "staging", "spam", 3600)
    assert "increase(" in ex
    assert "offset 3600s" in ex
    with pytest.raises(gd.DispatchError):
        gd.window_query("regression", "production", 'x"} or vector(1)', 60)


# ------------------------------------- review round 2: idempotent steps, deadline


@pytest.mark.parametrize("status", ["in_review", "done"])
@pytest.mark.parametrize("failure", ["fail_comment_once", "wakeup_lands_but_fails_once"])
def test_regression_retry_never_wakes_twice(env, status: str, failure: str) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks, status=status)
    occur(clock, "regression")
    setattr(tasks, failure, True)
    with pytest.raises(gd.DispatchError):
        d.handle_alert(regression())
    out = d.handle_alert(regression())
    assert out[0].startswith("regression_")
    woken = [w for w in tasks.wakeups if "grafana-watch:regression:" in w["instruction"]]
    assert len(woken) == 1
    assert sum("出现复发" in body for _, body in tasks.comments) == 1
    assert d.handle_alert(regression()) == [f"regression_already_recorded {fix['identifier']}"]


@pytest.mark.parametrize("failure", ["fail_comment_once", "wakeup_lands_but_fails_once"])
def test_general_reopen_retry_never_wakes_twice(env, failure: str) -> None:
    d, tasks, _ = env
    d.handle_alert(alert())
    tasks.tasks[0]["status"] = "done"
    setattr(tasks, failure, True)
    with pytest.raises(gd.DispatchError):
        d.handle_alert(alert(starts_at="2026-11-21T07:00:00Z"))
    assert d.handle_alert(alert(starts_at="2026-11-21T07:00:00Z")) == ["reopened AKKIA-100"]
    assert [w["agent"] for w in tasks.wakeups] == [LEADER]
    assert sum("再次触发" in body for _, body in tasks.comments) == 1


@pytest.mark.parametrize("kind", ["exercised", "regression"])
def test_activity_after_the_deadline_never_counts(env, kind: str) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    deadline = T0 + timedelta(days=7)
    # Fictional: the first activity happens after the deadline, and the verdict runs late.
    occur(clock, kind, deadline + timedelta(hours=1))
    late = (deadline + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    make = exercised if kind == "exercised" else regression
    assert d.handle_alert(make(starts_at=late)) == [f"after_deadline {fix['identifier']}"]
    # An alert whose episode started in time still has to show activity in the window.
    assert d.handle_alert(make(starts_at="2026-11-26T08:00:00Z")) == [
        f"outside_window {fix['identifier']}"
    ]
    assert d.watch_conclude(fix["identifier"]).startswith(f"extended {fix['identifier']}")
    assert len([w for w in tasks.wakeups if w["agent"] == OWNER]) == 0


def test_late_delivered_alert_inside_the_window_is_verified(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    deadline = T0 + timedelta(days=7)
    occur(clock, "exercised", deadline - timedelta(hours=1))
    clock.now = deadline + timedelta(hours=2)  # delivered late, before the verdict
    assert d.handle_alert(exercised(starts_at="2026-11-27T07:00:00Z")) == [
        f"exercised {fix['identifier']}"
    ]
    assert d.watch_conclude(fix["identifier"]).startswith(
        f"concluded {fix['identifier']}: observed_no_regression"
    )


def test_conclude_retry_does_not_repeat_the_extension(env) -> None:
    d, tasks, clock = env
    fix = start_watch(d, tasks)
    clock.now = T0 + timedelta(days=7)
    tasks.fail_comment_once = True
    with pytest.raises(gd.DispatchError):
        d.watch_conclude(fix["identifier"])
    # The retry finishes the extension: same new deadline, no second wakeup.
    assert (
        d.watch_conclude(fix["identifier"])
        == f"extended {fix['identifier']} until 2026-12-04T08:00:00Z"
    )
    assert len(tasks.wakeups) == 2
    assert sum("按约定自动延长一次" in body for _, body in tasks.comments) == 1
