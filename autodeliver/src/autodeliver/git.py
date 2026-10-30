"""Thin git wrapper. Staging is always by explicit path; there is no add -A anywhere."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .globs import norm

WORK_DIR = ".work/"
# Tool output that must never count as a change (and never be staged).
LOCAL_EXCLUDES = [WORK_DIR, ".claude/notes/", "__pycache__/", ".sonarqube/", "StrykerOutput/"]


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class NumStat:
    path: str
    added: int
    deleted: int
    binary: bool


class Git:
    def __init__(self, repo: Path):
        self.repo = Path(repo)

    def run(self, *args: str, check: bool = True, remote: bool = False) -> str:
        # The token is passed per command and never written to .git/config, where agent-written
        # test code (run by dotnet test) could read it.
        auth: list[str] = []
        token = os.environ.get("SYSTEM_ACCESSTOKEN")
        if remote and token:
            auth = ["-c", f"http.extraheader=AUTHORIZATION: bearer {token}"]
        proc = subprocess.run(
            ["git", *auth, *args], cwd=self.repo, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if check and proc.returncode != 0:
            err = proc.stderr.strip()[-800:].replace(token or "\0", "***")
            raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {err}")
        return proc.stdout

    # --- safety -----------------------------------------------------------------------------
    def ensure_work_dir_excluded(self) -> None:
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [p for p in LOCAL_EXCLUDES if p not in current.splitlines()]
        if missing:
            exclude.write_text(current.rstrip("\n") + ("\n" if current else "") + "\n".join(missing) + "\n", encoding="utf-8")

    def is_clean(self) -> bool:
        return self.run("status", "--porcelain", "-uall").strip() == ""

    def branch_exists(self, name: str) -> bool:
        local = self.run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).strip()
        remote = self.run("ls-remote", "--heads", "origin", name, remote=True).strip() if self.has_origin() else ""
        return bool(local or remote)

    def has_origin(self) -> bool:
        return "origin" in self.run("remote").split()

    # --- refs -------------------------------------------------------------------------------
    def fetch(self, branch: str) -> None:
        if self.has_origin():
            self.run("fetch", "--quiet", "origin", branch, remote=True)

    def resolve(self, ref: str) -> str:
        return self.run("rev-parse", ref).strip()

    def base_ref(self, target_branch: str) -> str:
        return f"origin/{target_branch}" if self.has_origin() else target_branch

    def create_branch(self, name: str, start: str) -> None:
        self.run("checkout", "--quiet", "-b", name, start)

    def head(self) -> str:
        return self.resolve("HEAD")

    # --- working tree -----------------------------------------------------------------------
    def working_changes(self) -> list[str]:
        """Every changed, deleted or untracked path in the working tree and index (repo-relative)."""
        out = self.run("status", "--porcelain=v1", "-z", "-uall")
        entries = [e for e in out.split("\0") if e]
        paths: list[str] = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            status, path = entry[:2], entry[3:]
            paths.append(norm(path))
            if status[0] in "RC" and i + 1 < len(entries):   # -z rename: next entry is the source
                paths.append(norm(entries[i + 1]))
                i += 1
            i += 1
        return sorted({p for p in paths if not p.startswith((WORK_DIR, ".claude/notes/"))})

    def discard(self, paths: list[str]) -> None:
        """Revert unauthorised edits: restore tracked files, delete untracked ones."""
        for path in paths:
            tracked = self.run("ls-files", "--error-unmatch", "--", path, check=False)
            if tracked.strip():
                self.run("checkout", "HEAD", "--", path)
            else:
                target = self.repo / path
                if target.is_file():
                    target.unlink()

    def stage(self, paths: list[str]) -> None:
        if paths:
            self.run("add", "--", *paths)

    def unstage_all(self) -> None:
        self.run("reset", "--quiet")

    # --- diffs (always against the index, i.e. exactly what would be committed) --------------
    def staged_files(self, base: str) -> list[str]:
        return [norm(p) for p in self.run("diff", "--cached", "--name-only", base).splitlines() if p.strip()]

    def staged_numstat(self, base: str) -> list[NumStat]:
        stats: list[NumStat] = []
        for line in self.run("diff", "--cached", "--numstat", base).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, deleted, path = parts[0], parts[1], parts[-1]
            binary = added == "-" or deleted == "-"
            stats.append(NumStat(norm(path), 0 if binary else int(added), 0 if binary else int(deleted), binary))
        return stats

    def staged_diff(self, base: str, unified: int = 0, paths: list[str] | None = None) -> str:
        args = ["diff", "--cached", f"-U{unified}", "--no-color", "--no-ext-diff", base]
        if paths:
            args += ["--", *paths]
        return self.run(*args)

    def has_staged_changes(self) -> bool:
        return self.run("diff", "--cached", "--name-only").strip() != ""

    # --- publish ----------------------------------------------------------------------------
    def commit(self, message: str) -> str:
        # Repo hooks run (no --no-verify): they are one more gate, not an obstacle.
        self.run("commit", "--quiet", "-m", message)
        return self.head()

    def push(self, branch: str) -> None:
        self.run("push", "--quiet", "-u", "origin", branch, remote=True)
