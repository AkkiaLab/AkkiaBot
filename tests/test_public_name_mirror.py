"""The e2e fake bot masks names exactly like the real bot (BOT-37)."""

import pytest

from akkiabot_core.telegram import public_name
from akkiabot_e2e.testing import _public_name  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "name", ["", "  ", "Bob", "成员测试", "群主测试员", "vcc 虚拟卡番茄 0开卡费"]
)
def test_e2e_mirror_matches_core(name: str) -> None:
    assert _public_name(name) == public_name(name)
