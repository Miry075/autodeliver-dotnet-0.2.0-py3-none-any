"""dotnet build / test / Stryker runners. Results come from parsed artefacts (TRX, JSON), not exit codes alone."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


SECRET_ENV = {"SYSTEM_ACCESSTOKEN", "AZURE_DEVOPS_PAT", "ANTHROPIC_API_KEY", "SONAR_TOKEN",
              "AZURE_DEVOPS_EXT_PAT", "VSS_NUGET_ACCESSTOKEN"}


@dataclass
class BuildResult:
    ok: bool
    warnings: int
    errors: int
    tail: str


@dataclass
class TestResult:
    ok: bool
    total: int = 0
    executed: int = 0
    passed: int = 0
    failed: int = 0
    failed_tests: list[str] = field(default_factory=list)
    tail: str = ""


@dataclass
class MutationResult:
    ran: bool
    score: float | None        # None = no mutants in scope
    detected: int = 0
    undetected: int = 0
    tail: str = ""


_WARN_RES = [re.compile(r"(\d+)\s+Warning\(s\)", re.I), re.compile(r"with\s+(\d+)\s+warning\(s\)", re.I)]
_ERR_RES = [re.compile(r"(\d+)\s+Error\(s\)", re.I), re.compile(r"with\s+(\d+)\s+error\(s\)", re.I)]


def parse_build_counts(output: str) -> tuple[int, int]:
    def last(patterns: list[re.Pattern[str]]) -> int:
        found = [int(m.group(1)) for p in patterns for m in p.finditer(output)]
        return found[-1] if found else 0
    return last(_WARN_RES), last(_ERR_RES)


def parse_trx_dir(results_dir: Path) -> TestResult:
    result = TestResult(ok=True)
    files = sorted(results_dir.rglob("*.trx"))
    for trx in files:
        root = ET.parse(trx).getroot()
        counters = root.find(".//{*}ResultSummary/{*}Counters")
        if counters is not None:
            result.total += int(counters.get("total", 0))
            result.executed += int(counters.get("executed", 0))
            result.passed += int(counters.get("passed", 0))
            result.failed += int(counters.get("failed", 0)) + int(counters.get("error", 0)) + int(counters.get("timeout", 0))
        for utr in root.iter():
            if utr.tag.endswith("UnitTestResult") and utr.get("outcome") in ("Failed", "Error", "Timeout"):
                result.failed_tests.append(utr.get("testName", "?"))
    result.ok = bool(files) and result.failed == 0
    return result


def mutation_score(report: dict) -> tuple[float | None, int, int]:
    detected = undetected = 0
    for file_data in report.get("files", {}).values():
        for mutant in file_data.get("mutants", []):
            status = mutant.get("status")
            if status in ("Killed", "Timeout"):
                detected += 1
            elif status in ("Survived", "NoCoverage"):
                undetected += 1
    total = detected + undetected
    return (round(100.0 * detected / total, 1) if total else None), detected, undetected


def detect_solution(repo: Path, configured: str) -> str:
    if configured:
        return configured
    candidates = sorted([*repo.glob("*.sln"), *repo.glob("*.slnx")])
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one .sln/.slnx at repo root, found {[c.name for c in candidates]}; set dotnet.solution")
    return candidates[0].name


class Dotnet:
    def __init__(self, repo: Path, cfg: dict):
        self.repo = Path(repo)
        self.cfg = cfg
        self.solution = detect_solution(self.repo, cfg["dotnet"].get("solution", ""))
        self.configuration = cfg["dotnet"]["configuration"]
        # Build and tests execute agent-written code: they never see pipeline secrets.
        inherited = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        self.env = {**inherited, **{k: str(v) for k, v in cfg["dotnet"].get("env", {}).items()}, "MSBUILDTERMINALLOGGER": "off"}

    def _run(self, args: list[str], timeout_s: int = 3600) -> tuple[int, str]:
        proc = subprocess.run(args, cwd=self.repo, env=self.env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout_s)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def build(self) -> BuildResult:
        code, out = self._run(["dotnet", "build", self.solution, "-c", self.configuration, "--nologo", "-v", "q"])
        warnings, errors = parse_build_counts(out)
        return BuildResult(ok=code == 0 and errors == 0, warnings=warnings, errors=errors, tail=out[-4000:])

    def test(self, results_dir: Path, test_filter: str | None = None) -> TestResult:
        if results_dir.exists():
            shutil.rmtree(results_dir)
        args = ["dotnet", "test", self.solution, "-c", self.configuration, "--no-build", "--nologo", "-v", "q",
                "--logger", "trx", "--results-directory", str(results_dir)]
        if test_filter:
            args += ["--filter", test_filter]
        code, out = self._run(args)
        result = parse_trx_dir(results_dir) if results_dir.exists() else TestResult(ok=False)
        if code != 0 and result.failed == 0 and result.executed > 0:
            result.ok = False                       # host crash / infra error after tests ran: fail closed
        if test_filter is None and result.executed == 0:
            result.ok = False                       # an empty full run proves nothing
        result.tail = out[-4000:]
        return result

    def mutate(self, base: str, out_dir: Path) -> MutationResult:
        m = self.cfg["mutation"]
        args = [str(a).format(solution=self.solution, base=base, out=str(out_dir)) for a in m["args"]]
        try:
            code, out = self._run(args, timeout_s=int(m["timeout_minutes"]) * 60)
        except subprocess.TimeoutExpired:
            return MutationResult(ran=False, score=None, tail="stryker timed out")
        reports = sorted(out_dir.rglob("mutation-report.json"))
        if not reports:
            return MutationResult(ran=False, score=None, tail=out[-4000:])
        score, detected, undetected = mutation_score(json.loads(reports[-1].read_text(encoding="utf-8")))
        return MutationResult(ran=True, score=score, detected=detected, undetected=undetected, tail=out[-2000:])
