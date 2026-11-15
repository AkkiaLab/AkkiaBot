from datetime import UTC, datetime

import pytest

from scripts.release.git import RepoState
from scripts.release.schedule import Phase, current_regular, feature_frozen, window_at
from scripts.release.versions import (
    ReleaseError,
    Version,
    check_new_version,
    next_regular,
    parse_branch,
    parse_rc,
    parse_tag,
    source_ref,
)

V = Version.parse


def test_parse_and_names() -> None:
    v = V("v1.2.3")
    assert (v.major, v.minor, v.patch) == (1, 2, 3)
    assert v.branch == "releases/v1.2.3"
    assert v.rc_tag(2) == "v1.2.3-rc.2"
    assert V("releases/v1.2.3") == v
    assert parse_branch("refs/heads/releases/v1.2.3") == v
    assert parse_branch("master") is None
    assert parse_tag("v1.2.3") == v
    assert parse_tag("v1.2.3-rc.1") is None
    assert parse_rc("refs/tags/v1.2.3-rc.4") == (v, 4)
    for bad in ("1.2.3", "v01.2.3", "v1.2", "v1.2.3-rc.0", "release/v1.2.3"):
        assert parse_branch(bad) is None
        assert parse_tag(bad) is None or bad.startswith("v1.2.3-")
    with pytest.raises(ReleaseError):
        V("v1.02.0")


def test_semantic_ordering() -> None:
    versions = [V("v1.10.0"), V("v1.9.0"), V("v1.9.1"), V("v2.0.0"), V("v1.0.10"), V("v1.0.2")]
    assert sorted(versions) == [
        V("v1.0.2"),
        V("v1.0.10"),
        V("v1.9.0"),
        V("v1.9.1"),
        V("v1.10.0"),
        V("v2.0.0"),
    ]


def test_sources() -> None:
    assert source_ref(V("v1.3.0"), []) == "master"
    assert source_ref(V("v2.0.0"), []) == "master"
    assert source_ref(V("v1.2.1"), [V("v1.2.0")]) == "v1.2.0"
    assert source_ref(V("v1.2.2"), [V("v1.2.0"), V("v1.2.1")]) == "v1.2.1"
    with pytest.raises(ReleaseError, match=r"v1\.2\.1"):
        source_ref(V("v1.2.2"), [V("v1.2.0")])


def test_next_regular_and_new_version_checks() -> None:
    with pytest.raises(ReleaseError, match="首个版本"):
        next_regular([])
    known = {V("v1.0.0"), V("v1.0.1"), V("v1.1.0")}
    assert next_regular(known) == V("v1.2.0")
    assert next_regular(known, major=True) == V("v2.0.0")
    with pytest.raises(ReleaseError, match="复用"):
        check_new_version(V("v1.1.0"), known)
    with pytest.raises(ReleaseError, match="新的 y"):
        check_new_version(V("v1.0.0"), {V("v1.0.1")})
    with pytest.raises(ReleaseError, match="倒序"):
        check_new_version(V("v0.9.0"), known)
    check_new_version(V("v1.0.2"), known)
    check_new_version(V("v1.2.0"), known)


@pytest.mark.parametrize(
    ("utc", "phase"),
    [
        ("2026-11-19T07:59:00", Phase.NONE),  # Thu 15:59 UTC+8
        ("2026-11-19T08:00:00", Phase.PREPARE),  # Thu 16:00
        ("2026-11-19T10:00:00", Phase.FREEZE),  # Thu 18:00
        ("2026-11-19T11:59:59", Phase.FREEZE),
        ("2026-11-19T12:00:00", Phase.RELEASE),  # Thu 20:00
        ("2026-11-19T15:59:00", Phase.RELEASE),  # Thu 23:59
        ("2026-11-19T16:00:00", Phase.NONE),  # Fri 00:00
        ("2026-11-22T10:30:00", Phase.FREEZE),  # Sun 18:30
        ("2026-11-17T12:00:00", Phase.NONE),  # Tue
    ],
)
def test_window_phase(utc: str, phase: Phase) -> None:
    assert window_at(datetime.fromisoformat(utc).replace(tzinfo=UTC)).phase == phase


def _state(*branches: str) -> RepoState:
    return RepoState(master="m", branches={V(b): f"sha-{b}" for b in branches})


def test_feature_freeze_classification() -> None:
    state = _state("v1.1.0", "v1.1.1", "v1.2.0")
    frozen_time = datetime(2026, 11, 19, 10, 30, tzinfo=UTC)
    open_time = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
    assert current_regular(state, set()) == V("v1.1.0")
    assert feature_frozen(V("v1.1.0"), frozen_time, state, set())
    assert not feature_frozen(V("v1.1.0"), open_time, state, set())
    assert not feature_frozen(V("v1.2.0"), frozen_time, state, set())  # next release stays open
    assert not feature_frozen(V("v1.1.1"), frozen_time, state, set())  # patch: 加车 by Leader
    sealed = {"releases/v1.1.0"}
    assert current_regular(state, sealed) == V("v1.2.0")
    assert feature_frozen(V("v1.2.0"), frozen_time, state, sealed)
