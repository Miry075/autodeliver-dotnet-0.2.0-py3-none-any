"""Overlay config, repo detection, `init`, the tool installer and the CLI surface."""
import hashlib
import io
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from autodeliver import __version__
from autodeliver.agents import ToolPolicy
from autodeliver.cli import main
from autodeliver.config import ConfigError, load_config
from autodeliver.repo_info import parse_remote
from autodeliver.tools import expected_sha256, gitleaks_asset, install_gitleaks


def overlay(repo: Path, data: dict) -> Path:
    path = repo / ".autodeliver" / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# --- overlay ---------------------------------------------------------------------------------------
def test_overlay_merges_and_safety_lists_are_additive(tmp_path):
    overlay(tmp_path, {"azure_devops": {"project": "Care"}, "scope": {"deny_paths": ["src/Legacy/**"],
                       "test_path_globs": ["tests/**"]}, "dotnet": {"env": {"FOO": "1"}}})
    cfg = load_config(tmp_path, require_remote=False)
    assert cfg["azure_devops"]["project"] == "Care"
    assert cfg["azure_devops"]["target_branch"] == "main"                  # untouched default kept
    assert "src/Legacy/**" in cfg["scope"]["deny_paths"] and "**/Migrations/**" in cfg["scope"]["deny_paths"]
    assert cfg["scope"]["test_path_globs"] == ["tests/**"]                  # ordinary list replaced
    assert cfg["dotnet"]["env"] == {"FOO": "1"}                             # free-form mapping replaced


def test_overlay_cannot_remove_deny_paths(tmp_path):
    overlay(tmp_path, {"scope": {"deny_paths": []}})
    assert ".autodeliver/**" in load_config(tmp_path, require_remote=False)["scope"]["deny_paths"]


@pytest.mark.parametrize("data,needle", [
    ({"scope": {"deny_pathz": ["x"]}}, "unknown config key: scope.deny_pathz"),
    ({"on_escalate": {"push_branch": True}}, "push_branch"),
    ({"models": {"reviewer": "claude-sonnet-5-5"}}, "reviewer"),
    ({"loops": {"dev": 0}}, "loops.dev"),
    ({"security_commands": [{"name": "x", "args": "aikido scan"}]}, "security_commands"),
])
def test_overlay_rejects_unsafe_or_unknown(tmp_path, data, needle):
    overlay(tmp_path, data)
    with pytest.raises(ConfigError, match=needle):
        load_config(tmp_path, require_remote=False)


def test_remote_settings_required_for_live_runs(tmp_path):
    with pytest.raises(ConfigError, match="autodeliver init"):
        load_config(tmp_path, require_remote=True)
    overlay(tmp_path, {"azure_devops": {"organization_url": "https://dev.azure.com/corilusnv", "project": "Care", "repository": "api"}})
    assert load_config(tmp_path, require_remote=True)["azure_devops"]["repository"] == "api"


# --- remote parsing -------------------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://dev.azure.com/corilusnv/Care%20Platform/_git/care-api", ("https://dev.azure.com/corilusnv", "Care Platform", "care-api")),
    ("https://corilusnv@dev.azure.com/corilusnv/Care/_git/care-api", ("https://dev.azure.com/corilusnv", "Care", "care-api")),
    ("git@ssh.dev.azure.com:v3/corilusnv/Care/care-api", ("https://dev.azure.com/corilusnv", "Care", "care-api")),
    ("https://corilusnv.visualstudio.com/DefaultCollection/Care/_git/care-api", ("https://dev.azure.com/corilusnv", "Care", "care-api")),
    ("corilusnv@vs-ssh.visualstudio.com:v3/corilusnv/Care/care-api", ("https://dev.azure.com/corilusnv", "Care", "care-api")),
    ("https://github.com/org/repo.git", None),
])
def test_parse_remote(url, expected):
    info = parse_remote(url)
    assert (None if info is None else (info.organization_url, info.project, info.repository)) == expected


# --- init / config show / doctor -------------------------------------------------------------------
def _git_repo(tmp: Path) -> Path:
    repo = tmp / "care-api"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["remote", "add", "origin", "https://dev.azure.com/corilusnv/Care/_git/care-api"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def test_init_scaffolds_valid_overlay_and_pipeline(tmp_path):
    repo = _git_repo(tmp_path)
    assert main(["init", "--repo", str(repo)]) == 0
    cfg = load_config(repo, require_remote=True)                           # generated overlay is valid as-is
    assert cfg["azure_devops"]["organization_url"] == "https://dev.azure.com/corilusnv"
    assert (cfg["azure_devops"]["project"], cfg["azure_devops"]["repository"]) == ("Care", "care-api")
    pipeline = yaml.safe_load((repo / ".azuredevops" / "autodeliver.yml").read_text())
    res = pipeline["resources"]["repositories"][0]
    assert res == {"repository": "autodeliver", "type": "git", "name": "Care/autodeliver-dotnet", "ref": f"refs/tags/v{__version__}"}
    job = pipeline["jobs"][0]
    assert job["template"] == "azure-pipelines/templates/autodeliver-job.yml@autodeliver"
    assert job["parameters"]["workItemId"] == "${{ parameters.workItemId }}"
    # init is idempotent and never overwrites without --force
    (repo / ".autodeliver" / "config.yml").write_text("azure_devops: {project: Custom}\n")
    assert main(["init", "--repo", str(repo)]) == 0
    assert "Custom" in (repo / ".autodeliver" / "config.yml").read_text()


def test_config_show_and_doctor_run(tmp_path):
    repo = _git_repo(tmp_path)
    main(["init", "--repo", str(repo)])
    assert main(["config", "show", "--repo", str(repo)]) == 0
    assert main(["doctor", "--repo", str(repo)]) in (0, 1)                  # reports, never crashes


def test_run_rejects_story_file_without_dry_run(tmp_path):
    story = tmp_path / "s.json"
    story.write_text("{}")
    assert main(["run", "--work-item", "1", "--story-file", str(story), "--repo", str(tmp_path)]) == 3


# --- tool installer -------------------------------------------------------------------------------
@pytest.mark.parametrize("system,machine,asset", [
    ("Linux", "x86_64", "gitleaks_8.21.2_linux_x64.tar.gz"),
    ("Linux", "aarch64", "gitleaks_8.21.2_linux_arm64.tar.gz"),
    ("Windows", "AMD64", "gitleaks_8.21.2_windows_x64.zip"),
    ("Darwin", "arm64", "gitleaks_8.21.2_darwin_arm64.tar.gz"),
])
def test_gitleaks_asset(system, machine, asset):
    assert gitleaks_asset("8.21.2", system, machine) == asset


def test_expected_sha256():
    sums = "aaa  gitleaks_1_linux_x64.tar.gz\nbbb *gitleaks_1_windows_x64.zip\n"
    assert expected_sha256(sums, "gitleaks_1_windows_x64.zip") == "bbb"
    with pytest.raises(RuntimeError):
        expected_sha256(sums, "missing")


def _fake_release(asset: str, binary_name: str) -> bytes:
    buf = io.BytesIO()
    if asset.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(binary_name, b"BIN")
            z.writestr("README.md", b"x")
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, data in ((binary_name, b"BIN"), ("../../evil", b"x")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_install_gitleaks_verifies_checksum(tmp_path, cfg):
    cfg["tools"]["install_dir"] = str(tmp_path / "tools")
    asset = gitleaks_asset(cfg["tools"]["gitleaks_version"])
    name = "gitleaks.exe" if os.name == "nt" else "gitleaks"
    payload = _fake_release(asset, name)
    good = {asset: payload, "sums": f"{hashlib.sha256(payload).hexdigest()}  {asset}\n".encode()}
    fetch = lambda url: good["sums"] if url.endswith("checksums.txt") else good[asset]
    binary = install_gitleaks(cfg, fetch)
    assert binary.read_bytes() == b"BIN" and not (tmp_path / "evil").exists()
    assert install_gitleaks(cfg, lambda url: 1 / 0) == binary                # already installed: no download
    cfg["tools"]["install_dir"] = str(tmp_path / "tools2")
    bad = lambda url: f"{'0' * 64}  {asset}\n".encode() if url.endswith("checksums.txt") else payload
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        install_gitleaks(cfg, bad)
    assert not (tmp_path / "tools2").exists() or not any((tmp_path / "tools2").rglob(name))


# --- distributables --------------------------------------------------------------------------------
def test_build_dist_produces_skill_and_plugin(tmp_path):
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("build_dist", root / "scripts" / "build_dist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    skill, plugin = mod.build(tmp_path)
    with zipfile.ZipFile(skill) as z:
        assert "autodeliver-dotnet/SKILL.md" in z.namelist()
        assert "autodeliver-dotnet/references/setup.md" in z.namelist()
    with zipfile.ZipFile(plugin) as z:
        names = z.namelist()
        assert ".claude-plugin/plugin.json" in names and "skills/autodeliver-dotnet/SKILL.md" in names
        import json
        assert json.loads(z.read(".claude-plugin/plugin.json"))["version"] == __version__
    first = skill.read_bytes()
    mod.build(tmp_path)
    assert skill.read_bytes() == first                                     # reproducible


def test_job_template_is_valid_yaml_and_cross_platform():
    root = Path(__file__).resolve().parent.parent
    tpl = yaml.safe_load((root / "azure-pipelines" / "templates" / "autodeliver-job.yml").read_text())
    steps = tpl["jobs"][0]["steps"]
    scripts = [s for s in steps if "bash" in s or "script" in s]
    assert not scripts, "use pwsh steps only so the template runs on Windows and Linux agents"
    checkout_self = steps[0]
    assert checkout_self["checkout"] == "self" and checkout_self["persistCredentials"] is False


# --- portability of path policy --------------------------------------------------------------------
def test_policy_blocks_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, repo / "src" / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        return                                                              # no symlink privilege (Windows)
    p = ToolPolicy(repo, write_paths=["src/link/x.cs"])
    assert not p.decide("Write", {"file_path": str(repo / "src" / "link" / "x.cs")})[0]
    assert not p.decide("Read", {"file_path": str(repo / "src" / "link" / "secret")})[0]
