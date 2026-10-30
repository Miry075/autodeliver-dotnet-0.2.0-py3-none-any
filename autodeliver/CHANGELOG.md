# Changelog

## 0.5.1
- Skill upload: the skill description contained `<ID>`, which Claude's upload validator rejects as an XML tag ("SKILL.md description cannot contain XML tags"). Replaced by a plain example, and `scripts/build_dist.py` now fails the build on angle brackets or a description over 1024 characters.

## 0.5.0

### Committed specs (agentic-SDLC convention: `docs/specs/<work-item-id>.md`)
- When the target branch has the spec, autodeliver reads the work item's details from it as well as from the ticket: Acceptance criteria and Existing acceptance criteria become AC-n (declared *additional* / *existing*), Security criteria are merged into SC-n, and Goal, Use cases, Edge cases, Design and Non-goals are given to every agent (Non-goals are out of scope).
- The spec's declared existing/additional split is a third vote in the baseline gate, next to the planner and the independent checker; any disagreement escalates.
- A Risk tier above `spec.max_risk_tier` (default Medium), a missing Risk tier, unanswered Open questions and a spec without additional criteria stop the run before any agent runs.
- Tracker criteria that are more than a pointer to the spec are shown to triage for comparison.
- New `spec:` settings: `mode` (auto, required, off), `path`, `max_risk_tier`, `block_on_open_questions`. The template's own guidance sentences are not mistaken for content.

## 0.4.0

### Azure DevOps Basic process (Epic / Issue / Task, To Do / Doing / Done, no Acceptance Criteria field)
- `azure_devops.criteria_section`: when the Acceptance Criteria field is empty or missing, read the criteria from this labelled section of the Description (bullets, numbered lines, lines or sentences). Markdown and HTML descriptions both work. The triage step is told where the criteria came from, so the same text is not flagged as "criteria hiding in the description".
- `azure_devops.acceptance_criteria_field`: the field to read, for processes that name it differently.
- `autodeliver init --process basic` writes the matching overlay (types `Issue`/`Task`, states `To Do`/`Doing`, dev-done state `Doing`, never `Done`).
- A clearer eligibility message names the section as well as the field.

## 0.3.3
- `autodeliver run` without `--dry-run` now refuses to start from a terminal unless `--yes` is given (a real run pushes a branch, opens a PR and writes to the work item). Azure Pipelines is unaffected: it sets `TF_BUILD`.
- A missing `AZURE_DEVOPS_PAT` / `SYSTEM_ACCESSTOKEN` prints how to set it instead of a traceback.

## 0.3.2
- Microsoft.Testing.Platform: dropped `--nologo` from the test command. The new runner rejects it with exit code 5 ("invalid command line"), which `doctor --probe-tests` caught on a real xunit.v3 solution. Every other option was confirmed accepted there: `--solution`, `-c`, `--no-build`, `--results-directory`, `--filter-trait NAME=VALUE` (a filter that matches nothing exits 8).
- `doctor --probe-tests` now prints the command and the start of the output when a probe fails.

## 0.3.1

### Microsoft.Testing.Platform (xunit.v3, `"test": {"runner": ...}` in global.json)
- autodeliver ran `dotnet test … --logger trx --filter …`, which the new runner rejects, so every run would have stopped at the baseline check. The runner is now detected from `global.json` (`dotnet.test_runner: auto | vstest | mtp`) and called as `dotnet test --solution … --filter-trait NAME=VALUE`. Results come from the console summary (total / failed / succeeded), and a missing summary, a rejected command line or a non-zero exit code is never a pass.
- `autodeliver doctor --probe-tests` builds and runs the tests once, plus a filter that matches nothing, so the runner support is verified on the machine before a real run.
- `dotnet.mtp_filter_option` (default `--filter-trait`) can be changed if another framework needs a different option.

### Test libraries
- NSubstitute is no longer assumed. The test-author prompt and the testing standard say to use the mocking library the repository already references, else hand-written fakes, and never to add a package. `doctor` requires xUnit and only reports the mocking library.

### Other
- Runs in a linked git worktree (`.git` is a file there) instead of crashing when writing the local exclude file, so unfinished work in the main checkout can stay where it is.

## 0.3.0

### Claude subscription login
- Runs on your Claude Pro/Max subscription: with no credential variable set, the Claude Code login on the machine is used; in a pipeline, set `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. `ANTHROPIC_API_KEY` still works and is required for shared or company pipelines (see README, "Claude credentials").
- `doctor` reports which credential a run will use, accepts either one in `--ci`, and warns when both are set (the API key wins).
- Undefined pipeline secrets (the literal `$(NAME)` Azure passes through) are removed at start-up instead of being tried as credentials.
- The pipeline template passes `CLAUDE_CODE_OAUTH_TOKEN` as well as `ANTHROPIC_API_KEY`.

### Security
- Tool policy: allowed `git`/`dotnet` commands could escape it through their arguments. Refused now: `git --output`, `--no-index`, `--ext-diff`, `--textconv`, `-O`; any path argument outside the repository (`dotnet build -o /tmp/x`, `--logger "trx;LogFilePath=/…"`, `../other.csproj`); any argument naming a NEVER_READ file (`git show HEAD:.env`, `git diff -- secrets.json`).
- Grep over a directory can no longer print matching lines (it could show the contents of NEVER_READ files); file names and counts are still allowed, line output only for a single checked file.
- `VSS_NUGET_ACCESSTOKEN` is now blanked in the agents' own shell, like the other pipeline secrets. `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_AUTH_TOKEN` are never passed to build, test, Stryker or Sonar.
- PR title, PR summary, the evidence-pack PR thread and escalation comments are PHI-scanned like the commit message; flagged text is withheld (the full text stays in the run record).

### Other
- `scripts/install.sh`: one-command install for macOS and Linux (private venv in `~/.autodeliver`, `autodeliver` on PATH, `--github`, `--uninstall`).
- No more pytest collection warning for `dotnet.TestResult`.
