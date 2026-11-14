"""1Password service-account bootstrap and the ``op run`` wrapper.

* ``token-init`` stores the machine's read-only service account token in an
  owner-only file (prompted without echo, never on the command line).
* ``session-store`` saves a freshly logged-in Telegram session into the vault
  with the owner's own sign-in, piping it on stdin (never argv / terminal).
* ``op-exec`` passes that token to ``op run`` through the environment (never
  argv) so ``op`` resolves ``op://`` references from the env file and injects
  the values into the child process only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from akkiabot_e2e.errors import ConfigError
from akkiabot_e2e.files import config_home, read_private_file, write_private_file

TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"  # noqa: S105 - variable name, not a value
TOKEN_PREFIX = "ops_"  # noqa: S105 - format prefix, not a value


def default_token_file() -> Path:
    return Path(
        os.environ.get("AKKIABOT_OP_TOKEN_FILE") or config_home() / "op-service-account-token"
    )


def token_init(path: Path, prompt: Callable[[str], str], *, overwrite: bool = False) -> None:
    token = prompt("粘贴本机服务账号令牌（输入不回显，回车结束）：").strip()
    if not token.startswith(TOKEN_PREFIX):
        raise ConfigError("这不像 1Password 服务账号令牌（应以 ops_ 开头），未保存")
    write_private_file(path, token + "\n", overwrite=overwrite)


def op_command(env_file: Path, argv: Sequence[str]) -> list[str]:
    if not argv:
        raise ConfigError("op-exec 之后需要要执行的命令，例如：op-exec -- akkiabot-e2e run")
    return ["op", "run", f"--env-file={env_file}", "--", *argv]


def op_environment(base: Mapping[str, str], token_file: Path) -> tuple[dict[str, str], str]:
    """Environment for ``op run`` plus a note on which credential is used."""
    env = dict(base)
    if env.get(TOKEN_ENV):
        return env, f"使用环境变量 {TOKEN_ENV} 中的服务账号令牌"
    if token_file.exists():
        env[TOKEN_ENV] = read_private_file(token_file)
        return env, f"使用 {token_file} 中的服务账号令牌"
    return env, "未找到服务账号令牌，改用 op 当前的登录状态（例如桌面应用集成）"


def op_exec(env_file: Path, argv: Sequence[str], token_file: Path) -> None:
    """Replace this process with ``op run`` (does not return on success)."""
    if not env_file.is_file():
        raise ConfigError(f"找不到引用文件 {env_file}")
    if shutil.which("op") is None:
        raise ConfigError("没有找到 1Password CLI（op），请先按 docs/e2e-acceptance.md 安装")
    command = op_command(env_file, argv)
    env, note = op_environment(os.environ, token_file)
    print(note, flush=True)
    os.execvpe(command[0], command, env)  # noqa: S606 - fixed program, argv from the caller


# ------------------------------------------------------------ session import

#: Runs ``argv`` with ``stdin`` bytes; returns (exit code, stdout, stderr).
OpRunner = Callable[[Sequence[str], bytes | None], tuple[int, bytes, bytes]]


def run_op(argv: Sequence[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
    if shutil.which("op") is None:
        raise ConfigError("没有找到 1Password CLI（op），请先按 docs/e2e-acceptance.md 安装")
    env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
    if stdin is None:
        result = subprocess.run(  # noqa: S603 - fixed program, no secret in argv
            list(argv), stdin=subprocess.DEVNULL, capture_output=True, env=env, check=False
        )
    else:
        result = subprocess.run(  # noqa: S603 - fixed program; the secret travels via stdin
            list(argv), input=stdin, capture_output=True, env=env, check=False
        )
    return result.returncode, result.stdout, result.stderr


def _op_error(action: str, stderr: bytes, redact: Callable[[str], str]) -> ConfigError:
    detail = redact(stderr.decode("utf-8", "replace").strip())
    return ConfigError(f"{action}失败：{detail or '无输出'}")


def store_session(
    session: str,
    *,
    vault: str,
    item: str,
    field: str,
    run: OpRunner = run_op,
) -> None:
    """Write ``session`` into ``vault/item/field`` without argv or terminal output.

    Uses the caller's own (writable) 1Password sign-in: the service account
    token is removed from the child environment because it is read-only.
    The item is fetched as JSON, the field value replaced in memory, and the
    JSON piped to ``op item edit`` on stdin (supported by op 2.x; assignment
    arguments would expose the value in the process list). The value is then
    read back with ``op read`` and compared in memory.
    """

    def redact(text: str) -> str:
        return text.replace(session, "[REDACTED]") if session else text

    code, out, err = run(
        ["op", "item", "get", item, f"--vault={vault}", "--format=json", "--reveal"], None
    )
    if code != 0:
        raise _op_error(f"读取 1Password 条目 {vault}/{item} ", err, redact)
    data = json.loads(out)
    fields = [f for f in data.get("fields", []) if f.get("label") == field]
    if len(fields) != 1:
        raise ConfigError(
            f"条目 {vault}/{item} 中应有且只有一个名为 {field} 的字段（找到 {len(fields)} 个），"
            "请先在 1Password 中建立该密码字段"
        )
    fields[0]["value"] = session
    fields[0]["type"] = "CONCEALED"
    payload = json.dumps(data).encode()

    code, _, err = run(["op", "item", "edit", item, f"--vault={vault}"], payload)
    if code != 0:
        raise _op_error("写入 1Password ", err, redact)

    code, out, err = run(["op", "read", f"op://{vault}/{item}/{field}"], None)
    if code != 0:
        raise _op_error("回读 1Password 字段", err, redact)
    if out.decode().strip() != session:
        raise ConfigError(f"回读 {vault}/{item}/{field} 的值与本地会话不一致，未删除本地文件")


def shred_file(path: Path) -> None:
    """Overwrite then delete a small secret file (best effort on journaling/SSD storage)."""
    size = path.stat().st_size
    with path.open("r+b") as handle:
        handle.write(b"\0" * size)
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()
