"""Configuration: packaged safe defaults + a small per-repository overlay (.autodeliver/config.yml).

Merge rules:
- Mappings merge recursively; scalars and ordinary lists in the overlay replace the default.
- Safety lists (ADDITIVE_ONLY) are unioned: a repository can add deny paths or PHI identifiers, never remove them.
- Unknown keys in the overlay are errors (a typo must not silently fall back to a default).
- Invariants are validated after the merge; the run refuses to start on any violation.
"""
from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

OVERLAY_PATH = Path(".autodeliver") / "config.yml"
FREE_FORM_KEYS = {("security_commands",), ("conventions_files",), ("dotnet", "env")}
ADDITIVE_ONLY = {
    ("scope", "deny_paths"), ("scope", "dependency_files"), ("scope", "csproj_dependency_patterns"),
    ("phi", "blocking_identifiers"), ("phi", "sink_patterns"),
}
MANDATORY_DENY = [".autodeliver/**", ".azuredevops/**"]

REQUIRED_SECTIONS = [
    "azure_devops", "spec", "eligibility", "branch", "commit", "scope", "dotnet", "tests", "mutation", "phi",
    "sonar", "loops", "models", "budget", "plan_check", "on_escalate", "on_success", "tools",
]
REQUIRED_LOOPS = ["plan", "tests", "dev", "review", "quality"]
REQUIRED_MODELS = ["mapper", "triage", "planner", "plan_checker", "test_author", "developer", "reviewer", "pr_writer"]


class ConfigError(ValueError):
    pass


def defaults() -> dict[str, Any]:
    text = resources.files("autodeliver").joinpath("defaults/config.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _merge(base: dict, overlay: dict, path: tuple[str, ...] = ()) -> dict:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        here = (*path, key)
        if here in FREE_FORM_KEYS:
            out[key] = copy.deepcopy(value)
            continue
        if key not in base:
            raise ConfigError(f"unknown config key: {'.'.join(here)}")
        if here in ADDITIVE_ONLY:
            if not isinstance(value, list):
                raise ConfigError(f"{'.'.join(here)} must be a list")
            out[key] = list(dict.fromkeys([*base.get(key, []), *value]))
        elif isinstance(value, dict) and isinstance(base.get(key), dict) and here not in FREE_FORM_KEYS:
            out[key] = _merge(base[key], value, here)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(repo: str | Path, overlay: str | Path | None = None, *, require_remote: bool = True) -> dict[str, Any]:
    path = Path(overlay) if overlay else Path(repo) / OVERLAY_PATH
    data: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: not a mapping")
        data = loaded
    elif overlay:
        raise ConfigError(f"{path}: not found")
    cfg = _merge(defaults(), data)
    validate(cfg, require_remote=require_remote)
    return cfg


def validate(cfg: dict[str, Any], *, require_remote: bool = True) -> None:
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        raise ConfigError(f"missing sections: {missing}")
    for key in REQUIRED_LOOPS:
        value = cfg["loops"].get(key)
        if not isinstance(value, int) or not 1 <= value <= 5:
            raise ConfigError(f"loops.{key} must be an integer between 1 and 5")
    for key in REQUIRED_MODELS:
        if not cfg["models"].get(key):
            raise ConfigError(f"models.{key} is required")
    if cfg["models"]["planner"] == cfg["models"]["plan_checker"]:
        raise ConfigError("models.planner and models.plan_checker must differ (decorrelated check)")
    if cfg["models"]["developer"] == cfg["models"]["reviewer"]:
        raise ConfigError("models.developer and models.reviewer must differ (decorrelated review)")
    if cfg["azure_devops"].get("inherit_criteria", "none") not in ("none", "parent"):
        raise ConfigError("azure_devops.inherit_criteria must be none or parent")
    if not 1 <= int(cfg["azure_devops"].get("inherit_max_depth", 3)) <= 5:
        raise ConfigError("azure_devops.inherit_max_depth must be between 1 and 5")
    sp = cfg["spec"]
    if sp["mode"] not in ("auto", "required", "off"):
        raise ConfigError("spec.mode must be auto, required or off")
    if "{id}" not in str(sp["path"]):
        raise ConfigError("spec.path must contain {id}, e.g. docs/specs/{id}.md")
    if str(sp["max_risk_tier"]).lower() not in ("low", "medium", "high"):
        raise ConfigError("spec.max_risk_tier must be Low, Medium or High")
    if not 0 < float(cfg["plan_check"]["min_file_overlap"]) <= 1:
        raise ConfigError("plan_check.min_file_overlap must be in (0, 1]")
    if cfg["on_escalate"].get("push_branch"):
        raise ConfigError("on_escalate.push_branch must stay false: never push code that failed a gate")
    if not cfg["on_success"].get("draft_pr", True) is True:
        raise ConfigError("on_success.draft_pr must stay true: a human promotes the PR")
    for entry in MANDATORY_DENY:
        if entry not in cfg["scope"]["deny_paths"]:
            raise ConfigError(f"scope.deny_paths must contain '{entry}' so agents cannot edit the pipeline or its config")
    states = [str(s).strip().lower() for s in cfg["on_success"].get("dev_done_states", [])]
    if "closed" in states:
        raise ConfigError("on_success.dev_done_states must not contain Closed: closing is the reviewer's or tester's call")
    if cfg["on_success"].get("transition") and not states:
        raise ConfigError("on_success.dev_done_states is empty while transition is enabled")
    if float(cfg["on_success"].get("max_hours", 0)) <= 0:
        raise ConfigError("on_success.max_hours must be positive")
    if float(cfg["budget"]["max_total_usd"]) <= 0:
        raise ConfigError("budget.max_total_usd must be positive")
    if not 0 <= float(cfg["mutation"]["min_score"]) <= 100:
        raise ConfigError("mutation.min_score must be a percentage")
    for c in cfg.get("security_commands") or []:
        if not isinstance(c, dict) or not c.get("name") or not isinstance(c.get("args"), list) or not c["args"]:
            raise ConfigError("each security_commands entry needs a name and a non-empty args list")
    if require_remote:
        ado = cfg["azure_devops"]
        for key in ("organization_url", "project", "repository", "target_branch"):
            if not ado.get(key) or "CHANGE_ME" in str(ado[key]):
                raise ConfigError(f"azure_devops.{key} is not configured (run `autodeliver init`)")
        if cfg["sonar"].get("enabled"):
            for key in ("organization", "project_key"):
                if "CHANGE_ME" in str(cfg["sonar"].get(key, "CHANGE_ME")):
                    raise ConfigError(f"sonar.{key} is not configured")


def warnings(cfg: dict[str, Any]) -> list[str]:
    """Allowed but weakening choices, surfaced by `autodeliver doctor`."""
    out = []
    if not cfg["mutation"].get("enabled"):
        out.append("mutation testing is disabled: tests are not checked for strength")
    if not cfg["phi"].get("gitleaks"):
        out.append("gitleaks is disabled: secrets scanning relies on built-in patterns only")
    if float(cfg["mutation"]["min_score"]) < 50:
        out.append(f"mutation.min_score is low ({cfg['mutation']['min_score']}%)")
    if cfg["scope"]["max_changed_lines"] > 600:
        out.append(f"scope.max_changed_lines is high ({cfg['scope']['max_changed_lines']}); reviews get shallow")
    return out
