"""Thin ``git`` wrapper over a local clone whose ``origin`` is the GitHub repository.

All reads come from ``origin`` refs after a fetch, so a stale local branch never
decides anything. Writes are plain (non-force) pushes: if the remote moved in
the meantime the push is rejected and the caller re-plans from a fresh fetch.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from scripts.release.versions import (
    MASTER,
    ReleaseError,
    Version,
    parse_branch,
    parse_rc,
    parse_tag,
)


class GitError(ReleaseError):
    def __init__(self, args: list[str], code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} 失败（退出码 {code}）：{stderr.strip()[:500]}")
        self.code = code
        self.stderr = stderr


@dataclass
class RepoState:
    """Snapshot of the remote: release branches, formal tags and RC tags."""

    master: str
    branches: dict[Version, str] = field(default_factory=dict)
    tags: dict[Version, str] = field(default_factory=dict)
    rcs: dict[Version, dict[int, str]] = field(default_factory=dict)

    @property
    def known_versions(self) -> set[Version]:
        return set(self.branches) | set(self.tags) | set(self.rcs)

    def latest_rc(self, version: Version) -> tuple[int, str] | None:
        numbers = self.rcs.get(version, {})
        if not numbers:
            return None
        top = max(numbers)
        return top, numbers[top]


class Git:
    def __init__(self, workdir: Path, remote: str = "origin") -> None:
        self.workdir = workdir
        self.remote = remote

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(  # noqa: S603 - fixed executable, arguments are refs/paths
            ["git", *args],  # noqa: S607
            cwd=self.workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise GitError(list(args), proc.returncode, proc.stderr)
        return proc

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    # ------------------------------------------------------------- reading
    def fetch(self) -> None:
        self.run(
            "fetch",
            "--prune",
            "--prune-tags",
            "--force",
            self.remote,
            f"+refs/heads/{MASTER}:refs/remotes/{self.remote}/{MASTER}",
            f"+refs/heads/releases/*:refs/remotes/{self.remote}/releases/*",
            "+refs/tags/v*:refs/tags/v*",
        )

    def state(self, *, fetch: bool = True) -> RepoState:
        """Read branches and tags from the remote (``ls-remote`` is authoritative)."""
        if fetch:
            self.fetch()
        lines = self.out("ls-remote", "--heads", "--tags", self.remote).splitlines()
        master = ""
        state = RepoState(master="")
        for line in lines:
            sha, _, ref = line.partition("\t")
            if ref.endswith("^{}"):
                # Annotated tag: the peeled line carries the commit.
                name = ref.removesuffix("^{}")
                peeled = True
            else:
                name, peeled = ref, False
            if name == f"refs/heads/{MASTER}":
                master = sha
            elif (version := parse_branch(name)) is not None:
                state.branches[version] = sha
            elif (version := parse_tag(name)) is not None:
                if peeled or version not in state.tags:
                    state.tags[version] = sha
            elif (rc := parse_rc(name)) is not None:
                bucket = state.rcs.setdefault(rc[0], {})
                if peeled or rc[1] not in bucket:
                    bucket[rc[1]] = sha
        if not master:
            raise GitError(["ls-remote"], 1, f"远端没有 {MASTER} 分支")
        state.master = master
        return state

    def fetch_pr(self, number: int, head_ref: str) -> None:
        """Make a PR head available locally (``refs/pull/N/head``, else its branch)."""
        local = f"refs/remotes/{self.remote}/pr/{number}"
        proc = self.run("fetch", self.remote, f"+refs/pull/{number}/head:{local}", check=False)
        if proc.returncode != 0:
            self.run("fetch", self.remote, f"+refs/heads/{head_ref}:{local}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = self.run("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if proc.returncode not in (0, 1):
            raise GitError(["merge-base", "--is-ancestor"], proc.returncode, proc.stderr)
        return proc.returncode == 0

    def rev_list(self, include: str, *exclude: str, limit: int | None = None) -> list[str]:
        args = ["rev-list", "--topo-order"]
        if limit is not None:
            args.append(f"--max-count={limit}")
        args.append(include)
        args.extend(f"^{e}" for e in exclude)
        out = self.out(*args)
        return out.splitlines() if out else []

    def count(self, include: str, exclude: str) -> int:
        return int(self.out("rev-list", "--count", include, f"^{exclude}"))

    def changed_files(self, base: str, head: str, *paths: str) -> list[str]:
        out = self.out("diff", "--name-only", base, head, "--", *paths)
        return out.splitlines() if out else []

    def files_touched(self, include: str, exclude: str, files: list[str]) -> list[str]:
        """Commits in ``exclude..include`` that touch any of ``files``."""
        if not files:
            return []
        out = self.out("log", "--format=%H", include, f"^{exclude}", "--", *files)
        return out.splitlines() if out else []

    # ------------------------------------------------------------- writing
    def push_ref(self, sha: str, ref: str) -> bool:
        """Non-force push of ``sha`` to ``ref``. False when the remote rejected it."""
        proc = self.run("push", "--porcelain", self.remote, f"{sha}:{ref}", check=False)
        return proc.returncode == 0

    def merge_forward(self, source: str, target: str, message: str) -> tuple[str | None, list[str]]:
        """Merge commit ``target + source`` (``--no-ff``) in a detached worktree state.

        Returns (merge sha, []) on success or (None, conflicting files) after
        aborting. Never touches the remote.
        """
        self.run("checkout", "--quiet", "--detach", target)
        proc = self.run("merge", "--no-ff", "--no-edit", "-m", message, source, check=False)
        if proc.returncode == 0:
            return self.out("rev-parse", "HEAD"), []
        conflicted = self.out("diff", "--name-only", "--diff-filter=U").splitlines()
        self.run("merge", "--abort", check=False)
        self.run("reset", "--quiet", "--hard", target)
        if not conflicted:
            raise GitError(["merge", source], proc.returncode, proc.stderr)
        return None, conflicted
