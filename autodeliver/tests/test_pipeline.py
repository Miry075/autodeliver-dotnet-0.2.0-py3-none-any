"""End-to-end state machine runs on a real temporary git repo, with fake agents and a fake dotnet."""
import json
import subprocess
from pathlib import Path

from autodeliver.ado import OfflineAzureDevOps
from autodeliver.agents import AgentResult
from autodeliver.dotnet import BuildResult, MutationResult, TestResult
from autodeliver.pipeline import Pipeline

STORY = {"title": "Refuse edits on closed episodes", "type": "User Story", "state": "Active", "tags": ["auto-deliver"],
         "description": "Closed care episodes must be read-only.",
         "acceptance_criteria": ["Editing an open episode succeeds and stores the note",      # existing, at risk
                                 "Editing a closed episode is refused with a domain error"],  # additional
         "security_criteria": ["Refusals are written to the audit log"],
         "comments": ["PO: refusal message must not contain the patient name"],
         "completed_work": 1.5,
         "type_states": ["New", "Active", "Resolved", "Closed"]}
ACCEPT = "tests/Care.Tests/EpisodeAcceptanceTests.cs"
SWEEP = {k: {"status": "ok", "note": ""} for k in ["phi_in_logs", "audit_trail", "retention", "data_boundary", "authz"]}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def make_repo(tmp: Path, with_origin: bool = False, files: dict[str, str] | None = None) -> Path:
    repo = tmp / "repo"
    (repo / "src" / "Care").mkdir(parents=True)
    (repo / "tests" / "Care.Tests").mkdir(parents=True)
    (repo / "Care.sln").write_text("sln")
    (repo / "AGENTS.md").write_text("conventions")
    (repo / "src" / "Care" / "Episode.cs").write_text("public class Episode { }\n")
    (repo / "tests" / "Care.Tests" / "EpisodeTests.cs").write_text("public class EpisodeTests { }\n")
    for rel, text in (files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    if with_origin:
        origin = tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
    return repo


class FakeDotnet:
    solution = "Care.sln"

    def __init__(self, repo: Path):
        self.repo = repo
        self.filters: list[str | None] = []

    def implemented(self) -> bool:
        return "IMPLEMENTED" in (self.repo / "src" / "Care" / "Episode.cs").read_text()

    def build(self):
        return BuildResult(True, 0, 0, "")

    def test(self, results_dir: Path, test_filter=None):
        self.filters.append(test_filter)
        if test_filter == "AC=AC-1":                   # existing behaviour: green before and after
            ok = True
        elif test_filter == "AC=AC-2":                 # new behaviour: red until implemented
            ok = self.implemented()
        else:
            ok = self.implemented() or not (self.repo / ACCEPT).exists()
        n = 1 if test_filter else 5
        return TestResult(ok=ok, total=n, executed=n, passed=n if ok else 0, failed=0 if ok else n,
                          failed_tests=[] if ok else ["EpisodeAcceptanceTests.X"])

    def mutate(self, base, out_dir):
        return MutationResult(ran=True, score=82.0, detected=9, undetected=2)


def planner_output(classify_ac1: str = "existing"):
    return {
        "ac_baseline": [{"ac_id": "AC-1", "kind": classify_ac1, "evidence": ["src/Care/Episode.cs:1"], "at_risk": True, "note": ""},
                        {"ac_id": "AC-2", "kind": "additional", "evidence": [], "at_risk": False, "note": ""}],
        "what": "Guard Episode.Edit when closed", "why": "Closed episodes are legal records",
        "use_cases": [{"id": "UC-1", "description": "clinician edits open episode", "kind": "actor", "in_scope": True, "reason": ""},
                      {"id": "UC-2", "description": "edit on closed episode", "kind": "edge", "in_scope": True, "reason": ""},
                      {"id": "UC-3", "description": "bulk import job", "kind": "related", "in_scope": False, "reason": "import bypasses Edit"}],
        "options": [{"name": "Domain guard", "summary": "guard in aggregate", "trade_off": "none", "design_fit": "invariant in root",
                     "testability": "pure unit tests", "covers_all_use_cases": True, "preserves_existing_ac": True},
                    {"name": "Handler check", "summary": "check in handler", "trade_off": "anemic", "design_fit": "logic leaks to service",
                     "testability": "needs mocks", "covers_all_use_cases": True, "preserves_existing_ac": True}],
        "chosen_option": "Domain guard", "chosen_rationale": "invariant belongs to the aggregate", "single_option_reason": "",
        "summary": "Guard edits in Episode",
        "tasks": [{"id": "T1", "title": "guard", "ac_ids": ["AC-2"], "files": [{"path": "src/Care/Episode.cs", "change": "modify"}]}],
        "test_plan": [{"ac_id": "AC-1", "use_case_ids": ["UC-1"], "test_file": ACCEPT, "test_name": "Edit_WhenOpen_StoresNote", "layer": "domain"},
                      {"ac_id": "AC-2", "use_case_ids": ["UC-2"], "test_file": ACCEPT, "test_name": "Edit_WhenClosed_Throws", "layer": "domain"}],
        "contracts": [{"file": "src/Care/Episode.cs", "signature": "public void Edit(string note)"}],
        "citations": ["src/Care/Episode.cs:1"], "assumptions": [], "followups": [],
        "commit_type": "feat", "commit_summary": "refuse edits on closed episodes",
        "commit_why": "Closed episodes are part of the legal record and must not change.",
    }


class FakeAgents:
    def __init__(self, repo: Path, developer_behaviour: str = "good", checker_ac1: str = "existing"):
        self.repo = repo
        self.behaviour = developer_behaviour
        self.checker_ac1 = checker_ac1
        self.calls: list[str] = []
        self.variables: dict[str, dict] = {}

    def _write(self, policy, rel: str, content: str, *, bypass: bool = False):
        allowed, _ = policy.decide("Write", {"file_path": rel})
        if allowed or bypass:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(content)

    def run(self, role, model_key, variables, policy, budget, attempt):
        self.calls.append(role)
        self.variables[role] = variables
        out = {
            "mapper": lambda: {"locations": [{"path": "src/Care/Episode.cs", "line": 1, "symbol": "Episode",
                                              "layer": "domain", "note": "aggregate"}],
                               "test_projects": [{"path": "tests/Care.Tests", "framework": "xunit"}]},
            "triage": lambda: {"acs": [{"id": a["id"], "testable": True, "restated": a["text"], "issue": ""}
                                       for a in variables["acceptance_criteria"]],
                               "extra_criteria": [], "blockers": [], "estimated_tasks": 1,
                               "estimated_files": ["src/Care/Episode.cs"], "touches_restricted": []},
            "planner": lambda: planner_output(),
            "plan_checker": lambda: {"ac_classification": [{"ac_id": "AC-1", "kind": self.checker_ac1, "evidence": "Episode.cs:1"},
                                                           {"ac_id": "AC-2", "kind": "additional", "evidence": ""}],
                                     "files": [{"path": "src/Care/Episode.cs", "change": "modify", "reason": "guard"}],
                                     "concerns": ["audit"], "blockers": []},
            "reviewer": lambda: {
                "verdict": "APPROVE",
                "existing_ac_assessment": [{"ac_id": "AC-1", "holds": True, "evidence": "Edit_WhenOpen_StoresNote"}],
                "ac_assessment": [{"ac_id": "AC-2", "met": True, "evidence": "Edit_WhenClosed_Throws"}] +
                                 [{"ac_id": sc["id"], "met": True, "evidence": "Episode.cs:12 audit call"}
                                  for sc in (variables.get("security_criteria") if isinstance(variables.get("security_criteria"), list) else [])],
                "use_case_assessment": [{"uc_id": "UC-1", "status": "met", "evidence": "t"}, {"uc_id": "UC-2", "status": "met", "evidence": "t"}],
                "findings": [], "health_sweep": SWEEP,
                "design_principles": [{"principle": "Aggregate invariant", "how": "guard in Episode.Edit"}],
                "test_coverage": [{"item": "closed guard", "layer": "domain", "tests": ["Edit_WhenClosed_Throws"]}],
                "untested": [], "risks": [], "still_needed": [],
                "effort_estimate": {"reading": 0.5, "implementing": 1.0, "testing": 1.1, "self_review": 0.25}},
            "pr_writer": lambda: {"title": "Refuse edits on closed episodes", "summary_markdown": "Adds a domain guard."},
        }
        if role == "test_author":
            self._write(policy, ACCEPT, 'public class EpisodeAcceptanceTests {\n'
                                        '  [Fact, Trait("AC", "AC-1")] public void Edit_WhenOpen_StoresNote() {}\n'
                                        '  [Fact, Trait("AC", "AC-2")] public void Edit_WhenClosed_Throws() {}\n}\n')
            result = {"status": "done", "tests": [], "notes": "", "questions": []}
        elif role == "developer":
            if self.behaviour == "good":
                self._write(policy, "src/Care/Episode.cs", "public class Episode { /* IMPLEMENTED */ }\n")
            else:   # misbehaving: weakens a frozen test and writes a deny path, never implements
                self._write(policy, ACCEPT, "// weakened", bypass=True)
                self._write(policy, "src/Care/Auth/Policy.cs", "// sneaky", bypass=True)
            result = {"status": "done", "summary": "", "test_dispute": None, "questions": [], "followups": []}
        else:
            result = out[role]()
        return AgentResult(result, 0.10, 3, "s", [])


def run_pipeline(tmp_path: Path, cfg, behaviour="good", *, dry_run=True, with_origin=False, checker_ac1="existing", story=None,
                 files=None, keep_branch=True):
    repo = make_repo(tmp_path, with_origin, files)
    ado = OfflineAzureDevOps(story or STORY, cfg["azure_devops"].get("criteria_section", ""))    # as the CLI does
    agents = FakeAgents(repo, behaviour, checker_ac1)
    p = Pipeline(cfg, repo, 4242, ado, agents, dry_run=dry_run, dotnet_factory=lambda: FakeDotnet(repo), keep_branch=keep_branch)
    return p, p.run(), repo, ado, agents


def test_happy_path_one_commit_with_tests_and_evidence(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg)
    assert code == 0, (repo / ".work/autodeliver/4242/escalation.md").read_text() if code else ""
    # tests in the same commit as the code they cover, AB# convention, why-body
    log = git(repo, "log", "--format=%s%n%b", "main..HEAD").strip().splitlines()
    assert log[0] == "feat: refuse edits on closed episodes (AB#4242)"
    assert "legal record" in "\n".join(log[1:])
    files = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == sorted(["src/Care/Episode.cs", ACCEPT])
    assert git(repo, "status", "--porcelain") == ""
    # stage order and routing: code map before triage/analysis, checker independent of the map
    assert agents.calls == ["mapper", "triage", "planner", "plan_checker", "test_author", "developer", "reviewer", "pr_writer"]
    assert "code_map" in agents.variables["planner"] and "code_map" not in agents.variables["plan_checker"]
    assert agents.variables["triage"]["comments"] == STORY["comments"]               # comments reach analysis
    assert agents.variables["triage"]["security_criteria"][0]["id"] == "SC-1"
    # notes file for azure-workitem-flow hand-over, excluded from git
    notes = (repo / ".claude" / "notes" / "4242.md").read_text()
    assert "## Existing AC" in notes and "AC-1" in notes.split("## Additional AC")[0]
    evidence = (repo / ".work/autodeliver/4242/evidence.md").read_text()
    assert evidence.index("Existing acceptance criteria") < evidence.index("Additional acceptance criteria")
    assert "2.75h" in evidence                                                         # 0.5+1.0+1.0(1.1 rounded)+0.25
    assert ado.writes == []                                                            # dry run: no ticket writes


def test_existing_classification_disagreement_escalates(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, checker_ac1="additional")
    assert code == 2 and p.state.stage == "plan"
    assert agents.calls.count("planner") == cfg["loops"]["plan"]
    assert "existing/additional" in (repo / ".work/autodeliver/4242/escalation.md").read_text()


def test_criteria_found_only_in_comments_escalate(tmp_path, cfg):
    repo = make_repo(tmp_path)

    class Extra(FakeAgents):
        def run(self, role, *a, **k):
            r = super().run(role, *a, **k)
            if role == "triage":
                r.output["extra_criteria"] = [{"source": "comment", "text": "refusal message must not contain the patient name"}]
            return r
    p = Pipeline(cfg, repo, 7, OfflineAzureDevOps(STORY), Extra(repo), dry_run=True, dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 2 and p.state.stage == "triage"


def test_misbehaving_developer_is_reverted_and_escalated(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, "bad")
    assert code == 2 and p.state.status == "escalated" and p.state.stage == "develop"
    assert agents.calls.count("developer") == cfg["loops"]["dev"]
    assert "weakened" not in (repo / ACCEPT).read_text()                  # frozen test restored from the copy
    assert not (repo / "src/Care/Auth/Policy.cs").exists()                # deny-path file removed
    assert git(repo, "log", "--format=%s", "main..HEAD").strip() == ""    # nothing committed
    assert [w[0] for w in ado.writes] == ["comment", "tags"]
    assert "autodeliver-blocked" in ado.writes[1][1] and "Nothing was pushed" in ado.writes[0][1]


def test_live_run_pushes_pr_then_hours_and_dev_done_state(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, dry_run=False, with_origin=True)
    assert code == 0, (repo / ".work/autodeliver/4242/escalation.md").read_text() if code else ""
    kinds = [w[0] for w in ado.writes]
    assert kinds == ["pr", "pr_thread", "fields", "tags", "comment"]
    fields = ado.writes[2][1]
    assert list(fields) == ["Microsoft.VSTS.Scheduling.CompletedWork", "Microsoft.VSTS.Scheduling.RemainingWork", "System.State"]
    assert fields["Microsoft.VSTS.Scheduling.CompletedWork"] == 1.5 + 2.75             # cumulative
    assert fields["System.State"] == "Resolved" and fields["Microsoft.VSTS.Scheduling.RemainingWork"] == 0
    pr = ado.writes[0][1]
    assert pr["title"].startswith("AB#4242: ") and "Implements AB#4242" in pr["description"]
    assert pr["target"] == "main" and pr["draft"] is True and pr["work_item"] == 4242
    assert "new total 4.25h" in ado.writes[4][1]
    pushed = subprocess.run(["git", "ls-remote", "--heads", str(tmp_path / "origin.git")], capture_output=True, text=True).stdout
    assert p.state.branch in pushed


def test_never_moves_to_closed(tmp_path, cfg):
    story = {**STORY, "type_states": ["New", "Active", "Closed"]}
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, dry_run=False, with_origin=True, story=story)
    assert code == 0
    fields = [w[1] for w in ado.writes if w[0] == "fields"][0]
    assert "System.State" not in fields
    assert any("no dev-done state" in n for n in p.state.resolution["notes"])


def test_not_opted_in_writes_nothing(tmp_path, cfg):
    repo = make_repo(tmp_path)
    ado = OfflineAzureDevOps({**STORY, "tags": []})
    p = Pipeline(cfg, repo, 1, ado, FakeAgents(repo), dry_run=True, dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 1 and ado.writes == []
    assert git(repo, "branch", "--show-current").strip() == "main"


def test_dirty_tree_refuses_to_start(tmp_path, cfg):
    repo = make_repo(tmp_path)
    (repo / "src" / "Care" / "Episode.cs").write_text("local edit")
    p = Pipeline(cfg, repo, 2, OfflineAzureDevOps(STORY), FakeAgents(repo), dry_run=True, dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 2 and p.state.stage == "prepare"
    assert (repo / "src" / "Care" / "Episode.cs").read_text() == "local edit"


def test_every_prompt_renders_with_the_variables_the_pipeline_passes(tmp_path, cfg):
    """The fakes skip rendering; a missing {{variable}} would otherwise only fail in a live run."""
    from autodeliver.agents import AgentRunner
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg)
    assert code == 0
    runner = AgentRunner(repo, cfg, tmp_path / "t")
    for role, variables in agents.variables.items():
        text = runner.prompt(role, variables)
        assert "{{" not in text, role
        if role in ("developer", "reviewer", "planner"):
            assert "Design standards" in text


# --- v0.3.0: generated text that leaves the run record is PHI-checked like the commit message ----------
def test_pr_text_flagged_by_phi_scanner_is_withheld(tmp_path, cfg):
    from conftest import make_niss
    niss = make_niss("850101123")

    class Leaky(FakeAgents):
        def run(self, role, *a, **k):
            r = super().run(role, *a, **k)
            if role == "pr_writer":
                r.output.update(title=f"Fix for patient {niss}", summary_markdown=f"Reproduced with NISS {niss}.")
            return r
    repo = make_repo(tmp_path, with_origin=True)
    ado = OfflineAzureDevOps(STORY)
    p = Pipeline(cfg, repo, 4242, ado, Leaky(repo), dry_run=False, dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 0
    pr = ado.writes[0][1]
    assert niss not in pr["title"] and niss not in pr["description"]
    assert pr["title"] == "AB#4242: implement work item 4242" and "Summary withheld" in pr["description"]


def test_escalation_comment_withholds_flagged_questions(tmp_path, cfg):
    from conftest import make_niss
    niss = make_niss("900202456")

    class Blocked(FakeAgents):
        def run(self, role, *a, **k):
            r = super().run(role, *a, **k)
            if role == "developer":
                r.output.update(status="blocked", questions=[f"Which record is {niss}?", "Is the guard per episode?"])
            return r
    repo = make_repo(tmp_path)
    ado = OfflineAzureDevOps(STORY)
    p = Pipeline(cfg, repo, 4242, ado, Blocked(repo), dry_run=True, dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 2
    comment = ado.writes[0][1]
    assert niss not in comment and "withheld" in comment and "Is the guard per episode?" in comment
    assert niss in (repo / ".work/autodeliver/4242/escalation.md").read_text()     # full text kept in the run record


def test_pipeline_runs_in_a_linked_git_worktree(tmp_path, cfg):
    """`.git` is a file there. Used to keep a developer's unfinished work out of the run."""
    main = make_repo(tmp_path)
    (main / "src" / "Care" / "Episode.cs").write_text("// unfinished local edit\n")           # dirty main checkout
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    assert (wt / ".git").is_file()
    ado = OfflineAzureDevOps(STORY)
    agents = FakeAgents(wt)
    p = Pipeline(cfg, wt, 4242, ado, agents, dry_run=True, dotnet_factory=lambda: FakeDotnet(wt), keep_branch=True)
    assert p.run() == 0, (wt / ".work/autodeliver/4242/escalation.md").read_text()
    assert git(wt, "log", "--format=%s", "-1").startswith("feat: refuse edits on closed episodes (AB#4242)")
    assert (main / "src" / "Care" / "Episode.cs").read_text() == "// unfinished local edit\n"    # untouched


def test_basic_process_issue_with_criteria_in_the_description_end_to_end(tmp_path, cfg):
    """Azure DevOps Basic: Issue / Task, states To Do / Doing / Done, no Acceptance Criteria field."""
    cfg["azure_devops"]["criteria_section"] = "Acceptance criteria"
    cfg["eligibility"]["work_item_types"] = ["Issue", "Task"]
    cfg["eligibility"]["allowed_states"] = ["To Do", "Doing"]
    cfg["on_success"]["dev_done_states"] = ["Doing"]
    story = {"title": "Refuse edits on closed episodes", "type": "Issue", "state": "To Do", "tags": ["auto-deliver"],
             "description": ("**User story:** As a clinician I want closed episodes read-only.\n\n"
                             "**Acceptance criteria:** Editing an open episode succeeds and stores the note. "
                             "Editing a closed episode is refused with a domain error.\n\n**Priority:** Must"),
             "type_states": ["To Do", "Doing", "Done"]}
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, dry_run=False, with_origin=True, story=story)
    assert code == 0, (repo / ".work/autodeliver/4242/escalation.md").read_text() if code else ""
    assert [a["id"] for a in p.state.acs] == ["AC-1", "AC-2"]
    assert "'Acceptance criteria' section of the Description" in agents.variables["triage"]["criteria_source"]
    fields = [w[1] for w in ado.writes if w[0] == "fields"][0]
    assert fields["System.State"] == "Doing"                                      # never Done: a human completes it


def test_basic_process_is_still_rejected_with_the_defaults(tmp_path, cfg):
    story = {"title": "t", "type": "Issue", "state": "To Do", "tags": ["auto-deliver"],
             "description": "**Acceptance criteria:** Something that must hold for users."}
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, story=story)
    text = (repo / ".work/autodeliver/4242/escalation.md").read_text()
    assert code == 2 and "type 'Issue'" in text and "state 'To Do'" in text and "no usable acceptance criteria" in text


# --- v0.5.0: committed spec (agentic-SDLC convention) ---------------------------------------------------
SPEC = """# 4242: Refuse edits on closed episodes

- **Work item:** 4242
- **Risk tier:** Medium

## Goal

Closed care episodes are legal records and must be read-only.

## Existing acceptance criteria

- Editing an open episode succeeds and stores the note (src/Care/Episode.cs:1)

## Acceptance criteria

1. Given a closed episode, when it is edited, then a domain error is raised.

## Use cases

- A clinician edits a closed episode by mistake.

## Non-goals

- Reopening closed episodes.

## Security criteria

- Refusals are written to the audit log

## Open questions

- None
"""
SPEC_FILE = "docs/specs/4242.md"


def test_committed_spec_supplies_criteria_security_context_and_the_declared_baseline(tmp_path, cfg):
    story = {**STORY, "acceptance_criteria": ["See docs/specs/4242.md."], "security_criteria": ["Refusals are written to the audit log",
                                                                                              "Edits are rate limited"]}
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, story=story, files={SPEC_FILE: SPEC})
    assert code == 0, (repo / ".work/autodeliver/4242/escalation.md").read_text() if code else ""
    assert [(a["id"], a["declared"]) for a in p.state.acs] == [("AC-1", "existing"), ("AC-2", "additional")]
    assert p.state.acs[0]["text"].startswith("Editing an open episode succeeds") and p.state.tracker_criteria == []
    assert [s["text"] for s in p.state.security] == ["Refusals are written to the audit log", "Edits are rate limited"]
    for role in ("triage", "planner", "plan_checker", "reviewer"):
        v = agents.variables[role]
        assert "Committed spec docs/specs/4242.md" in v["description"] and "Reopening closed episodes" in v["description"]
        assert all("declared" not in a for a in v["acceptance_criteria"])                # the declaration is a separate vote
        assert "committed spec" in v["criteria_source"] or role not in ("triage",)
    assert git(repo, "status", "--porcelain") == ""


def test_spec_declaration_that_the_code_contradicts_escalates_the_baseline(tmp_path, cfg):
    wrong = SPEC.replace("- Editing an open episode succeeds and stores the note (src/Care/Episode.cs:1)\n", "- None: new implementation\n") \
                .replace("1. Given a closed episode", "1. Editing an open episode succeeds and stores the note.\n2. Given a closed episode")
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, files={SPEC_FILE: wrong})
    text = (repo / ".work/autodeliver/4242/escalation.md").read_text()
    assert code == 2 and p.state.stage == "plan" and "the reviewed spec declares it additional" in text
    assert agents.calls.count("developer") == 0


def test_risk_tier_and_open_questions_stop_the_run_before_any_agent_runs(tmp_path, cfg):
    for broken, needle in ((SPEC.replace("Medium", "High"), "above spec.max_risk_tier"),
                           (SPEC.replace("- None\n", "- Who may reopen after 30 days?\n"), "Who may reopen after 30 days?"),
                           (SPEC.replace("- **Risk tier:** Medium\n", ""), "Risk tier is missing")):
        sub = tmp_path / needle[:6].strip().replace(" ", "_")
        sub.mkdir()
        p, code, repo, ado, agents = run_pipeline(sub, cfg, files={SPEC_FILE: broken})
        assert code == 2 and p.state.stage == "eligibility" and agents.calls == []
        assert needle in (repo / ".work/autodeliver/4242/escalation.md").read_text()


def test_spec_off_ignores_it_and_required_demands_it(tmp_path, cfg):
    cfg["spec"]["mode"] = "off"
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, files={SPEC_FILE: SPEC.replace("Medium", "High")})
    assert code == 0 and p.state.spec is None and all("declared" not in a for a in p.state.acs)
    sub = tmp_path / "req"
    sub.mkdir()
    cfg["spec"]["mode"] = "required"
    p, code, repo, ado, agents = run_pipeline(sub, cfg)
    assert code == 2 and "no committed spec at docs/specs/4242.md" in (repo / ".work/autodeliver/4242/escalation.md").read_text()


def test_tracker_criteria_that_differ_from_the_spec_are_shown_to_triage(tmp_path, cfg):
    story = {**STORY, "acceptance_criteria": ["Editing a closed episode is refused with a domain error",
                                              "A supervisor can still edit closed episodes within 24 hours"]}
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, story=story, files={SPEC_FILE: SPEC})
    assert code == 0 and len(p.state.tracker_criteria) == 2
    d = agents.variables["triage"]["description"]
    assert "Acceptance criteria as written in the tracker" in d and "within 24 hours" in d


# --- v0.6.0: criteria inherited from the parent story (Epic > Issue > Task) --------------------------------
PARENT_MD = ("**User story:** As a clinician I want closed episodes read-only.\n\n**Acceptance criteria:**\n"
             "- Editing an open episode succeeds and stores the note\n- Editing a closed episode is refused with a domain error\n\n"
             "**Priority:** Must")
PARENT_MD_3 = PARENT_MD.replace("\n\n**Priority:**", "\n- Every refusal is exported to the central audit service\n\n**Priority:**")
CHILD_LINKS = [{"id": "4242", "relation": "child", "type": "Task", "title": "Guard on the aggregate", "state": "To Do"},
               {"id": "4243", "relation": "child", "type": "Task", "title": "Export refusals to the audit service", "state": "To Do"}]


def basic_cfg(cfg):
    cfg["azure_devops"]["criteria_section"] = "Acceptance criteria"
    cfg["azure_devops"]["inherit_criteria"] = "parent"
    cfg["eligibility"]["work_item_types"] = ["Issue", "Task"]
    cfg["eligibility"]["allowed_states"] = ["To Do", "Doing"]
    return cfg


def task_story(parent_md=PARENT_MD, **over):
    parent = {"title": "F09 Refuse edits", "type": "Issue", "state": "To Do", "description": parent_md, "linked": CHILD_LINKS}
    return {"title": "Guard on the aggregate", "type": "Task", "state": "To Do", "tags": ["auto-deliver"],
            "description": "done when dotnet build and dotnet test pass.", "parent_id": 138, "parents": {"138": parent}, **over}


def test_a_task_inherits_the_acceptance_criteria_of_its_parent_issue(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, basic_cfg(cfg), story=task_story())
    assert code == 0, (repo / ".work/autodeliver/4242/escalation.md").read_text() if code else ""
    assert [(a["id"], a["source"]) for a in p.state.acs] == [("AC-1", "parent #138"), ("AC-2", "parent #138")]
    assert p.state.inherited["id"] == 138 and [s["id"] for s in p.state.inherited["siblings"]] == ["4243"]       # not itself
    for role in ("triage", "planner", "plan_checker", "reviewer"):
        v = agents.variables[role]
        assert "inherited from the parent Issue #138" in v["description"] and "Export refusals to the audit service" in v["description"]
        assert "the parent Issue #138" in v["criteria_source"]
        assert v["description"].startswith("done when dotnet build")                        # the Task's own text stays first
    assert (repo / ".work/autodeliver/4242/evidence.md").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_criteria_that_a_sibling_task_delivers_are_deferred_not_demanded(tmp_path, cfg):
    story = task_story(PARENT_MD_3)

    class Defer(FakeAgents):
        def run(self, role, *a, **k):
            r = super().run(role, *a, **k)
            if role == "planner":
                r.output["ac_baseline"].append({"ac_id": "AC-3", "kind": "deferred", "evidence": [], "at_risk": False,
                                                "note": "delivered by #4243 (export refusals to the audit service)"})
            if role == "plan_checker":
                r.output["ac_classification"].append({"ac_id": "AC-3", "kind": "deferred", "evidence": ""})
            return r
    repo = make_repo(tmp_path)
    agents = Defer(repo)
    c = basic_cfg(cfg)
    p = Pipeline(c, repo, 4242, OfflineAzureDevOps(story, c["azure_devops"]["criteria_section"]), agents, dry_run=True,
                 dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 0, (repo / ".work/autodeliver/4242/escalation.md").read_text()
    assert [a["id"] for a in p.state.acs] == ["AC-1", "AC-2", "AC-3"]
    assert "AC-3" not in [a["id"] for a in agents.variables["developer"]["ac_baseline"]]        # not this change's job
    assert "Criteria deferred to other work items" in agents.variables["reviewer"]["description"]
    assert "AC-3" not in {t["ac_id"] for t in p.state.plan["test_plan"]}                         # no test demanded for it
    assert "## Deferred to other work items" in (repo / ".work/autodeliver/4242/notes.md").read_text()
    assert "Deferred to other work items" in (repo / ".work/autodeliver/4242/evidence.md").read_text()


def test_deferral_needs_both_models_to_agree(tmp_path, cfg):
    story = task_story(PARENT_MD_3)

    class OneSided(FakeAgents):
        def run(self, role, *a, **k):
            r = super().run(role, *a, **k)
            if role == "planner":
                r.output["ac_baseline"].append({"ac_id": "AC-3", "kind": "deferred", "evidence": [], "at_risk": False, "note": "#4243"})
            if role == "plan_checker":
                r.output["ac_classification"].append({"ac_id": "AC-3", "kind": "additional", "evidence": ""})
            return r
    repo = make_repo(tmp_path)
    c = basic_cfg(cfg)
    p = Pipeline(c, repo, 4242, OfflineAzureDevOps(story, c["azure_devops"]["criteria_section"]), OneSided(repo), dry_run=True,
                 dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 2 and p.state.stage == "plan"
    assert "AC-3: plan says deferred, independent check says additional" in (repo / ".work/autodeliver/4242/escalation.md").read_text()


def test_deferral_is_refused_without_inheritance_or_without_a_named_work_item():
    plan = {"ac_baseline": [{"ac_id": "AC-1", "kind": "additional", "evidence": [], "at_risk": False, "note": ""},
                            {"ac_id": "AC-2", "kind": "deferred", "evidence": [], "at_risk": False, "note": ""}],
            "test_plan": [{"ac_id": "AC-1", "use_case_ids": [], "test_file": "t.cs", "test_name": "x", "layer": "domain"}],
            "use_cases": [], "tasks": [], "options": [], "chosen_option": ""}
    from autodeliver import gates
    from autodeliver.config import load_config
    cfg = load_config(".", require_remote=False)
    assert any("only allowed for criteria inherited" in d for d in gates.plan_shape(plan, ["AC-1", "AC-2"], cfg, lambda p: True).details)
    assert any("without naming the work item" in d for d in gates.plan_shape(plan, ["AC-1", "AC-2"], cfg, lambda p: True, allow_deferred=True).details)


def test_own_criteria_win_and_inheritance_can_be_switched_off(tmp_path, cfg):
    c = basic_cfg(cfg)
    own = task_story(description="**Acceptance criteria:**\n- Editing an open episode succeeds and stores the note\n"
                                 "- Editing a closed episode is refused with a domain error")
    p, code, repo, ado, agents = run_pipeline(tmp_path, c, story=own)
    assert code == 0 and p.state.inherited is None and p.state.acs[0]["source"] != "parent #138"
    c["azure_devops"]["inherit_criteria"] = "none"
    sub = tmp_path / "none"
    sub.mkdir()
    p, code, repo, ado, agents = run_pipeline(sub, c, story=task_story())
    assert code == 2 and "no usable acceptance criteria" in (repo / ".work/autodeliver/4242/escalation.md").read_text()


def test_inheritance_walks_up_but_not_past_the_depth_limit(tmp_path, cfg):
    story = task_story("(no criteria on the Issue)")
    story["parents"]["138"]["parent_id"] = 100
    story["parents"]["100"] = {"title": "Epic", "type": "Epic", "state": "To Do", "description": PARENT_MD, "linked": []}
    c = basic_cfg(cfg)
    p, code, repo, ado, agents = run_pipeline(tmp_path, c, story=story)
    assert code == 0 and p.state.inherited["id"] == 100 and p.state.inherited["type"] == "Epic"
    c["azure_devops"]["inherit_max_depth"] = 1
    sub = tmp_path / "shallow"
    sub.mkdir()
    p, code, repo, ado, agents = run_pipeline(sub, c, story=story)
    assert code == 2 and p.state.inherited is None


# --- v0.6.2: a dry run leaves nothing behind in the checkout ------------------------------------------------
def test_dry_run_returns_the_checkout_to_where_it_started_and_keeps_the_result_as_a_patch(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, keep_branch=False)
    assert code == 0
    assert git(repo, "branch", "--list", "feature/*").strip() != "" and git(repo, "branch", "--show-current").strip() == "main"
    assert git(repo, "status", "--porcelain") == "" and not (repo / "tests/Care.Tests/EpisodeAcceptanceTests.cs").exists()
    assert "IMPLEMENTED" not in (repo / "src/Care/Episode.cs").read_text()
    patch = (repo / ".work/autodeliver/4242/dry-run.patch").read_text()
    assert "IMPLEMENTED" in patch and "EpisodeAcceptanceTests.cs" in patch                  # code and tests, as committed
    assert any("branch feature/4242" in e["message"] and "kept" in e["message"] for e in p.state.events)


def test_a_second_dry_run_on_the_same_checkout_works(tmp_path, cfg):
    repo = make_repo(tmp_path)
    for _ in range(2):
        p = Pipeline(cfg, repo, 4242, OfflineAzureDevOps(STORY), FakeAgents(repo), dry_run=True, dotnet_factory=lambda: FakeDotnet(repo))
        assert p.run() == 0, (repo / ".work/autodeliver/4242/escalation.md").read_text()
    names = git(repo, "branch", "--list", "feature/*").split()
    assert len(names) == 2 and any(n.endswith("-dryrun-2") for n in names)     # the first branch is untouched


def test_dry_run_cleanup_after_an_escalation_keeps_unfinished_files_and_restores_a_detached_checkout(tmp_path, cfg):
    repo = make_repo(tmp_path)
    start = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "--detach")
    agents = FakeAgents(repo, "bad")
    p = Pipeline(cfg, repo, 4242, OfflineAzureDevOps(STORY), agents, dry_run=True, dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 2
    assert git(repo, "rev-parse", "HEAD").strip() == start and git(repo, "branch", "--show-current").strip() == ""
    assert git(repo, "branch", "--list", "feature/*").strip() != "" and git(repo, "status", "--porcelain") == ""
    assert "EpisodeAcceptanceTests.cs" in (repo / ".work/autodeliver/4242/dry-run.patch").read_text()    # staged work is in the patch


def test_keep_branch_and_real_runs_are_left_alone(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, cfg, keep_branch=True)
    assert code == 0 and git(repo, "branch", "--list", "feature/*").strip() != "" and not (repo / ".work/autodeliver/4242/dry-run.patch").exists()
    sub = tmp_path / "live"
    sub.mkdir()
    p, code, repo, ado, agents = run_pipeline(sub, cfg, dry_run=False, with_origin=True, keep_branch=False)
    assert code == 0 and git(repo, "branch", "--list", "feature/*").strip() != ""


def test_files_the_agents_left_untracked_are_copied_before_cleanup(tmp_path, cfg):
    repo = make_repo(tmp_path)

    class Untracked(FakeAgents):
        def run(self, role, *a, **k):
            r = super().run(role, *a, **k)
            if role == "developer":
                (repo / "src/Care/NewHelper.cs").write_text("// agent-created, never staged\n")
            return r
    p = Pipeline(cfg, repo, 4242, OfflineAzureDevOps(STORY), Untracked(repo), dry_run=True, dotnet_factory=lambda: FakeDotnet(repo))
    p.run()
    assert not (repo / "src/Care/NewHelper.cs").exists() and git(repo, "status", "--porcelain") == ""


def test_autodeliver_never_deletes_a_branch():
    import re
    root = Path(__file__).resolve().parents[1] / "src" / "autodeliver"
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r'"branch"[^\n]*"-[dD]"|"--delete"|delete_branch|push[^\n]*--delete|:refs/heads', text), f


# --- v0.7.0: the board follows the work (item and child tasks) ---------------------------------------------
def board_story(**over):
    story = {**STORY, "type": "Issue", "state": "To Do", "type_states": ["To Do", "Doing", "Done"],
             "linked": [{"id": "501", "relation": "child", "type": "Task", "title": "a", "state": "To Do"},
                        {"id": "502", "relation": "child", "type": "Task", "title": "b", "state": "Done"},
                        {"id": "503", "relation": "parent", "type": "Epic", "title": "e", "state": "To Do"}]}
    return {**story, **over}


def board_cfg(cfg):
    cfg["eligibility"]["work_item_types"] = ["Issue", "Task"]
    cfg["eligibility"]["allowed_states"] = ["To Do", "Doing"]
    cfg["on_success"]["dev_done_states"] = ["Doing"]
    return cfg


def test_a_real_run_starts_the_item_and_its_open_children_and_leaves_finished_ones(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, board_cfg(cfg), dry_run=False, with_origin=True, story=board_story())
    assert code == 0, (repo / ".work/autodeliver/4242/escalation.md").read_text() if code else ""
    moves = [c for c in p.state.status_changes if c["phase"] == "start"]
    assert [(c["id"], c["from"], c["to"]) for c in moves] == [(4242, "To Do", "Doing"), (501, "To Do", "Doing")]
    assert [w[1] for w in ado.writes if w[0] == "state"] == ["Doing", "Doing"]          # 502 (Done) and the parent untouched
    assert "### Board" in (repo / ".work/autodeliver/4242/evidence.md").read_text()


def test_a_dry_run_reports_the_board_moves_but_writes_nothing(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, board_cfg(cfg), story=board_story())
    assert code == 0 and [w for w in ado.writes if w[0] == "state"] == []
    assert any("dry run: would move" in e["message"] and "AB#501" in e["message"] for e in p.state.events)


def test_children_follow_the_item_to_the_dev_done_state_after_the_pr(tmp_path, cfg):
    c = board_cfg(cfg)
    c["on_start"]["transition"] = False
    c["on_success"]["dev_done_states"] = ["Doing"]
    p, code, repo, ado, agents = run_pipeline(tmp_path, c, dry_run=False, with_origin=True, story=board_story())
    assert code == 0
    assert [(x["id"], x["to"], x["phase"]) for x in p.state.status_changes] == [(501, "Doing", "success")]
    assert [w[0] for w in ado.writes].index("pr") < [w[0] for w in ado.writes].index("state")     # PR first


def test_a_run_that_escalates_after_starting_puts_the_board_back(tmp_path, cfg):
    p, code, repo, ado, agents = run_pipeline(tmp_path, board_cfg(cfg), "bad", dry_run=False, with_origin=True, story=board_story())
    assert code == 2
    states = [w[1] for w in ado.writes if w[0] == "state"]
    assert states == ["Doing", "Doing", "To Do", "To Do"]                                 # moved, then restored
    assert all(c.get("reverted") for c in p.state.status_changes if c["phase"] == "start")


def test_a_rejected_transition_is_reported_and_does_not_stop_the_run(tmp_path, cfg):
    class Strict(OfflineAzureDevOps):
        def set_state(self, wid, state):
            if wid == 501:
                raise RuntimeError("TF401320: rule error")
            super().set_state(wid, state)
    repo = make_repo(tmp_path, True)
    p = Pipeline(board_cfg(cfg), repo, 4242, Strict(board_story()), FakeAgents(repo), dry_run=False,
                 dotnet_factory=lambda: FakeDotnet(repo))
    assert p.run() == 0
    assert any(c["id"] == 501 and c["done"] is False for c in p.state.status_changes)


def test_mover_never_picks_closed_and_respects_the_process_spelling():
    from autodeliver.workflow import pick_state
    assert pick_state(["To Do", "Doing", "Done"], ["Resolved", "Done"]) == "Done"
    assert pick_state(["New", "Active", "Closed"], ["closed", "Resolved"]) is None
    assert pick_state(["to do", "DOING"], ["Doing"]) == "DOING"
