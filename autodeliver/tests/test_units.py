import copy
from pathlib import Path

import pytest

from conftest import ROOT, make_niss
from autodeliver import gates
from autodeliver.ado import extract_acceptance_criteria
from autodeliver.agents import BASH_DOTNET, ToolPolicy, render
from autodeliver.config import ConfigError, validate
from autodeliver.dotnet import BuildResult, MutationResult, TestResult, mutation_score, parse_build_counts, parse_trx_dir
from autodeliver.git import NumStat
from autodeliver.globs import match, match_any
from autodeliver.phi import AddedLine, PhiScanner, added_lines, niss_valid


# --- globs -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("path,pattern,expected", [
    ("src/Auth/Login.cs", "**/Auth/**", True),
    ("Auth/Login.cs", "**/Auth/**", True),
    ("src/Authoring/X.cs", "**/Auth/**", False),
    (".autodeliver/config.yml", ".autodeliver/**", True),
    ("src\\Data\\Migrations\\001.cs", "**/Migrations/**", True),
    ("src/App/appsettings.Production.json", "**/appsettings*.json", True),
    ("tests/A.Tests/X.cs", "tests/**", True),
    ("src/A.Tests/X.cs", "**/*.Tests/**", True),
    ("src/A/X.cs", "**/*.Tests/**", False),
])
def test_glob(path, pattern, expected):
    assert match(path, pattern) is expected


# --- config ----------------------------------------------------------------------------------------
def test_config_rejects_unsafe_values(cfg):
    for mutate, needle in [
        (lambda c: c["models"].__setitem__("reviewer", c["models"]["developer"]), "reviewer"),
        (lambda c: c["models"].__setitem__("plan_checker", c["models"]["planner"]), "plan_checker"),
        (lambda c: c["on_escalate"].__setitem__("push_branch", True), "push_branch"),
        (lambda c: c["scope"]["deny_paths"].remove(".autodeliver/**"), "autodeliver"),
        (lambda c: c["on_success"].__setitem__("draft_pr", False), "draft_pr"),
        (lambda c: c["loops"].__setitem__("dev", 50), "loops.dev"),
    ]:
        bad = copy.deepcopy(cfg)
        mutate(bad)
        with pytest.raises(ConfigError, match=needle):
            validate(bad, require_remote=False)


def test_config_requires_remote_settings(cfg):
    with pytest.raises(ConfigError, match="azure_devops"):
        validate(cfg, require_remote=True)


# --- acceptance criteria extraction ----------------------------------------------------------------
def test_extract_ac_from_list():
    acs = extract_acceptance_criteria("<ul><li>AC1: The export contains a header row</li><li>2) Empty lists return 204</li></ul>")
    assert acs == [{"id": "AC-1", "text": "The export contains a header row", "source": "acceptance"},
                   {"id": "AC-2", "text": "Empty lists return 204", "source": "acceptance"}]
    from autodeliver.ado import extract_criteria
    assert extract_criteria("<ul><li>SC1: Refusals are audited</li></ul>", "SC") == [
        {"id": "SC-1", "text": "Refusals are audited", "source": "security"}]


def test_extract_ac_from_paragraphs():
    acs = extract_acceptance_criteria("<div>- Given a closed episode, editing is refused</div><div><br></div><div>- Audit entry is written on refusal</div>")
    assert [a["id"] for a in acs] == ["AC-1", "AC-2"]
    assert acs[1]["text"].startswith("Audit entry")


def test_extract_ac_empty():
    assert extract_acceptance_criteria("") == []


# --- PHI scanner -----------------------------------------------------------------------------------
def test_niss_checksum():
    assert niss_valid(make_niss("850101123"))
    base = "050101123"                                     # born 2000+: check digits use 2 + base
    assert niss_valid(base + f"{97 - (2_000_000_000 + int(base)) % 97:02d}")
    assert not niss_valid("85010112300")
    assert not niss_valid("85130112345")                   # month 13 invalid


def test_scanner_flags_valid_niss_but_not_random_digits(cfg):
    s = PhiScanner(cfg)
    n = make_niss("850101123")
    hits = s.scan([AddedLine("tests/A.cs", 3, f'var x = "{n[:2]}.{n[2:4]}.{n[4:6]}-{n[6:9]}.{n[9:]}";')])
    assert [f.rule for f in hits] == ["phi.national-number"]
    assert n not in hits[0].render()                        # reports are masked
    assert not s.scan([AddedLine("tests/A.cs", 4, 'var x = "12345678901";')])


@pytest.mark.parametrize("line,expected", [
    ('_logger.LogInformation("Saved {PatientName}", p.Name);', "blocking"),
    ('_logger.LogWarning("Lookup failed for {Niss}", niss);', "blocking"),
    ('throw new InvalidOperationException($"Bad birth date {patient.BirthDate}");', "blocking"),
    ('_logger.LogInformation("Processed {PatientId}", id);', "warning"),
    ('_logger.LogInformation("Request from {IpAddress}", ip);', None),
    ('_logger.LogInformation("Sending email notification");', None),
    ('var birthDate = patient.BirthDate;', None),
])
def test_scanner_sinks(cfg, line, expected):
    hits = [f for f in PhiScanner(cfg).scan([AddedLine("src/A.cs", 1, line)]) if f.rule == "phi.sink"]
    assert (hits[0].severity if hits else None) == expected


def test_scanner_secrets_and_email(cfg):
    s = PhiScanner(cfg)
    rules = {f.rule for f in s.scan([
        AddedLine("a.cs", 1, 'var cs = "Server=x;User Id=sa;Password=Sup3rS3cret;";'),
        AddedLine("a.cs", 2, "-----BEGIN RSA PRIVATE KEY-----"),
        AddedLine("a.cs", 3, 'var mail = "jan.peeters@realmail.be";'),
        AddedLine("a.cs", 4, 'var mail = "someone@example.com";'),
    ])}
    assert rules == {"secret.password", "secret.private-key", "phi.email"}


def test_added_lines_parses_unified_diff():
    diff = "\n".join([
        "diff --git a/src/A.cs b/src/A.cs", "--- a/src/A.cs", "+++ b/src/A.cs",
        "@@ -10,0 +11,2 @@", "+line eleven", "+line twelve",
        "diff --git a/old.cs b/old.cs", "--- a/old.cs", "+++ /dev/null", "@@ -1 +0,0 @@", "-gone",
    ])
    assert added_lines(diff) == [AddedLine("src/A.cs", 11, "line eleven"), AddedLine("src/A.cs", 12, "line twelve")]


# --- dotnet parsing ---------------------------------------------------------------------------------
def test_build_counts():
    assert parse_build_counts("Build succeeded.\n    3 Warning(s)\n    0 Error(s)\n") == (3, 0)
    assert parse_build_counts("Build failed with 2 error(s) and 5 warning(s)") == (5, 2)
    assert parse_build_counts("Build succeeded with 4 warning(s) in 3.2s") == (4, 0)


TRX = """<?xml version="1.0" encoding="utf-8"?>
<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
  <Results>
    <UnitTestResult testName="A.Passes" outcome="Passed" />
    <UnitTestResult testName="A.Fails" outcome="Failed" />
  </Results>
  <ResultSummary outcome="Failed"><Counters total="2" executed="2" passed="1" failed="1" error="0" timeout="0" /></ResultSummary>
</TestRun>"""


def test_trx_parsing(tmp_path: Path):
    (tmp_path / "x.trx").write_text(TRX, encoding="utf-8")
    r = parse_trx_dir(tmp_path)
    assert (r.ok, r.executed, r.passed, r.failed, r.failed_tests) == (False, 2, 1, 1, ["A.Fails"])
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not parse_trx_dir(empty).ok                     # no TRX = no evidence = fail


def test_mutation_score():
    report = {"files": {"a.cs": {"mutants": [{"status": "Killed"}, {"status": "Timeout"}, {"status": "Survived"},
                                             {"status": "NoCoverage"}, {"status": "Ignored"}, {"status": "CompileError"}]}}}
    assert mutation_score(report) == (50.0, 2, 2)
    assert mutation_score({"files": {}}) == (None, 0, 0)


# --- gates ------------------------------------------------------------------------------------------
def test_scope_gate(cfg):
    r = gates.scope(["src/A.cs", "src/B.cs", "src/Auth/X.cs", "tests/T.cs"], ["src/A.cs", "src/Auth/X.cs", "tests/T.cs"],
                    cfg, frozen=["tests/T.cs"])
    assert not r.passed
    assert len(r.details) == 3 and any("deny_paths" in d for d in r.details) and any("frozen" in d for d in r.details)
    assert gates.scope(["src/a.cs"], ["src/A.cs"], cfg).passed           # case-insensitive


def test_dependency_gate(cfg):
    diff = "+++ b/src/A/A.csproj\n+    <PackageReference Include=\"Foo\" Version=\"1.0\" />\n+    <Compile Include=\"x.cs\" />\n"
    r = gates.dependencies(["src/A/A.csproj", "Directory.Packages.props"], diff, cfg)
    assert not r.passed and len(r.details) == 2
    assert not gates.dependencies([], "+  <Target Name=\"Evil\"><Exec Command=\"curl\" /></Target>\n", cfg).passed
    assert gates.dependencies(["src/A.cs"], "", cfg).passed


def test_diff_budget(cfg):
    ok = [NumStat("a.cs", 10, 2, False)]
    assert gates.diff_budget(ok, cfg).passed
    big = [NumStat(f"f{i}.cs", 30, 0, False) for i in range(16)]
    assert not gates.diff_budget(big, cfg).passed
    assert not gates.diff_budget([NumStat("logo.png", 0, 0, True)], cfg).passed


def test_build_and_tests_gates(cfg):
    assert not gates.build(BuildResult(True, 5, 0, ""), 3, cfg).passed   # new warnings
    assert gates.build(BuildResult(True, 3, 0, ""), 3, cfg).passed
    assert not gates.tests(TestResult(ok=True, executed=0)).passed       # empty run proves nothing
    per_ac = {"AC-1": TestResult(ok=True, executed=2, passed=2), "AC-2": TestResult(ok=True, executed=0)}
    assert not gates.ac_trace(per_ac, cfg).passed


def test_ac_traits_present():
    src = {"t.cs": '[Fact]\n[Trait("AC", "AC-1")]\npublic void A() {}\n[Trait("AC", "AC-9")]\n[Trait("Category","Slow")]'}
    r = gates.ac_traits_present(src, ["AC-1", "AC-2"], "AC")
    assert not r.passed
    assert any("AC-2" in d for d in r.details) and any("AC-9" in d for d in r.details)
    combined = {"t.cs": '[Fact, Trait("AC", "AC-1")]\n[Theory , TraitAttribute( "AC" , "AC-2" )]\n// Trait("AC","AC-3") in a comment'}
    r = gates.ac_traits_present(combined, ["AC-1", "AC-2", "AC-3"], "AC")
    assert [d for d in r.details] == ['AC-3: no [Trait("AC", "AC-3")] test']


def test_mutation_gate(cfg):
    assert not gates.mutation(MutationResult(ran=False, score=None), cfg).passed
    assert gates.mutation(MutationResult(ran=True, score=None), cfg).passed
    assert not gates.mutation(MutationResult(ran=True, score=40.0, undetected=6), cfg).passed


def _plan():
    return {"ac_baseline": [{"ac_id": "AC-1", "kind": "additional", "evidence": [], "at_risk": False, "note": ""},
                            {"ac_id": "AC-2", "kind": "existing", "evidence": ["src/A.cs:3"], "at_risk": True, "note": ""},
                            {"ac_id": "AC-3", "kind": "existing", "evidence": ["src/A.cs:9"], "at_risk": False, "note": ""}],
            "what": "w", "why": "y",
            "use_cases": [{"id": "UC-1", "description": "d", "kind": "actor", "in_scope": True, "reason": ""},
                          {"id": "UC-2", "description": "d", "kind": "related", "in_scope": False, "reason": "other flow"}],
            "options": [{"name": "A", "summary": "", "trade_off": "", "design_fit": "", "testability": "",
                         "covers_all_use_cases": True, "preserves_existing_ac": True}],
            "chosen_option": "A", "chosen_rationale": "simplest", "single_option_reason": "only sensible design",
            "tasks": [{"id": "T1", "title": "x", "ac_ids": ["AC-1"],
                       "files": [{"path": "src/A.cs", "change": "modify"}, {"path": "src/New.cs", "change": "create"}]}],
            "test_plan": [{"ac_id": "AC-1", "use_case_ids": ["UC-1"], "test_file": "tests/X.Tests/ATests.cs", "test_name": "A_B_C", "layer": "domain"},
                          {"ac_id": "AC-2", "use_case_ids": [], "test_file": "tests/X.Tests/ATests.cs", "test_name": "Pin_A", "layer": "domain"}],
            "citations": ["src/A.cs:10"]}


ACS = ["AC-1", "AC-2", "AC-3"]


def test_plan_shape(cfg):
    exists = lambda p: p == "src/A.cs"
    r = gates.plan_shape(_plan(), ACS, cfg, exists)
    assert r.passed, r.details
    assert gates.tested_ac_ids(_plan()) == ["AC-1", "AC-2"]          # additional + at-risk existing, not AC-3
    bad = _plan()
    bad["tasks"][0]["files"].append({"path": "src/Data/Migrations/001.cs", "change": "create"})
    bad["citations"] = []
    bad["test_plan"] = bad["test_plan"][:1]                         # at-risk existing AC-2 no longer pinned
    bad["ac_baseline"][2]["evidence"] = []                          # existing without evidence
    r = gates.plan_shape(bad, ACS, cfg, exists)
    assert not r.passed and len(r.details) == 4, r.details


def test_plan_shape_use_cases_and_options(cfg):
    exists = lambda p: p == "src/A.cs"
    p = _plan()
    p["use_cases"].append({"id": "UC-3", "description": "edge", "kind": "edge", "in_scope": True, "reason": ""})
    p["use_cases"][1]["reason"] = ""
    p["options"][0]["preserves_existing_ac"] = False
    p["single_option_reason"] = ""
    details = gates.plan_shape(p, ACS, cfg, exists).details
    assert any("UC-3" in d for d in details) and any("UC-2" in d for d in details)
    assert any("preserve the existing" in d for d in details) and any("single_option_reason" in d for d in details)


def test_classification_agreement():
    check = {"ac_classification": [{"ac_id": "AC-1", "kind": "additional", "evidence": ""},
                                   {"ac_id": "AC-2", "kind": "existing", "evidence": ""},
                                   {"ac_id": "AC-3", "kind": "existing", "evidence": ""}]}
    assert gates.classification_agreement(_plan(), check).passed
    check["ac_classification"][2]["kind"] = "additional"
    r = gates.classification_agreement(_plan(), check)
    assert not r.passed and r.details == ["AC-3: plan says existing, independent check says additional"]


def test_plan_files_split(cfg):
    assert gates.plan_files(_plan(), tests_only=True, cfg=cfg) == ["tests/X.Tests/ATests.cs"]
    assert gates.plan_files(_plan(), tests_only=False, cfg=cfg) == ["src/A.cs", "src/New.cs"]


def test_plan_agreement():
    assert gates.plan_agreement(["src/A.cs", "src/B.cs"], ["src/a.cs", "src/B.cs"], 0.5).passed
    assert not gates.plan_agreement(["src/A.cs"], ["src/Z.cs", "src/Y.cs"], 0.5).passed


def _review(**over):
    v = {"verdict": "APPROVE", "findings": [{"severity": "minor", "file": "a", "line": 1, "issue": "x", "suggestion": ""}],
         "existing_ac_assessment": [{"ac_id": "AC-2", "holds": True, "evidence": "Pin_A"},
                                    {"ac_id": "AC-3", "holds": True, "evidence": "suite"}],
         "ac_assessment": [{"ac_id": "AC-1", "met": True, "evidence": "t"}, {"ac_id": "SC-1", "met": True, "evidence": "x"}],
         "use_case_assessment": [{"uc_id": "UC-1", "status": "met", "evidence": "t"}],
         "health_sweep": {k: {"status": "ok", "note": ""} for k in gates.SWEEP_KEYS}}
    v.update(over)
    return v


def test_review_gate():
    plan = _plan()
    assert gates.review(_review(), plan, ["SC-1"]).passed
    assert not gates.review(_review(findings=[{"severity": "major", "file": "a", "line": 1, "issue": "x"}]), plan, ["SC-1"]).passed
    sweep = {k: {"status": "ok", "note": ""} for k in gates.SWEEP_KEYS}
    sweep["authz"] = {"status": "issue", "note": "endpoint is anonymous"}
    assert not gates.review(_review(health_sweep=sweep), plan, ["SC-1"]).passed
    assert not gates.review(_review(), plan, ["SC-1", "SC-2"]).passed               # SC-2 not assessed
    regressed = _review(existing_ac_assessment=[{"ac_id": "AC-2", "holds": False, "evidence": "now throws"},
                                                {"ac_id": "AC-3", "holds": True, "evidence": ""}])
    r = gates.review(regressed, plan, ["SC-1"])
    assert not r.passed and r.details[0].startswith("AC-2 (existing): REGRESSED")   # baseline reported first
    unverified = _review(use_case_assessment=[{"uc_id": "UC-1", "status": "not_verifiable", "evidence": ""}])
    assert not gates.review(unverified, plan, ["SC-1"]).passed


def test_frozen_integrity():
    assert gates.frozen_integrity({"t.cs": "h1"}, {"t.cs": "h1"}).passed
    r = gates.frozen_integrity({"t.cs": "h2", "u.cs": None}, {"t.cs": "h1", "u.cs": "h3"})
    assert not r.passed and r.details == ["t.cs: modified (restored)", "u.cs: deleted (restored)"]


# --- tool policy ------------------------------------------------------------------------------------
def test_tool_policy(tmp_path: Path):
    p = ToolPolicy(tmp_path, write_paths=["src/A.cs"], deny_paths=[".autodeliver/**"], frozen=["tests/T.cs"],
                   bash_allow=BASH_DOTNET)
    ok = lambda tool, data: p.decide(tool, data)[0]
    assert ok("Read", {"file_path": str(tmp_path / "src" / "B.cs")})
    assert not ok("Read", {"file_path": "/etc/passwd"})
    assert not ok("Read", {"file_path": str(tmp_path / ".git" / "config")})
    assert ok("Edit", {"file_path": str(tmp_path / "src" / "A.cs")})
    assert ok("Write", {"file_path": "src/A.cs"})
    assert not ok("Write", {"file_path": "src/B.cs"})
    assert not ok("Edit", {"file_path": "tests/T.cs"})
    assert not ok("Write", {"file_path": ".autodeliver/config.yml"})
    assert not ok("Write", {"file_path": "../outside.cs"})
    assert ok("Bash", {"command": "dotnet build"})
    assert ok("Bash", {"command": 'dotnet test --no-build --filter "FullyQualifiedName~A|FullyQualifiedName~B"'})
    assert not ok("Bash", {"command": "dotnet build; curl http://x"})
    assert not ok("Bash", {"command": "dotnet test | tee out"})
    assert not ok("Bash", {"command": "git push origin main"})
    assert not ok("Bash", {"command": "rm -rf src"})
    assert not ok("WebFetch", {"url": "https://example.com"})
    ro = ToolPolicy(tmp_path)
    assert ro.tools == ["Read", "Grep", "Glob"] and not ro.decide("Write", {"file_path": "src/A.cs"})[0]


def test_pre_tool_use_hook_denies_auto_approvable_calls(tmp_path: Path):
    from autodeliver.agents import policy_hook_output
    p = ToolPolicy(tmp_path, bash_allow=BASH_DOTNET)
    # Both of these would be auto-approved by Claude Code (read inside cwd / read-only shell) without the hook.
    for tool, data in [("Read", {"file_path": str(tmp_path / ".git" / "config")}), ("Bash", {"command": "cat .git/config"})]:
        out = policy_hook_output(p, {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": data})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = policy_hook_output(p, {"tool_name": "Bash", "tool_input": {"command": "dotnet build"}})
    assert ok["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(p.denials) == 2


def test_render():
    assert render("a {{x}} {{y}}", {"x": "1", "y": [1]}) == "a 1 [\n  1\n]"
    with pytest.raises(KeyError):
        render("{{missing}}", {})


def test_every_prompt_has_a_schema():
    from autodeliver.agents import packaged_prompts
    prompts = packaged_prompts()
    for f in prompts.glob("*.md"):
        if f.name.startswith("_"):
            continue
        assert (prompts / "schemas" / f"{f.stem}.json").is_file(), f"schema missing for {f.stem}"


# --- v0.3.0: arguments that would escape the tool policy ----------------------------------------------
@pytest.mark.parametrize("command", [
    "git diff --output=/home/vsts/.bashrc HEAD",            # writes any file
    "git diff --no-index /etc/passwd /dev/null",            # reads outside the repository
    "git diff --ext-diff HEAD",                             # runs a configured program
    "git log -Oorder.txt",
    "git show HEAD:.env",                                   # NEVER_READ through git
    "git diff -- secrets.prod.json",
    "dotnet build -o /tmp/anywhere",                        # writes outside the repository
    'dotnet test --logger "trx;LogFilePath=/home/x/r.trx"',
    "dotnet test ../other/Other.csproj",
    "dotnet test --results-directory .work/x",
])
def test_tool_policy_refuses_escaping_arguments(tmp_path: Path, command):
    p = ToolPolicy(tmp_path, bash_allow=BASH_DOTNET)
    assert not p.decide("Bash", {"command": command})[0]


@pytest.mark.parametrize("command", [
    "git show HEAD:src/A.cs", "git diff HEAD~1 -- src/A.cs", "git log --oneline -n 20", "git status --porcelain",
    'dotnet test --filter "AC=AC-1"', "dotnet build src/App/App.csproj -c Release --nologo",
    "dotnet test ./tests/A.Tests/A.Tests.csproj", "dotnet restore",
])
def test_tool_policy_keeps_ordinary_commands(tmp_path: Path, command):
    assert ToolPolicy(tmp_path, bash_allow=BASH_DOTNET).decide("Bash", {"command": command})[0]


def test_grep_cannot_print_never_read_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.cs").write_text("x")
    ok = lambda data: ToolPolicy(tmp_path).decide("Grep", data)[0]
    assert ok({"pattern": "x"}) and ok({"pattern": "x", "path": "src", "output_mode": "files_with_matches"})
    assert ok({"pattern": "x", "path": "src/A.cs", "output_mode": "content"})
    assert not ok({"pattern": "x", "output_mode": "content"})                  # whole repo, line output
    assert not ok({"pattern": "x", "path": "src", "output_mode": "content"})
    assert not ok({"pattern": "x", "path": ".env"}) and not ok({"pattern": "x", "path": "/etc"})


def test_agent_shell_keeps_only_the_claude_credential():
    from autodeliver.agents import AGENT_BLANKED_ENV
    from autodeliver.dotnet import SECRET_ENV
    assert "VSS_NUGET_ACCESSTOKEN" in AGENT_BLANKED_ENV and "SYSTEM_ACCESSTOKEN" in AGENT_BLANKED_ENV
    assert set(SECRET_ENV) - set(AGENT_BLANKED_ENV) == {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"} <= SECRET_ENV        # never reaches build/test


# --- v0.3.0: Claude credentials ------------------------------------------------------------------------
def test_claude_auth_mode_and_unexpanded_macros():
    from autodeliver import auth
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x", "ANTHROPIC_API_KEY": "$(ANTHROPIC_API_KEY)", "SONAR_TOKEN": ""}
    assert auth.drop_unexpanded(env) == ["ANTHROPIC_API_KEY", "SONAR_TOKEN"]
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"} and auth.mode(env) == "subscription_token"
    assert auth.mode({}) == "saved_login"
    assert auth.mode({"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"}) == "api_key"     # the CLI's precedence


def test_doctor_ci_accepts_a_subscription_token(tmp_path, monkeypatch, capsys):
    from autodeliver.cli import main
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    main(["doctor", "--ci", "--repo", str(tmp_path)])
    assert "FAIL  Claude credentials" in capsys.readouterr().out
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    main(["doctor", "--ci", "--repo", str(tmp_path)])
    line = next(l for l in capsys.readouterr().out.splitlines() if "Claude credentials" in l)
    assert line.startswith("OK") and "CLAUDE_CODE_OAUTH_TOKEN" in line and "subscription" in line


# --- v0.3.0: Microsoft.Testing.Platform (xunit.v3 with "test": {"runner": ...} in global.json) --------
MTP_OK = """Running tests from tests/Lalana.Connect.Tests/bin/Debug/net10.0/Lalana.Connect.Tests.dll (net10.0|arm64)
tests/Lalana.Connect.Tests/bin/Debug/net10.0/Lalana.Connect.Tests.dll (net10.0|arm64) passed (23s 319ms)

Test run summary: Passed!
  total: 380
  failed: 0
  succeeded: 380
  skipped: 0
  duration: 23s 738ms
"""
MTP_FAILED = """  failed Lalana.Connect.Tests.InvoiceTests.Rejects_empty_vat (12ms)
    Assert.Equal() Failure
  failed Lalana.Connect.Tests.InvoiceTests.Totals_round (1s 3ms)

Test run summary: Failed!
  total: 380
  failed: 2
  succeeded: 377
  skipped: 1
  duration: 24s 1ms
"""
GLOBAL_MTP = '{\n  "sdk": {"version": "10.0.100", "rollForward": "latestFeature"},\n  "test": {"runner": "Microsoft.Testing.Platform"}\n}'


def test_parse_mtp_summary_from_real_output():
    from autodeliver.dotnet import parse_mtp_output
    r = parse_mtp_output(MTP_OK, 0)
    assert (r.ok, r.total, r.executed, r.passed, r.failed, r.code) == (True, 380, 380, 380, 0, 0)
    bad = parse_mtp_output(MTP_FAILED, 2)
    assert (bad.ok, bad.executed, bad.passed, bad.failed) == (False, 379, 377, 2)
    assert bad.failed_tests == ["Lalana.Connect.Tests.InvoiceTests.Rejects_empty_vat", "Lalana.Connect.Tests.InvoiceTests.Totals_round"]


def test_parse_mtp_never_passes_without_a_summary_or_with_a_bad_exit_code():
    from autodeliver.dotnet import MTP_ZERO_TESTS, parse_mtp_output
    assert not parse_mtp_output("Zero tests ran", MTP_ZERO_TESTS).ok                       # filter matched nothing
    assert not parse_mtp_output("error: Unknown option '--logger'", 5).ok                  # rejected command line
    assert not parse_mtp_output(MTP_OK, 2).ok                                              # summary says passed, process did not
    assert parse_mtp_output("\x1b[32m" + MTP_OK.replace("Passed!", "\x1b[32mPassed!\x1b[0m"), 0).ok   # colour codes


def test_runner_detection_from_global_json(tmp_path):
    from autodeliver.dotnet import detect_runner
    assert detect_runner(tmp_path) == "vstest"                                              # no global.json
    (tmp_path / "global.json").write_text('{"sdk": {"version": "10.0.100"}}')
    assert detect_runner(tmp_path) == "vstest"
    (tmp_path / "global.json").write_text(GLOBAL_MTP)
    assert detect_runner(tmp_path) == "mtp"
    assert detect_runner(tmp_path, "vstest") == "vstest"                                    # explicit config wins
    (tmp_path / "global.json").write_text("﻿// comment\n" + GLOBAL_MTP)               # BOM + comment are legal
    assert detect_runner(tmp_path) == "mtp"


def test_dotnet_test_command_per_runner(tmp_path, cfg):
    from autodeliver.dotnet import Dotnet
    (tmp_path / "App.slnx").write_text("<Solution/>")
    vs = Dotnet(tmp_path, cfg)
    assert vs.runner == "vstest" and "--logger" in vs.test_args(tmp_path / "r", "AC=AC-1") and "--filter" in vs.test_args(tmp_path / "r", "AC=AC-1")
    (tmp_path / "global.json").write_text(GLOBAL_MTP)
    mtp = Dotnet(tmp_path, cfg)
    args = mtp.test_args(tmp_path / "r", "AC=AC-1")
    assert args[:4] == ["dotnet", "test", "--solution", "App.slnx"] and "--no-build" in args
    assert args[-2:] == ["--filter-trait", "AC=AC-1"]
    # all rejected by the new runner (exit code 5); --nologo was confirmed rejected on a real xunit.v3 solution
    assert not {"--logger", "--filter", "-v", "--nologo"} & set(args)
    assert "--nologo" in vs.test_args(tmp_path / "r")                                       # VSTest mode keeps it


def test_dotnet_test_reads_mtp_console_summary(tmp_path, cfg, monkeypatch):
    from autodeliver.dotnet import Dotnet
    (tmp_path / "App.slnx").write_text("<Solution/>")
    (tmp_path / "global.json").write_text(GLOBAL_MTP)
    d = Dotnet(tmp_path, cfg)
    monkeypatch.setattr(d, "_run", lambda args, timeout_s=3600: (0, MTP_OK))
    r = d.test(tmp_path / "r")
    assert r.ok and r.executed == 380 and r.code == 0
    monkeypatch.setattr(d, "_run", lambda args, timeout_s=3600: (8, "Zero tests ran"))
    none = d.test(tmp_path / "r", "AC=AC-9")
    assert not none.ok and none.executed == 0 and none.code == 8                           # red, not a crash
    monkeypatch.setattr(d, "_run", lambda args, timeout_s=3600: (0, "no summary at all"))
    assert not d.test(tmp_path / "r").ok                                                    # empty full run proves nothing


def test_agents_may_run_the_mtp_test_command(tmp_path):
    p = ToolPolicy(tmp_path, bash_allow=BASH_DOTNET)
    assert p.decide("Bash", {"command": 'dotnet test --solution LalanaConnect.slnx --no-build --filter-trait "AC=AC-1"'})[0]
    assert not p.decide("Bash", {"command": "dotnet test --solution ../other.slnx"})[0]


def test_doctor_test_stack_xunit_required_mocking_reported(tmp_path):
    from autodeliver.cli import _test_stack
    tests = tmp_path / "tests" / "App.Tests"
    tests.mkdir(parents=True)
    (tests / "App.Tests.csproj").write_text('<Project><ItemGroup><PackageReference Include="xunit.v3" /></ItemGroup></Project>')
    ok, detail = _test_stack(tmp_path)                                                      # central package management, no mocks
    assert ok and "none (hand-written fakes)" in detail
    (tmp_path / "Directory.Packages.props").write_text('<PackageVersion Include="Moq" Version="4.20.0" />')
    assert "Moq" in _test_stack(tmp_path)[1]
    (tests / "App.Tests.csproj").write_text('<Project><ItemGroup><PackageReference Include="NUnit" /></ItemGroup></Project>')
    ok, detail = _test_stack(tmp_path)
    assert not ok and "xunit missing in: App.Tests.csproj" == detail


def test_prompts_do_not_mandate_a_mocking_library(cfg):
    from autodeliver.agents import AgentRunner, packaged_prompts
    text = (packaged_prompts() / "test_author.md").read_text() + (packaged_prompts() / "standards" / "testing.md").read_text()
    assert "Use xUnit and NSubstitute" not in text and "Never add a package" in text


def test_probe_output_shows_the_start_of_a_long_failure():
    from autodeliver.cli import _head_tail
    out = "error: Unknown option '--x'\n" + "line\n" * 400 + "Exit code: 5"
    shown = _head_tail(out)
    assert shown.startswith("error: Unknown option") and shown.endswith("Exit code: 5") and "[...]" in shown
    assert _head_tail("short") == "short"


# --- v0.3.3: a real run from a terminal must be asked for; a missing token is a message, not a traceback ---
def test_real_run_from_a_terminal_needs_yes(tmp_path, monkeypatch, capsys):
    from autodeliver.cli import main
    monkeypatch.delenv("TF_BUILD", raising=False)
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    monkeypatch.delenv("SYSTEM_ACCESSTOKEN", raising=False)
    assert main(["run", "--work-item", "179", "--repo", str(tmp_path)]) == 3
    err = capsys.readouterr().err
    assert "REAL run" in err and "--dry-run" in err and "--yes" in err


def test_dry_run_and_pipeline_runs_are_not_blocked_by_the_guard(tmp_path, monkeypatch, capsys):
    from autodeliver.cli import main
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    monkeypatch.delenv("SYSTEM_ACCESSTOKEN", raising=False)
    monkeypatch.delenv("TF_BUILD", raising=False)
    (tmp_path / ".autodeliver").mkdir()
    (tmp_path / ".autodeliver" / "config.yml").write_text(
        "azure_devops:\n  organization_url: https://dev.azure.com/o\n  project: p\n  repository: r\n")
    assert main(["run", "--work-item", "179", "--dry-run", "--repo", str(tmp_path)]) == 3     # reaches the token check
    err = capsys.readouterr().err
    assert "AZURE_DEVOPS_PAT" in err and "Work Items: Read" in err and "REAL run" not in err and "Traceback" not in err
    monkeypatch.setenv("TF_BUILD", "True")                                                      # inside Azure Pipelines
    assert main(["run", "--work-item", "179", "--repo", str(tmp_path)]) == 3
    assert "REAL run" not in capsys.readouterr().err


# --- v0.4.0: acceptance criteria written in the Description (Azure DevOps Basic process) ----------------
ISSUE_MD = ("**User story:** As the platform, I want to send through an access point hidden behind an interface so I can switch provider\n\n"
            "**Acceptance criteria:** Given a validated invoice, when sent via the sandbox, then a transmission id is stored. "
            "Core has no reference to the provider SDK.\n\n"
            "**Priority:** Must · **Planned:** WE2 · Spec: docs/spec.md")


def _lis(ul: str) -> list[str]:
    import re
    return re.findall(r"<li>(.*?)</li>", ul)


def test_criteria_from_a_markdown_description_paragraph():
    from autodeliver.ado import criteria_from_description
    assert _lis(criteria_from_description(ISSUE_MD, "Acceptance criteria")) == [
        "Given a validated invoice, when sent via the sandbox, then a transmission id is stored.",
        "Core has no reference to the provider SDK."]
    assert criteria_from_description(ISSUE_MD, "Definition of done") == ""
    assert criteria_from_description(ISSUE_MD, "") == ""


def test_criteria_from_html_bullets_headings_and_numbered_lists():
    from autodeliver.ado import criteria_from_description
    html_desc = ("<div><b>Acceptance criteria:</b></div><ul><li>An empty name is rejected</li>"
                 "<li>A valid name is stored &amp; returned</li></ul><div><b>Priority:</b> Must</div>")
    assert _lis(criteria_from_description(html_desc, "acceptance criteria")) == ["An empty name is rejected", "A valid name is stored &amp; returned"]
    md = "Intro\n\n## Acceptance criteria\n1. First rule holds\n2. Second rule holds,\n   continued here\n\n**Notes:** not a criterion"
    assert _lis(criteria_from_description(md, "Acceptance criteria")) == ["First rule holds", "Second rule holds, continued here"]
    one_per_line = "**Acceptance criteria:**\nOrders over 100 are free of delivery fees\nOrders under 100 pay the fee\n**Priority:** 1"
    assert _lis(criteria_from_description(one_per_line, "Acceptance criteria")) == [
        "Orders over 100 are free of delivery fees", "Orders under 100 pay the fee"]


def test_description_criteria_reach_the_pipeline_only_when_configured_and_the_field_is_empty():
    from autodeliver.ado import OfflineAzureDevOps
    story = {"title": "F09", "description": ISSUE_MD, "tags": ["auto-deliver"], "type": "Issue", "state": "To Do"}
    off = OfflineAzureDevOps(story).get_work_item(1)
    assert off.acceptance_html == "" and off.criteria_source == "the Acceptance Criteria field"
    on = OfflineAzureDevOps(story, "Acceptance criteria").get_work_item(1)
    assert len(_lis(on.acceptance_html)) == 2 and "'Acceptance criteria' section of the Description" in on.criteria_source
    both = OfflineAzureDevOps({**story, "acceptance_criteria": ["The field wins when it has content"]}, "Acceptance criteria").get_work_item(1)
    assert _lis(both.acceptance_html) == ["The field wins when it has content"] and both.criteria_source.endswith("field")


def test_azure_devops_reader_takes_criteria_from_the_description(monkeypatch, cfg):
    import copy
    from autodeliver.ado import AzureDevOps

    class Resp:
        status_code, content = 200, b"{}"
        def __init__(self, data): self._d = data
        def json(self): return self._d
    cfg = copy.deepcopy(cfg)
    cfg["azure_devops"]["criteria_section"] = "Acceptance criteria"
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "x")
    ado = AzureDevOps(cfg)
    fields = {"System.WorkItemType": "Issue", "System.State": "To Do", "System.Title": "F09 Access point adapter",
              "System.Description": ISSUE_MD, "System.Tags": "F09; auto-deliver"}
    ado.session.get = lambda url, **k: Resp({"comments": []} if "comments" in url else {"fields": fields, "relations": []})
    wi = ado.get_work_item(138)
    assert len(_lis(wi.acceptance_html)) == 2 and "section of the Description" in wi.criteria_source
    cfg["azure_devops"]["criteria_section"] = ""
    assert AzureDevOps(cfg).criteria_section == ""


# --- v0.5.0: the committed spec of the agentic-SDLC convention (docs/specs/<id>.md) ---------------------
FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_the_starters_sample_spec():
    from autodeliver.spec import parse_spec
    sp = parse_spec((FIXTURES / "spec_42.md").read_text(), "docs/specs/42.md")
    assert sp.title == "Issue a credit note for an invoice" and sp.risk_tier == "High"
    assert sp.existing == [] and len(sp.additional) == 3 and sp.additional[0].startswith("Given an issued invoice")
    assert sp.security == [] and sp.open_questions == []
    assert "Refunding money" in sp.sections["non_goals"] and len(sp.sections["edge_cases"].splitlines()) == 2


def test_parse_the_unfilled_template_finds_nothing_real():
    from autodeliver.spec import parse_spec, problems
    sp = parse_spec((FIXTURES / "spec_template.md").read_text(), "docs/specs/1.md")
    assert sp.risk_tier is None and sp.existing == [] and sp.additional == [] and sp.security == []
    assert sp.open_questions == []                                                 # the template's own guidance is not a question
    assert any("Risk tier is missing" in p for p in problems(sp, "Medium", True))
    assert any("nothing to build" in p for p in problems(sp, "Medium", True))


def test_spec_sections_existing_security_open_questions_and_tiers():
    from autodeliver.spec import parse_spec, problems
    raw = ("# 7: Reopen an episode\n\n- **Risk tier:** Medium\n\n## Existing acceptance criteria\n\n"
           "- A closed episode is read-only (src/Care/Episode.cs:12)\n- Audit rows are append-only (src/Care/Audit.cs:30)\n\n"
           "## Acceptance criteria\n\n1. Given a closed episode, when a supervisor reopens it, then it becomes editable.\n\n"
           "## Security criteria\n\n- Reopening is written to the audit log\n\n## Open questions\n\n- None\n")
    sp = parse_spec(raw, "docs/specs/7.md")
    assert len(sp.existing) == 2 and len(sp.additional) == 1 and sp.security == ["Reopening is written to the audit log"]
    assert sp.open_questions == [] and problems(sp, "Medium", True) == []
    assert any("above spec.max_risk_tier" in p for p in problems(sp, "Low", True))
    open_q = parse_spec(raw.replace("- None\n", "- Who may reopen after 30 days?\n"), "docs/specs/7.md")
    assert open_q.open_questions == ["Who may reopen after 30 days?"]
    assert any("open question" in p for p in problems(open_q, "Medium", True)) and problems(open_q, "Medium", False) == []


def test_tracker_pointer_to_the_spec_is_not_a_criterion():
    from autodeliver.spec import is_pointer
    assert is_pointer("See docs/specs/42.md.", "docs/specs/42.md") and is_pointer("details in specs/42.md", "docs/specs/42.md")
    assert not is_pointer("An empty name is rejected", "docs/specs/42.md")


def test_classification_gate_counts_the_spec_declaration_as_a_third_vote():
    plan = {"ac_baseline": [{"ac_id": "AC-1", "kind": "existing"}, {"ac_id": "AC-2", "kind": "additional"}]}
    check = {"ac_classification": [{"ac_id": "AC-1", "kind": "existing"}, {"ac_id": "AC-2", "kind": "additional"}]}
    assert gates.classification_agreement(plan, check).passed
    assert gates.classification_agreement(plan, check, {"AC-1": "existing", "AC-2": "additional"}).passed
    wrong = gates.classification_agreement(plan, check, {"AC-1": "existing", "AC-2": "existing"})
    assert not wrong.passed and "reviewed spec declares it existing" in wrong.details[0]
    split = {"ac_classification": [{"ac_id": "AC-1", "kind": "additional"}, {"ac_id": "AC-2", "kind": "additional"}]}
    assert not gates.classification_agreement(plan, split, {"AC-1": "existing"}).passed


def test_spec_config_values_are_validated(cfg):
    for key, bad in (("mode", "sometimes"), ("path", "docs/spec.md"), ("max_risk_tier", "Severe")):
        broken = copy.deepcopy(cfg)
        broken["spec"][key] = bad
        with pytest.raises(ConfigError, match="spec"):
            validate(broken)
