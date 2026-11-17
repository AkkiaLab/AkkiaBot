"""Tests for scripts/sentry_dispatch.py (BOT-55). All Sentry data here is fictional."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import sentry_dispatch as sd

ORG = "akkia"
TARGET = "akkiabot-staging"


def sentry_issue(issue_id: str = "1001", **overrides: Any) -> dict[str, Any]:
    issue = {
        "id": issue_id,
        "shortId": f"AKKIABOT-STAGING-{issue_id}",
        "project": {"id": "42", "slug": TARGET},
        "status": "unresolved",
        "substatus": "new",
        "level": "error",
        "title": "ValueError: fictional failure",
        "culprit": "app.handlers in handle",
        "permalink": f"https://example.invalid/issues/{issue_id}/",
        "firstSeen": "2026-11-17T08:00:00Z",
        "lastSeen": "2026-11-17T08:05:00Z",
        "count": "3",
        "userCount": 0,
    }
    issue.update(overrides)
    return issue


class FakeSentry:
    def __init__(self, issues: dict[str, dict[str, Any]] | None = None) -> None:
        self.issues = issues or {}
        self.seen: list[dict[str, Any]] = []

    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        return self.issues.get(issue_id)

    def tag_values(self, issue_id: str, key: str) -> list[tuple[str, int]]:
        return [("production", 3)] if key == "environment" else [("akkiabot@0.0.0-fake", 3)]

    def project_id(self, slug: str) -> str:
        return "42"

    def issues_seen_since(self, project_id: str, since: datetime):
        yield from self.seen


class FakeTasks:
    """In-memory Multica with the server's active-duplicate-title guard."""

    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.creates = 0
        self.create_delay = 0.0
        self.fail_create_after_insert = False
        self.fail_metadata_once = False
        self._lock = threading.Lock()

    def _matches(self, key: str) -> list[dict[str, Any]]:
        return [dict(t) for t in self.tasks if t["metadata"].get(sd.META_KEY) == key]

    def find_by_metadata(self, key: str) -> list[dict[str, Any]]:
        return self._matches(key)

    def find_by_marker(self, key: str) -> list[dict[str, Any]]:
        # Like the real `issue search`: results carry an empty metadata object.
        return [{**t, "metadata": {}} for t in self.tasks if sd.marker(key) in t["description"]]

    def list_linked(self) -> list[dict[str, Any]]:
        return [dict(t) for t in self.tasks if t["metadata"].get(sd.META_SOURCE) == sd.SOURCE_VALUE]

    def create(self, title: str, description: str, status: str, priority: str) -> None:
        time.sleep(self.create_delay)
        with self._lock:
            self.creates += 1
            if any(
                t["title"] == title and t["status"] not in sd.CLOSED_TASK_STATUSES
                for t in self.tasks
            ):
                raise sd.DispatchError("multica issue create failed (exit 1)")
            number = len(self.tasks) + 1
            self.tasks.append(
                {
                    "id": f"task-{number}",
                    "identifier": f"BOT-{900 + number}",
                    "number": 900 + number,
                    "title": title,
                    "description": description,
                    "status": status,
                    "priority": priority,
                    "metadata": {},
                }
            )
        if self.fail_create_after_insert:
            self.fail_create_after_insert = False
            raise sd.DispatchError("multica issue create timed out")

    def _task(self, task_id: str) -> dict[str, Any]:
        return next(t for t in self.tasks if t["id"] == task_id)

    def set_metadata(self, task_id: str, key: str, value: str) -> None:
        if self.fail_metadata_once:
            self.fail_metadata_once = False
            raise sd.DispatchError("multica issue metadata failed (exit 1)")
        self._task(task_id)["metadata"][key] = value

    def comment(self, task_id: str, body: str) -> None:
        self.comments.append((task_id, body))

    def set_status(self, task_id: str, status: str) -> None:
        self._task(task_id)["status"] = status


@pytest.fixture
def world(tmp_path: Path):
    sentry = FakeSentry({"1001": sentry_issue()})
    tasks = FakeTasks()
    dispatcher = sd.Dispatcher(
        sentry=sentry,
        tasks=tasks,
        org=ORG,
        target_project=TARGET,
        lock_path=tmp_path / "dispatch.lock",
        log=lambda _line: None,
    )
    return sentry, tasks, dispatcher


KEY = sd.issue_key(ORG, TARGET, "1001")
# mock: fabricated, unusable Telegram-token-shaped value for the redaction test.
MOCK_TELEGRAM_SHAPED_TOKEN = "123456789:AAmockmockmockmockmockmockmockmockmock"


def test_created_webhook_creates_one_linked_task(world) -> None:
    _, tasks, dispatcher = world
    assert dispatcher.sync("1001", may_create=True, reason="webhook created").startswith("created")
    (task,) = tasks.tasks
    assert sd.marker(KEY) in task["description"]
    assert task["metadata"] == {
        sd.META_KEY: KEY,
        sd.META_SOURCE: sd.SOURCE_VALUE,
        sd.META_STATE: "unresolved/new",
    }
    assert task["status"] == "todo"
    assert task["priority"] == "high"
    # Redelivery / duplicate notification: reuse, no comment spam.
    assert dispatcher.sync("1001", may_create=True, reason="webhook created").startswith(
        "unchanged"
    )
    assert len(tasks.tasks) == 1
    assert tasks.comments == []


def test_concurrent_notifications_create_once(world) -> None:
    _, tasks, dispatcher = world
    tasks.create_delay = 0.2
    results: list[str] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(dispatcher.sync("1001", may_create=True, reason="w"))
        )
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(tasks.tasks) == 1
    assert tasks.creates == 1
    assert sorted(r.split()[0] for r in results) == [
        "created",
        "unchanged",
        "unchanged",
        "unchanged",
    ]


def test_timed_out_create_is_looked_up_not_resent(world) -> None:
    _, tasks, dispatcher = world
    tasks.fail_create_after_insert = True
    assert dispatcher.sync("1001", may_create=True, reason="w").startswith("created")
    assert tasks.creates == 1
    assert len(tasks.tasks) == 1


def test_interrupted_metadata_write_is_found_by_marker(world) -> None:
    _, tasks, dispatcher = world
    tasks.fail_metadata_once = True
    with pytest.raises(sd.DispatchError):
        dispatcher.sync("1001", may_create=True, reason="w")
    assert tasks.tasks[0]["metadata"] == {}
    dispatcher.sync("1001", may_create=True, reason="w")
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0]["metadata"][sd.META_KEY] == KEY


def test_resolved_only_syncs_and_out_of_order_created_converges(world) -> None:
    sentry, tasks, dispatcher = world
    dispatcher.sync("1001", may_create=True, reason="w")
    tasks.tasks[0]["status"] = "in_review"
    sentry.issues["1001"] = sentry_issue(status="resolved", substatus=None)
    assert dispatcher.sync("1001", may_create=False, reason="webhook resolved").startswith(
        "updated"
    )
    assert tasks.tasks[0]["status"] == "in_review"
    assert "不会自动关闭" in tasks.comments[-1][1]
    # A late "created" delivery reads the current (resolved) state: nothing changes.
    assert dispatcher.sync("1001", may_create=True, reason="webhook created").startswith(
        "unchanged"
    )
    assert len(tasks.comments) == 1


def test_resolved_without_task_creates_nothing(world) -> None:
    sentry, tasks, dispatcher = world
    sentry.issues["1001"] = sentry_issue(status="resolved", substatus=None)
    assert dispatcher.sync("1001", may_create=True, reason="w") == "no_task_needed"
    assert tasks.tasks == []


@pytest.mark.parametrize(
    ("task_status", "expected"), [("done", "todo"), ("in_progress", "in_progress")]
)
def test_regression_reopens_only_closed_tasks(world, task_status: str, expected: str) -> None:
    sentry, tasks, dispatcher = world
    dispatcher.sync("1001", may_create=True, reason="w")
    tasks.tasks[0]["status"] = task_status
    sentry.issues["1001"] = sentry_issue(substatus="regressed")
    dispatcher.sync("1001", may_create=True, reason="webhook unresolved")
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0]["status"] == expected
    assert "回归" in tasks.comments[-1][1]


def test_closed_task_is_reused_not_duplicated(world) -> None:
    sentry, tasks, dispatcher = world
    dispatcher.sync("1001", may_create=True, reason="w")
    tasks.tasks[0]["status"] = "done"
    sentry.issues["1001"] = sentry_issue(lastSeen="2026-11-18T00:00:00Z")
    dispatcher.sync("1001", may_create=True, reason="w")
    assert len(tasks.tasks) == 1


def test_non_target_and_unknown_issues_are_ignored(world) -> None:
    sentry, tasks, dispatcher = world
    sentry.issues["2002"] = sentry_issue("2002", project={"id": "7", "slug": "akkia-bot-lab"})
    assert dispatcher.sync("2002", may_create=True, reason="w") == "project_not_target"
    assert dispatcher.sync("3003", may_create=True, reason="w") == "sentry_issue_not_found"
    with pytest.raises(sd.DispatchError):
        dispatcher.sync("1 OR 1", may_create=True, reason="w")
    assert tasks.tasks == []


def test_test_events_are_parked_in_backlog(world) -> None:
    sentry, tasks, dispatcher = world
    sentry.issues["1001"] = sentry_issue(title=f"RuntimeError: {sd.TEST_MARKER} relay check")
    dispatcher.sync("1001", may_create=True, reason="w")
    assert tasks.tasks[0]["status"] == "backlog"
    assert tasks.tasks[0]["title"].startswith("[测试] ")


def test_reconcile_creates_missed_and_syncs_open_tasks(world) -> None:
    sentry, tasks, dispatcher = world
    since = datetime(2026, 11, 17, 7, 0, tzinfo=UTC)
    # Missed "created": new since the cursor.
    sentry.issues["1002"] = sentry_issue("1002")
    # Ongoing old issue without a task: not created by reconciliation.
    sentry.issues["1003"] = sentry_issue(
        "1003", firstSeen="2026-11-01T00:00:00Z", substatus="ongoing"
    )
    sentry.seen = [sentry.issues["1002"], sentry.issues["1003"]]
    # Task whose "resolved" webhook was lost.
    dispatcher.sync("1001", may_create=True, reason="w")
    sentry.issues["1001"] = sentry_issue(status="resolved", substatus=None)
    results = dispatcher.reconcile(since)
    assert {r.split(":")[0] for r in results} == {"1001", "1002", "1003"}
    assert len(tasks.tasks) == 2
    assert tasks.tasks[0]["metadata"][sd.META_STATE] == "resolved/-"


def test_reconcile_failure_raises_so_cursor_is_kept(world, tmp_path: Path) -> None:
    sentry, _, dispatcher = world
    sentry.seen = [{"id": "1001", "firstSeen": "2026-11-17T08:00:00Z"}]

    def boom(issue_id: str) -> dict[str, Any]:
        raise sd.DispatchError("Sentry API HTTP 502")

    sentry.get_issue = boom  # type: ignore[method-assign]
    with pytest.raises(sd.DispatchError, match="cursor not advanced"):
        dispatcher.reconcile(datetime(2026, 11, 17, tzinfo=UTC))


def test_sentry_client_paginates_and_fails_closed() -> None:
    calls: list[str] = []
    pages = {
        0: ([{"id": "1"}], '<https://sentry.invalid/next>; rel="next"; results="true"; cursor="a"'),
        1: (
            [{"id": "2"}],
            '<https://sentry.invalid/next2>; rel="next"; results="false"; cursor="b"',
        ),
    }

    def http(url: str, headers: Mapping[str, str]):
        assert headers["Authorization"] == "Bearer mock-sentry-token"
        body, link = pages[len(calls)]
        calls.append(url)
        return 200, {"Link": link}, json.dumps(body).encode()

    client = sd.SentryClient(token="mock-sentry-token", org=ORG, http=http)  # mock token
    ids = [i["id"] for i in client.issues_seen_since("42", datetime(2026, 11, 17, tzinfo=UTC))]
    assert ids == ["1", "2"]
    assert "lastSeen%3A%3E%3D2026-11-17T00%3A00%3A00" in calls[0]

    def failing(url: str, headers: Mapping[str, str]):
        return 500, {}, b""

    with pytest.raises(sd.DispatchError, match="HTTP 500"):
        list(
            sd.SentryClient(token="mock-sentry-token", org=ORG, http=failing).issues_seen_since(
                "42", datetime.now(UTC)
            )
        )


def test_cursor_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "reconcile-cursor.json"
    assert sd.read_cursor(path) is None
    value = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    sd.write_cursor(path, value)
    assert sd.read_cursor(path) == value
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("raw", "hidden"),
    [
        ("Post https://internal.example.invalid/api/verify?k=v failed", "/api/verify"),
        (f"bot {MOCK_TELEGRAM_SHAPED_TOKEN} rejected", "AAmock"),
        ("token=mock-value-123 refused", "mock-value-123"),
        ("user mock.person@example.invalid failed", "mock.person"),
        ("dial 10.0.0.12:443 timeout", "10.0.0.12"),
        ("chat -1001234567890 missing", "1001234567890"),
    ],
)
def test_redact_hides_secret_and_personal_shapes(raw: str, hidden: str) -> None:
    assert hidden not in sd.redact(raw)


def test_redact_truncates_and_strips_backticks() -> None:
    out = sd.redact("`x` " + "a " * 200, limit=20)
    assert len(out) == 20
    assert "`" not in out


def test_stale_snapshot_cannot_overwrite_newer_state(tmp_path: Path) -> None:
    """Notification A reads an older state, B applies a newer one: B's must win."""
    sentry = FakeSentry({"1001": sentry_issue()})
    tasks = FakeTasks()
    dispatcher = sd.Dispatcher(
        sentry=sentry,
        tasks=tasks,
        org=ORG,
        target_project=TARGET,
        lock_path=tmp_path / "dispatch.lock",
        log=lambda _line: None,
    )
    dispatcher.sync("1001", may_create=True, reason="w")
    assert tasks.tasks[0]["metadata"][sd.META_STATE] == "unresolved/new"

    sentry.issues["1001"] = sentry_issue(substatus="escalating")
    a_reading, release_a = threading.Event(), threading.Event()
    original_get = sentry.get_issue
    first = {"armed": True}

    def stalling_get(issue_id: str) -> dict[str, Any] | None:
        snapshot = original_get(issue_id)
        if first["armed"]:
            first["armed"] = False
            a_reading.set()
            assert release_a.wait(5)
        return snapshot

    sentry.get_issue = stalling_get  # type: ignore[method-assign]
    a = threading.Thread(target=lambda: dispatcher.sync("1001", may_create=True, reason="A"))
    a.start()
    assert a_reading.wait(5)
    # Sentry moves on while A still holds its (now stale) snapshot.
    sentry.issues["1001"] = sentry_issue(status="resolved", substatus=None)
    b = threading.Thread(target=lambda: dispatcher.sync("1001", may_create=False, reason="B"))
    b.start()
    time.sleep(0.3)  # without the lock around the read, B would finish here
    release_a.set()
    a.join(5)
    b.join(5)
    assert tasks.tasks[0]["metadata"][sd.META_STATE] == "resolved/-"
    assert [body.split("：")[1].split("。")[0] for _, body in tasks.comments] == [
        "`unresolved/new` → `unresolved/escalating`",
        "`unresolved/escalating` → `resolved/-`",
    ]


# mock: fabricated, unusable credential-shaped values for the output-level redaction test.
MOCK_SHORT_BEARER = "mock.Short-1"
MOCK_BASIC = "bW9jazptb2Nr"
MOCK_URL_PASSWORD = "mockpass"
MOCK_JWT = "eyJmb2NrIjoxfQ.eyJtb2NrIjoxfQ.mocksig"


@pytest.mark.parametrize(
    ("free_text", "secret"),
    [
        (f"HTTPError Authorization: Bearer {MOCK_SHORT_BEARER} rejected", MOCK_SHORT_BEARER),
        (f"headers={{'Authorization': 'Bearer {MOCK_SHORT_BEARER}'}}", MOCK_SHORT_BEARER),
        (f"Proxy-Authorization: Basic {MOCK_BASIC}", MOCK_BASIC),
        (f"sent Bearer {MOCK_SHORT_BEARER} to upstream", MOCK_SHORT_BEARER),
        (
            f"connect redis://mockuser:{MOCK_URL_PASSWORD}@cache.invalid:6379/0 failed",
            MOCK_URL_PASSWORD,
        ),
        (f"dsn https://mockkey:{MOCK_URL_PASSWORD}@o0.ingest.invalid/1", MOCK_URL_PASSWORD),
        (f"login mockuser:{MOCK_URL_PASSWORD}@db.invalid refused", MOCK_URL_PASSWORD),
        (f"api_key={MOCK_SHORT_BEARER}&x=1 invalid", MOCK_SHORT_BEARER),
        (f'{{"password": "{MOCK_URL_PASSWORD}"}}', MOCK_URL_PASSWORD),
        (f"jwt {MOCK_JWT} expired", MOCK_JWT),
        (f"key={MOCK_SHORT_BEARER} invalid", MOCK_SHORT_BEARER),
        (f"key: {MOCK_SHORT_BEARER} invalid", MOCK_SHORT_BEARER),
        (f"Bearer={MOCK_SHORT_BEARER} invalid", MOCK_SHORT_BEARER),
        (f"bearer: {MOCK_SHORT_BEARER} invalid", MOCK_SHORT_BEARER),
        (f"Basic={MOCK_BASIC}", MOCK_BASIC),
        (f"token: {MOCK_SHORT_BEARER}", MOCK_SHORT_BEARER),
        (f"secret={MOCK_SHORT_BEARER}", MOCK_SHORT_BEARER),
        (f"private_key: {MOCK_SHORT_BEARER}", MOCK_SHORT_BEARER),
        (f"authorization={MOCK_SHORT_BEARER}", MOCK_SHORT_BEARER),
        (f"passwd={MOCK_URL_PASSWORD}", MOCK_URL_PASSWORD),
        (f"bot {MOCK_TELEGRAM_SHAPED_TOKEN} rejected", MOCK_TELEGRAM_SHAPED_TOKEN.split(":")[1]),
    ],
)
def test_task_title_and_description_never_carry_credentials(free_text: str, secret: str) -> None:
    snap = sd.Snapshot(
        issue_id="1001",
        short_id="AKKIABOT-STAGING-Z",
        project_slug=TARGET,
        status="unresolved",
        substatus="new",
        level="error",
        title=free_text,
        culprit=free_text,
        permalink="https://example.invalid/issues/1001/",
        first_seen="2026-11-17T08:00:00Z",
        last_seen="2026-11-17T08:05:00Z",
        count="1",
        user_count=0,
        environments=[(free_text, 1)],
        releases=[(free_text, 1)],
    )
    outputs = [sd.task_title(snap), sd.task_description(snap, KEY)]
    for output in outputs:
        assert secret not in output
        assert "mockuser:" not in output
