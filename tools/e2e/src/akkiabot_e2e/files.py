"""Owner-only files and directories (sessions, tokens, reports, server logs)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from akkiabot_e2e.errors import SafetyError


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "akkiabot"


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "akkiabot-e2e"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make sure only the owner can access it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_private_file(path: Path, content: str, *, overwrite: bool = False) -> None:
    """Write ``content`` to a new ``0600`` file; never widens an existing file's mode."""
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise SafetyError(f"{path} 已存在；确认要覆盖时加 --force") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(content)


def read_private_file(path: Path) -> str:
    """Read a secret file, refusing files that other users could read or replace."""
    info = path.stat()
    if info.st_uid != os.getuid():
        raise SafetyError(f"{path} 不属于当前用户，拒绝读取")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SafetyError(f"{path} 的权限过宽（应为 600），执行 chmod 600 {path} 后重试")
    return path.read_text(encoding="utf-8").strip()
