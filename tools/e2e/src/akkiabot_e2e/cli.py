"""``akkiabot-e2e`` command line.

Subcommands: ``token-init``, ``op-exec``, ``check-env``, ``login``, ``session-store``,
``bootstrap-bot``, ``run``, ``list``. Everything Telegram-facing talks to the
Telegram **test environment** only. ``run`` sends test messages only when every
required variable is present (otherwise it reports "skipped" with exit code
77 — never success).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path

from akkiabot_e2e.config import (
    ApiCredentials,
    build_redactor,
    env_float,
    load_api_credentials,
    load_config,
    missing_variables,
)
from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.files import (
    ensure_private_dir,
    read_private_file,
    state_home,
    write_private_file,
)
from akkiabot_e2e.group import VaultGroupRegistry
from akkiabot_e2e.login import default_session_file, interactive_login
from akkiabot_e2e.onepassword import default_token_file, op_exec, token_init
from akkiabot_e2e.pacing import PacedAccount, Pacer
from akkiabot_e2e.redaction import Redactor, Secret
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import Connect, run_acceptance
from akkiabot_e2e.scenarios import SCENARIOS, select_scenarios
from akkiabot_e2e.transport import UserInfo
from akkiabot_e2e.vault import E2E_ITEM, RUNTIME_ITEM, FieldValue, Vault, shred_file

EXIT_PASSED, EXIT_FAILED, EXIT_USAGE, EXIT_SKIPPED = 0, 1, 2, 77
DEFAULT_ENV_FILE = Path("tools/e2e/staging.env.tpl")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akkiabot-e2e", description="AkkiaBot 真实 Telegram 验收")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("token-init", help="把本机 1Password 服务账号令牌存入仅本人可读的文件")
    init.add_argument("--token-file", type=Path, default=None)
    init.add_argument("--force", action="store_true", help="覆盖已有令牌文件（轮换令牌时用）")

    op = sub.add_parser("op-exec", help="用服务账号令牌执行 op run --env-file=<引用文件> -- <命令>")
    op.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    op.add_argument("--token-file", type=Path, default=None)
    op.add_argument("argv", nargs=argparse.REMAINDER, help="-- 之后的命令")

    sub.add_parser("check-env", help="连接 Telegram 测试环境并确认 test_mode（不需要账号）")

    login = sub.add_parser("login", help="在测试环境登录测试账号，会话直接写入 1Password")
    login.add_argument("--role", choices=("admin", "member"), required=True)
    login.add_argument(
        "--out", type=Path, default=None, help="不写 1Password，改写入仅本人可读的文件"
    )
    login.add_argument("--force", action="store_true", help="与 --out 一起使用时覆盖已有文件")

    store = sub.add_parser("session-store", help="把 login --out 生成的会话文件写入 1Password")
    store.add_argument("--role", choices=("admin", "member"), required=True)
    store.add_argument("--session-file", type=Path, default=None)
    store.add_argument("--keep-file", action="store_true", help="写入并回读成功后保留本地会话文件")

    boot = sub.add_parser(
        "bootstrap-bot", help="用群主测试账号通过测试环境 BotFather 创建测试 bot 并写回 1Password"
    )
    boot.add_argument("--force", action="store_true", help="已有可用的测试 bot 时仍新建一个")

    run = sub.add_parser("run", help="执行真实验收（会用测试账号在测试群发消息）")
    run.add_argument(
        "--scenarios", action="append", help="里程碑（m2,m3）或场景名，可重复；默认全部"
    )
    run.add_argument("--create-group", action="store_true", help="没有可用测试群时创建一个")
    run.add_argument(
        "--serve", action="store_true", help="在本进程旁启动 akkiabot serve 作为被测 bot"
    )
    run.add_argument("--report-dir", type=Path, default=None)

    sub.add_parser("list", help="列出验收场景")
    return parser


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Before Telethon is imported: it logs at import time.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # Imported lazily: Telethon is only needed for live runs.
    from akkiabot_e2e.bot_api import quiet_http_logs
    from akkiabot_e2e.telethon_transport import quiet_third_party_logs

    quiet_third_party_logs()
    quiet_http_logs()


def _cmd_list() -> int:
    for s in SCENARIOS:
        print(f"{s.name:<22} {s.milestone}  {s.description}")
    print(f"{'m2.message-log':<22} M2  （仅 --serve）群消息被记录到 message_log")
    return EXIT_PASSED


def _vault() -> Vault:
    return Vault()


def _store_session(role: str, user_id: int, session: Secret) -> None:
    vault = _vault()
    vault.set_fields(E2E_ITEM, {f"{role}_session": FieldValue(session.reveal())})
    allowed = vault.add_ids(E2E_ITEM, "allowed_user_ids", [user_id])
    print(f"会话已写入 1Password {vault.name}/{E2E_ITEM}/{role}_session 并回读校验（值未显示）。")
    print(f"allowed_user_ids 现为 {len(allowed)} 个账号。")


def _cmd_check_env(env: Mapping[str, str]) -> int:
    api = load_api_credentials(env)
    _setup_logging()
    from akkiabot_e2e.telethon_transport import check_test_environment

    dc = asyncio.run(check_test_environment(api))
    print(f"已连接 Telegram 测试环境（DC {dc}，服务器确认 test_mode=true）。")
    return EXIT_PASSED


def _cmd_login(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    api = load_api_credentials(env)
    _setup_logging()
    from akkiabot_e2e.telethon_transport import TelethonLogin

    if args.out is not None and args.out.exists() and not args.force:
        raise E2EError(f"{args.out} 已存在；确认要覆盖时加 --force")
    user, session = asyncio.run(
        interactive_login(TelethonLogin(api), prompt=input, secret_prompt=getpass.getpass)
    )
    print(f"已登录测试环境 {args.role} 账号：{user.first_name}（user id {user.id}）")
    if args.out is not None:
        write_private_file(args.out, session.reveal() + "\n", overwrite=args.force)
        print(
            f"会话已写入 {args.out}（权限 600，内容不会显示），之后执行 session-store 写入 1Password。"
        )
        return EXIT_PASSED
    try:
        _store_session(args.role, user.id, session)
    except E2EError:
        # Do not lose a fresh login: keep it locally (0600) for session-store.
        fallback = default_session_file(args.role)
        write_private_file(fallback, session.reveal() + "\n", overwrite=True)
        print(
            f"写入 1Password 失败，会话已暂存到 {fallback}（权限 600），修复后执行 session-store。"
        )
        raise
    return EXIT_PASSED


def _cmd_session_store(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    path: Path = args.session_file or default_session_file(args.role)
    session = Secret(read_private_file(path))
    api = load_api_credentials(env)
    from akkiabot_e2e.telethon_transport import session_user

    async def locked_user() -> UserInfo:
        # Connecting with an existing session: same lock as `run` / `bootstrap-bot`.
        async with session_lock():
            return await session_user(api, session)

    user = asyncio.run(locked_user())
    _store_session(args.role, user.id, session)
    if args.keep_file:
        print(f"按要求保留了本地会话文件 {path}，请尽快删除。")
    else:
        shred_file(path)
        print(f"已覆盖并删除本地会话文件 {path}。")
    return EXIT_PASSED


async def _bootstrap_bot(env: Mapping[str, str], force: bool) -> str:
    import httpx

    from akkiabot_e2e.bot_api import check_test_bot
    from akkiabot_e2e.botfather import create_test_bot

    api = load_api_credentials(env)
    token, username = env.get("TELEGRAM_BOT_TOKEN", "").strip(), env.get("E2E_BOT_USERNAME", "")
    username = username.strip().removeprefix("@")
    async with httpx.AsyncClient(timeout=15) as http:
        if token and username and not force:
            try:
                await check_test_bot(Secret(token), username, http)
            except E2EError:
                pass
            else:
                return f"已有可用的测试 bot @{username}，未新建（需要新建时加 --force）"
        if not env.get("E2E_ADMIN_SESSION", "").strip():
            raise E2EError("缺少 E2E_ADMIN_SESSION：请先执行 just e2e-login admin")
        pacer = Pacer(
            max_wait=env_float(env, "E2E_FLOOD_MAX_WAIT", 120.0),
            send_interval=env_float(env, "E2E_SEND_INTERVAL", 2.0),
        )
        connect = _telethon_connect(api)
        # The whole connect → BotFather dialogue → close happens under the session lock.
        async with session_lock():
            raw = await connect("admin", Secret(env["E2E_ADMIN_SESSION"].strip()))
            admin = PacedAccount(raw, pacer, "admin")
            try:
                username, token = await create_test_bot(admin)
            finally:
                await admin.close()
        vault = _vault()
        vault.set_fields(RUNTIME_ITEM, {"bot_token": FieldValue(token)})
        vault.set_fields(E2E_ITEM, {"bot_username": FieldValue(username, concealed=False)})
        await check_test_bot(Secret(token), username, http)
    return (
        f"已在测试环境创建 @{username}，token 已写入 1Password（值未显示）并通过测试环境 getMe 校验"
    )


def _telethon_connect(api: ApiCredentials) -> Connect:
    from akkiabot_e2e.telethon_transport import connect_account

    async def connect(role: str, session: Secret):
        return await connect_account(api, role, session)

    return connect


def _skipped_report(missing: list[str]) -> Report:
    return Report(
        run_id="-",
        status="skipped",
        started_at=datetime.now(UTC).isoformat(),
        detail=f"缺少 {', '.join(missing)}，未发送任何消息。真实验收状态：未执行（不是通过）",
    )


#: Poll interval while waiting for the session lock (tests shorten it).
LOCK_POLL = 2.0


def session_lock() -> AbstractAsyncContextManager[None]:
    """One machine-wide lock for every entry point that connects with an existing session."""
    from akkiabot_e2e.lock import run_lock

    return run_lock(state_home() / "run.lock", poll=LOCK_POLL)


async def _live(args: argparse.Namespace, env: Mapping[str, str], run_dir: Path) -> Report:
    config = load_config(env)
    selected = select_scenarios(args.scenarios)
    run_id = run_dir.name
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if args.serve or token:
        import httpx

        from akkiabot_e2e.bot_api import check_test_bot

        if not token:
            raise E2EError("--serve 需要 TELEGRAM_BOT_TOKEN（先执行 just e2e-bootstrap-bot）")
        async with httpx.AsyncClient(timeout=15) as http:
            await check_test_bot(Secret(token), config.bot_username, http)
    # The group allowlist and the pending create-group operation live in 1Password.
    registry = VaultGroupRegistry(_vault())
    async with session_lock():
        if not args.serve:
            return await run_acceptance(
                config,
                selected,
                connect=_telethon_connect(config.api),
                run_id=run_id,
                allow_create=args.create_group,
                registry=registry,
            )
        from akkiabot_e2e.server import ManagedServer

        async with ManagedServer(run_dir, env) as server:
            report = await run_acceptance(
                config,
                selected,
                connect=_telethon_connect(config.api),
                run_id=run_id,
                allow_create=args.create_group,
                registry=registry,
                message_log=server.logged_message_ids,
            )
            report.server_log = str(server.log_path)
            return report


def _cmd_run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    redact = build_redactor(env)
    missing = missing_variables(env)
    if missing:
        print(_skipped_report(missing).to_markdown(redact))
        return EXIT_SKIPPED
    select_scenarios(args.scenarios)  # validate before touching Telegram
    _setup_logging()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ensure_private_dir(
        (args.report_dir or state_home() / "runs") / f"{stamp}-{uuid.uuid4().hex[:6]}"
    )
    report = asyncio.run(_live(args, env, run_dir))
    json_path, md_path = report.save(run_dir, redact)
    print(report.to_markdown(redact))
    print(f"报告：{md_path}\nJSON：{json_path}")
    return EXIT_PASSED if report.status == "passed" else EXIT_FAILED


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    redact: Redactor = build_redactor(env)
    try:
        match args.command:
            case "list":
                return _cmd_list()
            case "token-init":
                token_init(
                    args.token_file or default_token_file(), getpass.getpass, overwrite=args.force
                )
                print("已保存服务账号令牌（权限 600）。")
                return EXIT_PASSED
            case "op-exec":
                argv_rest = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
                op_exec(args.env_file, argv_rest, args.token_file or default_token_file())
                return EXIT_PASSED  # pragma: no cover - exec replaces the process
            case "login":
                return _cmd_login(args, env)
            case "session-store":
                return _cmd_session_store(args, env)
            case "check-env":
                return _cmd_check_env(env)
            case "bootstrap-bot":
                _setup_logging()
                print(asyncio.run(_bootstrap_bot(env, args.force)))
                return EXIT_PASSED
            case "run":
                return _cmd_run(args, env)
    except (E2EError, ValueError) as exc:
        print(f"错误：{redact(str(exc))}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_USAGE
