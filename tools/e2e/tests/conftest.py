from __future__ import annotations

import pytest

from akkiabot_e2e.config import E2EConfig, load_config
from akkiabot_e2e.testing import (
    ADMIN,
    BOT,
    MEMBER,
    FakeTelegram,
    live_env,
)


@pytest.fixture
def config() -> E2EConfig:
    return load_config(live_env())


@pytest.fixture
def world() -> FakeTelegram:
    w = FakeTelegram()
    w.add_user(ADMIN)
    w.add_user(MEMBER)
    w.add_bot(BOT)
    return w
