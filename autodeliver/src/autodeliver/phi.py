"""Deterministic PHI / secret sweep over the *added* lines of the staged diff.

Findings are reported masked: a report must never become a second copy of the data it flags.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AddedLine:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str          # blocking | warning
    path: str
    line: int
    detail: str            # masked, safe to print

    def render(self) -> str:
        return f"{self.severity.upper()} {self.rule} {self.path}:{self.line} {self.detail}"


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(unified_diff: str) -> list[AddedLine]:
    out: list[AddedLine] = []
    path: str | None = None
    lineno = 0
    for raw in unified_diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        m = _HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            out.append(AddedLine(path, lineno, raw[1:]))
            lineno += 1
        elif raw.startswith(" "):
            lineno += 1
    return out


def mask(text: str, limit: int = 120) -> str:
    text = re.sub(r"\d", "#", text)
    text = re.sub(r'"[^"]{3,}"', '"…"', text)
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- Belgian national register number (INSZ/NISS), validated by its mod-97 check digits ---------
_NISS = re.compile(r"(?<![\d])(\d{2})[.\-\s]?(\d{2})[.\-\s]?(\d{2})[.\-\s]?(\d{3})[.\-\s]?(\d{2})(?![\d])")


def niss_valid(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit():
        return False
    month, day = int(digits[2:4]), int(digits[4:6])
    if not (month <= 12 or 20 <= month <= 32 or 40 <= month <= 52) or day > 31:
        return False
    base, check = int(digits[:9]), int(digits[9:])
    return 97 - (base % 97) == check or 97 - ((2_000_000_000 + base) % 97) == check


_SECRETS = [
    ("secret.private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("secret.storage-key", re.compile(r"(?i)AccountKey=[A-Za-z0-9+/=]{20,}")),
    ("secret.sas", re.compile(r"(?i)[?&]sig=[A-Za-z0-9%+/=]{20,}")),
    ("secret.jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("secret.password", re.compile(r"(?i)\b(password|pwd)\s*=\s*[^;\"'\s{}]{4,}")),
    ("secret.assignment", re.compile(r"(?i)\b(api[_-]?key|client[_-]?secret|secret|token)\b\s*[:=]\s*[\"'][A-Za-z0-9_\-+/=]{16,}[\"']")),
]
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
_SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "test.local", "contoso.com", "invalid")
_STRING = re.compile(r'\$?@?"(?:[^"\\]|\\.)*"')
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _code_identifiers(line: str) -> list[str]:
    """Identifiers outside string literals, plus {Placeholders} inside them (structured logging / interpolation)."""
    holes: list[str] = []

    def keep_holes(m: re.Match[str]) -> str:
        holes.extend(p.split(".")[-1] for p in _PLACEHOLDER.findall(m.group(0)))
        return " "

    code = _STRING.sub(keep_holes, line)
    return [i.lower() for i in _IDENT.findall(code)] + [h.lower() for h in holes]


class PhiScanner:
    def __init__(self, cfg: dict):
        phi = cfg["phi"]
        self.sinks = [re.compile(p) for p in phi["sink_patterns"]]
        self.blocking = [i.lower() for i in phi["blocking_identifiers"]]
        self.warning = [i.lower() for i in phi["warning_identifiers"]]
        self.allow = {i.lower() for i in phi.get("identifier_allowlist", [])}

    def _sensitive(self, ident: str, names: list[str]) -> str | None:
        if ident in self.allow:
            return None
        for name in names:
            if ident == name or (len(name) >= 5 and ident.endswith(name)):
                return name
        return None

    def scan(self, lines: list[AddedLine]) -> list[Finding]:
        findings: list[Finding] = []
        for al in lines:
            text = al.text
            for m in _NISS.finditer(text):
                if niss_valid("".join(m.groups())):
                    findings.append(Finding("phi.national-number", "blocking", al.path, al.line,
                                            "valid INSZ/NISS checksum; use a synthetic generator"))
            for rule, rx in _SECRETS:
                if rx.search(text):
                    findings.append(Finding(rule, "blocking", al.path, al.line, mask(text)))
            for m in _EMAIL.finditer(text):
                if not m.group(0).lower().endswith(_SAFE_EMAIL_DOMAINS):
                    findings.append(Finding("phi.email", "warning", al.path, al.line, "real-looking e-mail address"))
            if any(s.search(text) for s in self.sinks):
                idents = _code_identifiers(text)
                hit_block = next((h for i in idents if (h := self._sensitive(i, self.blocking))), None)
                if hit_block:
                    findings.append(Finding("phi.sink", "blocking", al.path, al.line,
                                            f"'{hit_block}' reaches a log/exception/telemetry sink: {mask(text)}"))
                    continue
                hit_warn = next((h for i in idents if (h := self._sensitive(i, self.warning))), None)
                if hit_warn:
                    findings.append(Finding("phi.sink", "warning", al.path, al.line,
                                            f"'{hit_warn}' reaches a sink; confirm it is pseudonymous: {mask(text)}"))
        return findings


def run_gitleaks(repo: Path, args: list[str], binary: str | None) -> tuple[bool, str]:
    """Returns (clean, message). Missing binary or scanner error fails closed."""
    if not binary:
        return False, "gitleaks not found (run `autodeliver tools install`, or set phi.gitleaks: false explicitly)"
    args = [binary, *[str(a) for a in args[1:]]]
    proc = subprocess.run(args, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode == 0:
        return True, "no leaks"
    if proc.returncode == 1:
        return False, "gitleaks reported leaks (output redacted; run locally with --redact for detail)"
    return False, f"gitleaks error {proc.returncode}: {mask(proc.stderr[-300:], 300)}"
