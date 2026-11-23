"""Vault writes go through stdin only, are read back, and never leak values in errors."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from akkiabot_e2e import cli
from akkiabot_e2e.errors import ConfigError
from akkiabot_e2e.files import write_private_file
from akkiabot_e2e.group import VaultGroupRegistry
from akkiabot_e2e.testing import ADMIN, MOCK_ADMIN_SESSION, live_env
from akkiabot_e2e.vault import FieldValue, Vault

# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_VAULT_BOT_TOKEN = "1:fake-token-for-tests"


class FakeOp:
    """In-memory ``op``: items by title; records argv and stdin of every call."""

    def __init__(self, items: dict[str, list[dict]] | None = None) -> None:
        self.items = {
            title: {"id": f"id-{title}", "title": title, "fields": fields}
            for title, fields in (items or {}).items()
        }
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.fail: str | None = None
        self.corrupt_reads = False
        self.stale_reads = 0
        """Number of upcoming reads that still return the previous value."""
        self.previous: dict[tuple[str, str], str] = {}

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
        argv = list(argv)
        self.calls.append((argv, stdin))
        verb = "read" if argv[1] == "read" else argv[2]
        if verb == self.fail:
            return 1, b"", f"[ERROR] boom {MOCK_ADMIN_SESSION}".encode()
        if verb == "read":
            _, _, _, title, label = argv[2].split("/")
            item = self.items.get(title)
            field = next((f for f in (item or {}).get("fields", []) if f["label"] == label), None)
            if field is None:
                return 1, b"", b"[ERROR] no such field"
            value = "corrupted" if self.corrupt_reads else field.get("value", "")
            if self.stale_reads > 0:
                self.stale_reads -= 1
                value = self.previous.get((title, label), "")
            return 0, value.encode() + b"\n", b""
        if verb == "get":
            item = self.items.get(argv[3])
            if item is None:
                return 1, b"", b'[ERROR] "x" isn\'t an item in the "AkkiaBot" vault.'
            return 0, json.dumps(item).encode(), b""
        data = json.loads(stdin or b"{}")
        old = self.items.get(data.get("title", ""), {})
        for f in old.get("fields", []):
            self.previous[(old["title"], f["label"])] = f.get("value", "")
        if verb == "create":
            data["id"] = f"id-{data['title']}"
        self.items[data["title"]] = data
        return 0, b"{}", b""

    def argv_text(self) -> str:
        return " ".join(" ".join(argv) for argv, _ in self.calls)


def labels(op: FakeOp, title: str) -> dict[str, tuple[str, str | None]]:
    return {f["label"]: (f.get("type", ""), f.get("value")) for f in op.items[title]["fields"]}


def test_set_fields_edits_existing_item_via_stdin_only() -> None:
    op = FakeOp({"e2e-telegram": [{"id": "a", "label": "api_id", "type": "STRING", "value": "1"}]})

    Vault(run=op).set_fields("e2e-telegram", {"admin_session": FieldValue(MOCK_ADMIN_SESSION)})

    assert MOCK_ADMIN_SESSION not in op.argv_text()
    edit = [stdin for argv, stdin in op.calls if argv[:3] == ["op", "item", "edit"]]
    assert len(edit) == 1
    assert MOCK_ADMIN_SESSION in (edit[0] or b"").decode()
    assert labels(op, "e2e-telegram") == {
        "api_id": ("STRING", "1"),
        "admin_session": ("CONCEALED", MOCK_ADMIN_SESSION),
    }


def test_set_fields_creates_missing_item() -> None:
    op = FakeOp()
    Vault(run=op).set_fields("e2e-bot", {"bot_token": FieldValue(MOCK_VAULT_BOT_TOKEN)})
    assert ["op", "item", "create", "--vault=AkkiaBot", "-"] in [a for a, _ in op.calls]
    assert labels(op, "e2e-bot") == {"bot_token": ("CONCEALED", MOCK_VAULT_BOT_TOKEN)}
    assert "fake-token" not in op.argv_text()


def test_write_errors_are_redacted_and_readback_is_checked() -> None:
    op = FakeOp({"e2e-telegram": []})
    op.fail = "edit"
    with pytest.raises(ConfigError, match="写入") as info:
        Vault(run=op).set_fields("e2e-telegram", {"admin_session": FieldValue(MOCK_ADMIN_SESSION)})
    assert MOCK_ADMIN_SESSION not in str(info.value)

    op.fail, op.corrupt_reads = None, True
    waits: list[float] = []
    with pytest.raises(ConfigError, match="不一致"):
        Vault(run=op, sleep=waits.append).set_fields(
            "e2e-telegram", {"admin_session": FieldValue(MOCK_ADMIN_SESSION)}
        )
    assert len(waits) == 4  # gave up after 5 reads


def test_readback_tolerates_eventual_consistency() -> None:
    # Regression: a read right after the edit returned the old (empty) value and
    # the fresh member login was reported as a failed write.
    op = FakeOp({"e2e-telegram": [{"id": "s", "label": "member_session", "value": ""}]})
    op.stale_reads = 2
    waits: list[float] = []
    Vault(run=op, sleep=waits.append).set_fields(
        "e2e-telegram", {"member_session": FieldValue(MOCK_ADMIN_SESSION)}
    )
    assert waits == [1.0, 2.0]
    assert labels(op, "e2e-telegram")["member_session"] == ("CONCEALED", MOCK_ADMIN_SESSION)


def test_add_ids_merges_and_drops_placeholder() -> None:
    op = FakeOp({"e2e-telegram": [{"id": "x", "label": "allowed_chat_ids", "value": "0"}]})
    vault = Vault(run=op)
    assert vault.add_ids("e2e-telegram", "allowed_chat_ids", [-1002]) == [-1002]
    assert vault.add_ids("e2e-telegram", "allowed_chat_ids", [-1001, -1002]) == [-1002, -1001]
    assert labels(op, "e2e-telegram")["allowed_chat_ids"] == ("STRING", "-1002,-1001")


def test_remove_ids_removes_only_given_ids_and_reads_back() -> None:
    op = FakeOp(
        {"e2e-telegram": [{"id": "x", "label": "allowed_chat_ids", "value": "-1003,-1002,-1001"}]}
    )
    vault = Vault(run=op)
    assert vault.read_ids("e2e-telegram", "allowed_chat_ids") == [-1003, -1002, -1001]
    assert vault.remove_ids("e2e-telegram", "allowed_chat_ids", {-1002, -9999}) == [-1003, -1001]
    assert labels(op, "e2e-telegram")["allowed_chat_ids"] == ("STRING", "-1003,-1001")
    assert vault.read_ids("e2e-telegram", "missing_field") == []


class FlakyOp(FakeOp):
    """``FakeOp`` whose n-th call of a verb (1-based) fails like a network error."""

    def __init__(self, items: dict[str, list[dict]], fail_on: dict[str, set[int]]) -> None:
        super().__init__(items)
        self.fail_on = fail_on
        self.counts: dict[str, int] = {}

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
        verb = "read" if argv[1] == "read" else argv[2]
        self.counts[verb] = self.counts.get(verb, 0) + 1
        if self.counts[verb] in self.fail_on.get(verb, set()):
            self.calls.append((list(argv), stdin))
            return 1, b"", b"[ERROR] Post https://my.1password.com: connection reset"
        return super().__call__(argv, stdin)


ALLOWED = [{"id": "x", "label": "allowed_chat_ids", "value": "-1003,-1002,-1001"}]


def edits(op: FakeOp) -> int:
    return sum(1 for argv, _ in op.calls if argv[1:3] == ["item", "edit"])


def test_id_list_read_failure_never_writes() -> None:
    """A failed read must not look like an empty list (it would wipe the allowlist)."""
    for method, ids in (("remove_ids", {-1002}), ("add_ids", [-1004])):
        op = FlakyOp({"e2e-telegram": ALLOWED}, {"get": {1}, "read": {1}})
        with pytest.raises(ConfigError, match="读取 1Password 条目"):
            getattr(Vault(run=op), method)("e2e-telegram", "allowed_chat_ids", ids)
        assert edits(op) == 0
        assert labels(op, "e2e-telegram")["allowed_chat_ids"][1] == "-1003,-1002,-1001"


def test_prune_reread_failure_after_delete_keeps_allowlist() -> None:
    from akkiabot_e2e.prune import VaultAllowlist

    # Planning read (get #1) succeeds; the re-read before removing (get #2) fails.
    op = FlakyOp({"e2e-telegram": ALLOWED}, {"get": {2}})
    allowlist = VaultAllowlist(Vault(run=op))
    assert allowlist.chat_ids() == {-1003, -1002, -1001}
    with pytest.raises(ConfigError):
        allowlist.remove({-1002})
    assert edits(op) == 0
    assert labels(op, "e2e-telegram")["allowed_chat_ids"][1] == "-1003,-1002,-1001"


def test_empty_readback_needs_a_successful_read() -> None:
    pending = [{"id": "p", "label": "pending_group_operation", "value": "0123abcd4567"}]
    # `op read` fails and the confirming item read fails too: not verified.
    op = FlakyOp({"e2e-telegram": pending}, {"read": set(range(1, 10)), "get": set(range(2, 10))})
    with pytest.raises(ConfigError, match="回读"):
        Vault(run=op, sleep=lambda _: None).clear_field("e2e-telegram", "pending_group_operation")
    # `op read` fails but the item shows the field empty: verified.
    op = FlakyOp({"e2e-telegram": pending}, {"read": set(range(1, 10))})
    Vault(run=op, sleep=lambda _: None).clear_field("e2e-telegram", "pending_group_operation")
    assert labels(op, "e2e-telegram")["pending_group_operation"][1] == ""


def test_group_registry_records_and_clears_the_pending_operation() -> None:
    op = FakeOp({"e2e-telegram": [{"id": "c", "label": "allowed_chat_ids", "value": "-1001"}]})
    registry = VaultGroupRegistry(Vault(run=op))
    assert registry.pending_operation() is None  # field does not exist yet

    registry.begin("0123abcd4567")
    assert registry.pending_operation() == "0123abcd4567"
    assert labels(op, "e2e-telegram")["pending_group_operation"] == ("STRING", "0123abcd4567")

    registry.register(-1002)
    registry.finish()
    assert registry.pending_operation() is None
    assert labels(op, "e2e-telegram") == {
        "allowed_chat_ids": ("STRING", "-1002,-1001"),
        "pending_group_operation": ("STRING", ""),
    }


def test_clearing_accepts_an_empty_field_that_reads_back_as_missing() -> None:
    op = FakeOp({"e2e-telegram": [{"id": "p", "label": "pending_group_operation", "value": "x"}]})

    def run(argv, stdin):
        code, out, err = op(argv, stdin)
        if argv[1] == "read" and out == b"\n":
            return 1, b"", b"[ERROR] field has no value"
        return code, out, err

    Vault(run=run, sleep=lambda _: None).clear_field("e2e-telegram", "pending_group_operation")
    assert labels(op, "e2e-telegram")["pending_group_operation"] == ("STRING", "")


def test_session_store_cli_writes_vault_then_deletes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session_file = tmp_path / "admin.session"
    write_private_file(session_file, MOCK_ADMIN_SESSION + "\n")
    op = FakeOp({"e2e-telegram": []})
    op.fail = "edit"
    monkeypatch.setattr(cli, "_vault", lambda: Vault(run=op))

    async def session_user(api, session, role):
        return ADMIN

    monkeypatch.setattr("akkiabot_e2e.telethon_transport.session_user", session_user)
    args = ["session-store", "--role", "admin", "--session-file", str(session_file)]

    assert cli.main(args, env=live_env()) == cli.EXIT_USAGE
    assert session_file.exists()  # kept when the vault write failed

    op.fail = None
    assert cli.main(args, env=live_env()) == cli.EXIT_PASSED
    assert not session_file.exists()
    assert labels(op, "e2e-telegram")["allowed_user_ids"] == ("STRING", str(ADMIN.id))
    out = capsys.readouterr()
    assert MOCK_ADMIN_SESSION not in out.out + out.err
