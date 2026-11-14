import pytest
from telegram import BotCommand, Update
from telegram.ext import CommandHandler

from akkiabot_core.config import Settings
from akkiabot_server.bot.commands import COMMANDS
from akkiabot_server.bot.registry import Command, CommandRegistry
from akkiabot_server.bot.types import Context
from akkiabot_server.runtime import Runtime
from akkiabot_server.testing import FakeBotApi


async def _noop(_update: Update, _context: Context) -> None:
    return None


def _registry() -> CommandRegistry:
    return CommandRegistry(
        [
            Command("start", "开始使用", _noop, public=False),
            Command("help", "获取帮助信息", _noop, usage="/help [命令名]", docs="详细说明"),
            Command("id", "获取ID信息", _noop),
        ]
    )


def test_bot_commands_only_public() -> None:
    assert _registry().bot_commands() == [
        BotCommand("help", "获取帮助信息"),
        BotCommand("id", "获取ID信息"),
    ]


def test_help_text_lists_public_commands_in_order() -> None:
    assert _registry().help_text() == (
        "可用命令列表：\n1. 获取帮助信息\n用法：/help [命令名]\n2. 获取ID信息\n用法：/id"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("help", "详细说明"),
        ("/id", "用法：/id"),
        ("ID", "用法：/id"),
        ("start", "命令 start 不存在"),  # hidden commands are not documented
        ("nope", "命令 nope 不存在"),
    ],
)
def test_command_help(name: str, expected: str) -> None:
    assert _registry().command_help(name) == expected


def test_handlers_match_commands() -> None:
    handlers = _registry().handlers()
    assert all(isinstance(h, CommandHandler) for h in handlers)
    assert [next(iter(h.commands)) for h in handlers if isinstance(h, CommandHandler)] == [
        "start",
        "help",
        "id",
    ]


def test_duplicate_command_rejected() -> None:
    registry = _registry()
    with pytest.raises(ValueError, match="重复注册"):
        registry.add(Command("id", "另一个 id", _noop))


@pytest.mark.parametrize("name", ["", "Help", "with-dash", "x" * 33])
def test_invalid_name_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="非法命令名"):
        Command(name, "描述文本", _noop)


@pytest.mark.parametrize("description", ["ab", "x" * 257])
def test_invalid_description_rejected(description: str) -> None:
    with pytest.raises(ValueError, match="描述长度"):
        Command("ok", description, _noop)


def test_builtin_commands() -> None:
    registry = CommandRegistry(COMMANDS)
    assert [c.name for c in registry] == [
        "start",
        "help",
        "id",
        "ip",
        "config",
        "ban",
        "warn",
        "spam",
    ]
    assert [c.command for c in registry.bot_commands()] == [
        "help",
        "id",
        "ip",
        "config",
        "ban",
        "warn",
        "spam",
    ]


async def test_start_publishes_command_menu(settings: Settings, api: FakeBotApi) -> None:
    rt = Runtime.build(settings, request=api, get_updates_request=api)
    await rt.start()
    try:
        assert api.calls_to("setMyCommands") == [
            {
                "commands": [
                    {"command": "help", "description": "获取帮助信息"},
                    {"command": "id", "description": "获取ID信息"},
                    {"command": "ip", "description": "查询IP信息"},
                    {"command": "config", "description": "功能配置"},
                    {"command": "ban", "description": "封禁用户"},
                    {"command": "warn", "description": "警告用户"},
                    {"command": "spam", "description": "举报垃圾信息"},
                ]
            }
        ]
    finally:
        await rt.stop()


async def test_command_menu_failure_is_not_fatal(settings: Settings, api: FakeBotApi) -> None:
    api.fail("setMyCommands")
    rt = Runtime.build(settings, request=api, get_updates_request=api)
    await rt.start()
    try:
        assert rt.application.running
    finally:
        await rt.stop()
