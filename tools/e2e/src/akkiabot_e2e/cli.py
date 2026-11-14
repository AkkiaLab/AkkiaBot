"""``akkiabot-e2e`` command line.

Subcommands: ``token-init``, ``op-exec``, ``login``, ``session-store``, ``run``, ``list``.
``run`` is the only entry point that sends real Telegram messages, and it
does so only when every required variable is present (otherwise it reports
"skipped" with exit code 77 — never success).
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
from datetime import UTC, datetime
from pathlib import Path

from akkiabot_e2e.config import (
    ApiCredentials,
    build_redactor,
    load_api_credentials,
    load_config,
    missing_variables,
)
from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.files import ensure_private_dir, read_private_file, state_home
from akkiabot_e2e.login import default_session_file, interactive_login
from akkiabot_e2e.onepassword import (
    default_token_file,
    op_exec,
    shred_file,
    store_session,
    token_init,
)
from akkiabot_e2e.redaction import Redactor, Secret
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import Connect, run_acceptance
from akkiabot_e2e.scenarios import SCENARIOS, select_scenarios

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

    login = sub.add_parser("login", help="首次登录测试账号，会话写入仅本人可读的文件")
    login.add_argument("--role", choices=("admin", "member"), required=True)
    login.add_argument("--out", type=Path, default=None)
    login.add_argument("--force", action="store_true")

    store = sub.add_parser(
        "session-store", help="把 login 生成的会话写入 1Password（经 stdin，不进命令行参数或终端）"
    )
    store.add_argument("--role", choices=("admin", "member"), required=True)
    store.add_argument("--session-file", type=Path, default=None)
    store.add_argument("--vault", default="AkkiaBot-Staging")
    store.add_argument("--item", default="telegram-e2e")
    store.add_argument("--keep-file", action="store_true", help="写入并回读成功后保留本地会话文件")

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
    # Imported lazily: Telethon is only needed for live runs.
    from akkiabot_e2e.telethon_transport import quiet_third_party_logs

    quiet_third_party_logs()


def _cmd_list() -> int:
    for s in SCENARIOS:
        print(f"{s.name:<22} {s.milestone}  {s.description}")
    print(f"{'m2.message-log':<22} M2  （仅 --serve）群消息被记录到 message_log")
    return EXIT_PASSED


def _cmd_login(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    api = load_api_credentials(env)
    _setup_logging()
    from akkiabot_e2e.telethon_transport import TelethonLogin

    out: Path = args.out or default_session_file(args.role)
    user = asyncio.run(
        interactive_login(
            TelethonLogin(api),
            out,
            prompt=input,
            secret_prompt=getpass.getpass,
            overwrite=args.force,
        )
    )
    print(f"已登录 {args.role} 测试账号：{user.first_name}（user id {user.id}）")
    print(
        f"会话已写入 {out}（权限 600，内容不会显示）。下一步按 docs/e2e-acceptance.md 存入 1Password。"
    )
    return EXIT_PASSED


def _cmd_session_store(args: argparse.Namespace) -> int:
    path: Path = args.session_file or default_session_file(args.role)
    field = f"{args.role}_session"
    store_session(read_private_file(path), vault=args.vault, item=args.item, field=field)
    print(f"已写入并回读校验 {args.vault}/{args.item}/{field}（值未显示）。")
    if args.keep_file:
        print(f"按要求保留了本地会话文件 {path}，请尽快删除。")
    else:
        shred_file(path)
        print(f"已覆盖并删除本地会话文件 {path}。")
    return EXIT_PASSED


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


async def _live(args: argparse.Namespace, env: Mapping[str, str], run_dir: Path) -> Report:
    from akkiabot_e2e.lock import run_lock

    config = load_config(env)
    selected = select_scenarios(args.scenarios)
    run_id = run_dir.name
    async with run_lock(state_home() / "run.lock"):
        if not args.serve:
            return await run_acceptance(
                config,
                selected,
                connect=_telethon_connect(config.api),
                run_id=run_id,
                allow_create=args.create_group,
            )
        from akkiabot_e2e.server import ManagedServer

        async with ManagedServer(run_dir, env) as server:
            report = await run_acceptance(
                config,
                selected,
                connect=_telethon_connect(config.api),
                run_id=run_id,
                allow_create=args.create_group,
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
                return _cmd_session_store(args)
            case "run":
                return _cmd_run(args, env)
    except (E2EError, ValueError) as exc:
        print(f"错误：{redact(str(exc))}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_USAGE
