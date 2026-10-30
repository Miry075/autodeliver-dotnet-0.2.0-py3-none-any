# Changelog

## 0.8.0
### Weigh open points instead of stopping on every one
- Triage now weighs each open point (blocker or criterion found outside the criteria) with an impact (`negligible` / `minor` / `material`), a reason, a concrete default and whether it is reversible. The run proceeds on a point only when all of these hold: impact at or below `analysis.proceed_up_to` (default `negligible`), reversible, a default exists, and it mentions none of `analysis.always_escalate` (legal, VAT, tax, PCMN, accounting, retention, GDPR, personal data, security, authentication, migration, pricing, payment...). More than `analysis.max_assumptions` (default 3) small points together still stop the run: that is an under-specified ticket.
- A second model checks every default: the plan checker must `accept` each assumption (A-1, A-2...) or the run escalates. Blockers raised by the checker itself pass only when negligible.
- Agents implement the default exactly and name the assumption id where it shows. The draft PR description, the evidence pack and the ticket comment list every decision taken without asking, so the reviewer can change it.
- Untestable criteria, restricted areas, oversized tickets and planner `BLOCKER:` assumptions still always escalate. `analysis.proceed_up_to: none` restores the previous behaviour.

## 0.7.1
- Fix: the plan-agreement gate escalated when the planner and the independent checker proposed the same new files in different folders (AB#185: `Core/Tracking/InvoiceStatus.cs` vs `Core/Invoicing/InvoiceStatus.cs` scored 0.00 overlap). A file that does not exist yet now matches when both create it under the same name in the same project; the folder difference is reported in the gate details for the reviewer. Existing files still need the exact path.
- On a file disagreement the planner is told to put new files in the existing folder their types belong with.
- The plan escalation now names the gate that failed instead of always blaming an existing/additional disagreement.

## 0.7.0
- **The board follows the work.** When the plan is approved (real runs), the work item and its still-open child tasks move from To Do to the process's in-progress state (Doing / Active / In Progress). After the draft PR exists, open children follow the item to the dev-done state their type has. If the run stops later, every card it moved goes back to its previous state. A dry run only lists what it would move (`dry run: would move ...` in the run log). Finished children (Done, Resolved...) are never touched, Closed is never written, and a state the process rejects is reported in the evidence pack ("### Board") instead of stopping the delivery. Config: `on_start`, `on_success.cascade_children`.
- New command `autodeliver complete --work-item ID [--yes] [--force]`: after the merge, moves the item and its child tasks to Done (Resolved on Agile). Without `--yes` it only lists the moves; it refuses an item that lacks the `autodeliver-pr` tag unless `--force`. Config: `on_complete`.
- **autodeliver never deletes a branch.** The 0.6.2 dry-run cleanup ran `git branch -D` on its own dry-run branch; that is gone. A dry run still returns the checkout to where it started and keeps the result as `dry-run.patch`, but leaves its branch; the next dry run on the same item takes a free `-dryrun-N` name. A test fails if any branch-deleting git call is ever added.

## 0.6.2
- A dry run no longer leaves a `feature/<id>-...` branch behind (which made the next run stop with "branch already exists"). When it ends, the repository goes back to the branch or commit it started on, the local branch is deleted, and the result is kept as `dry-run.patch` (and `dry-run-files/` for files the agents left untracked) in the run record. `--keep-branch` skips this. Real runs are unchanged.

## 0.6.1
- Fix: every stage ended with "agent failure: no structured output". The Claude CLI makes the model return its result by calling a `StructuredOutput` tool, and the tool policy denied it as an unknown tool, so no stage could return anything. The policy now allows `StructuredOutput` (and still denies every other unknown tool).
- A stage that ends without structured output now reports the stop reason, the turns, the calls the policy denied, the model's final text and the transcript file, and the transcript records the final result.

## 0.6.0

### Criteria from the parent story (Epic > Issue > Task)
- `azure_devops.inherit_criteria: parent` (`init --process basic` sets it) and `inherit_max_depth` (default 3): a work item without criteria of its own takes the criteria of its parent, found in the Acceptance Criteria field or in the Description section. Its own title and description remain the scope; the parent's description and the sibling Tasks are given to every agent.
- New criterion kind `deferred` for criteria that a sibling Task delivers: needs the sibling named in `note`, only allowed for inherited criteria, must be agreed by the planner and the independent checker, is not tested or assessed here, and is reported in the notes and the evidence pack.
- Triage judges testability only for the criteria the work item itself covers.

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
