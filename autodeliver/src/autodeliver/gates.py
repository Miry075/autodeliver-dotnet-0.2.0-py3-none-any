"""Deterministic gates. Each returns a GateResult; none of them asks an agent anything."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from .dotnet import BuildResult, MutationResult, TestResult
from .git import NumStat
from .globs import match_any, norm, same_path
from .phi import Finding


@dataclass
class GateResult:
    name: str
    passed: bool
    summary: str
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _in(path: str, paths: Iterable[str]) -> bool:
    return any(same_path(path, p) for p in paths)


# --- scope ---------------------------------------------------------------------------------------
def scope(changed: list[str], allowed: list[str], cfg: dict, frozen: list[str] | None = None) -> GateResult:
    """Every changed path must be explicitly allowed, outside deny_paths, and not a frozen file."""
    deny = cfg["scope"]["deny_paths"]
    frozen = frozen or []
    violations: list[str] = []
    for path in changed:
        if match_any(path, deny):
            violations.append(f"{path}: matches deny_paths")
        elif _in(path, frozen):
            violations.append(f"{path}: frozen acceptance test (only the test author may change it)")
        elif not _in(path, allowed):
            violations.append(f"{path}: not in the files owned by this stage")
    return GateResult("scope", not violations,
                      f"{len(changed)} changed, {len(violations)} out of scope", violations)


def dependencies(changed: list[str], csproj_diff: str, cfg: dict) -> GateResult:
    s = cfg["scope"]
    details = [f"{p}: dependency/build file changed" for p in changed if match_any(p, s["dependency_files"])]
    for line in csproj_diff.splitlines():
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        if any(pat in line for pat in s["csproj_dependency_patterns"]):
            details.append(f"csproj dependency line changed: {line[:160].strip()}")
    return GateResult("dependencies", not details,
                      "no dependency changes" if not details else f"{len(details)} dependency change(s)", details)


def diff_budget(stats: list[NumStat], cfg: dict) -> GateResult:
    s = cfg["scope"]
    files = len(stats)
    lines = sum(st.added + st.deleted for st in stats)
    binaries = [st.path for st in stats if st.binary]
    details: list[str] = []
    if files > s["max_changed_files"]:
        details.append(f"{files} files > max {s['max_changed_files']}")
    if lines > s["max_changed_lines"]:
        details.append(f"{lines} changed lines > max {s['max_changed_lines']}")
    details += [f"{b}: binary file" for b in binaries]
    return GateResult("diff_budget", not details, f"{files} files, {lines} lines", details)


# --- build & tests -------------------------------------------------------------------------------
def build(result: BuildResult, baseline_warnings: int | None, cfg: dict) -> GateResult:
    details: list[str] = []
    if not result.ok:
        details.append(f"build failed with {result.errors} error(s)")
    if cfg["dotnet"]["fail_on_new_warnings"] and baseline_warnings is not None and result.warnings > baseline_warnings:
        details.append(f"warnings increased {baseline_warnings} -> {result.warnings}")
    return GateResult("build", not details, f"errors={result.errors} warnings={result.warnings}", details)


def tests(result: TestResult) -> GateResult:
    details = [f"failed: {t}" for t in result.failed_tests[:30]]
    if result.executed == 0:
        details.append("no tests executed")
    return GateResult("tests", result.ok and result.executed > 0,
                      f"executed={result.executed} passed={result.passed} failed={result.failed}", details)


def ac_trace(per_ac: dict[str, TestResult], cfg: dict) -> GateResult:
    minimum = cfg["tests"]["min_tests_per_ac"]
    details: list[str] = []
    for ac_id, res in per_ac.items():
        if res.executed < minimum:
            details.append(f"{ac_id}: {res.executed} test(s) executed, need >= {minimum}")
        elif res.failed:
            details.append(f"{ac_id}: {res.failed} failing")
    return GateResult("ac_trace", not details, f"{len(per_ac)} acceptance criteria traced", details)


# Matches [Trait("AC","AC-1")], [Fact, Trait("AC", "AC-1")] and [TraitAttribute(...)].
_TRAIT = re.compile(r'[\[,]\s*Trait(?:Attribute)?\s*\(\s*"(?P<name>[^"]+)"\s*,\s*"(?P<value>[^"]+)"\s*\)')


def ac_traits_present(test_sources: dict[str, str], ac_ids: list[str], trait_name: str) -> GateResult:
    """Static check before the code exists: every AC id is tagged on at least one test."""
    found: dict[str, int] = {a: 0 for a in ac_ids}
    unknown: list[str] = []
    for path, text in test_sources.items():
        for m in _TRAIT.finditer(text):
            if m.group("name") != trait_name:
                continue
            value = m.group("value")
            if value in found:
                found[value] += 1
            else:
                unknown.append(f"{path}: unknown {trait_name} id '{value}'")
    details = [f"{a}: no [Trait(\"{trait_name}\", \"{a}\")] test" for a, n in found.items() if n == 0] + unknown
    return GateResult("ac_traits", not details, f"{sum(found.values())} AC-tagged test attribute(s)", details)


def mutation(result: MutationResult, cfg: dict) -> GateResult:
    minimum = float(cfg["mutation"]["min_score"])
    if not result.ran:
        return GateResult("mutation", False, "mutation testing did not produce a report", [result.tail[-500:]])
    if result.score is None:
        return GateResult("mutation", True, "no mutants in changed code")
    passed = result.score >= minimum
    return GateResult("mutation", passed, f"score {result.score}% (min {minimum}%)",
                      [] if passed else [f"{result.undetected} surviving mutant(s); strengthen the assertions"])


# --- PHI / security --------------------------------------------------------------------------------
def phi(findings: list[Finding], gitleaks: tuple[bool, str] | None) -> GateResult:
    blocking = [f for f in findings if f.severity == "blocking"]
    details = [f.render() for f in findings]
    ok = not blocking
    if gitleaks is not None:
        clean, message = gitleaks
        details.append(f"gitleaks: {message}")
        ok = ok and clean
    return GateResult("phi_secrets", ok,
                      f"{len(blocking)} blocking, {len(findings) - len(blocking)} warning", details)


def commands(results: list[tuple[str, int, str]]) -> GateResult:
    failed = [f"{name}: exit {code}" for name, code, _ in results if code != 0]
    return GateResult("security_commands", not failed, f"{len(results)} scanner(s)", failed)


# --- plan --------------------------------------------------------------------------------------------
def plan_files(plan: dict, *, tests_only: bool | None = None, cfg: dict) -> list[str]:
    test_globs = cfg["scope"]["test_path_globs"]
    files = {norm(f["path"]) for t in plan.get("tasks", []) for f in t.get("files", [])}
    files |= {norm(t["test_file"]) for t in plan.get("test_plan", [])}
    if tests_only is None:
        return sorted(files)
    return sorted(f for f in files if match_any(f, test_globs) == tests_only)


def baseline(plan: dict) -> dict[str, dict]:
    return {b["ac_id"]: b for b in plan.get("ac_baseline", [])}


def tested_ac_ids(plan: dict) -> list[str]:
    """AC ids that must have trait-tagged tests: every additional AC and every at-risk existing AC."""
    return sorted(a for a, b in baseline(plan).items() if b["kind"] == "additional" or b.get("at_risk"))


def additional_ac_ids(plan: dict) -> list[str]:
    return sorted(a for a, b in baseline(plan).items() if b["kind"] == "additional")


def pinned_existing_ac_ids(plan: dict) -> list[str]:
    return sorted(a for a, b in baseline(plan).items() if b["kind"] == "existing" and b.get("at_risk"))


def plan_shape(plan: dict, ac_ids: list[str], cfg: dict, exists, allow_deferred: bool = False) -> GateResult:
    details: list[str] = []
    s = cfg["scope"]
    # existing-AC baseline first (azure-workitem-flow step 2)
    base = baseline(plan)
    details += [f"{a}: not classified in ac_baseline" for a in ac_ids if a not in base]
    details += [f"ac_baseline references unknown {a}" for a in set(base) - set(ac_ids)]
    if len(base) != len(plan.get("ac_baseline", [])):
        details.append("ac_baseline lists a criterion more than once")
    for a, b in base.items():
        if b["kind"] == "existing" and not b.get("evidence"):
            details.append(f"{a}: classified existing without the code that satisfies it today")
        if b["kind"] == "deferred":
            if not allow_deferred:
                details.append(f"{a}: deferred is only allowed for criteria inherited from a parent work item")
            elif not str(b.get("note", "")).strip():
                details.append(f"{a}: deferred without naming the work item that delivers it (note)")
    if base and not additional_ac_ids(plan):
        details.append("no additional criteria: nothing new to build (all criteria already implemented?)")
    # tests: every additional + at-risk existing AC, every in-scope use case
    covered = {t["ac_id"] for t in plan.get("test_plan", [])}
    details += [f"{a}: no planned test" for a in tested_ac_ids(plan) if a not in covered]
    details += [f"test_plan references unknown {a}" for a in covered - set(ac_ids)]
    use_cases = {u["id"]: u for u in plan.get("use_cases", [])}
    uc_covered = {u for t in plan.get("test_plan", []) for u in t.get("use_case_ids", [])}
    for uid, u in use_cases.items():
        if u["in_scope"] and uid not in uc_covered:
            details.append(f"{uid}: in-scope use case without a planned test")
        if not u["in_scope"] and not u.get("reason", "").strip():
            details.append(f"{uid}: out of scope without a reason")
    details += [f"test_plan references unknown use case {u}" for u in uc_covered - set(use_cases)]
    if not use_cases:
        details.append("no use cases enumerated")
    # options (step 4): 1-3 honest options, the chosen one must cover everything
    options = {o["name"]: o for o in plan.get("options", [])}
    chosen = options.get(plan.get("chosen_option", ""))
    if not chosen:
        details.append(f"chosen_option '{plan.get('chosen_option')}' is not one of the options")
    else:
        if not chosen["covers_all_use_cases"]:
            details.append("chosen option does not cover all in-scope use cases")
        if not chosen["preserves_existing_ac"]:
            details.append("chosen option does not preserve the existing acceptance criteria")
    if len(options) == 1 and not plan.get("single_option_reason", "").strip():
        details.append("a single option needs single_option_reason")
    if not plan.get("what", "").strip() or not plan.get("why", "").strip():
        details.append("what/why missing")
    # files
    for t in plan.get("test_plan", []):
        if not match_any(t["test_file"], s["test_path_globs"]):
            details.append(f"{t['test_file']}: planned test file is not under test_path_globs")
    for task in plan.get("tasks", []):
        for f in task.get("files", []):
            path, change = norm(f["path"]), f["change"]
            if match_any(path, s["deny_paths"]):
                details.append(f"{path}: planned change in deny_paths")
            if match_any(path, s["dependency_files"]):
                details.append(f"{path}: planned dependency/build file change")
            if change == "modify" and not exists(path):
                details.append(f"{path}: planned 'modify' but file does not exist")
            if change == "create" and exists(path):
                details.append(f"{path}: planned 'create' but file already exists")
    n_files = len(plan_files(plan, cfg=cfg))
    if n_files > s["max_changed_files"]:
        details.append(f"plan touches {n_files} files > max {s['max_changed_files']}")
    if len(plan.get("tasks", [])) > cfg["eligibility"]["max_estimated_tasks"]:
        details.append(f"{len(plan['tasks'])} tasks > max {cfg['eligibility']['max_estimated_tasks']}")
    if not plan.get("citations"):
        details.append("no code citations: plan is not grounded in the repository")
    return GateResult("plan_shape", not details, f"{n_files} planned files, {len(use_cases)} use cases", details)


def classification_agreement(plan: dict, check: dict, declared: dict[str, str] | None = None) -> GateResult:
    """Replaces the human confirmation of the existing-AC baseline: two independent classifications must match.
    When the repository has a reviewed spec, its declared split (Existing vs Acceptance criteria) is a third
    vote that was confirmed by a human; all three must agree."""
    mine = {a: b["kind"] for a, b in baseline(plan).items()}
    theirs = {c["ac_id"]: c["kind"] for c in check.get("ac_classification", [])}
    details = [f"{a}: plan says {mine.get(a, '-')}, independent check says {theirs.get(a, '-')}"
               for a in sorted(set(mine) | set(theirs)) if mine.get(a) != theirs.get(a)]
    for a, want in sorted((declared or {}).items()):
        if mine.get(a) == theirs.get(a) and mine.get(a) != want:
            details.append(f"{a}: the reviewed spec declares it {want}, but plan and independent check both say {mine.get(a, '-')}")
        elif mine.get(a) != theirs.get(a) and want not in (mine.get(a), theirs.get(a)):
            details.append(f"{a}: the reviewed spec declares it {want}")
    note = f", {len(declared)} declared by the spec" if declared else ""
    return GateResult("ac_classification", not details, f"{len(mine)} criteria classified{note}", details)


def _project(path: str) -> str:
    """The project a file belongs to: the path up to the first folder holding a .csproj-like name, else the first two
    segments (src/Lalana.Connect.Core/Invoicing/X.cs -> src/lalana.connect.core)."""
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def plan_agreement(planner: list[str], checker: list[str], threshold: float,
                   exists: Callable[[str], bool] | None = None) -> GateResult:
    """Do both models touch the same files? A file that does not exist yet counts as the same file when both create
    it under the same name in the same project: the folder of a new file is a placement choice, not a disagreement
    on what to build. Placement differences are reported so the reviewer can check the convention."""
    a = {norm(p).lower() for p in planner}
    b = {norm(p).lower() for p in checker}
    exists = exists or (lambda p: True)
    new_b = {(_project(p), p.rsplit("/", 1)[-1]): p for p in b - a if not exists(p)}
    moved: list[tuple[str, str]] = []
    for p in sorted(a - b):
        key = (_project(p), p.rsplit("/", 1)[-1])
        if not exists(p) and key in new_b:
            moved.append((p, new_b.pop(key)))
    matched = len(a & b) + len(moved)
    union = len(a | b) - len(moved)
    overlap = matched / union if union else 1.0
    passed = overlap >= threshold
    only_a = sorted((a - b) - {m[0] for m in moved})
    only_b = sorted((b - a) - {m[1] for m in moved})
    details = [f"same new file, different folder: plan {x} / check {y}" for x, y in moved]
    if not passed:
        details += [f"only in plan: {only_a}", f"only in independent check: {only_b}"]
    return GateResult("plan_agreement", passed, f"file overlap {overlap:.2f} (min {threshold})", details)


# --- review --------------------------------------------------------------------------------------------
SWEEP_KEYS = ["phi_in_logs", "audit_trail", "retention", "data_boundary", "authz"]


def review(verdict: dict, plan: dict, security_ids: list[str]) -> GateResult:
    details: list[str] = []
    base = baseline(plan)
    existing = sorted(a for a, b in base.items() if b["kind"] == "existing")
    held = {a["ac_id"]: a for a in verdict.get("existing_ac_assessment", [])}
    for a in existing:                                       # regression baseline first
        if a not in held:
            details.append(f"{a} (existing): not assessed by reviewer")
        elif not held[a]["holds"]:
            details.append(f"{a} (existing): REGRESSED: {held[a].get('evidence', '')}")
    assessed = {a["ac_id"]: a for a in verdict.get("ac_assessment", [])}
    for a in additional_ac_ids(plan) + list(security_ids):
        if a not in assessed:
            details.append(f"{a}: not assessed by reviewer")
        elif not assessed[a]["met"]:
            details.append(f"{a}: reviewer says not met: {assessed[a].get('evidence', '')}")
    uc_status = {u["uc_id"]: u for u in verdict.get("use_case_assessment", [])}
    for u in plan.get("use_cases", []):
        if not u["in_scope"]:
            continue
        got = uc_status.get(u["id"])
        if got is None:
            details.append(f"{u['id']}: use case not assessed")
        elif got["status"] != "met":
            details.append(f"{u['id']}: use case {got['status']}: {got.get('evidence', '')}")
    for f in verdict.get("findings", []):
        if f["severity"] in ("blocker", "major"):
            details.append(f"{f['severity']}: {f.get('file', '?')}:{f.get('line', '?')} {f['issue']}")
    sweep = verdict.get("health_sweep", {})
    for key in SWEEP_KEYS:
        item = sweep.get(key)
        if item is None:
            details.append(f"health sweep '{key}' not reported")
        elif item["status"] == "issue":
            details.append(f"health sweep '{key}': {item.get('note', '')}")
    return GateResult("review", not details, f"verdict {verdict.get('verdict')}", details)


def frozen_integrity(current: dict[str, str | None], frozen: dict[str, str]) -> GateResult:
    """current: path -> sha256 now (None = missing). Any difference fails; the pipeline restores the copy."""
    details = [f"{p}: {'deleted' if current.get(p) is None else 'modified'} (restored)"
               for p, h in sorted(frozen.items()) if current.get(p) != h]
    return GateResult("frozen_tests", not details, f"{len(frozen)} frozen acceptance test file(s)", details)


def summarise(results: list[GateResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"- [{'PASS' if r.passed else 'FAIL'}] {r.name}: {r.summary}")
        lines += [f"    - {d}" for d in r.details[:20]]
    return "\n".join(lines)


# --- weighing open points (triage extras/blockers, checker blockers) --------------------------------------
IMPACT = {"negligible": 0, "minor": 1, "material": 2}


def weigh(points: list[dict[str, Any]], cfg: dict, *, ceiling: str | None = None) -> tuple[list[dict], list[dict]]:
    """Split open points into (proceed, stop). A point is proceeded on only when every rule allows it: impact at
    or below the configured ceiling, reversible, a concrete default, and no always-escalate topic. Too many small
    points together are one big gap, so above max_assumptions everything stops. The model's own weight is never
    enough on its own: the plan checker reviews every proceeded point again later."""
    a = cfg["analysis"]
    limit = ceiling or str(a["proceed_up_to"])
    topics = [str(t) for t in a.get("always_escalate", [])]
    proceed: list[dict] = []
    stop: list[dict] = []
    for p in points:
        text = " ".join(str(p.get(k, "")) for k in ("question", "why", "text", "default", "impact_reason"))
        why: list[str] = []
        impact = str(p.get("impact", "material"))
        if limit == "none":
            why.append("weighing is switched off")
        elif IMPACT.get(impact, 2) > IMPACT[limit]:
            why.append(f"impact {impact}")
        if not p.get("reversible"):
            why.append("not reversible")
        if not str(p.get("default", "")).strip():
            why.append("no default")
        hit = [t for t in topics if re.search(rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])", text, re.IGNORECASE)]
        if hit:
            why.append(f"mentions {', '.join(hit)}")
        (stop if why else proceed).append({**p, "stop_reasons": why})
    if len(proceed) > int(a["max_assumptions"]):
        stop += [{**p, "stop_reasons": [f"{len(proceed)} open points exceed analysis.max_assumptions={a['max_assumptions']}"]}
                 for p in proceed]
        proceed = []
    return proceed, stop


def describe_point(p: dict[str, Any]) -> str:
    base = p.get("question") or p.get("text", "")
    weight = f"[{p.get('impact', '?')}{', reversible' if p.get('reversible') else ''}]"
    extra = f" Default: {p['default']}." if str(p.get("default", "")).strip() else ""
    stop = f" Stopped: {'; '.join(p['stop_reasons'])}." if p.get("stop_reasons") else ""
    return f"{base} {weight}{extra}{stop}".strip()
