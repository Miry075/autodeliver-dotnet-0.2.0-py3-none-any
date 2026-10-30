"""The state machine. Agents do the work inside a stage; this code alone decides whether a stage passed.

Stages follow the azure-workitem-flow conventions, with every human pause replaced by a deterministic gate
or an independent second opinion, and any doubt turned into an escalation:

  fetch (fields, comments, security criteria, linked items)  -> eligibility -> prepare (branch, green base)
  -> code map (haiku) -> triage -> analysis & plan (existing-AC baseline first, what/why, use cases, options)
     + independent check (classification and files must agree)
  -> acceptance tests (red first for additional AC, green pins for at-risk existing AC, then frozen)
  -> develop (+ all gates) -> independent review (existing AC first, additional AC, use cases, sweep)
  -> [sonar] -> one commit (code + tests, AB#id, why-body) -> draft PR -> completed hours + dev-done state
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable

from . import gates, report, spec as specmod, workflow
from .ado import COMPLETED, REMAINING, extract_criteria
from .agents import BASH_DOTNET, AgentError, AgentRunner, ToolPolicy
from .dotnet import Dotnet
from .gates import GateResult
from .git import Git
from .globs import match_any, norm, same_path
from .phi import AddedLine, PhiScanner, added_lines, run_gitleaks
from .state import Escalation, RunState
from .tools import find_gitleaks


def _scrubbed_env() -> dict[str, str]:
    import os
    from .dotnet import SECRET_ENV
    return {k: v for k, v in os.environ.items() if k not in SECRET_ENV}


class NotEligible(Exception):
    """Work item never opted in: exit quietly, write nothing to it."""


def slugify(text: str, max_len: int) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "change"


def sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def quarter_hours(value: float) -> float:
    return round(round(float(value) * 4) / 4, 2)


class Pipeline:
    def __init__(self, cfg: dict, repo: Path, work_item_id: int, ado: Any, agents: AgentRunner, *,
                 dry_run: bool, git: Git | None = None, dotnet_factory: Callable[[], Dotnet] | None = None,
                 sonar_fn: Callable[..., Any] | None = None, keep_branch: bool = False):
        self.keep_branch = keep_branch
        self.cfg = cfg
        self.repo = Path(repo)
        self.ado = ado
        self.agents = agents
        self.dry_run = dry_run
        self.git = git or Git(self.repo)
        self._dotnet_factory = dotnet_factory or (lambda: Dotnet(self.repo, cfg))
        self._sonar_fn = sonar_fn
        self.dotnet: Dotnet | None = None
        self.phi = PhiScanner(cfg)
        self.work = self.repo / ".work" / "autodeliver" / str(work_item_id)
        self.state = RunState(work_item_id)
        self.wi = None
        self.base = ""

    # ===== plumbing ====================================================================================
    def save(self) -> None:
        self.state.save(self.work / "state.json")

    def _write(self, name: str, content: str | dict | list) -> Path:
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2), encoding="utf-8")
        return path

    def _enter(self, stage: str) -> None:
        self.state.stage = stage
        self.state.log(stage, "start")
        self.save()

    def _record(self, results: list[GateResult], name: str) -> bool:
        self.state.gate_results = [r.as_dict() for r in results]
        self._write(f"gates/{name}.md", gates.summarise(results))
        self.save()
        return all(r.passed for r in results)

    def _agent(self, role: str, model_key: str, variables: dict, policy: ToolPolicy, attempt: int) -> dict:
        remaining = float(self.cfg["budget"]["max_total_usd"]) - self.state.cost_usd
        if remaining < 1.0:
            raise Escalation(self.state.stage, f"run budget exhausted (${self.state.cost_usd:.2f} spent)")
        try:
            result = self.agents.run(role, model_key, variables, policy, remaining, attempt)
        except AgentError as e:
            self.state.cost_usd += e.cost_usd
            raise
        self.state.cost_usd += result.cost_usd
        self.state.log(self.state.stage, f"{role}#{attempt} turns={result.turns} cost=${result.cost_usd:.2f}"
                                         f" denials={len(result.denials)}")
        self._write(f"agents/{role}-{attempt}.json", result.output)
        self.save()
        return result.output

    def _readonly(self) -> ToolPolicy:
        return ToolPolicy(self.repo, deny_paths=self.cfg["scope"]["deny_paths"])

    def _writer(self, paths: list[str], frozen: list[str] | None = None) -> ToolPolicy:
        return ToolPolicy(self.repo, write_paths=paths, deny_paths=self.cfg["scope"]["deny_paths"],
                          frozen=frozen or [], bash_allow=BASH_DOTNET)

    def _conventions(self) -> list[str]:
        return [f for f in self.cfg.get("conventions_files", []) if (self.repo / f).is_file()]

    def _ac_ids(self) -> list[str]:
        return [a["id"] for a in self.state.acs]

    def _sc_ids(self) -> list[str]:
        return [s["id"] for s in self.state.security]

    def _work_item_vars(self) -> dict:
        wi = self.wi
        description = wi.description_text or "(empty)"
        source = wi.criteria_source
        if self.state.spec:
            description += (f"\n\n--- Committed spec {self.state.spec['path']} (reviewed in a pull request; its Non-goals are out of scope) ---\n"
                            + self.state.spec["text"])
            if self.state.tracker_criteria:
                description += ("\n\n--- Acceptance criteria as written in the tracker (compare with the spec's; report any "
                                "requirement that only appears here) ---\n" + "\n".join(f"- {t}" for t in self.state.tracker_criteria))
            if any(a.get("source") == "spec" for a in self.state.acs):
                source = f"the committed spec {self.state.spec['path']}"
        if self.state.inherited:
            inh = self.state.inherited
            description += (f"\n\n--- Acceptance criteria inherited from the parent {inh['type']} #{inh['id']} '{inh['title']}' ---\n"
                            "This work item is only part of that story. The scope of THIS change is the title and description above; "
                            "a criterion that another work item of the story delivers is `deferred`, not part of this change.\n"
                            + inh["description"])
            if inh["siblings"]:
                description += "\n\nOther work items of that story:\n" + "\n".join(
                    f"- #{s['id']} {s['type']}: {s['title']} [{s['state']}]" for s in inh["siblings"])
            deferred = [f"{b['ac_id']}: {b.get('note', '')}" for b in (self.state.plan or {}).get("ac_baseline", []) if b["kind"] == "deferred"]
            if deferred:
                description += "\n\nCriteria deferred to other work items (not part of this change):\n" + "\n".join(f"- {d}" for d in deferred)
            source = f"the parent {inh['type']} #{inh['id']} ({inh['criteria_source']})"
        if self.state.assumptions:
            description += ("\n\n--- Decisions taken in place of a human answer (weighed as small; implement the default) ---\n"
                            + "\n".join(f"- {a['id']} [{a['impact']}]: {a['question']} -> default: {a['default']}"
                                         for a in self.state.assumptions))
        return {"work_item_id": str(self.state.work_item_id), "work_item_type": wi.type, "title": wi.title,
                "description": description,
                "acceptance_criteria": [{k: v for k, v in a.items() if k != "declared"} for a in self.state.acs],
                "criteria_source": source,
                "security_criteria": self.state.security or "(none)",
                "comments": wi.comments or "(none)", "linked": wi.linked or "(none)",
                "conventions": self._conventions() or ["(none found; follow the surrounding code)"]}

    def _ac_baseline_vars(self) -> list[dict]:
        base = gates.baseline(self.state.plan)
        return [{**ac, "kind": base[ac["id"]]["kind"], "at_risk": base[ac["id"]].get("at_risk", False),
                 "evidence": base[ac["id"]].get("evidence", [])} for ac in self.state.acs
                if base[ac["id"]]["kind"] != "deferred"]            # deferred criteria are another work item's job

    def _out_of_scope(self, changed: list[str], allowed: list[str]) -> list[str]:
        deny = self.cfg["scope"]["deny_paths"]
        return [p for p in changed if match_any(p, deny) or not any(same_path(p, a) for a in allowed)]

    # ===== entry point =================================================================================
    def run(self) -> int:
        try:
            self.eligibility()
            self.prepare()
            self.code_map()
            self.triage()
            self.plan()
            self.start_work()
            self.acceptance_tests()
            self.develop()
            self.review()
            if self.cfg["sonar"].get("enabled"):
                self.quality()
            self.commit()
            self.publish()
            self.state.status = "succeeded"
            self.state.log("done", "draft PR published" if not self.dry_run else "dry run complete")
            return 0
        except NotEligible as e:
            self.state.status = "not_eligible"
            self.state.log("eligibility", str(e))
            return 1
        except Escalation as e:
            self.escalate(e)
            return 2
        except AgentError as e:
            self.escalate(Escalation(self.state.stage, f"agent failure ({e.role}): {e.reason}"))
            return 2
        except Exception as e:  # noqa: BLE001 - anything unexpected must still stop safely and report
            self.escalate(Escalation(self.state.stage, f"internal error: {type(e).__name__}: {str(e)[:300]}"))
            return 3
        finally:
            self._dry_run_cleanup()
            self.save()

    def _dry_run_cleanup(self) -> None:
        """A dry run leaves nothing behind in the checkout: what the agents produced is kept as a patch in the run
        record, then the repository goes back to where it was. The branch is never deleted (autodeliver deletes no branch,
        ever); the next dry run simply takes a new `-dryrun-N` name. `--keep-branch` also stays on the branch."""
        s = self.state
        if not self.dry_run or self.keep_branch or not s.branch or not s.restore_ref:
            return
        try:
            tip = s.commit_sha
            patch = self.git.diff_against(s.base_sha, tip) if tip else self.git.diff_against(s.base_sha)
            new_files = [] if tip else self.git.untracked()
            for rel in new_files:                                   # files the agents created and did not commit
                target = self.work / "dry-run-files" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.repo / rel, target)
            if patch.strip() or new_files:
                self._write("dry-run.patch", patch)
            self.git.switch_back(s.restore_ref)
            s.log("cleanup", f"dry run: back on {s.restore_ref[:12]}, branch {s.branch} kept (never deleted); "
                             + (f"changes kept in dry-run.patch" + (f" and {len(new_files)} new file(s) in dry-run-files/" if new_files else "")
                                if patch.strip() or new_files else "no changes were made"))
        except Exception as e:  # noqa: BLE001 - cleanup must never hide the result of the run
            s.log("cleanup", f"dry run cleanup failed ({str(e)[:200]}): the branch {s.branch} was kept")

    # ===== fetch + eligibility =========================================================================
    def eligibility(self) -> None:
        self._enter("eligibility")
        el = self.cfg["eligibility"]
        self.wi = self.ado.get_work_item(self.state.work_item_id)
        tags = {t.lower() for t in self.wi.tags}
        if el["required_tag"].lower() not in tags:
            raise NotEligible(f"tag '{el['required_tag']}' missing")
        if self.cfg["on_escalate"]["add_tag"].lower() in tags:
            raise NotEligible(f"tag '{self.cfg['on_escalate']['add_tag']}' present: a human must clear it first")
        problems = []
        if self.wi.type not in el["work_item_types"]:
            problems.append(f"type '{self.wi.type}' not in {el['work_item_types']}")
        if self.wi.state not in el["allowed_states"]:
            problems.append(f"state '{self.wi.state}' not in {el['allowed_states']}")
        tracker = extract_criteria(self.wi.acceptance_html, "AC")
        tracker_security = extract_criteria(self.wi.security_html, "SC")
        self.state.acs, self.state.security = tracker, tracker_security
        self.state.log("eligibility", f"{len(tracker)} acceptance criteria read from {self.wi.criteria_source}")
        committed = self._committed_spec()
        if committed is not None:
            problems += self._adopt_spec(committed, tracker, tracker_security)
        elif self.cfg["spec"]["mode"] == "required":
            problems.append(f"no committed spec at {self.cfg['spec']['path'].format(id=self.state.work_item_id)}")
        if committed is None and not self.state.acs and self.cfg["azure_devops"].get("inherit_criteria", "none") == "parent":
            self._inherit_from_parent()
        if committed is None and len(self.state.acs) < el["min_acceptance_criteria"]:
            section = self.cfg["azure_devops"].get("criteria_section", "")
            problems.append(f"no usable acceptance criteria in the Acceptance Criteria field"
                            + (f" or the '{section}' section of the Description" if section else ""))
        if problems:
            raise Escalation("eligibility", "work item is not eligible for autonomous delivery", problems)

    # ===== criteria inherited from the parent story (Epic > Issue/Story > Task) ============================
    def _inherit_from_parent(self) -> None:
        """A Task carries no criteria of its own: they sit on the story it belongs to. Walks up until a work item
        has some. The Task's own title and description stay the scope of this change."""
        depth = int(self.cfg["azure_devops"].get("inherit_max_depth", 3))
        current = self.wi
        for _ in range(depth):
            if not current.parent_id:
                return
            parent = self.ado.get_work_item(current.parent_id)
            found = extract_criteria(parent.acceptance_html, "AC")
            if found:
                own = str(self.state.work_item_id)
                siblings = [{"id": l["id"], "type": l["type"], "title": l["title"], "state": l["state"]}
                            for l in parent.linked if l["relation"] == "child" and l["id"] != own]
                self.state.acs = [{**c, "source": f"parent #{parent.id}"} for c in found]
                if not self.state.security:
                    self.state.security = extract_criteria(parent.security_html, "SC")
                self.state.inherited = {"id": parent.id, "type": parent.type, "title": parent.title,
                                        "description": parent.description_text, "siblings": siblings,
                                        "criteria_source": parent.criteria_source}
                self.state.log("eligibility", f"{len(found)} acceptance criteria inherited from the parent {parent.type} "
                                              f"#{parent.id} ({parent.criteria_source}); {len(siblings)} sibling work items")
                return
            current = parent

    # ===== the committed spec (agentic-SDLC convention) =================================================
    def _committed_spec(self) -> "specmod.Spec | None":
        sc = self.cfg["spec"]
        if sc["mode"] == "off":
            return None
        path = sc["path"].format(id=self.state.work_item_id)
        target = self.cfg["azure_devops"]["target_branch"]
        try:
            self.git.fetch(target)                                      # read-only; the spec is read as committed on the base
        except Exception as e:  # noqa: BLE001 - offline machines still read the local ref
            self.state.log("eligibility", f"could not fetch {target}: {str(e)[:120]}")
        raw = self.git.show_file(self.git.base_ref(target), path)
        return None if raw is None else specmod.parse_spec(raw, path)

    def _adopt_spec(self, sp: "specmod.Spec", tracker: list[dict], tracker_security: list[dict]) -> list[str]:
        """The spec replaces the tracker's criteria; what the tracker says on top is shown to triage to compare."""
        sc = self.cfg["spec"]
        acs: list[dict] = []
        for declared, texts in (("existing", sp.existing), ("additional", sp.additional)):
            for text in texts:
                acs.append({"id": f"AC-{len(acs) + 1}", "text": text, "source": "spec", "declared": declared})
        norm_text = lambda t: " ".join(t.lower().split())
        sec = [c["text"] for c in tracker_security]
        sec += [t for t in sp.security if norm_text(t) not in {norm_text(x) for x in sec}]
        extra = [c["text"] for c in tracker if not specmod.is_pointer(c["text"], sp.path)]
        self.state.tracker_criteria = extra
        self.state.spec = sp.summary()
        if acs:
            self.state.acs = acs
        self.state.security = [{"id": f"SC-{n}", "text": t, "source": "security"} for n, t in enumerate(sec, 1)]
        self.state.log("eligibility", f"spec {sp.path}: {len(sp.existing)} existing and {len(sp.additional)} additional criteria, "
                                      f"{len(sp.security)} security, risk tier {sp.risk_tier or 'not set'}"
                                      + (f"; {len(extra)} tracker criteria shown to triage for comparison" if extra else ""))
        return specmod.problems(sp, sc["max_risk_tier"], bool(sc["block_on_open_questions"]))

    def _declared(self) -> dict[str, str]:
        return {a["id"]: a["declared"] for a in self.state.acs if a.get("declared")}

    # ===== step 1: branch ==============================================================================
    def prepare(self) -> None:
        self._enter("prepare")
        ado = self.cfg["azure_devops"]
        self.git.ensure_work_dir_excluded()
        if not self.git.is_clean():
            raise Escalation("prepare", "working tree is not clean; refusing to switch branches")
        self.git.fetch(ado["target_branch"])
        base_ref = self.git.base_ref(ado["target_branch"])
        name = self.cfg["branch"]["pattern"].format(
            id=self.state.work_item_id, slug=slugify(self.wi.title, self.cfg["branch"]["slug_max_length"]))
        if self.git.branch_exists(name):
            if not self.dry_run:
                raise Escalation("prepare", f"branch {name} already exists; a previous run or a human owns it")
            n = 2                                                    # dry runs never reuse or delete: take a free name
            while self.git.branch_exists(f"{name}-dryrun-{n}"):
                n += 1
            name = f"{name}-dryrun-{n}"
        self.state.restore_ref = self.git.current_ref()
        self.git.create_branch(name, base_ref)
        self.state.branch = name
        self.base = self.state.base_sha = self.git.resolve(base_ref)
        self.dotnet = self._dotnet_factory()
        build = self.dotnet.build()
        if not build.ok:
            raise Escalation("prepare", f"baseline build of {ado['target_branch']} is red; not starting on a broken base")
        self.state.baseline_warnings = build.warnings
        baseline = self.dotnet.test(self.work / "tr" / "baseline")
        if not baseline.ok:
            raise Escalation("prepare", f"baseline tests are red ({baseline.failed} failing); not starting on a broken base")
        self.state.log("prepare", f"branch {name}, baseline warnings={build.warnings}, tests={baseline.executed}")

    # ===== steps 2-4: map, triage, analysis & plan ======================================================
    def code_map(self) -> None:
        self._enter("code_map")
        self.state.code_map = self._agent("mapper", "mapper", self._work_item_vars(), self._readonly(), 1)

    def triage(self) -> None:
        self._enter("triage")
        el = self.cfg["eligibility"]
        out = self._agent("triage", "triage", {**self._work_item_vars(), "code_map": self.state.code_map,
                          "max_tasks": str(el["max_estimated_tasks"]), "max_files": str(el["max_estimated_files"]),
                          "deny_paths": self.cfg["scope"]["deny_paths"]}, self._readonly(), 1)
        self.state.triage = out
        returned = [a["id"] for a in out.get("acs", [])]
        questions: list[str] = []
        if sorted(returned) != sorted(self._ac_ids()):
            questions.append(f"triage returned AC ids {returned}, expected {self._ac_ids()}")
        for a in out.get("acs", []):
            if not a.get("testable"):
                questions.append(f"{a['id']} is not testable as written: {a.get('issue', '')}")
        points = [{**x, "kind": "extra criterion", "question": f"criterion found in the {x['source']} but not in the "
                   f"acceptance criteria: {x['text']} (add it to the criteria, or state it is not required)"}
                  for x in out.get("extra_criteria", [])]
        points += [{**b, "kind": "blocker", "question": f"{b['question']} ({b.get('why', '')})"} for b in out.get("blockers", [])]
        proceed, stop = gates.weigh(points, self.cfg)
        questions += [gates.describe_point(p) for p in stop]
        questions += [f"touches restricted area {r['area']}: {r['reason']}" for r in out.get("touches_restricted", [])]
        if out.get("estimated_tasks", 0) > el["max_estimated_tasks"] or len(out.get("estimated_files", [])) > el["max_estimated_files"]:
            questions.append(f"too large for autonomous delivery: ~{out.get('estimated_tasks')} tasks, "
                             f"~{len(out.get('estimated_files', []))} files; split the work item")
        if questions:
            if proceed:
                questions += [f"(would have proceeded on) {gates.describe_point(p)}" for p in proceed]
            raise Escalation("triage", "the work item needs clarification before it can be delivered unattended", questions)
        self._adopt_assumptions(proceed, "triage")
        restated = {a["id"]: a.get("restated", "") for a in out["acs"]}
        for ac in self.state.acs:
            ac["restated"] = restated.get(ac["id"], "")

    # ===== open points weighed as small: proceed on a stated default ===================================
    def _adopt_assumptions(self, points: list[dict], source: str, reviewed: bool | None = None) -> None:
        known = {a["question"] for a in self.state.assumptions}
        for p in points:
            if p.get("question", "") in known:          # a later planning loop raising the same point again
                continue
            n = len(self.state.assumptions) + 1
            self.state.assumptions.append({
                "id": f"A-{n}", "source": source, "question": p.get("question", ""), "default": p.get("default", ""),
                "impact": p.get("impact", ""), "impact_reason": p.get("impact_reason", ""),
                "review": None if reviewed is None else ("not reviewed (raised by the checker itself)" if not reviewed else "")})
            self.state.log(source, f"proceeding on A-{n} ({p.get('impact')}): {p.get('default', '')[:200]}")
        self.save()

    def _review_assumptions(self, check: dict) -> None:
        """Second opinion: the plan checker (another model) must accept every default triage chose."""
        pending = [a for a in self.state.assumptions if a["review"] is None]
        if not pending:
            return
        verdicts = {v["id"]: v for v in check.get("assumption_review", [])}
        refused = []
        for a in pending:
            v = verdicts.get(a["id"])
            if v is None:
                refused.append(f"{a['id']}: the independent check did not review it ({a['question']})")
            elif v["verdict"] != "accept":
                refused.append(f"{a['id']}: the independent check says a human must decide: {v.get('reason', '')} ({a['question']})")
            else:
                a["review"] = f"accepted by the independent check: {v.get('reason', '')}"
        if refused:
            raise Escalation("plan", "an open point weighed as small was not accepted by the second model", refused)
        self.save()

    def plan(self) -> None:
        self._enter("plan")
        s = self.cfg["scope"]
        feedback = "(first attempt)"
        last: list[GateResult] = []
        for attempt in range(1, self.cfg["loops"]["plan"] + 1):
            self.state.loops["plan"] = attempt
            plan = self._agent("planner", "planner", {**self._work_item_vars(), "code_map": self.state.code_map,
                               "feedback": feedback, "test_globs": s["test_path_globs"], "deny_paths": s["deny_paths"],
                               "max_files": str(s["max_changed_files"])}, self._readonly(), attempt)
            blockers = [a for a in plan.get("assumptions", []) if str(a).strip().upper().startswith("BLOCKER:")]
            uncertain = [f"{b['ac_id']}: cannot tell whether it is already implemented ({b.get('note', '')})"
                         for b in plan.get("ac_baseline", []) if b["kind"] == "uncertain"]
            if blockers or uncertain:
                raise Escalation("plan", "the existing/additional baseline or a behaviour decision needs a human",
                                 uncertain + blockers)
            shape = gates.plan_shape(plan, self._ac_ids(), self.cfg, lambda p: (self.repo / p).is_file(),
                                     allow_deferred=bool(self.state.inherited))
            if not shape.passed:
                last = [shape]
                feedback = gates.summarise(last)
                self._record(last, f"plan-{attempt}")
                continue
            check = self._agent("plan_checker", "plan_checker", self._work_item_vars(), self._readonly(), attempt)
            self._review_assumptions(check)
            if check.get("blockers"):
                # The checker is the second opinion; nobody reviews its own points again, so only negligible ones pass.
                points = [{**b, "question": f"{b['question']} ({b.get('why', '')})"} for b in check["blockers"]]
                room = max(0, int(self.cfg["analysis"]["max_assumptions"]) - len(self.state.assumptions))
                proceed, stop = gates.weigh(points, {**self.cfg, "analysis": {**self.cfg["analysis"], "max_assumptions": room}},
                                            ceiling="negligible" if self.cfg["analysis"]["proceed_up_to"] != "none" else "none")
                if stop:
                    raise Escalation("plan", "independent check found blocking ambiguity", [gates.describe_point(p) for p in stop])
                self._adopt_assumptions(proceed, "plan check", reviewed=False)
            checker_files = [f["path"] for f in check.get("files", []) if not match_any(f["path"], s["test_path_globs"])]
            agreement = gates.plan_agreement(gates.plan_files(plan, tests_only=False, cfg=self.cfg), checker_files,
                                             float(self.cfg["plan_check"]["min_file_overlap"]),
                                             lambda p: (self.repo / p).is_file())
            classification = gates.classification_agreement(plan, check, self._declared())
            last = [shape, classification, agreement]
            self._record(last, f"plan-{attempt}")
            if agreement.passed and classification.passed:
                self.state.plan, self.state.check = plan, check
                self._write_notes()
                return
            feedback = gates.summarise([r for r in (classification, agreement) if not r.passed]) + \
                ("\nPut new files in the existing folder whose types they belong with, as the code map shows; "
                 "a new folder needs a reason." if not agreement.passed else "") + \
                "\nIndependent reviewer concerns:\n" + "\n".join(f"- {c}" for c in check.get("concerns", []))
        failed = [r.name for r in last if not r.passed]
        why = {"ac_classification": "the two models disagree on which criteria already hold: a human must confirm the baseline",
               "plan_agreement": "the two models would change different files: a human must say which design is meant",
               "plan_shape": "the plan does not cover every criterion with a task and a test"}
        raise Escalation("plan", "analysis did not pass its gates within the loop limit: "
                         + "; ".join(why.get(n, n) for n in failed),
                         [gates.summarise(last)])

    # ===== board: the item and its child tasks move together ============================================
    def _mover(self) -> workflow.Mover:
        if not hasattr(self, "_mover_obj"):
            self._mover_obj = workflow.Mover(self.ado)
        return self._mover_obj

    def _record_changes(self, phase: str, changes: list[dict[str, Any]]) -> None:
        for c in changes:
            c["phase"] = phase
        self.state.status_changes += changes

    def start_work(self) -> None:
        """Plan approved: the item and its open children go to 'in progress' (a dry run only reports it)."""
        cfg = self.cfg["on_start"]
        if not cfg.get("transition"):
            return
        try:
            changes, notes = self._mover().plan(self.wi, list(cfg["in_progress_states"]), cascade=bool(cfg.get("cascade_children")))
        except Exception as e:  # noqa: BLE001 - board hygiene never stops a delivery
            self.state.log("start", f"could not plan the board update: {str(e)[:200]}")
            return
        for n in notes:
            self.state.log("start", n)
        if not changes:
            self.state.log("start", "board already shows the work as started")
            return
        if self.dry_run:
            self.state.log("start", "dry run: would move " + "; ".join(workflow.describe(changes)))
            self._record_changes("start (dry run)", changes)
            return
        problems = self._mover().apply(changes)
        self._record_changes("start", changes)
        self.state.log("start", "moved " + "; ".join(workflow.describe(changes)))
        for p in problems:
            self.state.log("start", p)
        self.save()

    def _cascade_to_children(self) -> list[str]:
        """After the PR exists: open children follow the item to the dev-done state their type has."""
        ok = self.cfg["on_success"]
        if not (ok.get("transition") and ok.get("cascade_children")):
            return []
        now = {str(c["id"]): c["to"] for c in self.state.status_changes if c.get("phase") == "start" and c.get("done")}
        changes, notes = self._mover().plan(self.wi, list(ok["dev_done_states"]), cascade=True, include_item=False,
                                            child_current=now)
        notes += self._mover().apply(changes)
        self._record_changes("success", changes)
        return workflow.describe(changes) + notes

    def _restore_board(self) -> None:
        """A run that stops after it moved cards puts them back, so the board never claims work that did not land."""
        if self.dry_run or not self.cfg["on_start"].get("restore_on_escalate"):
            return
        started = [c for c in self.state.status_changes if c.get("phase") == "start" and c.get("done")]
        if not started:
            return
        for p in self._mover().revert(started):
            self.state.log(self.state.stage, p)
        self.state.log(self.state.stage, "board restored: " + "; ".join(f"AB#{c['id']} -> {c['from']}" for c in started if c.get("reverted")))

    def _write_notes(self) -> None:
        """Same notes file azure-workitem-flow resumes from, so a human can take over an escalated item."""
        p = self.state.plan
        base = gates.baseline(p)
        lines = [f"# AB#{self.state.work_item_id}: {self.wi.title}", "", "## Existing AC (regression baseline)"]
        lines += [f"- {a['id']}: {a['text']} ({', '.join(base[a['id']].get('evidence', []))})"
                  + (" [at risk: pinned]" if base[a['id']].get("at_risk") else "")
                  for a in self.state.acs if base[a["id"]]["kind"] == "existing"] or ["- none (net-new implementation)"]
        lines += ["", "## Additional AC"] + [f"- {a['id']}: {a['text']}" for a in self.state.acs if base[a["id"]]["kind"] == "additional"]
        deferred = [a for a in self.state.acs if base[a["id"]]["kind"] == "deferred"]
        if deferred:
            lines += ["", "## Deferred to other work items (inherited criteria)"] + [
                f"- {a['id']}: {a['text']} → {base[a['id']].get('note', '')}" for a in deferred]
        if self.state.security:
            lines += ["", "## Security criteria"] + [f"- {s['id']}: {s['text']}" for s in self.state.security]
        lines += ["", "## What", p.get("what", ""), "", "## Why", p.get("why", ""), "", "## Use cases"]
        lines += [f"- {u['id']} ({u['kind']}{'' if u['in_scope'] else ', out of scope: ' + u.get('reason', '')}): {u['description']}"
                  for u in p.get("use_cases", [])]
        lines += ["", "## Chosen option", f"{p.get('chosen_option')}: {p.get('chosen_rationale', '')}"]
        clean = []
        for n, line in enumerate(lines, 1):
            blocking = [f for f in self.phi.scan([AddedLine("notes", n, line)]) if f.severity == "blocking"]
            clean.append("[line removed: possible personal data]" if blocking else line)
        text = "\n".join(clean) + "\n"
        self._write("notes.md", text)
        notes = self.repo / ".claude" / "notes" / f"{self.state.work_item_id}.md"     # excluded from git
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(text, encoding="utf-8")

    # ===== step 5a: acceptance tests (frozen) ===========================================================
    def acceptance_tests(self) -> None:
        self._enter("acceptance_tests")
        plan = self.state.plan
        test_files = gates.plan_files(plan, tests_only=True, cfg=self.cfg)
        trait = self.cfg["tests"]["ac_trait_name"]
        feedback = "(first attempt)"
        for attempt in range(1, self.cfg["loops"]["tests"] + 1):
            self.state.loops["tests"] = attempt
            out = self._agent("test_author", "test_author", {**self._work_item_vars(), "feedback": feedback,
                              "ac_baseline": self._ac_baseline_vars(), "use_cases": plan.get("use_cases", []),
                              "test_plan": plan["test_plan"], "contracts": plan.get("contracts", []),
                              "test_files": test_files, "trait_name": trait}, self._writer(test_files), attempt)
            if out.get("status") != "done":
                raise Escalation("acceptance_tests", "test author could not write the acceptance tests",
                                 out.get("questions", []))
            self.git.unstage_all()
            changed = self.git.working_changes()
            results = [gates.scope(changed, test_files, self.cfg)]
            bad = self._out_of_scope(changed, test_files)
            if bad:
                self.git.discard(bad)
                changed = [c for c in changed if c not in bad]
            self.git.stage(changed)
            results += self._static_gates()
            sources = {p: (self.repo / p).read_text(encoding="utf-8", errors="replace")
                       for p in changed if (self.repo / p).is_file()}
            results.append(gates.ac_traits_present(sources, gates.tested_ac_ids(plan), trait))
            if all(r.passed for r in results):
                results.append(self._red_first(trait, attempt))
            passed = self._record(results, f"acceptance-tests-{attempt}") and bool(changed)
            self.git.unstage_all()
            if passed:
                self._freeze([c for c in changed if (self.repo / c).is_file()])
                return
            feedback = gates.summarise([r for r in results if not r.passed]) or "no test files were written"
        raise Escalation("acceptance_tests", "acceptance tests did not pass their gates within the loop limit", [feedback])

    def _freeze(self, files: list[str]) -> None:
        """Tests stay uncommitted (one commit with the code, per convention); integrity is held by hash + copy."""
        for rel in files:
            copy = self.work / "frozen" / rel
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.repo / rel, copy)
            self.state.frozen_hashes[rel] = sha256(self.repo / rel)
        self.state.frozen_test_files = sorted(files)
        self.save()

    def _restore_frozen(self) -> GateResult:
        current = {p: sha256(self.repo / p) for p in self.state.frozen_hashes}
        result = gates.frozen_integrity(current, self.state.frozen_hashes)
        for rel, h in self.state.frozen_hashes.items():
            if current[rel] != h:
                (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.work / "frozen" / rel, self.repo / rel)
        return result

    def _red_first(self, trait: str, attempt: int) -> GateResult:
        """Additional-AC tests must not already pass on the base; at-risk existing-AC pins must pass on it."""
        plan = self.state.plan
        build = self.dotnet.build()
        if not build.ok:
            return GateResult("red_first", True, "tests do not compile yet against the planned contracts (expected red); "
                                                 "existing-AC pins are verified after implementation")
        additional, pinned = gates.additional_ac_ids(plan), gates.pinned_existing_ac_ids(plan)
        run = lambda a: self.dotnet.test(self.work / "tr" / f"red-{attempt}-{a}", f"{trait}={a}")
        add_results = {a: run(a) for a in additional}
        pin_results = {a: run(a) for a in pinned}
        green = [a for a, r in add_results.items() if r.executed and not r.failed]
        details: list[str] = []
        if additional and len(green) == len(additional):
            details.append("every additional-AC test already passes on the base: they do not pin the new behaviour")
        details += [f"{a}: existing-AC pin fails on the base (classification or test is wrong)"
                    for a, r in pin_results.items() if r.failed or not r.executed]
        info = [f"{a}: additional AC already green on base" for a in green] if len(green) < len(additional) else []
        return GateResult("red_first", not details,
                          f"{len(additional) - len(green)}/{len(additional)} additional AC red, "
                          f"{len(pinned)} existing pin(s) checked", details + info)

    def _static_gates(self) -> list[GateResult]:
        staged = self.git.staged_files(self.base)
        csproj = [p for p in staged if p.lower().endswith(".csproj")]
        results = [gates.dependencies(staged, self.git.staged_diff(self.base, 0, csproj) if csproj else "", self.cfg),
                   gates.diff_budget(self.git.staged_numstat(self.base), self.cfg)]
        findings = self.phi.scan(added_lines(self.git.staged_diff(self.base, 0)))
        leaks = (run_gitleaks(self.repo, self.cfg["phi"]["gitleaks_args"], find_gitleaks(self.cfg))
                 if self.cfg["phi"].get("gitleaks") else None)
        results.append(gates.phi(findings, leaks))
        return results

    # ===== step 5b-6: develop + gates ===================================================================
    def _verify(self, allowed: list[str], tag: str) -> list[GateResult]:
        """All deterministic gates on exactly what would be committed (code + frozen tests)."""
        self.git.unstage_all()
        results = [self._restore_frozen()]
        permitted = sorted({*allowed, *self.state.frozen_test_files})
        changed = self.git.working_changes()
        results.append(gates.scope(changed, permitted, self.cfg))
        bad = self._out_of_scope(changed, permitted)
        if bad:
            self.git.discard(bad)
            changed = [c for c in changed if c not in bad]
        self.git.stage(changed)
        results += self._static_gates()
        build = self.dotnet.build()
        results.append(gates.build(build, self.state.baseline_warnings, self.cfg))
        if build.ok:
            full = self.dotnet.test(self.work / "tr" / f"{tag}-full")
            results.append(gates.tests(full))
            trait = self.cfg["tests"]["ac_trait_name"]
            per_ac = {a: self.dotnet.test(self.work / "tr" / f"{tag}-{a}", f"{trait}={a}")
                      for a in gates.tested_ac_ids(self.state.plan)}
            results.append(gates.ac_trace(per_ac, self.cfg))
            if not full.ok:
                self._write(f"gates/{tag}-test-output.txt", full.tail)
        else:
            self._write(f"gates/{tag}-build-output.txt", build.tail)
        cmds = []
        for c in self.cfg.get("security_commands", []) or []:
            args = [str(a).format(base=self.base) for a in c["args"]]
            proc = subprocess.run(args, cwd=self.repo, env=_scrubbed_env(),
                                  capture_output=True, text=True, encoding="utf-8", errors="replace")
            cmds.append((c["name"], proc.returncode, proc.stdout[-500:]))
        if cmds:
            results.append(gates.commands(cmds))
        if all(r.passed for r in results) and self.cfg["mutation"].get("enabled"):
            m = self.dotnet.mutate(self.base, self.work / "stryker" / tag)
            self.state.mutation_score = m.score
            results.append(gates.mutation(m, self.cfg))
        self._record(results, tag)
        return results

    def _dev_allowed(self) -> list[str]:
        return [p for p in gates.plan_files(self.state.plan, cfg=self.cfg)
                if not any(same_path(p, f) for f in self.state.frozen_test_files)]

    def _developer(self, feedback: str, attempt: int) -> None:
        frozen = self.state.frozen_test_files
        allowed = self._dev_allowed()
        out = self._agent("developer", "developer", {**self._work_item_vars(), "plan": self.state.plan,
                          "ac_baseline": self._ac_baseline_vars(), "use_cases": self.state.plan.get("use_cases", []),
                          "feedback": feedback, "allowed_files": allowed, "frozen_tests": frozen},
                          self._writer(allowed, frozen), attempt)
        status = out.get("status")
        if status == "test_dispute":
            d = out.get("test_dispute") or {}
            raise Escalation(self.state.stage, "developer disputes a frozen acceptance test (the spec may be wrong)",
                             [f"{d.get('test', '?')}: {d.get('reason', '')}"])
        if status != "done":
            raise Escalation(self.state.stage, "developer is blocked", out.get("questions", []))
        self.state.plan.setdefault("followups", []).extend(out.get("followups", []))

    def develop(self) -> None:
        self._enter("develop")
        feedback = "(first attempt)"
        for attempt in range(1, self.cfg["loops"]["dev"] + 1):
            self.state.loops["dev"] = attempt
            self._developer(feedback, attempt)
            results = self._verify(self._dev_allowed(), f"dev-{attempt}")
            if all(r.passed for r in results):
                return
            feedback = gates.summarise([r for r in results if not r.passed])
        raise Escalation("develop", "implementation did not pass the gates within the loop limit", [feedback])

    def _fix_then_verify(self, feedback: str, tag: str) -> None:
        self.state.loops["dev"] += 1
        self._developer(feedback, self.state.loops["dev"])
        results = self._verify(self._dev_allowed(), tag)
        if not all(r.passed for r in results):
            raise Escalation(self.state.stage, "fix broke a gate", [gates.summarise([r for r in results if not r.passed])])

    def review(self) -> None:
        self._enter("review")
        last = None
        plan = self.state.plan
        for attempt in range(1, self.cfg["loops"]["review"] + 1):
            self.state.loops["review"] = attempt
            diff = self.git.staged_diff(self.base, 5)
            verdict = self._agent("reviewer", "reviewer", {
                **self._work_item_vars(), "diff": diff[:120_000], "ac_baseline": self._ac_baseline_vars(),
                "use_cases": [{k: u[k] for k in ("id", "description", "kind", "in_scope")} for u in plan.get("use_cases", [])],
                "gate_summary": report.gate_table(self.state.gate_results)}, self._readonly(), attempt)
            last = gates.review(verdict, plan, self._sc_ids())
            self._write(f"gates/review-{attempt}.md", gates.summarise([last]))
            if last.passed:
                self.state.review = verdict
                self.state.effort = verdict.get("effort_estimate")
                self.state.gate_results.append(last.as_dict())
                return
            if attempt < self.cfg["loops"]["review"]:
                self._fix_then_verify("Reviewer findings to address:\n" + gates.summarise([last]), f"review-fix-{attempt}")
        raise Escalation("review", "independent review did not approve within the loop limit",
                         [gates.summarise([last])] if last else [])

    def quality(self) -> None:
        self._enter("quality")
        from . import sonar
        analyse = self._sonar_fn or sonar.analyse
        for attempt in range(1, self.cfg["loops"]["quality"] + 1):
            self.state.loops["quality"] = attempt
            res = analyse(self.repo, self.cfg, self.dotnet.solution, self.state.branch, self.git.staged_files(self.base))
            if not res.ran:
                raise Escalation("quality", f"Sonar analysis did not run: {res.message}")
            result = GateResult("sonar", res.gate_status in ("OK", "NONE") and not res.issues,
                                f"quality gate {res.gate_status}, {len(res.issues)} issue(s) in changed files",
                                [f"{i.get('severity')} {i.get('rule')} {i.get('component', '').split(':')[-1]}:{i.get('line', '?')} "
                                 f"{i.get('message', '')}" for i in res.issues[:30]])
            self.state.gate_results.append(result.as_dict())
            self.save()
            if result.passed:
                return
            if attempt < self.cfg["loops"]["quality"]:
                self._fix_then_verify("Sonar findings (fix with the smallest change; no suppressions):\n" +
                                      gates.summarise([result]), f"quality-fix-{attempt}")
                self.review()          # the code changed after approval: review again
                self.state.stage = "quality"
        raise Escalation("quality", "Sonar findings remain after the loop limit")

    # ===== step 7: one commit ===========================================================================
    def commit(self) -> None:
        self._enter("commit")
        plan = self.state.plan
        self._restore_frozen()                                  # belt and braces before the only commit
        self.git.stage(self.state.frozen_test_files)
        ctype = plan.get("commit_type") if plan.get("commit_type") in ("feat", "fix", "refactor", "perf") else "feat"
        summary = " ".join(str(plan.get("commit_summary") or self.wi.title).split())[:72]
        if self._has_phi(summary):
            summary = f"implement work item {self.state.work_item_id}"
        if not self.git.has_staged_changes():
            raise Escalation("commit", "nothing staged to commit after the gates")
        missing = [f for f in self.state.frozen_test_files if f not in self.git.staged_files(self.base)]
        if missing:
            raise Escalation("commit", "acceptance tests are not part of the commit", missing)
        subject = self.cfg["commit"]["message_template"].format(type=ctype, summary=summary, id=self.state.work_item_id)
        why = "\n".join(l.strip() for l in str(plan.get("commit_why", "")).splitlines() if l.strip())[:600]
        message = subject + (f"\n\n{why}" if why and not self._has_phi(why) else "")
        self.state.commit_sha = self.git.commit(message)
        leftover = self.git.working_changes()
        if leftover:
            raise Escalation("commit", "unexpected uncommitted files after commit", leftover[:20])

    def _has_phi(self, text: str) -> bool:
        lines = [AddedLine("COMMIT_MSG", n, l) for n, l in enumerate(text.splitlines() or [text], 1)]
        return any(f.severity == "blocking" for f in self.phi.scan(lines))

    # ===== step 8: PR, then completed hours + dev-done =================================================
    def publish(self) -> None:
        self._enter("publish")
        wid = self.state.work_item_id
        text = self._agent("pr_writer", "pr_writer", {**self._work_item_vars(), "plan_summary": self.state.plan.get("summary", ""),
                           "gate_table": report.gate_table(self.state.gate_results)}, self._readonly(), 1)
        self.state.resolution = self._resolution_plan()
        evidence = report.evidence_pack(self.state)
        self._write("evidence.md", evidence)
        # Everything that leaves the run record is checked like the commit message: the PR writer reads the
        # ticket, which may quote patient data.
        raw_title = " ".join(str(text.get("title", self.wi.title)).split())[:200]
        title = f"AB#{wid}: {raw_title if not self._has_phi(raw_title) else f'implement work item {wid}'}"
        summary = str(text.get("summary_markdown", ""))
        if self._has_phi(summary):
            self.state.log("publish", "PR summary withheld: the PHI scanner flagged it")
            summary = "_Summary withheld: the PHI scanner flagged the generated text. See the evidence pack in the run record._"
        if self._has_phi(evidence):
            self.state.log("publish", "evidence pack not posted to the PR: the PHI scanner flagged it")
            evidence = "Evidence pack withheld from the PR: the PHI scanner flagged it. It is in the pipeline run record."
        decided = "".join(f"\n- **{a['id']}** {a['question']} -> **{a['default']}**" for a in self.state.assumptions)
        if decided and self._has_phi(decided):
            decided = "\n- (withheld: the PHI scanner flagged the text; see the evidence pack in the run record)"
        description = (f"Implements AB#{wid}.\n\n" + summary + "\n\n" +
                       (f"**Decisions taken without asking (verify):**{decided}\n\n" if decided else "") +
                       report.gate_table(self.state.gate_results) +
                       "\n\nFull evidence pack in the first PR thread. Generated by autodeliver; human approval required.")
        self._write("pr-description.md", description)
        if self.dry_run:
            self.state.log("publish", "dry run: not pushing, no PR, no work item writes")
            return
        ado = self.cfg["azure_devops"]
        self.git.push(self.state.branch)
        pr = self.ado.create_pr(self.state.branch, ado["target_branch"], title, description, wid,
                                draft=bool(self.cfg["on_success"]["draft_pr"]))
        self.ado.add_pr_thread(pr["pullRequestId"], evidence)
        self.state.pr_url = self.ado.pr_web_url(pr)
        self._apply_resolution()
        moved = self._cascade_to_children()
        if moved and self.state.resolution is not None:
            self.state.resolution["notes"] += [f"child work items: {m}" for m in moved]
        ok = self.cfg["on_success"]
        tags = [t for t in self.wi.tags if t.lower() != str(ok.get("remove_tag", "")).lower()] + [ok["add_tag"]]
        self.ado.set_tags(wid, tags)
        if ok.get("comment"):
            self.ado.comment(wid, report.success_html(self.state))

    def _resolution_plan(self) -> dict[str, Any]:
        """What step 8 will write: hours (cumulative) first, then the dev-done state. Never Closed."""
        ok = self.cfg["on_success"]
        est = self.state.effort or {}
        parts = {k: quarter_hours(est.get(k, 0)) for k in ("reading", "implementing", "testing", "self_review")}
        hours = quarter_hours(sum(parts.values()))
        plan: dict[str, Any] = {"breakdown": parts, "hours": hours, "fields": {}, "notes": []}
        if ok.get("write_hours"):
            if 0 < hours <= float(ok["max_hours"]):
                previous = self.wi.completed_work or 0.0
                plan["previous_completed"] = previous
                plan["fields"][COMPLETED] = round(previous + hours, 2)
                if ok.get("zero_remaining_work"):
                    plan["fields"][REMAINING] = 0
            else:
                plan["notes"].append(f"estimate {hours}h outside (0, {ok['max_hours']}]: hours not written")
        if ok.get("transition"):
            states = self.ado.type_states(self.wi.type)
            lookup = {s.lower(): s for s in states}
            target = next((lookup[s.lower()] for s in ok["dev_done_states"] if s.lower() in lookup), None)
            if target is None:
                plan["notes"].append(f"no dev-done state of {ok['dev_done_states']} exists for {self.wi.type} {states}")
            elif target.lower() == "closed":
                plan["notes"].append("refusing to move to Closed")
            elif target == self.wi.state:
                plan["notes"].append(f"already in {target}")
            else:
                plan["fields"]["System.State"] = target
        return plan

    def _apply_resolution(self) -> None:
        res = self.state.resolution
        if not res["fields"]:
            return
        wid = self.state.work_item_id
        try:
            self.ado.update_fields(wid, res["fields"])
            res["written"] = dict(res["fields"])
        except Exception as e:  # noqa: BLE001 - the PR exists; report instead of escalating
            res["notes"].append(f"update failed: {str(e)[:200]}")
            hours_only = {k: v for k, v in res["fields"].items() if k != "System.State"}
            if hours_only and "System.State" in res["fields"]:
                try:
                    self.ado.update_fields(wid, hours_only)
                    res["written"] = hours_only
                    res["notes"].append("hours written; state transition rejected by the process template")
                except Exception as e2:  # noqa: BLE001
                    res["notes"].append(f"hours update failed: {str(e2)[:200]}")
        self.save()

    # ===== failure =====================================================================================
    def escalate(self, e: Escalation) -> None:
        self.state.status = "escalated"
        self.state.log(e.stage, f"ESCALATED: {e.reason}")
        body = [f"# Escalated at {e.stage}", e.reason, ""] + [f"- {q}" for q in e.questions]
        self._write("escalation.md", "\n".join(body))
        self.save()
        if self.wi is None:
            return
        cfg = self.cfg["on_escalate"]
        # The full questions stay in escalation.md; the ticket comment gets lines the PHI scanner accepts.
        questions = [q if not self._has_phi(str(q)) else "(withheld: possible personal data, see escalation.md in the run record)"
                     for q in e.questions]
        try:
            self._restore_board()
            if cfg.get("comment"):
                self.ado.comment(self.state.work_item_id, report.escalation_html(self.state, e.stage, e.reason, questions))
            self.ado.set_tags(self.state.work_item_id, [*self.wi.tags, cfg["add_tag"]])
            if cfg.get("set_state"):
                self.ado.set_state(self.state.work_item_id, cfg["set_state"])
        except Exception as err:  # noqa: BLE001 - reporting must not mask the escalation itself
            self.state.log(e.stage, f"could not update work item: {err}")
