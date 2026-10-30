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


def make_repo(tmp: Path, with_origin: bool = False) -> Path:
    repo = tmp / "repo"
    (repo / "src" / "Care").mkdir(parents=True)
    (repo / "tests" / "Care.Tests").mkdir(parents=True)
    (repo / "Care.sln").write_text("sln")
    (repo / "AGENTS.md").write_text("conventions")
    (repo / "src" / "Care" / "Episode.cs").write_text("public class Episode { }\n")
    (repo / "tests" / "Care.Tests" / "EpisodeTests.cs").write_text("public class EpisodeTests { }\n")
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
                "ac_assessment": [{"ac_id": "AC-2", "met": True, "evidence": "Edit_WhenClosed_Throws"},
                                  {"ac_id": "SC-1", "met": True, "evidence": "Episode.cs:12 audit call"}],
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


def run_pipeline(tmp_path: Path, cfg, behaviour="good", *, dry_run=True, with_origin=False, checker_ac1="existing", story=None):
    repo = make_repo(tmp_path, with_origin)
    ado = OfflineAzureDevOps(story or STORY)
    agents = FakeAgents(repo, behaviour, checker_ac1)
    p = Pipeline(cfg, repo, 4242, ado, agents, dry_run=dry_run, dotnet_factory=lambda: FakeDotnet(repo))
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
