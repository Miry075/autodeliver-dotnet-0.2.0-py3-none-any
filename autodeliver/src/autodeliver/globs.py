"""Path normalisation and glob matching with `**` support (fnmatch lets `*` cross `/`)."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable


def norm(path: str) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    pattern = norm(pattern)
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    # Case-insensitive: Windows checkouts and NTFS do not distinguish case.
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def match(path: str, pattern: str) -> bool:
    return bool(_compile(pattern).match(norm(path)))


def match_any(path: str, patterns: Iterable[str]) -> bool:
    return any(match(path, p) for p in patterns)


def same_path(a: str, b: str) -> bool:
    return norm(a).lower() == norm(b).lower()
