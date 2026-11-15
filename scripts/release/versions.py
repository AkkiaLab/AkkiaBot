"""Version, branch and tag names of the release flow (docs/release.md §1).

* release branch ``releases/vX.Y.Z``, formal tag ``vX.Y.Z``, RC tag ``vX.Y.Z-rc.N``;
* ``Z == 0`` is a regular (or major) release cut from the newest ``master``;
* ``Z > 0`` is a patch release cut from the formal tag ``vX.Y.(Z-1)``.

Parsing is strict (no leading zeros, no ``v`` omitted) so that a typo never
creates a branch or tag outside the scheme.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

MASTER = "master"
BRANCH_PREFIX = "releases/"

_NUM = r"(0|[1-9][0-9]*)"
_VERSION_RE = re.compile(rf"^v{_NUM}\.{_NUM}\.{_NUM}$")
_RC_RE = re.compile(rf"^v{_NUM}\.{_NUM}\.{_NUM}-rc\.([1-9][0-9]*)$")


class ReleaseError(Exception):
    """A release rule was violated; the message is shown to the operator (Chinese)."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        """``vX.Y.Z`` (also accepts ``releases/vX.Y.Z``)."""
        name = text.removeprefix("refs/heads/").removeprefix("refs/tags/")
        name = name.removeprefix(BRANCH_PREFIX)
        match = _VERSION_RE.match(name)
        if match is None:
            raise ReleaseError(f"版本号必须形如 vX.Y.Z（不含前导零），当前为 {text!r}")
        return cls(int(match[1]), int(match[2]), int(match[3]))

    def __str__(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"

    @property
    def tag(self) -> str:
        return str(self)

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}{self}"

    @property
    def is_patch(self) -> bool:
        return self.patch > 0

    def rc_tag(self, number: int) -> str:
        if number < 1:
            raise ReleaseError("RC 编号从 1 开始")
        return f"{self}-rc.{number}"

    @property
    def previous_patch(self) -> Version:
        """The formal release a patch release is cut from."""
        if not self.is_patch:
            raise ReleaseError(f"{self} 是常规版本，没有上一个小版本")
        return Version(self.major, self.minor, self.patch - 1)


def parse_branch(name: str) -> Version | None:
    """``releases/vX.Y.Z`` → Version; anything else → None."""
    name = name.removeprefix("refs/heads/")
    if not name.startswith(BRANCH_PREFIX):
        return None
    match = _VERSION_RE.match(name.removeprefix(BRANCH_PREFIX))
    return Version(int(match[1]), int(match[2]), int(match[3])) if match else None


def parse_tag(name: str) -> Version | None:
    """Formal tag ``vX.Y.Z`` → Version; RC tags and others → None."""
    match = _VERSION_RE.match(name.removeprefix("refs/tags/"))
    return Version(int(match[1]), int(match[2]), int(match[3])) if match else None


def parse_rc(name: str) -> tuple[Version, int] | None:
    """RC tag ``vX.Y.Z-rc.N`` → (Version, N); anything else → None."""
    match = _RC_RE.match(name.removeprefix("refs/tags/"))
    if match is None:
        return None
    return Version(int(match[1]), int(match[2]), int(match[3])), int(match[4])


def source_ref(version: Version, formal_tags: Iterable[Version]) -> str:
    """Where a new release branch is cut from (workspace rule, 2026-11-15).

    Regular releases come from the newest ``master``; a patch release
    ``vX.Y.Z`` from the formal tag ``vX.Y.(Z-1)``, which must exist: patch
    releases are cut one after another (v1.2.0 → v1.2.1 → v1.2.2).
    """
    if not version.is_patch:
        return MASTER
    previous = version.previous_patch
    if previous not in set(formal_tags):
        raise ReleaseError(
            f"小版本 {version} 必须从上一个正式版本 tag {previous} 创建，但 {previous} 还没有正式 tag"
        )
    return previous.tag


def next_regular(known: Iterable[Version], *, major: bool = False) -> Version:
    """Next regular release after every known branch/tag: ``vX.(Y+1).0`` or ``v(X+1).0.0``.

    With nothing known there is no automatic first version: the first release
    is chosen explicitly (docs/release.md §6), never derived from an example.
    """
    versions = sorted(set(known))
    if not versions:
        raise ReleaseError(
            "仓库中还没有任何版本分支或 tag，首个版本须按 docs/release.md §6 显式指定"
        )
    top = versions[-1]
    return Version(top.major + 1, 0, 0) if major else Version(top.major, top.minor + 1, 0)


def check_new_version(version: Version, known: Iterable[Version]) -> None:
    """A new release must not reuse a version or squeeze in below a released one."""
    versions = set(known)
    if version in versions:
        raise ReleaseError(f"{version} 已存在（分支或 tag），版本号不能复用")
    if not version.is_patch:
        same_line = [v for v in versions if (v.major, v.minor) == (version.major, version.minor)]
        if same_line:
            raise ReleaseError(
                f"{version.major}.{version.minor} 系列已存在，常规版本须占用新的 y 值"
            )
        higher = [v for v in versions if not v.is_patch and v > version]
        if higher:
            raise ReleaseError(f"常规版本 {version} 低于已存在的 {max(higher)}，不能倒序创建")
