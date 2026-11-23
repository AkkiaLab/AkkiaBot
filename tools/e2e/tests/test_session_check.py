"""Session check on connect: expired session vs. temporary Telegram failure.

Telethon's ``is_user_authorized()`` turns every ``RPCError`` (server errors
included) into ``False``; a test DC answering ``-500 No workers running`` used to
be reported as "session expired, log in again". None of these tests connect to
Telegram: errors are built the way Telethon builds them from an ``rpc_error``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from telethon import errors, functions
from telethon.sessions import StringSession
from telethon.tl.types import RpcError

from akkiabot_e2e import cli, telethon_transport
from akkiabot_e2e.config import ApiCredentials
from akkiabot_e2e.errors import RateLimitExceeded, SessionInvalid, TelegramUnavailable
from akkiabot_e2e.files import write_private_file
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import run_acceptance
from akkiabot_e2e.scenarios import SCENARIOS
from akkiabot_e2e.telethon_transport import TEST_DC_ADDRESSES, TelethonAccount
from akkiabot_e2e.testing import MOCK_ADMIN_SESSION, MOCK_API_HASH, MOCK_MEMBER_SESSION, live_env

API = ApiCredentials(api_id=1, api_hash=Secret(MOCK_API_HASH))
SESSION = Secret(MOCK_ADMIN_SESSION)


def rpc(code: int, message: str, request: Any = None) -> errors.RPCError:
    """The exception Telethon raises for an ``rpc_error`` answer."""
    return errors.rpc_message_to_error(RpcError(code, message), request)


class Config:
    test_mode = True


class FakeClient:
    """Just enough of ``TelegramClient`` for ``TelethonAccount.connect``."""

    def __init__(
        self,
        get_state: BaseException | None,
        dc_id: int = 1,
        get_config: BaseException | None = None,
    ) -> None:
        self.get_state = get_state
        self.get_config = get_config
        self.session = StringSession()
        self.session.set_dc(dc_id, TEST_DC_ADDRESSES[dc_id], 443)
        self.requests: list[str] = []
        self.disconnected = False

    async def connect(self) -> None:
        pass

    async def __call__(self, request: Any) -> Any:
        self.requests.append(type(request).__name__)
        if isinstance(request, functions.help.GetConfigRequest):
            if self.get_config:
                raise self.get_config
            return Config()
        if isinstance(request, functions.updates.GetStateRequest) and self.get_state:
            raise self.get_state
        return object()

    async def get_dialogs(self, limit: int) -> list[Any]:
        return []

    async def is_user_authorized(self) -> bool:  # pragma: no cover - must not be used
        raise AssertionError("is_user_authorized() swallows server errors")

    async def disconnect(self) -> None:
        self.disconnected = True


def use(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    monkeypatch.setattr(telethon_transport, "_client", lambda api, session: client)


async def test_valid_session_connects_with_an_authorized_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(get_state=None)
    use(monkeypatch, client)
    await TelethonAccount.connect(API, SESSION, "admin")
    assert "GetStateRequest" in client.requests
    assert not client.disconnected


@pytest.mark.parametrize(
    ("code", "message"),
    [(401, "AUTH_KEY_UNREGISTERED"), (401, "SESSION_REVOKED"), (406, "AUTH_KEY_DUPLICATED")],
)
async def test_authorization_error_is_an_expired_session(
    monkeypatch: pytest.MonkeyPatch, code: int, message: str
) -> None:
    client = FakeClient(get_state=rpc(code, message))
    use(monkeypatch, client)
    with pytest.raises(SessionInvalid) as info:
        await TelethonAccount.connect(API, SESSION, "admin")
    assert "just e2e-login admin" in str(info.value)
    assert message in str(info.value)
    assert client.disconnected


@pytest.mark.parametrize(("code", "message"), [(-500, "No workers running"), (-503, "Timeout")])
async def test_server_error_is_temporary_and_names_the_dc(
    monkeypatch: pytest.MonkeyPatch, code: int, message: str
) -> None:
    client = FakeClient(get_state=rpc(code, message), dc_id=1)
    use(monkeypatch, client)
    with pytest.raises(TelegramUnavailable) as info:
        await TelethonAccount.connect(API, SESSION, "admin")
    text = str(info.value)
    assert "DC1" in text
    assert message in text
    assert "不要重新登录" in text
    assert "e2e-login" not in text
    assert info.value.retry_after is None
    assert client.disconnected


async def test_flood_wait_reports_the_wait_and_is_temporary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(get_state=rpc(420, "FLOOD_WAIT_37"), dc_id=2)
    use(monkeypatch, client)
    with pytest.raises(TelegramUnavailable) as info:
        await TelethonAccount.connect(API, SESSION, "member")
    text = str(info.value)
    assert "37 秒" in text
    assert "DC2" in text
    assert "e2e-login" not in text
    assert info.value.retry_after == 37
    assert client.disconnected


async def test_server_error_on_get_config_is_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(get_state=None, get_config=rpc(-500, "No workers running"))
    use(monkeypatch, client)
    with pytest.raises(TelegramUnavailable):
        await TelethonAccount.connect(API, SESSION, "admin")
    assert client.disconnected
    with pytest.raises(TelegramUnavailable) as info:
        await telethon_transport.check_test_environment(API)
    assert "不要重新登录" in str(info.value)


class OfflineDC:
    """A real ``TelegramClient`` (including its own ``connect()``) whose network
    boundary, the MTProto sender, answers every request with ``-500 No workers running``.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sends: list[str] = []
        self.disconnects = 0
        real_client = telethon_transport._client

        def client(api: ApiCredentials, session: Secret | None) -> Any:
            client: Any = real_client(api, None)  # fresh session pinned to test DC2

            async def sender_connect(connection: Any) -> bool:
                return True

            def send(request: Any, ordered: bool = False) -> asyncio.Future[Any]:
                self.sends.append(type(request).__name__)
                future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
                future.set_exception(rpc(-500, "No workers running", request))
                return future

            real_disconnect = client.disconnect

            def disconnect() -> Any:
                self.disconnects += 1
                return real_disconnect()

            client._sender.connect = sender_connect
            client._sender.send = send
            client.disconnect = disconnect
            return client

        monkeypatch.setattr(telethon_transport, "_client", client)


async def test_real_connect_with_failing_initial_request_is_temporary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TelegramClient.connect()`` sends the initial ``help.getConfig`` itself."""
    dc = OfflineDC(monkeypatch)
    with pytest.raises(TelegramUnavailable) as info:
        await TelethonAccount.connect(API, SESSION, "admin")
    assert "DC2" in str(info.value)
    assert "-500 No workers running" in str(info.value)
    assert "e2e-login" not in str(info.value)
    assert dc.sends == ["InvokeWithLayerRequest"]
    assert dc.disconnects == 1

    with pytest.raises(TelegramUnavailable):
        await telethon_transport.check_test_environment(API)
    assert dc.disconnects == 2


def test_cli_exit_code_75_when_the_initial_request_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dc = OfflineDC(monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "VaultAllowlist", lambda vault: object())
    monkeypatch.setattr(cli, "_vault", lambda: object())
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    for command in (["prune-groups"], ["check-env"]):
        assert cli.main(command, env=live_env()) == cli.EXIT_TEMPFAIL
        err = capsys.readouterr().err
        assert "DC2" in err
        assert "e2e-login" not in err
    assert dc.disconnects == 2


def test_session_store_names_the_role_when_the_session_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, FakeClient(get_state=rpc(401, "AUTH_KEY_UNREGISTERED")))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    session_file = tmp_path / "member.session"
    write_private_file(session_file, MOCK_MEMBER_SESSION + "\n")
    args = ["session-store", "--role", "member", "--session-file", str(session_file)]
    assert cli.main(args, env=live_env()) == cli.EXIT_SESSION_INVALID
    err = capsys.readouterr().err
    assert "just e2e-login member" in err
    assert "e2e-login session" not in err
    assert session_file.exists()


async def test_real_telethon_surfaces_the_server_error_after_its_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requests after ``connect()`` go through Telethon's own request / retry loop.

    ``connect()`` is covered above; here it is skipped so the -500 hits
    ``updates.getState``. Without ``raise_last_call_error`` Telethon would give
    up with a bare ``ValueError``.
    """
    sends: list[str] = []

    real_client = telethon_transport._client

    def client_with_failing_dc(api: ApiCredentials, session: Secret | None) -> Any:
        client: Any = real_client(api, None)

        async def connect() -> None:
            pass

        def send(request: Any, ordered: bool = False) -> asyncio.Future[Any]:
            sends.append(type(request).__name__)
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            future.set_exception(rpc(-500, "No workers running", request))
            return future

        client.connect = connect
        client._sender.send = send
        return client

    async def no_sleep(seconds: float) -> None:
        pass

    async def trusted(client: Any) -> None:
        pass

    monkeypatch.setattr(telethon_transport, "_client", client_with_failing_dc)
    monkeypatch.setattr(telethon_transport, "ensure_test_environment", trusted)
    monkeypatch.setattr("telethon.client.users.asyncio.sleep", no_sleep)
    with pytest.raises(TelegramUnavailable) as info:
        await TelethonAccount.connect(API, SESSION, "admin")
    assert "DC2" in str(info.value)
    assert "-500 No workers running" in str(info.value)
    assert set(sends) == {"InvokeWithoutUpdatesRequest"}
    assert len(sends) > 1  # Telethon retried before giving up


# --- run: the report remembers why setup failed; the exit code follows it ----


def failing_connect(exc: Exception):
    async def connect(role: str, session: Secret) -> Any:
        raise exc

    return connect


@pytest.mark.parametrize(
    ("exc", "setup_failure", "skip_reason"),
    [
        (SessionInvalid("admin 会话已失效"), "session_invalid", "aborted"),
        (TelegramUnavailable("DC1 暂时故障"), "telegram_unavailable", "aborted"),
        (TelegramUnavailable("限流", retry_after=30), "telegram_unavailable", "rate_limited"),
        (RateLimitExceeded("限流"), None, "rate_limited"),
    ],
)
async def test_run_records_why_setup_failed(
    exc: Exception, setup_failure: str | None, skip_reason: str, config: Any
) -> None:
    report = await run_acceptance(
        config, SCENARIOS[:1], connect=failing_connect(exc), run_id="r", catalog=SCENARIOS
    )
    assert report.status == "failed"
    assert report.setup_failure == setup_failure
    assert report.results[0].skip_reason == skip_reason


@pytest.mark.parametrize(
    ("setup_failure", "code"),
    [
        ("session_invalid", cli.EXIT_SESSION_INVALID),
        ("telegram_unavailable", cli.EXIT_TEMPFAIL),
        (None, cli.EXIT_FAILED),
    ],
)
def test_cli_run_exit_code_follows_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup_failure: Any, code: int
) -> None:
    async def live(args: Any, env: Any, run_dir: Path) -> Report:
        report = Report(run_id="r", status="failed", started_at="t", detail="准备阶段失败")
        report.setup_failure = setup_failure
        return report

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_live", live)
    args = ["run", "--report-dir", str(tmp_path / "r")]
    assert cli.main(args, env=live_env()) == code


# --- prune-groups (and other direct commands): exit code from the exception ---


@pytest.mark.parametrize(
    ("exc", "code", "hint"),
    [
        (SessionInvalid("admin 会话已失效，请重新执行 just e2e-login admin"), 3, "e2e-login"),
        (
            TelegramUnavailable("Telegram 测试环境 DC1 暂时故障（-500 No workers running）"),
            75,
            "DC1",
        ),
        (TelegramUnavailable("要求等待 30 秒", retry_after=30), 75, "30 秒"),
    ],
)
def test_cli_prune_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc: Exception,
    code: int,
    hint: str,
) -> None:
    async def connect(session: Secret) -> Any:
        raise exc

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_owner_connect", lambda api: connect)
    monkeypatch.setattr(cli, "VaultAllowlist", lambda vault: object())
    monkeypatch.setattr(cli, "_vault", lambda: object())
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    assert cli.main(["prune-groups"], env=live_env()) == code
    assert hint in capsys.readouterr().err


def test_exit_codes_are_distinct() -> None:
    codes = [
        cli.EXIT_PASSED,
        cli.EXIT_FAILED,
        cli.EXIT_USAGE,
        cli.EXIT_SESSION_INVALID,
        cli.EXIT_TEMPFAIL,
        cli.EXIT_SKIPPED,
    ]
    assert len(set(codes)) == len(codes)
    assert (cli.EXIT_SESSION_INVALID, cli.EXIT_TEMPFAIL) == (3, 75)
