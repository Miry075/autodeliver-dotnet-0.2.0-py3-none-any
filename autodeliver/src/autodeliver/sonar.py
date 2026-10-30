"""Local SonarCloud analysis via dotnet-sonarscanner, so nothing is pushed before the gates pass.

Requires: `dotnet tool install --global dotnet-sonarscanner`, a JDK on the agent, SONAR_TOKEN in env.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests


@dataclass
class SonarResult:
    ran: bool
    gate_status: str = "NONE"
    issues: list[dict] = field(default_factory=list)
    message: str = ""


def _read_report_task(repo: Path) -> dict[str, str]:
    for path in repo.rglob(".sonarqube/out/.sonar/report-task.txt"):
        pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
        return {k.strip(): v.strip() for k, v in pairs}
    return {}


def analyse(repo: Path, cfg: dict, solution: str, branch: str, changed_files: list[str]) -> SonarResult:
    s = cfg["sonar"]
    token = os.environ.get("SONAR_TOKEN", "")
    if not token or token.startswith("$("):
        return SonarResult(False, message="SONAR_TOKEN not set")
    from .dotnet import SECRET_ENV
    env = {**{k: v for k, v in os.environ.items() if k not in SECRET_ENV}, "MSBUILDTERMINALLOGGER": "off"}
    begin = ["dotnet", "sonarscanner", "begin", f"/k:{s['project_key']}", f"/o:{s['organization']}",
             f"/d:sonar.host.url={s['host_url']}", f"/d:sonar.branch.name={branch}",
             "/d:sonar.token=" + token]
    steps = [begin,
             ["dotnet", "build", solution, "-c", cfg["dotnet"]["configuration"], "--nologo", "-v", "q"],
             ["dotnet", "sonarscanner", "end", "/d:sonar.token=" + token]]
    for args in steps:
        proc = subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            shown = " ".join(a if "token" not in a else "/d:sonar.token=***" for a in args[:3])
            return SonarResult(False, message=f"{shown} failed: {proc.stdout[-300:]}{proc.stderr[-300:]}")

    task = _read_report_task(repo)
    if "ceTaskId" not in task:
        return SonarResult(False, message="report-task.txt not found after scanner end")
    http = requests.Session()
    http.auth = (token, "")
    host = s["host_url"].rstrip("/")
    deadline = time.time() + int(s["poll_timeout_minutes"]) * 60
    analysis_id = None
    while time.time() < deadline:
        ce = http.get(f"{host}/api/ce/task", params={"id": task["ceTaskId"]}, timeout=30).json()["task"]
        if ce["status"] == "SUCCESS":
            analysis_id = ce.get("analysisId")
            break
        if ce["status"] in ("FAILED", "CANCELED"):
            return SonarResult(False, message=f"compute engine task {ce['status']}")
        time.sleep(15)
    if not analysis_id:
        return SonarResult(False, message="timed out waiting for SonarCloud analysis")

    gate = http.get(f"{host}/api/qualitygates/project_status", params={"analysisId": analysis_id}, timeout=30).json()
    issues = http.get(f"{host}/api/issues/search", timeout=30, params={
        "componentKeys": s["project_key"], "branch": branch, "resolved": "false",
        "inNewCodePeriod": "true", "severities": ",".join(s["fix_severities"]), "ps": 500,
    }).json().get("issues", [])
    changed = {f.lower() for f in changed_files}
    scoped = [i for i in issues if i.get("component", "").split(":", 1)[-1].lower() in changed]
    return SonarResult(True, gate.get("projectStatus", {}).get("status", "NONE"), scoped)
