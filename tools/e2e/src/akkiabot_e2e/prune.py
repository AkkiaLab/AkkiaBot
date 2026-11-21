"""Weekly clean-up of old staging release acceptance groups.

Every ``just e2e-staging`` run creates a group of its own (``--fresh-group``,
see :func:`akkiabot_e2e.group.fresh_group_title`), so the test accounts' group
count grows with every release. ``prune-groups`` deletes the old ones within
the scope the owner approved:

* only supergroups **created by the admin test account** whose title is exactly
  ``"AkkiaBot Staging 发版验收群 <run id>"`` (run id = ``YYYYMMDDTHHMMSSZ-xxxxxx``)
  **and** whose id is in ``e2e-telegram/allowed_chat_ids``;
* the newest :data:`KEEP` of them (by the UTC time in the run id: the run creates
  its group right after it starts) are kept as evidence;
* everything else — the resident "AkkiaBot E2E 自动验收群", any title that does
  not match the pattern, same-pattern groups outside the allowlist — is never
  deleted; out-of-allowlist matches are only listed in the report;
* deleted ids are removed from ``allowed_chat_ids`` afterwards.

The default is a dry run. Output names run ids and counts only, never chat ids.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.group import ALLOWED_CHATS_FIELD, fresh_group_title
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.transport import ChatRef
from akkiabot_e2e.vault import E2E_ITEM, Vault

logger = logging.getLogger(__name__)

#: Base title of the staging release acceptance groups (tools/e2e/staging-release.env.tpl).
STAGING_RELEASE_CHAT_TITLE = "AkkiaBot Staging 发版验收群"
#: Number of newest in-scope groups kept as evidence.
KEEP = 2

_RUN_ID = r"(?P<run_id>\d{8}T\d{6}Z-[0-9a-f]{6})"
TITLE_RE = re.compile(
    "^" + re.escape(fresh_group_title(STAGING_RELEASE_CHAT_TITLE, "")) + _RUN_ID + "$"
)


class GroupOwner(Protocol):
    """The part of the admin test account that clean-up needs."""

    async def owned_supergroups_with_prefix(self, prefix: str) -> list[ChatRef]:
        """Supergroups created by this account whose title starts with ``prefix``."""
        ...

    async def delete_supergroup(self, chat_id: int) -> None: ...

    async def close(self) -> None: ...


class Allowlist(Protocol):
    """Durable chat allowlist (``e2e-telegram/allowed_chat_ids``)."""

    def chat_ids(self) -> set[int]: ...

    def remove(self, chat_ids: set[int]) -> None:
        """Remove ``chat_ids``; must verify the write."""
        ...


class VaultAllowlist:
    """:class:`Allowlist` backed by the ``e2e-telegram`` 1Password item."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def chat_ids(self) -> set[int]:
        return set(self.vault.read_ids(E2E_ITEM, ALLOWED_CHATS_FIELD))

    def remove(self, chat_ids: set[int]) -> None:
        self.vault.remove_ids(E2E_ITEM, ALLOWED_CHATS_FIELD, chat_ids)


@dataclass(frozen=True, slots=True)
class Candidate:
    run_id: str
    chat_id: int


@dataclass(frozen=True, slots=True)
class PrunePlan:
    keep: tuple[Candidate, ...]
    delete: tuple[Candidate, ...]
    outside_allowlist: tuple[Candidate, ...]
    """Same title pattern but not in the allowlist: never deleted, only reported."""


def plan(groups: list[ChatRef], allowed: set[int], *, keep: int = KEEP) -> PrunePlan:
    """Split the admin's groups into keep / delete / out-of-scope; newest first."""
    if keep < 1:
        raise E2EError("至少要保留 1 个验收群")
    matched: list[Candidate] = []
    for chat in groups:
        found = TITLE_RE.match(chat.title)
        if found is not None:
            matched.append(Candidate(found["run_id"], chat.id))
    # The run id starts with a UTC timestamp, so lexical order is creation order.
    matched.sort(key=lambda c: c.run_id, reverse=True)
    in_scope = [c for c in matched if c.chat_id in allowed]
    return PrunePlan(
        keep=tuple(in_scope[:keep]),
        delete=tuple(in_scope[keep:]),
        outside_allowlist=tuple(c for c in matched if c.chat_id not in allowed),
    )


@dataclass
class PruneResult:
    plan: PrunePlan
    applied: bool
    deleted: list[Candidate] = field(default_factory=list)
    allowlist_updated: bool = False
    error: str | None = None

    def to_text(self) -> str:
        def runs(items: tuple[Candidate, ...] | list[Candidate]) -> str:
            return "、".join(c.run_id for c in items) if items else "无"

        p = self.plan
        mode = "执行删除" if self.applied else "dry-run，未删除任何群"
        lines = [
            f"staging 发版验收群清理（{mode}）",
            f"- 范围内（标题匹配、群主账号创建、在白名单中）：{len(p.keep) + len(p.delete)} 个",
            f"- 保留最近 {len(p.keep)} 个：{runs(p.keep)}",
        ]
        if self.applied:
            lines.append(f"- 已删除 {len(self.deleted)} 个：{runs(self.deleted)}")
            pending = [c for c in p.delete if c not in self.deleted]
            if pending:
                lines.append(f"- 未删除（中途停止）{len(pending)} 个：{runs(pending)}")
            if self.deleted:
                state = "已移除并回读校验" if self.allowlist_updated else "移除失败，见下方错误"
                lines.append(f"- allowed_chat_ids：{len(self.deleted)} 个 id {state}")
        else:
            lines.append(f"- 将删除 {len(p.delete)} 个：{runs(p.delete)}")
        lines.append(
            f"- 标题匹配但不在白名单中（不删除，仅列出）：{len(p.outside_allowlist)} 个："
            f"{runs(p.outside_allowlist)}"
        )
        if self.error:
            lines.append(f"- 错误：{self.error}")
        return "\n".join(lines)


async def prune_groups(
    admin: GroupOwner,
    allowlist: Allowlist,
    pacer: Pacer,
    *,
    apply: bool,
    keep: int = KEEP,
) -> PruneResult:
    """Plan (and with ``apply`` perform) the clean-up. Call with the session lock held."""

    async def call[T](label: str, fn: Callable[[], Awaitable[T]]) -> T:
        return await pacer.call(f"admin.{label}", fn)

    groups = await call(
        "owned_supergroups_with_prefix",
        lambda: admin.owned_supergroups_with_prefix(STAGING_RELEASE_CHAT_TITLE),
    )
    allowed = await asyncio.to_thread(allowlist.chat_ids)
    result = PruneResult(plan(groups, allowed, keep=keep), applied=apply)
    if not apply:
        return result
    try:
        for candidate in result.plan.delete:
            try:
                await call(
                    "delete_supergroup",
                    lambda c=candidate: admin.delete_supergroup(c.chat_id),
                )
            except E2EError:
                raise
            except Exception as exc:  # Telethon RPC errors; keep chat ids out of the message
                raise E2EError(
                    f"删除运行 {candidate.run_id} 的验收群失败：{type(exc).__name__}"
                ) from None
            result.deleted.append(candidate)
            logger.info("deleted staging acceptance group of run %s", candidate.run_id)
    except E2EError as exc:
        result.error = str(exc)
    finally:
        if result.deleted:
            try:
                await asyncio.to_thread(allowlist.remove, {c.chat_id for c in result.deleted})
                result.allowlist_updated = True
            except E2EError as exc:
                # A stale id of a deleted group only widens the allowlist by a dead chat.
                note = f"从 allowed_chat_ids 移除已删除群的 id 失败：{exc}"
                result.error = f"{result.error}；{note}" if result.error else note
    return result
