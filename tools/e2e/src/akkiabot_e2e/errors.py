"""Error types shared by the acceptance tool."""

from __future__ import annotations


class E2EError(Exception):
    """Base class; messages must never contain secret values."""


class ConfigError(E2EError):
    """Configuration is present but invalid."""


class SafetyError(E2EError):
    """A safety guard (account / chat allowlist, file permissions) refused to continue."""


class FloodWait(E2EError):  # noqa: N818 - mirrors Telegram's FLOOD_WAIT naming
    """Telegram asked us to wait ``seconds`` before retrying (raised by transports)."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"Telegram 要求等待 {seconds:g} 秒")
        self.seconds = seconds


class RateLimitExceeded(E2EError):  # noqa: N818 - reads better in reports
    """A flood wait was longer than we are willing to sleep; the run must stop."""


class CheckFailed(E2EError):  # noqa: N818 - an assertion outcome, not a crash
    """An acceptance assertion did not hold."""


class ScenarioSkipped(E2EError):  # noqa: N818 - an outcome, reported as "skipped"
    """A prerequisite of the scenario is missing (e.g. the bot has no LLM).

    ``reason`` is a report skip reason (see ``akkiabot_e2e.report.SKIP_REASONS``):
    ``missing_credentials`` by default, ``not_selected`` when it depends on a
    scenario this run did not select.
    """

    def __init__(self, message: str, *, reason: str = "missing_credentials") -> None:
        super().__init__(message)
        self.reason = reason


class InviteFailed(E2EError):  # noqa: N818 - reads better in reports
    """The owner account could not add a user to the group (e.g. ``USER_PRIVACY_RESTRICTED``).

    Never answered by falling back to an invite link: the caller reports it and stops.
    """


class SideEffectsUnsettled(E2EError):  # noqa: N818 - reads better in reports
    """A scenario could not confirm that the bot's asynchronous actions are over.

    E.g. spam was removed but the delayed history purge never showed up. Later
    scenarios would run next to unfinished side effects, so the caller stops.
    """
