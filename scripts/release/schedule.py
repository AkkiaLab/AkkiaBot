"""Release windows, feature freeze and which release a change belongs to.

Workspace rule (2026-11-15): regular releases ship on Thursday and Sunday at
20:00 UTC+8; at 18:00 the *current* regular release freezes new features.
Fixes and cascade merges are never frozen. The phase is derived from the
clock and the repository alone, so a delayed or repeated scheduled run makes
the same decision as an on-time one (docs/release.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum

from scripts.release.git import RepoState
from scripts.release.versions import Version

TZ = timezone(timedelta(hours=8), "UTC+8")
WINDOW_WEEKDAYS = frozenset({3, 6})  # Thursday, Sunday (Monday = 0)
PREPARE_AT = time(16, 0)
FREEZE_AT = time(18, 0)
RELEASE_AT = time(20, 0)


class Phase(StrEnum):
    NONE = "none"  # not a window day, or before PREPARE_AT
    PREPARE = "prepare"  # 16:00–18:00: make sure branches exist, decide skip
    FREEZE = "freeze"  # 18:00–20:00: features frozen, RC to staging
    RELEASE = "release"  # 20:00–24:00: seal and tag if the RC passed


@dataclass(frozen=True)
class Window:
    day: date | None
    phase: Phase


def window_at(now: datetime) -> Window:
    local = now.astimezone(TZ)
    if local.weekday() not in WINDOW_WEEKDAYS:
        return Window(None, Phase.NONE)
    clock = local.time()
    if clock >= RELEASE_AT:
        phase = Phase.RELEASE
    elif clock >= FREEZE_AT:
        phase = Phase.FREEZE
    elif clock >= PREPARE_AT:
        phase = Phase.PREPARE
    else:
        return Window(None, Phase.NONE)
    return Window(local.date(), phase)


def current_regular(state: RepoState, sealed: set[str]) -> Version | None:
    """The regular release that ships in the next window: lowest unsealed ``vX.Y.0``."""
    candidates = [v for v in state.branches if not v.is_patch and v.branch not in sealed]
    return min(candidates) if candidates else None


def next_regular_open(state: RepoState, sealed: set[str]) -> Version | None:
    """The regular release after the current one, if it already exists."""
    current = current_regular(state, sealed)
    if current is None:
        return None
    later = [v for v in state.branches if not v.is_patch and v > current and v.branch not in sealed]
    return min(later) if later else None


def feature_frozen(version: Version, now: datetime, state: RepoState, sealed: set[str]) -> bool:
    """Is ``version`` closed for new features right now?

    Only the current regular release freezes, from 18:00 of a window day until
    it is sealed or the day ends (a release that did not ship then carries
    over to the next window and reopens). Patch releases take what the Leader
    puts on them (fixes and 加车) and are not frozen by the clock.
    """
    if version.is_patch or version.branch in sealed:
        return False
    if current_regular(state, sealed) != version:
        return False
    return window_at(now).phase in {Phase.FREEZE, Phase.RELEASE}
