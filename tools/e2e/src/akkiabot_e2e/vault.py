"""Read / write fields of the test vault through ``op`` without exposing values.

Values only travel through stdin / stdout pipes of ``op`` (item JSON piped to
``op item create -`` / ``op item edit``, supported by op 2.x), never through
argv or the terminal; every write is read back with ``op read`` and compared
in memory. Error output from ``op`` is redacted before it is shown.

The service account used for the test vault may write (owner decision,
2026-11-14), so automation can store sessions, the test bot token and the test
group id itself. Production vaults are out of scope.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from akkiabot_e2e.errors import ConfigError

DEFAULT_VAULT = "AkkiaBot"
BOT_ITEM = "e2e-bot"
E2E_ITEM = "e2e-telegram"

#: Runs ``argv`` with ``stdin`` bytes; returns (exit code, stdout, stderr).
OpRunner = Callable[[Sequence[str], bytes | None], tuple[int, bytes, bytes]]


def run_op(argv: Sequence[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
    if shutil.which("op") is None:
        raise ConfigError("没有找到 1Password CLI（op），请先按 docs/e2e-acceptance.md 安装")
    if stdin is None:
        result = subprocess.run(  # noqa: S603 - fixed program, no secret in argv
            list(argv), stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    else:
        result = subprocess.run(  # noqa: S603 - fixed program; secrets travel via stdin
            list(argv), input=stdin, capture_output=True, check=False
        )
    return result.returncode, result.stdout, result.stderr


@dataclass(frozen=True, slots=True)
class FieldValue:
    value: str
    concealed: bool = True


class Vault:
    def __init__(
        self,
        name: str = DEFAULT_VAULT,
        *,
        run: OpRunner = run_op,
        readback_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name = name
        self._run = run
        self._readback_attempts = readback_attempts
        self._sleep = sleep

    def _error(self, action: str, stderr: bytes, secrets: Iterable[str]) -> ConfigError:
        detail = stderr.decode("utf-8", "replace").strip()
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            detail = detail.replace(secret, "[REDACTED]")
        return ConfigError(f"{action}失败：{detail or '无输出'}")

    def read(self, item: str, field: str) -> str | None:
        """Field value, or ``None`` when the item / field does not exist."""
        code, out, _ = self._run(["op", "read", f"op://{self.name}/{item}/{field}"], None)
        return out.decode("utf-8").rstrip("\n") if code == 0 else None

    def read_strict(self, item: str, field: str) -> str | None:
        """Like :meth:`read`, but a failed lookup raises instead of looking like a missing field.

        ``None`` only when ``op`` itself reports that the item does not exist or the item
        has no such field; any other failure (network, service, auth) raises
        :class:`ConfigError`. Use this before a read-modify-write.
        """
        data = self._get_item(item)
        if data is None:
            return None
        matches = [f for f in data.get("fields", []) if f.get("label") == field]
        if len(matches) > 1:
            raise ConfigError(f"条目 {self.name}/{item} 中有多个名为 {field} 的字段，请先合并")
        return str(matches[0].get("value") or "") if matches else None

    def _get_item(self, item: str) -> dict[str, Any] | None:
        argv = ["op", "item", "get", item, f"--vault={self.name}", "--format=json", "--reveal"]
        code, out, err = self._run(argv, None)
        if code == 0:
            return json.loads(out)
        if b"isn't an item" in err or b"not found" in err.lower():
            return None
        raise self._error(f"读取 1Password 条目 {self.name}/{item} ", err, ())

    def set_fields(self, item: str, values: Mapping[str, FieldValue]) -> None:
        """Create the item / fields as needed and set ``values``; verifies by reading back."""
        secrets = [v.value for v in values.values()]
        data = self._get_item(item)
        fields: list[dict[str, Any]]
        if data is None:
            data = {"title": item, "category": "SECURE_NOTE", "fields": []}
        fields = data.setdefault("fields", [])
        for label, field_value in values.items():
            matches = [f for f in fields if f.get("label") == label]
            if len(matches) > 1:
                raise ConfigError(f"条目 {self.name}/{item} 中有多个名为 {label} 的字段，请先合并")
            if not matches:
                matches = [{"id": label, "label": label}]
                fields.append(matches[0])
            matches[0]["type"] = "CONCEALED" if field_value.concealed else "STRING"
            matches[0]["value"] = field_value.value
        payload = json.dumps(data).encode()
        if "id" in data:
            argv = ["op", "item", "edit", item, f"--vault={self.name}"]
        else:
            argv = ["op", "item", "create", f"--vault={self.name}", "-"]
        code, _, err = self._run(argv, payload)
        if code != 0:
            raise self._error(f"写入 1Password 条目 {self.name}/{item} ", err, secrets)
        for label, field_value in values.items():
            self._verify(item, label, field_value.value)

    def clear_field(self, item: str, field: str) -> None:
        """Empty a text field (an empty field may read back as missing, which also counts)."""
        self.set_fields(item, {field: FieldValue("", concealed=False)})

    def _verify(self, item: str, label: str, expected: str) -> None:
        # A read right after an edit has returned the previous value at least once
        # (seen live on 2026-11-14; not known to hold for every case), so retry briefly.
        for attempt in range(self._readback_attempts):
            if self._read_back_matches(item, label, expected):
                return
            if attempt + 1 < self._readback_attempts:
                self._sleep(1.0 * (attempt + 1))
        raise ConfigError(f"回读 {self.name}/{item}/{label} 的值与写入不一致")

    def _read_back_matches(self, item: str, label: str, expected: str) -> bool:
        value = self.read(item, label)
        if expected or value is not None:
            return value == expected
        # An emptied field may read back as missing; but `op read` also returns
        # nothing on a failed call, so confirm with the item itself.
        try:
            return not self.read_strict(item, label)
        except ConfigError:
            return False

    def read_ids(self, item: str, field: str) -> list[int]:
        """A comma-separated id list field (``0`` placeholders dropped); ``[]`` when missing.

        Raises :class:`ConfigError` when the field cannot be read: callers write the
        list back, and an unreadable list must never be taken for an empty one.
        """
        current = self.read_strict(item, field) or ""
        return sorted({int(p) for p in current.replace(" ", "").split(",") if p not in {"", "0"}})

    def _write_ids(self, item: str, field: str, ids: Iterable[int]) -> list[int]:
        result = sorted(set(ids))
        self.set_fields(item, {field: FieldValue(",".join(map(str, result)), concealed=False)})
        return result

    def add_ids(self, item: str, field: str, ids: Iterable[int]) -> list[int]:
        """Merge ``ids`` into a comma-separated id list field (``0`` placeholders dropped)."""
        return self._write_ids(item, field, {*self.read_ids(item, field), *ids})

    def remove_ids(self, item: str, field: str, ids: Iterable[int]) -> list[int]:
        """Remove ``ids`` from a comma-separated id list field; verified like every write."""
        return self._write_ids(item, field, set(self.read_ids(item, field)) - set(ids))


def shred_file(path: Path) -> None:
    """Overwrite then delete a small secret file (best effort on journaling/SSD storage)."""
    size = path.stat().st_size
    with path.open("r+b") as handle:
        handle.write(b"\0" * size)
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()
