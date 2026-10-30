"""Claude Agent SDK runner. One fresh session per stage call; structured output only.

Every tool call goes through ToolPolicy in a PreToolUse hook. A hook runs before every other permission
step, so it also covers calls Claude Code would auto-approve on its own (reads inside the working
directory, read-only shell commands), which never reach can_use_tool. can_use_tool applies the same
policy as a second line. Nothing is pre-approved via allowed_tools.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .dotnet import SECRET_ENV
from .globs import match_any, norm, same_path

READ_TOOLS = ("Read", "Grep", "Glob")
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
NEVER_READ = [".git/**", ".work/**", "**/*.pfx", "**/*.snk", "**/.env", "**/secrets*.json", "**/*.user"]
_SHELL_META = re.compile(r"[;&`$<>\n\r]|\|\|")
_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
GIT_FORBIDDEN_OPTIONS = {"--output", "--no-index", "--ext-diff", "--textconv", "--output-directory"}
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_GIT_REV_PATH = re.compile(r"^[^:/\\]+:(?![\\/])(.+)$")       # HEAD:path, abc123:path (not C:\ or a URL)
# The agent's own shell (and the code `dotnet test` runs in it) gets none of these. The Claude credential
# in use (API key or subscription token) is the exception: the Claude CLI needs it, so that residual
# exposure is contained by network egress rules on the build agent.
AGENT_BLANKED_ENV = sorted(SECRET_ENV - {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})


def _looks_like_path(value: str) -> bool:
    return bool(value) and (value.startswith(("/", "\\", "~")) or _DRIVE.match(value) is not None
                            or ".." in value.replace("\\", "/").split("/")
                            or value.startswith(".") and len(value) > 1)


def _path_values(token: str) -> list[tuple[str, bool]]:
    """Values inside one argument that may name a file: the token itself, `opt=value`, `a;b=c` and git
    `rev:path`. Each comes with whether it looks like a path (then it must resolve inside the repository).
    Every value is matched against NEVER_READ, path-like or not (`git diff -- secrets.json`)."""
    part = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
    if part.startswith("-"):
        return []
    values: list[tuple[str, bool]] = []
    for piece in part.split(";"):
        piece = piece.split("=", 1)[1] if "=" in piece else piece
        m = _GIT_REV_PATH.match(piece)
        value = m.group(1) if m else piece
        if value:
            values.append((value, _looks_like_path(value)))
    return values


@dataclass
class ToolPolicy:
    repo: Path
    write_paths: list[str] = field(default_factory=list)   # exact repo-relative paths this stage may write
    deny_paths: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)
    bash_allow: list[str] = field(default_factory=list)    # regexes, full match
    denials: list[str] = field(default_factory=list)

    @property
    def tools(self) -> list[str]:
        tools = list(READ_TOOLS)
        if self.write_paths:
            tools += ["Edit", "Write", "MultiEdit"]
        if self.bash_allow:
            tools.append("Bash")
        return tools

    def _rel(self, raw: str | None) -> str | None:
        """Repo-relative POSIX path, or None when the target is outside the repository.

        normcase makes the comparison case-insensitive on Windows (C:\\Repo vs c:\\repo); realpath
        resolves symlinks and `..` so a link cannot point an allowed path outside the repo.
        """
        if not raw:
            return ""                      # tool default = cwd = repo root
        p = Path(raw)
        if not p.is_absolute():
            p = self.repo / p
        root = os.path.normcase(os.path.realpath(self.repo))
        target = os.path.normcase(os.path.realpath(p))
        try:
            rel = os.path.relpath(target, root)
        except ValueError:                 # different drive on Windows
            return None
        if rel == os.curdir:
            return ""
        if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
            return None
        return norm(rel)

    def _check_arguments(self, cmd: str) -> tuple[bool, str]:
        """The allowlist regexes accept any argument; these are the arguments that would escape the policy.

        git: options that write a file (--output), read outside the repository (--no-index) or run
        configured programs (--ext-diff, --textconv). Every command: a path-like value (including the
        right-hand side of `opt=value`, `;`-separated logger settings and git `rev:path`) must stay
        inside the repository and outside NEVER_READ, so `dotnet build -o /tmp/x`,
        `dotnet test --logger "trx;LogFilePath=/home/x"` and `git show HEAD:.env` are refused.
        """
        try:
            tokens = shlex.split(cmd, posix=True)
        except ValueError:
            return False, "could not parse the command"
        if tokens and tokens[0] == "git":
            for t in tokens[1:]:
                name = t.split("=", 1)[0]
                if name in GIT_FORBIDDEN_OPTIONS or (name.startswith("-O") and not name.startswith("--")):
                    return False, f"git option {name} is not allowed in this pipeline"
        for t in tokens[1:]:
            for value, path_like in _path_values(t):
                rel = self._rel(value) if path_like else norm(value)
                if rel is None:
                    return False, f"{value} is outside the repository"
                if rel and match_any(rel, NEVER_READ):
                    return False, f"{rel} is not readable in this pipeline"
        return True, "ok"

    def decide(self, tool: str, data: dict[str, Any]) -> tuple[bool, str]:
        if tool in ("Read", "Glob"):
            rel = self._rel(data.get("file_path") or data.get("path"))
            if rel is None:
                return False, "reads are limited to the repository"
            if rel and match_any(rel, NEVER_READ):
                return False, f"{rel} is not readable in this pipeline"
            return True, "ok"
        if tool in WRITE_TOOLS:
            rel = self._rel(data.get("file_path") or data.get("notebook_path"))
            if not rel:
                return False, "writes are limited to the repository"
            if match_any(rel, self.deny_paths):
                return False, f"{rel} is in deny_paths"
            if any(same_path(rel, f) for f in self.frozen):
                return False, f"{rel} is a frozen acceptance test; report a test_dispute instead of editing it"
            if not any(same_path(rel, w) for w in self.write_paths):
                return False, f"{rel} is not owned by this stage; owned files: {self.write_paths}"
            return True, "ok"
        if tool == "Bash":
            cmd = (data.get("command") or "").strip()
            unquoted = _QUOTED.sub("", cmd)
            if _SHELL_META.search(unquoted) or "|" in unquoted:
                return False, "shell operators are not allowed; run one command at a time"
            if not any(re.fullmatch(rx, cmd) for rx in self.bash_allow):
                return False, f"command not allowed; allowed patterns: {self.bash_allow}"
            return self._check_arguments(cmd)
        if tool == "Grep":
            rel = self._rel(data.get("path"))
            if rel is None:
                return False, "reads are limited to the repository"
            if rel and match_any(rel, NEVER_READ):
                return False, f"{rel} is not readable in this pipeline"
            # A directory search with content output would print lines of NEVER_READ files. File names and
            # counts are fine; to see lines, search a single file (whose path is checked above).
            searches_dir = not rel or (self.repo / rel).is_dir()
            if searches_dir and data.get("output_mode") == "content":
                return False, "content search is limited to a single file; search the directory for file names first"
            return True, "ok"
        if tool == "TodoWrite":
            return True, "ok"
        return False, f"tool {tool} is not available in this pipeline"


def policy_hook_output(policy: ToolPolicy, input_data: dict[str, Any]) -> dict[str, Any]:
    """PreToolUse hook body (pure, testable): deny anything the policy rejects."""
    tool = input_data.get("tool_name", "")
    allowed, reason = policy.decide(tool, input_data.get("tool_input") or {})
    if allowed:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "permissionDecisionReason": "autodeliver policy"}}
    policy.denials.append(f"{tool}: {reason}")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


BASH_DOTNET = [
    r"dotnet build(\s+[^\s]+)*",
    r"dotnet test(\s+[^\s]+|\s+\"[^\"]*\")*",
    r"dotnet restore(\s+[^\s]+)*",
    r"git (status|diff|log|show)(\s+[^\s]+)*",
]


@dataclass
class AgentResult:
    output: dict[str, Any]
    cost_usd: float
    turns: int
    session_id: str
    denials: list[str]


class AgentError(RuntimeError):
    def __init__(self, role: str, reason: str, cost_usd: float = 0.0):
        super().__init__(f"{role}: {reason}")
        self.role = role
        self.reason = reason
        self.cost_usd = cost_usd


_VAR = re.compile(r"\{\{(\w+)\}\}")


def render(template: str, variables: dict[str, Any]) -> str:
    missing = sorted(set(_VAR.findall(template)) - set(variables))
    if missing:
        raise KeyError(f"prompt variables missing: {missing}")
    return _VAR.sub(lambda m: v if isinstance(v := variables[m.group(1)], str) else json.dumps(v, indent=2), template)


# Which team standards (from dotnet-design-principles / dotnet-testing-standards) each stage applies.
ROLE_STANDARDS = {
    "planner": ["design", "testing"],
    "plan_checker": ["design"],
    "test_author": ["testing"],
    "developer": ["design", "testing"],
    "reviewer": ["design", "testing"],
}


def packaged_prompts() -> Path:
    return Path(str(resources.files("autodeliver").joinpath("prompts")))


class AgentRunner:
    def __init__(self, repo: Path, cfg: dict, transcripts_dir: Path, prompts_dir: Path | None = None):
        self.repo = Path(repo)
        self.cfg = cfg
        self.prompts_dir = Path(prompts_dir) if prompts_dir else packaged_prompts()
        self.transcripts_dir = Path(transcripts_dir)
        self.shared_rules = (self.prompts_dir / "_shared.md").read_text(encoding="utf-8")

    def prompt(self, role: str, variables: dict[str, Any]) -> str:
        text = render((self.prompts_dir / f"{role}.md").read_text(encoding="utf-8"), variables)
        for name in ROLE_STANDARDS.get(role, []):
            text += "\n\n" + (self.prompts_dir / "standards" / f"{name}.md").read_text(encoding="utf-8")
        return text

    def schema(self, role: str) -> dict[str, Any]:
        return json.loads((self.prompts_dir / "schemas" / f"{role}.json").read_text(encoding="utf-8"))

    def run(self, role: str, model_key: str, variables: dict[str, Any], policy: ToolPolicy,
            budget_usd: float, attempt: int) -> AgentResult:
        return asyncio.run(self._run(role, model_key, variables, policy, budget_usd, attempt))

    async def _run(self, role: str, model_key: str, variables: dict[str, Any], policy: ToolPolicy,
                   budget_usd: float, attempt: int) -> AgentResult:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
                                      ResultMessage, TextBlock)
        from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

        async def pre_tool_use(input_data: dict[str, Any], _tool_use_id: Any, _ctx: Any) -> dict[str, Any]:
            return policy_hook_output(policy, input_data)

        async def can_use_tool(tool_name: str, data: dict[str, Any], _ctx: Any):
            allowed, reason = policy.decide(tool_name, data)
            if allowed:
                return PermissionResultAllow(updated_input=data)
            policy.denials.append(f"{tool_name}: {reason}")
            return PermissionResultDeny(message=reason, interrupt=False)

        options = ClaudeAgentOptions(
            model=self.cfg["models"][model_key],
            cwd=str(self.repo),
            tools=policy.tools,
            allowed_tools=[],
            permission_mode="default",
            hooks={"PreToolUse": [HookMatcher(hooks=[pre_tool_use])]},     # no matcher: every tool call
            can_use_tool=can_use_tool,
            setting_sources=[],                        # no user/project settings can widen permissions
            strict_mcp_config=True,
            mcp_servers={},
            system_prompt={"type": "preset", "preset": "claude_code", "append": self.shared_rules},
            output_format={"type": "json_schema", "schema": self.schema(role)},
            max_turns=int(self.cfg["budget"]["max_turns"][model_key]),
            max_budget_usd=max(0.5, budget_usd),
            # Blank pipeline secrets for the agent's own shell. The Claude credential must stay for the CLI;
            # contain that residual exposure with network egress rules on the build agent.
            env={**{k: str(v) for k, v in self.cfg["dotnet"].get("env", {}).items()},
                 **{k: "" for k in AGENT_BLANKED_ENV}},
        )
        prompt = self.prompt(role, variables)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        transcript = self.transcripts_dir / f"{role}-{attempt}.jsonl"
        result: ResultMessage | None = None
        with transcript.open("w", encoding="utf-8") as log:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        text = " ".join(b.text for b in message.content if isinstance(b, TextBlock))
                        if text:
                            log.write(json.dumps({"assistant": text[:2000]}) + "\n")
                    elif isinstance(message, ResultMessage):
                        result = message
        if result is None:
            raise AgentError(role, "no result message")
        cost = float(result.total_cost_usd or 0.0)
        if result.subtype != "success" or result.is_error:
            raise AgentError(role, f"{result.subtype} / {result.stop_reason}: {result.errors or result.result}", cost)
        if not isinstance(result.structured_output, dict):
            raise AgentError(role, "no structured output", cost)
        return AgentResult(result.structured_output, cost, result.num_turns, result.session_id, list(policy.denials))
