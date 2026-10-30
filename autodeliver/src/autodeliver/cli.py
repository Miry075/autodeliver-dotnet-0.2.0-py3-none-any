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

from . import __version__
from .config import OVERLAY_PATH, ConfigError, load_config, warnings
from .repo_info import detect

PIPELINE_PATH = Path(".azuredevops") / "autodeliver.yml"


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
          " (ANTHROPIC_API_KEY), build identity permissions (see README)\n  3. autodeliver doctor")
    return 0


# --- doctor ----------------------------------------------------------------------------------------
def _secret(name: str) -> bool:
    """Set and not an unexpanded Azure Pipelines macro such as '$(ANTHROPIC_API_KEY)'."""
    value = os.environ.get(name, "")
    return bool(value) and not (value.startswith("$(") and value.endswith(")"))


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
    ok("ANTHROPIC_API_KEY", _secret("ANTHROPIC_API_KEY") or not args.ci, "secret variable (required in CI)")
    has_token = _secret("SYSTEM_ACCESSTOKEN") or _secret("AZURE_DEVOPS_PAT")
    ok("Azure DevOps token", has_token or not args.ci, "SYSTEM_ACCESSTOKEN (pipeline) or AZURE_DEVOPS_PAT (local)")
    tests_ok, detail = _test_stack(repo)
    ok("xUnit + NSubstitute in test projects", tests_ok, detail)

    width = max(len(c[0]) for c in checks)
    for name, passed, detail in checks:
        print(f"{'OK  ' if passed else 'FAIL'}  {name.ljust(width)}  {detail}")
    for w in warnings(cfg) if cfg else []:
        print(f"WARN  {w}")
    return 0 if all(p for _, p, _ in checks) else 1


def _test_stack(repo: Path) -> tuple[bool, str]:
    projects = [p for p in repo.rglob("*.csproj") if "test" in p.name.lower() and "bin" not in p.parts and "obj" not in p.parts]
    if not projects:
        return False, "no test projects found (name must contain 'test')"
    missing = []
    for p in projects:
        text = p.read_text(encoding="utf-8", errors="replace").lower()
        if "xunit" not in text or "nsubstitute" not in text:
            missing.append(p.name)
    return (not missing), (f"{len(projects)} test project(s)" if not missing else f"missing in: {', '.join(missing[:5])}")


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
    repo = args.repo.resolve()
    cfg = load_config(repo, args.config, require_remote=not offline)
    if offline:
        ado = OfflineAzureDevOps(json.loads(args.story_file.read_text(encoding="utf-8")))
    else:
        live = AzureDevOps(cfg)
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
    pipeline = Pipeline(cfg, repo, args.work_item, ado, agents, dry_run=args.dry_run)
    code = pipeline.run()
    s = pipeline.state
    print(f"autodeliver AB#{args.work_item}: {s.status} at {s.stage} · loops {s.loops} · ${s.cost_usd:.2f}"
          + (f" · {s.pr_url}" if s.pr_url else "") + f" · details {work}")
    return code


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
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = with_repo(sub.add_parser("doctor", help="check prerequisites and configuration"))
    p.add_argument("--ci", action="store_true", help="also require pipeline secrets")
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
    p.add_argument("--story-file", type=Path, help="dry run only: JSON work item instead of Azure DevOps")
    p.add_argument("--config", type=Path, help="overlay file (default: <repo>/.autodeliver/config.yml)")
    p.set_defaults(func=cmd_run)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
