"""Human-facing output: PR evidence pack (markdown) and work item comments (HTML; ADO does not render markdown).

The evidence pack follows azure-workitem-flow step 6's reporting order: existing AC first, then additional AC,
use cases, what was done against the chosen option, gates, health sweep, design principles, test coverage,
risks, what is still needed, and the human-equivalent effort.
"""
from __future__ import annotations

import html
import os

from .gates import SWEEP_KEYS, baseline
from .state import RunState


def run_url() -> str:
    base, project, build = (os.environ.get(k) for k in ("SYSTEM_COLLECTIONURI", "SYSTEM_TEAMPROJECT", "BUILD_BUILDID"))
    return f"{base.rstrip('/')}/{project}/_build/results?buildId={build}" if base and project and build else "(local run)"


def gate_table(results: list[dict]) -> str:
    rows = ["| Gate | Result | Summary |", "|---|---|---|"]
    rows += [f"| {r['name']} | {'PASS' if r['passed'] else 'FAIL'} | {r['summary']} |" for r in results]
    return "\n".join(rows)


def _cell(text: str, limit: int = 140) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evidence_pack(state: RunState) -> str:
    plan = state.plan or {}
    review = state.review or {}
    base = baseline(plan)
    tests_for: dict[str, list[str]] = {}
    for t in plan.get("test_plan", []):
        tests_for.setdefault(t["ac_id"], []).append(f"`{t['test_name']}`")
    held = {a["ac_id"]: a for a in review.get("existing_ac_assessment", [])}
    met = {a["ac_id"]: a for a in review.get("ac_assessment", [])}
    ucs = {u["uc_id"]: u for u in review.get("use_case_assessment", [])}
    lines = [
        "## Autodeliver evidence pack",
        f"Work item AB#{state.work_item_id} · branch `{state.branch}` · base `{(state.base_sha or '')[:10]}` · "
        f"agent cost ${state.cost_usd:.2f} · run {run_url()}",
        "",
        "### 1. Existing acceptance criteria (regression baseline)",
        "| AC | Criterion | Satisfied today by | Pinned by | Still holds |",
        "|---|---|---|---|---|",
    ]
    existing = [a for a in state.acs if base.get(a["id"], {}).get("kind") == "existing"]
    for ac in existing:
        b, h = base[ac["id"]], held.get(ac["id"], {})
        lines.append(f"| {ac['id']} | {_cell(ac['text'])} | {_cell(', '.join(b.get('evidence', [])), 80)} | "
                     f"{', '.join(tests_for.get(ac['id'], [])) or '—'} | {'yes' if h.get('holds') else 'NO' if h else 'n/a'} |")
    if not existing:
        lines.append("| — | none: net-new implementation | | | |")
    lines += ["", "### 2. Additional acceptance criteria", "| AC | Criterion | Tests | Reviewer |", "|---|---|---|---|"]
    for ac in state.acs:
        if base.get(ac["id"], {}).get("kind") != "additional":
            continue
        m = met.get(ac["id"], {})
        lines.append(f"| {ac['id']} | {_cell(ac['text'])} | {', '.join(tests_for.get(ac['id'], [])) or '—'} | "
                     f"{'met' if m.get('met') else 'NOT MET' if m else 'n/a'} |")
    deferred = [a for a in state.acs if base.get(a["id"], {}).get("kind") == "deferred"]
    if deferred:
        lines += ["", "### Deferred to other work items (criteria inherited from the parent story)", "| AC | Criterion | Delivered by |", "|---|---|---|"]
        lines += [f"| {a['id']} | {_cell(a['text'])} | {_cell(base[a['id']].get('note', ''), 80)} |" for a in deferred]
    if state.security:
        lines += ["", "### Security criteria", "| SC | Criterion | Reviewer | Evidence |", "|---|---|---|---|"]
        for sc in state.security:
            m = met.get(sc["id"], {})
            lines.append(f"| {sc['id']} | {_cell(sc['text'])} | {'met' if m.get('met') else 'NOT MET' if m else 'n/a'} | "
                         f"{_cell(m.get('evidence', ''), 80)} |")
    lines += ["", "### 3. Use cases", "| UC | Kind | Use case | Result |", "|---|---|---|---|"]
    for u in plan.get("use_cases", []):
        result = ucs.get(u["id"], {}).get("status", "n/a") if u["in_scope"] else f"out of scope: {u.get('reason', '')}"
        lines.append(f"| {u['id']} | {u['kind']} | {_cell(u['description'])} | {_cell(result, 80)} |")
    lines += ["", "### 4. What and why", f"**What:** {plan.get('what', '')}", "", f"**Why:** {plan.get('why', '')}", "",
              f"**Chosen option:** {plan.get('chosen_option', '')}. {plan.get('chosen_rationale', '')}"]
    others = [o for o in plan.get("options", []) if o["name"] != plan.get("chosen_option")]
    lines += [f"- Not chosen: {o['name']}: {_cell(o['trade_off'], 200)}" for o in others]
    if len(plan.get("options", [])) == 1:
        lines.append(f"- Single option: {plan.get('single_option_reason', '')}")
    lines += ["", "### 5. Gates (deterministic, run by code)", gate_table(state.gate_results), ""]
    lines += ["### 6. Health-sector sweep (reviewer)", "| Check | Status | Note |", "|---|---|---|"]
    sweep = review.get("health_sweep", {})
    for key in SWEEP_KEYS:
        item = sweep.get(key, {})
        lines.append(f"| {key} | {item.get('status', 'n/a')} | {_cell(item.get('note', ''), 160)} |")
    if review.get("design_principles"):
        lines += ["", "### 7. Design principles that applied"]
        lines += [f"- **{p['principle']}**: {p['how']}" for p in review["design_principles"]]
    if review.get("test_coverage") or review.get("untested"):
        lines += ["", "### 8. Test coverage"]
        lines += [f"- {c['item']} ({c['layer']}): {', '.join(c['tests'])}" for c in review.get("test_coverage", [])]
        lines += [f"- Not tested: {u['item']}: {u['reason']}" for u in review.get("untested", [])]
    minor = [f for f in review.get("findings", []) if f["severity"] in ("minor", "nit")]
    if minor:
        lines += ["", "### Non-blocking review findings"]
        lines += [f"- {f['severity']}: `{f.get('file', '?')}:{f.get('line', '?')}` {f['issue']}" for f in minor[:20]]
    if review.get("risks"):
        lines += ["", "### 9. Risks and side effects"] + [f"- {r}" for r in review["risks"]]
    needed = list(review.get("still_needed", [])) + [f"follow-up: {f}" for f in plan.get("followups", [])]
    if needed:
        lines += ["", "### 10. Still needed"] + [f"- {n}" for n in needed]
    if state.assumptions:
        lines += ["", "### Decisions taken without asking (weighed as small; verify and change if wrong)"] + [
            f"- **{a['id']}** [{a['impact']}] {a['question']}\n  - default implemented: {a['default']}\n"
            f"  - why it was small: {a['impact_reason']}\n  - second opinion: {a.get('review') or 'n/a'}"
            for a in state.assumptions]
    if plan.get("assumptions"):
        lines += ["", "### Assumptions made by the agents (verify these)"] + [f"- {a}" for a in plan["assumptions"]]
    res = state.resolution or {}
    if res:
        b = res.get("breakdown", {})
        lines += ["", "### 11. Completed hours (human-equivalent estimate)",
                  f"{res.get('hours', 0)}h = reading {b.get('reading', 0)} + implementing {b.get('implementing', 0)} + "
                  f"testing {b.get('testing', 0)} + self-review {b.get('self_review', 0)}"]
        if "System.State" in res.get("fields", {}):
            lines.append(f"Work item moves to **{res['fields']['System.State']}** (dev-done, not verified; never Closed).")
        lines += [f"- {n}" for n in res.get("notes", [])]
    if state.status_changes:
        lines += ["", "### Board"] + [
            f"- AB#{c['id']} ({c['type']}) {c['from']} -> {c['to']} [{c.get('phase', '')}]"
            + ("" if c.get("done", True) else " (rejected by the process)") for c in state.status_changes]
    lines += ["", "### Loops used", ", ".join(f"{k}={v}" for k, v in state.loops.items()),
              "", "> Generated by autodeliver. A human must review and approve; the pipeline cannot merge."]
    return "\n".join(lines)


def escalation_html(state: RunState, stage: str, reason: str, questions: list[str]) -> str:
    e = html.escape
    parts = [f"<b>[autodeliver] stopped at {e(stage)}</b><br>", f"{e(reason)}<br>"]
    if questions:
        parts.append("Needs a human decision:<ul>" + "".join(f"<li>{e(q)}</li>" for q in questions[:10]) + "</ul>")
    parts.append(f"Loops: {e(', '.join(f'{k}={v}' for k, v in state.loops.items()))} · "
                 f"cost ${state.cost_usd:.2f} · run {e(run_url())}<br>")
    parts.append("Nothing was pushed. To take over by hand, azure-workitem-flow resumes from the notes in the run "
                 "artifact (<i>notes.md</i>). Remove the <i>autodeliver-blocked</i> tag after resolving to allow a new run.")
    return "".join(parts)


def success_html(state: RunState) -> str:
    e = html.escape
    res = state.resolution or {}
    written = res.get("written", {})
    b = res.get("breakdown", {})
    lines = [f"<b>[autodeliver] draft PR ready for human review</b><br>",
             f"<a href=\"{e(state.pr_url or '')}\">{e(state.pr_url or '')}</a><br>",
             f"All gates passed; evidence pack is on the PR. Agent cost ${state.cost_usd:.2f}.<br>"]
    if "Microsoft.VSTS.Scheduling.CompletedWork" in written:
        lines.append(f"Completed Work +{res['hours']}h (reading {b.get('reading', 0)}, implementing {b.get('implementing', 0)}, "
                     f"testing {b.get('testing', 0)}, self-review {b.get('self_review', 0)}); new total "
                     f"{written['Microsoft.VSTS.Scheduling.CompletedWork']}h. Correct it if it is off.<br>")
    if "System.State" in written:
        lines.append(f"State: {e(written['System.State'])} (dev-done, not verified). Closing is the reviewer's or tester's call.<br>")
    if state.assumptions:
        lines.append("Decisions taken without asking (check them in the PR): "
                     + "; ".join(f"{e(a['id'])} {e(a['default'][:160])}" for a in state.assumptions) + "<br>")
    for note in res.get("notes", []):
        lines.append(f"Note: {e(note)}<br>")
    return "".join(lines)
