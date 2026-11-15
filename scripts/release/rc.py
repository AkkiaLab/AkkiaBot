"""Release candidates: RC tags, their validity and the staging diff baseline.

An RC tag ``vX.Y.Z-rc.N`` pins one exact commit of ``releases/vX.Y.Z`` for
staging (backend image and Mini App from the same sha). It is valid only
while it is the newest RC of its version and the branch still points at it:
any later fix or cascade merge needs a new RC (tags are never moved).
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.release.git import Git, RepoState
from scripts.release.github import GitHub
from scripts.release.versions import ReleaseError, Version, parse_rc

#: Commits inspected when looking for an accepted ancestor.
BASELINE_SCAN_LIMIT = 500


@dataclass(frozen=True)
class Candidate:
    version: Version
    number: int
    sha: str

    @property
    def tag(self) -> str:
        return self.version.rc_tag(self.number)


def create_rc(
    git: Git, gh: GitHub, version: Version, *, dry_run: bool = False
) -> tuple[Candidate, bool]:
    """Tag the branch head as the next RC. Returns (candidate, created).

    Idempotent: when the newest RC already points at the head it is returned
    unchanged, so a repeated or delayed trigger never mints a second RC for
    the same commit.
    """
    state = git.state()
    head = state.branches.get(version)
    if head is None:
        raise ReleaseError(f"{version.branch} 不存在")
    if version in state.tags:
        raise ReleaseError(f"{version} 已有正式 tag，不能再出 RC")
    if version.branch in gh.sealed_branches():
        raise ReleaseError(f"{version.branch} 已封版，不能再出 RC")
    latest = state.latest_rc(version)
    if latest is not None and latest[1] == head:
        return Candidate(version, latest[0], head), False
    number = (latest[0] if latest else 0) + 1
    candidate = Candidate(version, number, head)
    if not dry_run and not git.push_ref(head, f"refs/tags/{candidate.tag}"):
        raise ReleaseError(f"创建 {candidate.tag} 失败（并发创建？），请重新运行")
    return candidate, True


def check_rc(state: RepoState, tag: str, sealed: set[str]) -> Candidate:
    """An RC may be deployed / accepted / sealed only while it is current."""
    parsed = parse_rc(tag)
    if parsed is None:
        raise ReleaseError(f"{tag!r} 不是 RC tag（vX.Y.Z-rc.N）")
    version, number = parsed
    sha = state.rcs.get(version, {}).get(number)
    if sha is None:
        raise ReleaseError(f"{tag} 不存在")
    latest = state.latest_rc(version) or (number, sha)
    if latest[0] != number:
        raise ReleaseError(f"{tag} 已过期：{version.rc_tag(latest[0])} 更新")
    head = state.branches.get(version)
    if head != sha:
        raise ReleaseError(
            f"{tag} 已过期：{version.branch} 已前进到 {str(head)[:12]}，需要新 RC 并按影响重新验收"
        )
    if version.branch in sealed and version not in state.tags:
        raise ReleaseError(f"{version.branch} 已封版但还没有正式 tag：请重新运行封版以完成或回退")
    return Candidate(version, number, sha)


def select_baseline(
    git: Git, gh: GitHub, target: str, *, limit: int = BASELINE_SCAN_LIMIT
) -> str | None:
    """The accepted ancestor of ``target`` to diff the staging acceptance against.

    Only ancestors count, so a patch line (cut from an old tag) never picks up
    an acceptance of the newer regular line, and vice versa. Among the
    accepted ancestors the nearest maximal one wins: one that is not itself an
    ancestor of another accepted one, with the fewest commits up to ``target``.
    Returns None when nothing within ``limit`` commits was accepted (then the
    first RC of the line runs the full acceptance).
    """
    cache: dict[str, str | None] = {}
    accepted: list[str] = []
    while True:
        found = None
        for sha in git.rev_list(target, *accepted, limit=limit):
            if sha == target:
                continue
            if sha not in cache:
                cache[sha] = gh.acceptance(sha)
            if cache[sha] == "success":
                found = sha
                break
        if found is None:
            break
        accepted.append(found)
    if not accepted:
        return None
    maximal = [a for a in accepted if not any(b != a and git.is_ancestor(a, b) for b in accepted)]
    return min(maximal, key=lambda sha: git.count(target, sha))
