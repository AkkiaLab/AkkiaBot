"""1Password service-account bootstrap and the ``op run`` wrapper.

* ``token-init`` stores the machine's service account token in an
  owner-only file (prompted without echo, never on the command line).
* ``op-exec`` passes that token to ``op run`` through the environment (never
  argv) so ``op`` resolves ``op://`` references from the env file and injects
  the values into the child process only.
"""

from __future__ import annotations

import os
import shutil
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
