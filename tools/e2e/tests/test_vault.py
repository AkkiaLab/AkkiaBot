"""Vault writes go through stdin only, are read back, and never leak values in errors."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from akkiabot_e2e import cli
from akkiabot_e2e.errors import ConfigError
from akkiabot_e2e.files import write_private_file
from akkiabot_e2e.testing import ADMIN, ADMIN_SESSION, live_env
from akkiabot_e2e.vault import FieldValue, Vault


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

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
        argv = list(argv)
        self.calls.append((argv, stdin))
        verb = "read" if argv[1] == "read" else argv[2]
        if verb == self.fail:
            return 1, b"", f"[ERROR] boom {ADMIN_SESSION}".encode()
        if verb == "read":
            _, _, _, title, label = argv[2].split("/")
            item = self.items.get(title)
            field = next((f for f in (item or {}).get("fields", []) if f["label"] == label), None)
            if field is None:
                return 1, b"", b"[ERROR] no such field"
            value = "corrupted" if self.corrupt_reads else field.get("value", "")
            return 0, value.encode() + b"\n", b""
        if verb == "get":
            item = self.items.get(argv[3])
            if item is None:
                return 1, b"", b'[ERROR] "x" isn\'t an item in the "AkkiaBot-Staging" vault.'
            return 0, json.dumps(item).encode(), b""
        data = json.loads(stdin or b"{}")
        if verb == "create":
            data["id"] = f"id-{data['title']}"
        self.items[data["title"]] = data
        return 0, b"{}", b""

    def argv_text(self) -> str:
        return " ".join(" ".join(argv) for argv, _ in self.calls)


def labels(op: FakeOp, title: str) -> dict[str, tuple[str, str | None]]:
    return {f["label"]: (f.get("type", ""), f.get("value")) for f in op.items[title]["fields"]}


def test_set_fields_edits_existing_item_via_stdin_only() -> None:
    op = FakeOp({"telegram-e2e": [{"id": "a", "label": "api_id", "type": "STRING", "value": "1"}]})

    Vault(run=op).set_fields("telegram-e2e", {"admin_session": FieldValue(ADMIN_SESSION)})

    assert ADMIN_SESSION not in op.argv_text()
    edit = [stdin for argv, stdin in op.calls if argv[:3] == ["op", "item", "edit"]]
    assert len(edit) == 1
    assert ADMIN_SESSION in (edit[0] or b"").decode()
    assert labels(op, "telegram-e2e") == {
        "api_id": ("STRING", "1"),
        "admin_session": ("CONCEALED", ADMIN_SESSION),
    }


def test_set_fields_creates_missing_item() -> None:
    op = FakeOp()
    Vault(run=op).set_fields("runtime", {"bot_token": FieldValue("1:fake-token-for-tests")})
    assert ["op", "item", "create", "--vault=AkkiaBot-Staging", "-"] in [a for a, _ in op.calls]
    assert labels(op, "runtime") == {"bot_token": ("CONCEALED", "1:fake-token-for-tests")}
    assert "fake-token" not in op.argv_text()


def test_write_errors_are_redacted_and_readback_is_checked() -> None:
    op = FakeOp({"telegram-e2e": []})
    op.fail = "edit"
    with pytest.raises(ConfigError, match="写入") as info:
        Vault(run=op).set_fields("telegram-e2e", {"admin_session": FieldValue(ADMIN_SESSION)})
    assert ADMIN_SESSION not in str(info.value)

    op.fail, op.corrupt_reads = None, True
    with pytest.raises(ConfigError, match="不一致"):
        Vault(run=op).set_fields("telegram-e2e", {"admin_session": FieldValue(ADMIN_SESSION)})


def test_add_ids_merges_and_drops_placeholder() -> None:
    op = FakeOp({"telegram-e2e": [{"id": "x", "label": "allowed_chat_ids", "value": "0"}]})
    vault = Vault(run=op)
    assert vault.add_ids("telegram-e2e", "allowed_chat_ids", [-1002]) == [-1002]
    assert vault.add_ids("telegram-e2e", "allowed_chat_ids", [-1001, -1002]) == [-1002, -1001]
    assert labels(op, "telegram-e2e")["allowed_chat_ids"] == ("STRING", "-1002,-1001")


def test_session_store_cli_writes_vault_then_deletes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session_file = tmp_path / "admin.session"
    write_private_file(session_file, ADMIN_SESSION + "\n")
    op = FakeOp({"telegram-e2e": []})
    op.fail = "edit"
    monkeypatch.setattr(cli, "_vault", lambda: Vault(run=op))

    async def session_user(api, session):
        return ADMIN

    monkeypatch.setattr("akkiabot_e2e.telethon_transport.session_user", session_user)
    args = ["session-store", "--role", "admin", "--session-file", str(session_file)]

    assert cli.main(args, env=live_env()) == cli.EXIT_USAGE
    assert session_file.exists()  # kept when the vault write failed

    op.fail = None
    assert cli.main(args, env=live_env()) == cli.EXIT_PASSED
    assert not session_file.exists()
    assert labels(op, "telegram-e2e")["allowed_user_ids"] == ("STRING", str(ADMIN.id))
    out = capsys.readouterr()
    assert ADMIN_SESSION not in out.out + out.err
