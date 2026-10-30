"""autodeliver command line.

  autodeliver init      [--repo R] [--tool-repo PROJECT/REPO] [--force]
  autodeliver doctor    [--repo R] [--ci]
  autodeliver tools install [--repo R]
  autodeliver config show   [--repo R]
  autodeliver run (--work-item N | --next) [--dry-run [--story-file F]] [--repo R]

Exit codes for `run`: 0 draft PR published (or dry run complete) · 1 not eligible · 2 escalated · 3 internal error.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

import yaml

from . import __version__, auth
from .config import OVERLAY_PATH, ConfigError, load_config, warnings
from .dotnet import MTP_ZERO_TESTS
from .repo_info import detect

PIPELINE_PATH = Path(".azuredevops") / "autodeliver.yml"

# `init --process`: what differs from the packaged defaults (written for Agile-like projects with an
# Acceptance Criteria field). Basic has Epic / Issue / Task, states To Do / Doing / Done and no such field.
PROCESSES = {
    "agile": ("", ""),
    "basic": (
        "  criteria_section: Acceptance criteria   # no Acceptance Criteria field in Basic: read the criteria from this section of the Description\n"
        "  inherit_criteria: parent                # a Task takes the criteria of its parent Issue (Epic > Issue > Task)\n",
        "\n# Azure DevOps Basic process: Issue / Task; states To Do / Doing / Done\n"
        "eligibility:\n  work_item_types: [Issue, Task]\n  allowed_states: [To Do, Doing]\n"
        "on_success:\n  dev_done_states: [Doing]               # never Done: a human completes the item\n\n",
    ),
}


# --- init ------------------------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"{repo} is not a git repository", file=sys.stderr)
        return 2
    remote, branch = detect(repo)
    values = {
        "organization_url": remote.organization_url if remote else "https://dev.azure.com/CHANGE_ME",
        "project": remote.project if remote else "CHANGE_ME",
        "repository": remote.repository if remote else "CHANGE_ME",
        "target_branch": branch,
        "process_azure": PROCESSES[args.process][0],
        "process_blocks": PROCESSES[args.process][1],
        "version": __version__,
        "tool_repo": args.tool_repo or (f"{remote.project}/autodeliver-dotnet" if remote else "CHANGE_ME/autodeliver-dotnet"),
    }
    written = []
    for rel, template in ((OVERLAY_PATH, "config.yml.tmpl"), (PIPELINE_PATH, "pipeline.yml.tmpl")):
        target = repo / rel
        if target.exists() and not args.force:
            print(f"keep     {rel.as_posix()} (exists; --force to overwrite)")
            continue
        text = resources.files("autodeliver").joinpath(f"templates/{template}").read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.format(**values), encoding="utf-8", newline="\n")
        written.append(rel.as_posix())
        print(f"write    {rel.as_posix()}")
    if not remote:
        print("note     origin is not an Azure DevOps remote: fill in azure_devops.* in .autodeliver/config.yml")
    print("\nNext:\n  1. Review and commit " + (", ".join(written) or "the files above") +
          "\n  2. In Azure DevOps: create pipeline from .azuredevops/autodeliver.yml, variable group 'autodeliver-secrets'"
          " (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, or ANTHROPIC_API_KEY for a shared/company pipeline),"
          " build identity permissions (see README)\n  3. autodeliver doctor")
    return 0


# --- doctor ----------------------------------------------------------------------------------------
def _secret(name: str) -> bool:
    """Set and not an unexpanded Azure Pipelines macro such as '$(ANTHROPIC_API_KEY)'."""
    return auth.is_set(os.environ.get(name, ""))


def _version(cmd: list[str]) -> str | None:
    exe = shutil.which(cmd[0])
    if not exe:
        return None
    proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (proc.stdout or proc.stderr).strip().splitlines()[0] if proc.returncode == 0 and (proc.stdout or proc.stderr) else None


def cmd_doctor(args: argparse.Namespace) -> int:
    from .dotnet import detect_solution
    from .tools import dotnet_tools_dir, find_gitleaks

    repo = args.repo.resolve()
    checks: list[tuple[str, bool, str]] = []
    ok = lambda name, passed, detail="": checks.append((name, passed, detail))

    ok("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    ok("claude-agent-sdk", importlib.util.find_spec("claude_agent_sdk") is not None, "pip install autodeliver-dotnet")
    git = _version(["git", "--version"])
    ok("git", bool(git), git or "not found")
    dotnet = _version(["dotnet", "--version"])
    ok("dotnet SDK", bool(dotnet), dotnet or "not found")
    cfg = None
    try:
        cfg = load_config(repo, require_remote=True)
        ok("config", True, f"{OVERLAY_PATH.as_posix()} + packaged defaults")
    except ConfigError as e:
        ok("config", False, str(e))
    try:
        ok("solution", True, detect_solution(repo, cfg["dotnet"]["solution"] if cfg else ""))
    except RuntimeError as e:
        ok("solution", False, str(e))
    if cfg:
        tool_path = os.pathsep.join([os.environ.get("PATH", ""), str(dotnet_tools_dir())])
        if cfg["mutation"].get("enabled"):
            ok("dotnet-stryker", bool(shutil.which("dotnet-stryker", path=tool_path)), "autodeliver tools install")
        if cfg["sonar"].get("enabled"):
            ok("dotnet-sonarscanner", bool(shutil.which("dotnet-sonarscanner", path=tool_path)), "autodeliver tools install")
            ok("SONAR_TOKEN", _secret("SONAR_TOKEN"), "secret variable")
        if cfg["phi"].get("gitleaks"):
            found = find_gitleaks(cfg)
            ok("gitleaks", bool(found), found or "autodeliver tools install")
    claude_mode = auth.mode()
    if args.ci:
        ok("Claude credentials", claude_mode != "saved_login",
           f"{auth.DESCRIPTION[claude_mode]}" if claude_mode != "saved_login"
           else f"set {auth.OAUTH_TOKEN} (subscription) or {auth.API_KEY} as a secret variable")
    else:
        ok("Claude credentials", True, auth.DESCRIPTION[claude_mode])
    if claude_mode == "api_key" and _secret(auth.OAUTH_TOKEN):
        checks.append(("Claude credentials", True, f"both are set: the CLI uses {auth.API_KEY}, not your subscription"))
    has_token = _secret("SYSTEM_ACCESSTOKEN") or _secret("AZURE_DEVOPS_PAT")
    ok("Azure DevOps token", has_token or not args.ci, "SYSTEM_ACCESSTOKEN (pipeline) or AZURE_DEVOPS_PAT (local)")
    tests_ok, detail = _test_stack(repo)
    ok("xUnit test projects", tests_ok, detail)
    if cfg and getattr(args, "probe_tests", False):
        _probe_tests(repo, cfg, ok)

    width = max(len(c[0]) for c in checks)
    for name, passed, detail in checks:
        print(f"{'OK  ' if passed else 'FAIL'}  {name.ljust(width)}  {detail}")
    for w in warnings(cfg) if cfg else []:
        print(f"WARN  {w}")
    return 0 if all(p for _, p, _ in checks) else 1


def _test_stack(repo: Path) -> tuple[bool, str]:
    """xUnit is required. The mocking library is only reported: the agents follow what the repository uses."""
    projects = [p for p in repo.rglob("*.csproj") if "test" in p.name.lower() and "bin" not in p.parts and "obj" not in p.parts]
    if not projects:
        return False, "no test projects found (name must contain 'test')"
    shared = [repo / n for n in ("Directory.Build.props", "Directory.Packages.props")]
    texts = {p: p.read_text(encoding="utf-8", errors="replace").lower() for p in [*projects, *[f for f in shared if f.is_file()]]}
    missing = [p.name for p in projects if "xunit" not in texts[p]]
    mocks = sorted({lib for t in texts.values() for lib in ("NSubstitute", "Moq", "FakeItEasy") if lib.lower() in t})
    detail = f"{len(projects)} test project(s); mocking: {', '.join(mocks) or 'none (hand-written fakes)'}"
    return (not missing), (detail if not missing else f"xunit missing in: {', '.join(missing[:5])}")


def _head_tail(text: str, head: int = 700, tail: int = 500) -> str:
    """The cause of a rejected command line is at the start of the output, test failures near the end."""
    text = text.strip()
    return text if len(text) <= head + tail else f"{text[:head]}\n      [...]\n{text[-tail:]}"


def _probe_tests(repo: Path, cfg: dict, ok) -> None:
    """Run the real build and test commands once, so the runner support is verified on this machine."""
    import tempfile
    from .dotnet import Dotnet
    d = Dotnet(repo, cfg)
    tmp = Path(tempfile.mkdtemp(prefix="autodeliver-probe-"))
    ok("test runner", True, f"{d.runner} ({' '.join(d.test_args(tmp / 'x', 'AC=AC-1')[:6])} ...)")
    b = d.build()
    ok("probe: build", b.ok, f"{b.errors} error(s), {b.warnings} warning(s)" if b.ok else b.tail[-300:])
    if not b.ok:
        return
    full = d.test(tmp / "full")
    cmd = " ".join(d.test_args(tmp / "full"))
    ok("probe: full test run", full.ok and full.executed > 0,
       f"executed={full.executed} passed={full.passed} failed={full.failed} exit={full.code}" if full.ok else
       f"exit={full.code}, executed={full.executed}, failed={full.failed}\n      command: {cmd}\n"
       f"{_head_tail(full.tail)}")
    # A trait nobody uses: the runner must accept the option and report zero tests, not reject the command line.
    none = d.test(tmp / "none", f"{cfg['tests']['ac_trait_name']}=AC-0")
    accepted = none.executed == 0 and none.code in (0, MTP_ZERO_TESTS if d.runner == "mtp" else 0)
    ok("probe: trait filter", accepted, f"exit={none.code}, executed={none.executed}" if accepted else _head_tail(none.tail))


# --- tools -----------------------------------------------------------------------------------------
def cmd_tools(args: argparse.Namespace) -> int:
    from .tools import announce_path, dotnet_tools_dir, install_dotnet_tool, install_gitleaks

    cfg = load_config(args.repo.resolve(), require_remote=False)
    t = cfg["tools"]
    failed = False
    wanted = []
    if cfg["mutation"].get("enabled"):
        wanted.append(("dotnet-stryker", t.get("stryker_version", "")))
    if cfg["sonar"].get("enabled"):
        wanted.append(("dotnet-sonarscanner", t.get("sonarscanner_version", "")))
    for package, version in wanted:
        good, out = install_dotnet_tool(package, version)
        failed |= not good
        print(f"{'OK  ' if good else 'FAIL'}  {package}  {'' if good else out}")
    if wanted:
        announce_path(dotnet_tools_dir())
    if cfg["phi"].get("gitleaks"):
        try:
            binary = install_gitleaks(cfg)
            print(f"OK    gitleaks {t['gitleaks_version']}  {binary}")
            announce_path(binary.parent)
        except Exception as e:  # noqa: BLE001
            failed = True
            print(f"FAIL  gitleaks  {e}")
    return 1 if failed else 0


# --- config ----------------------------------------------------------------------------------------
def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.repo.resolve(), require_remote=False)
    print(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    return 0


# --- run -------------------------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    from .ado import AzureDevOps, OfflineAzureDevOps
    from .agents import AgentRunner
    from .pipeline import Pipeline

    offline = args.dry_run and args.story_file is not None
    if args.next and offline:
        print("--next needs Azure DevOps; use --work-item with --story-file", file=sys.stderr)
        return 3
    if args.story_file and not args.dry_run:
        print("--story-file is only allowed with --dry-run", file=sys.stderr)
        return 3
    # A real run pushes a branch, opens a PR and writes to the ticket. In the pipeline that is the point;
    # from a terminal it must be asked for explicitly (Azure Pipelines always sets TF_BUILD).
    if not args.dry_run and not os.environ.get("TF_BUILD") and not args.yes:
        print("autodeliver: this is a REAL run: it pushes a branch, opens a draft PR and writes to the work item.\n"
              "  To try it safely add --dry-run (reads the ticket, writes nothing, pushes nothing).\n"
              "  To really run it from this terminal, add --yes.", file=sys.stderr)
        return 3
    repo = args.repo.resolve()
    cfg = load_config(repo, args.config, require_remote=not offline)
    if offline:
        ado = OfflineAzureDevOps(json.loads(args.story_file.read_text(encoding="utf-8")),
                                 cfg["azure_devops"].get("criteria_section", ""))
    else:
        try:
            live = AzureDevOps(cfg)
        except RuntimeError as e:
            print(f"autodeliver: {e}", file=sys.stderr)
            if "AZURE_DEVOPS_PAT" in str(e):
                print('  Local use: create a token with the scope "Work Items: Read" and run\n'
                      '    read -s "AZURE_DEVOPS_PAT?Paste the token, then press Enter: "; export AZURE_DEVOPS_PAT; echo',
                      file=sys.stderr)
            return 3
        ado = _ReadOnly(live) if args.dry_run else live
        if args.next:
            el = cfg["eligibility"]
            found = live.next_eligible(el["required_tag"], cfg["on_escalate"]["add_tag"], el["work_item_types"])
            if found is None:
                print("autodeliver: no eligible work item")
                return 1
            args.work_item = found
    work = repo / ".work" / "autodeliver" / str(args.work_item)
    agents = AgentRunner(repo, cfg, work / "transcripts")
    pipeline = Pipeline(cfg, repo, args.work_item, ado, agents, dry_run=args.dry_run, keep_branch=args.keep_branch)
    code = pipeline.run()
    s = pipeline.state
    print(f"autodeliver AB#{args.work_item}: {s.status} at {s.stage} · loops {s.loops} · ${s.cost_usd:.2f}"
          + (f" · {s.pr_url}" if s.pr_url else "") + f" · details {work}")
    return code


def cmd_complete(args: argparse.Namespace) -> int:
    """After the merge: the item and its child tasks go to Done. A human decision, so it asks for --yes."""
    from .ado import AzureDevOps
    from .workflow import Mover, describe

    cfg = load_config(args.repo.resolve(), args.config)
    try:
        ado = AzureDevOps(cfg)
    except RuntimeError as e:
        print(f"autodeliver: {e}", file=sys.stderr)
        return 3
    wi = ado.get_work_item(args.work_item)
    oc = cfg["on_complete"]
    need = str(oc.get("require_tag", "")).lower()
    if need and need not in [t.lower() for t in wi.tags] and not args.force:
        print(f"autodeliver: AB#{wi.id} has no '{oc['require_tag']}' tag, so autodeliver did not deliver it. Use --force to move it anyway.",
              file=sys.stderr)
        return 3
    mover = Mover(ado)
    changes, notes = mover.plan(wi, list(oc["done_states"]), cascade=bool(oc.get("cascade_children")))
    for line in describe(changes) + notes:
        print(("would move " if not args.yes else "move ") + line if line.startswith("AB#") else f"note: {line}")
    if not changes:
        print("nothing to move")
        return 0
    if not args.yes:
        print("\nNothing written. Add --yes to apply (confirm the PR is merged first).")
        return 0
    problems = mover.apply(changes)
    for p in problems:
        print(f"autodeliver: {p}", file=sys.stderr)
    return 4 if problems else 0


class _ReadOnly:
    """Real reads, recorded writes: dry run against a live work item."""

    def __init__(self, inner):
        self.inner = inner
        self.writes: list = []

    def get_work_item(self, wid: int):
        return self.inner.get_work_item(wid)

    def type_states(self, work_item_type: str):
        return self.inner.type_states(work_item_type)

    def __getattr__(self, name: str):
        def record(*args, **kwargs):
            self.writes.append((name, args, kwargs))
            return {"pullRequestId": 0} if name == "create_pr" else None
        return record


# --- entry point -----------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="autodeliver", description="Unattended work item -> gated draft PR for .NET repos")
    ap.add_argument("--version", action="version", version=f"autodeliver {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    def with_repo(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--repo", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
        return p

    p = with_repo(sub.add_parser("init", help="create .autodeliver/config.yml and the pipeline in a repository"))
    p.add_argument("--tool-repo", help="Azure Repos path of the autodeliver tool repo, PROJECT/REPO")
    p.add_argument("--process", choices=sorted(PROCESSES), default="agile",
                   help="Azure DevOps process template: 'basic' (Issue/Task, To Do/Doing/Done, criteria in the Description)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = with_repo(sub.add_parser("doctor", help="check prerequisites and configuration"))
    p.add_argument("--ci", action="store_true", help="also require pipeline secrets")
    p.add_argument("--probe-tests", action="store_true",
                   help="also build and run the tests once, to verify the test-runner commands on this machine")
    p.set_defaults(func=cmd_doctor)

    tools = sub.add_parser("tools", help="manage external scanners")
    tsub = tools.add_subparsers(dest="tools_command", required=True)
    with_repo(tsub.add_parser("install", help="install stryker / sonarscanner / gitleaks as configured")).set_defaults(func=cmd_tools)

    config = sub.add_parser("config", help="inspect configuration")
    csub = config.add_subparsers(dest="config_command", required=True)
    with_repo(csub.add_parser("show", help="print the merged configuration")).set_defaults(func=cmd_config)

    p = with_repo(sub.add_parser("run", help="deliver one work item"))
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--work-item", type=int)
    target.add_argument("--next", action="store_true", help="pick the oldest tagged, unblocked work item")
    p.add_argument("--dry-run", action="store_true", help="no Azure DevOps writes, no push, no PR")
    p.add_argument("--keep-branch", action="store_true",
                   help="dry run only: leave the local branch and its commit in place instead of returning to where the run started")
    p.add_argument("--yes", action="store_true", help="confirm a real (non-dry) run from a terminal; not needed in a pipeline")
    p.add_argument("--story-file", type=Path, help="dry run only: JSON work item instead of Azure DevOps")
    p.add_argument("--config", type=Path, help="overlay file (default: <repo>/.autodeliver/config.yml)")
    p.set_defaults(func=cmd_run)

    p = with_repo(sub.add_parser("complete", help="after the merge: move a delivered work item and its child tasks to Done"))
    p.add_argument("--work-item", type=int, required=True)
    p.add_argument("--yes", action="store_true", help="apply (without it the moves are only listed)")
    p.add_argument("--force", action="store_true", help="also for an item autodeliver did not deliver (no autodeliver-pr tag)")
    p.add_argument("--config", type=Path, help="overlay file (default: <repo>/.autodeliver/config.yml)")
    p.set_defaults(func=cmd_complete)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    auth.drop_unexpanded()          # "$(CLAUDE_CODE_OAUTH_TOKEN)" from an undefined pipeline variable is not a credential
    try:
        return int(args.func(args))
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
