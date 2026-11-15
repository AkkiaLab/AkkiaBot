"""Staging lease shared by GitHub Actions and the runtime machine (docs/release.md §4.1).

The staging environment (backend stack, fixed Mini App alias, test sessions)
belongs to one RC from the frontend upload until the acceptance has finished.
A machine-local lock cannot be seen by Actions, and an Actions concurrency
group ends when its run ends, so the lease lives in the repository itself:

* ref ``refs/release/staging-lease`` points to a lease commit whose single
  parent is the leased sha and whose message records holder and expiry;
* acquire = push that ref with ``--force-with-lease=<ref>:`` (create only if
  absent), or compare-and-swap an *expired* lease — both atomic on the server;
* release = delete it, again compare-and-swap on our own lease commit;
* every writer of the staging alias (``webapp-deploy.yml``, manual runs included)
  must find a lease on exactly the sha it deploys (``lease check``), so a second
  RC or a manual run cannot replace the version under acceptance.

The ref is outside ``refs/heads`` and ``refs/tags``: no branch or tag rules,
no workflow triggers.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scripts.release.git import Git
from scripts.release.versions import ReleaseError

LEASE_REF = "refs/release/staging-lease"
DEFAULT_TTL = timedelta(hours=6)


@dataclass(frozen=True)
class Lease:
    commit: str
    sha: str
    holder: str
    label: str
    expires: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires


def read_lease(git: Git) -> Lease | None:
    line = git.out("ls-remote", git.remote, LEASE_REF)
    if not line:
        return None
    commit = line.split("\t", 1)[0]
    git.run("fetch", "--quiet", git.remote, f"+{LEASE_REF}:refs/remotes/{git.remote}/staging-lease")
    parent = git.out("rev-parse", f"{commit}^")
    fields: dict[str, str] = {}
    for item in git.out("log", "-1", "--format=%B", commit).splitlines():
        key, sep, value = item.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    try:
        expires = datetime.fromisoformat(fields["expires"])
    except (KeyError, ValueError):
        expires = datetime.min.replace(tzinfo=UTC)  # unreadable lease: treat as expired
    return Lease(commit, parent, fields.get("holder", "?"), fields.get("label", "?"), expires)


def _lease_commit(git: Git, sha: str, label: str, holder: str, expires: datetime) -> str:
    message = f"staging lease\n\nlabel={label}\nholder={holder}\nexpires={expires.isoformat()}\n"
    proc = git.run("commit-tree", f"{sha}^{{tree}}", "-p", sha, "-m", message)
    return proc.stdout.strip()


def acquire(
    git: Git,
    sha: str,
    label: str,
    *,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_TTL,
    holder: str | None = None,
) -> Lease:
    now = now or datetime.now(UTC)
    holder = holder or f"{os.environ.get('USER', '?')}@{socket.gethostname()}"
    current = read_lease(git)
    if current is not None and not current.expired(now):
        raise ReleaseError(
            f"staging 正被 {current.label}（{current.sha[:12]}，{current.holder}）占用，"
            f"租约到 {current.expires.isoformat()}；请等待其验收结束"
        )
    expected = current.commit if current else ""
    commit = _lease_commit(git, sha, label, holder, now + ttl)
    proc = git.run(
        "push",
        f"--force-with-lease={LEASE_REF}:{expected}",
        git.remote,
        f"{commit}:{LEASE_REF}",
        check=False,
    )
    if proc.returncode != 0:
        raise ReleaseError("取得 staging 租约失败：另一方刚刚取得或更新了租约，请稍后重试")
    return Lease(commit, sha, holder, label, now + ttl)


def release(git: Git, lease: Lease) -> bool:
    """Delete our lease; False when it is no longer ours (expired and taken over)."""
    proc = git.run(
        "push",
        f"--force-with-lease={LEASE_REF}:{lease.commit}",
        git.remote,
        f":{LEASE_REF}",
        check=False,
    )
    return proc.returncode == 0


@contextlib.contextmanager
def held(git: Git, sha: str, label: str, *, ttl: timedelta = DEFAULT_TTL) -> Iterator[Lease]:
    lease = acquire(git, sha, label, ttl=ttl)
    try:
        yield lease
    finally:
        release(git, lease)


def check(git: Git, sha: str, *, now: datetime | None = None) -> Lease:
    """Gate for writers of the staging alias: an unexpired lease on exactly ``sha``."""
    lease = read_lease(git)
    if lease is None:
        raise ReleaseError(
            "staging 没有租约：先用 `just release lease acquire <sha>` 或 `just release-staging` 取得"
        )
    if lease.sha != sha:
        raise ReleaseError(
            f"staging 租约属于 {lease.label}（{lease.sha[:12]}），不是 {sha[:12]}，拒绝覆盖"
        )
    if lease.expired(now or datetime.now(UTC)):
        raise ReleaseError(f"{lease.label} 的 staging 租约已于 {lease.expires.isoformat()} 过期")
    return lease
