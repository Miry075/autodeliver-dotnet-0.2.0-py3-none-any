"""Run state, persisted after every stage as an audit record (.work/autodeliver/<id>/state.json)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Escalation(Exception):
    """Stops the run. Carries what the human needs: stage, reason, open questions."""

    def __init__(self, stage: str, reason: str, questions: list[str] | None = None, report: str | None = None):
        super().__init__(f"[{stage}] {reason}")
        self.stage = stage
        self.reason = reason
        self.questions = questions or []
        self.report = report


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunState:
    work_item_id: int
    status: str = "running"            # running | escalated | succeeded
    stage: str = "init"
    loops: dict[str, int] = field(default_factory=lambda: {"plan": 0, "tests": 0, "dev": 0, "review": 0, "quality": 0})
    cost_usd: float = 0.0
    branch: str | None = None
    base_sha: str | None = None
    baseline_warnings: int | None = None
    acs: list[dict[str, str]] = field(default_factory=list)          # AC-n from the Acceptance Criteria field
    security: list[dict[str, str]] = field(default_factory=list)     # SC-n from the Security Criteria field
    inherited: dict[str, Any] | None = None                          # parent work item the criteria were taken from
    spec: dict[str, Any] | None = None                               # committed docs/specs/<id>.md, when the repo has one
    tracker_criteria: list[str] = field(default_factory=list)        # tracker criteria shown to triage when a spec took over
    code_map: dict[str, Any] | None = None
    triage: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    check: dict[str, Any] | None = None
    frozen_test_files: list[str] = field(default_factory=list)
    frozen_hashes: dict[str, str] = field(default_factory=dict)      # tests stay in the working tree until the one commit
    effort: dict[str, Any] | None = None                             # reviewer's human-equivalent estimate
    resolution: dict[str, Any] | None = None                         # what step 8 wrote
    gate_results: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] | None = None
    mutation_score: float | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    events: list[dict[str, str]] = field(default_factory=list)

    def log(self, stage: str, message: str) -> None:
        self.events.append({"at": now(), "stage": stage, "message": message})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "RunState":
        return cls(**json.loads(path.read_text(encoding="utf-8")))
