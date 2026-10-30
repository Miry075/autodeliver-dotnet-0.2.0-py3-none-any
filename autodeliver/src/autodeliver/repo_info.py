"""Detect Azure DevOps organisation / project / repository and the default branch from a git checkout."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


@dataclass(frozen=True)
class RemoteInfo:
    organization_url: str
    project: str
    repository: str


_PATTERNS = [
    # https://dev.azure.com/{org}/{project}/_git/{repo}  (optionally https://{user}@dev.azure.com/...)
    re.compile(r"^https://(?:[^@/]+@)?dev\.azure\.com/(?P<org>[^/]+)/(?P<project>[^/]+)/_git/(?P<repo>[^/?#]+)"),
    # https://{org}.visualstudio.com/[DefaultCollection/]{project}/_git/{repo}
    re.compile(r"^https://(?:[^@/]+@)?(?P<org>[^./]+)\.visualstudio\.com/(?:DefaultCollection/)?(?P<project>[^/]+)/_git/(?P<repo>[^/?#]+)"),
    # git@ssh.dev.azure.com:v3/{org}/{project}/{repo}
    re.compile(r"^(?:ssh://)?git@ssh\.dev\.azure\.com[:/]v3/(?P<org>[^/]+)/(?P<project>[^/]+)/(?P<repo>[^/?#]+)"),
    # {org}@vs-ssh.visualstudio.com:v3/{org}/{project}/{repo}
    re.compile(r"^(?:ssh://)?[^@]+@vs-ssh\.visualstudio\.com[:/]v3/(?P<org>[^/]+)/(?P<project>[^/]+)/(?P<repo>[^/?#]+)"),
]


def parse_remote(url: str) -> RemoteInfo | None:
    url = url.strip()
    for rx in _PATTERNS:
        m = rx.match(url)
        if m:
            repo = m.group("repo")
            repo = repo[:-4] if repo.endswith(".git") else repo
            return RemoteInfo(f"https://dev.azure.com/{unquote(m.group('org'))}", unquote(m.group("project")), unquote(repo))
    return None


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def detect(repo: Path) -> tuple[RemoteInfo | None, str]:
    remote = parse_remote(_git(repo, "remote", "get-url", "origin"))
    head = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")        # refs/remotes/origin/main
    branch = head.rsplit("/", 1)[-1] if head else ""
    if not branch:
        for candidate in ("main", "master", "develop"):
            if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}") or \
               _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"):
                branch = candidate
                break
    return remote, branch or "main"
