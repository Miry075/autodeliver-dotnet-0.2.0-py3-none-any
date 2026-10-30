# autodeliver-dotnet

Unattended delivery of one Azure DevOps work item in a .NET repository, ending in a **gated draft pull request**. Each stage (triage, plan, independent plan check, acceptance tests, implementation, independent review) runs as a fresh Claude Agent SDK session. **Only deterministic gates in code decide whether a stage passed.** Any failure escalates: the pipeline comments on the ticket, adds the `autodeliver-blocked` tag, and pushes nothing. A human always approves the merge.

Runs on Windows, Linux and macOS (Python 3.10+, git, .NET SDK).

## Conventions

The stages implement the team's `azure-workitem-flow` conventions. Each human pause there is replaced here by a deterministic gate or an independent second opinion, and any doubt becomes an escalation:

| azure-workitem-flow | autodeliver |
|---|---|
| Fetch fields, comments, Security Criteria, linked items | Same. Criteria found only in comments or the description escalate (a human must move them into the field) |
| 1. Branch `feature/<id>-<slug>` from an updated base | Same, plus clean tree, new branch and **green baseline** gates |
| 2. Existing-AC baseline first (confirmed), what and why | Opus classifies existing or additional with `path:line` evidence; an independent model must classify identically; `uncertain` escalates |
| 3. Use-case checklist | `UC-n` list (actors, edges, dependents, related); every in-scope UC needs a planned test |
| 4. 2-3 options (design fit, testability), recommendation | Same, with the chosen option required to cover every UC and preserve existing AC |
| 5. Implement on Sonnet, tests in the same step | Tests first (additional AC red, at-risk existing AC pinned green), then frozen; developer on Sonnet with the design and testing standards |
| 6. Review: existing AC first, additional AC, UCs, sweep | Independent Opus reviewer, same order; any regression, unmet AC/SC or unmet UC blocks |
| 7. One commit, named files, `<type>: <summary> (AB#<id>)`, why-body | Same: code **and** tests in one commit |
| 8. PR, then Completed Hours (human-equivalent, cumulative) and dev-done | Draft PR, then CompletedWork += estimate, RemainingWork 0, first configured dev-done state. **Never Closed** |

The `.claude/notes/<id>.md` file `azure-workitem-flow` resumes from is written too, so a human can take over an escalated item.

## Install

macOS and Linux, one command (own virtual environment in `~/.autodeliver`, `autodeliver` on your PATH, no sudo; re-run to upgrade):

```bash
./scripts/install.sh              # from this checkout
./scripts/install.sh --github     # or straight from GitHub
./scripts/install.sh --uninstall
```

Manually, or in a pipeline:

```bash
# in a virtual environment
pip install "autodeliver-dotnet @ git+https://dev.azure.com/<org>/<project>/_git/autodeliver-dotnet@v0.3.2"
autodeliver --version
```

From a clone: `pip install .` (or `pip install -e ".[dev]"` to develop).

## Set up a repository

```bash
cd <your .NET repo>
autodeliver init            # writes .autodeliver/config.yml and .azuredevops/autodeliver.yml
autodeliver tools install   # Stryker, gitleaks (checksum-verified), sonarscanner if enabled
autodeliver doctor          # every line must be OK
```

Then do the Azure DevOps side once: pipeline, variable group `autodeliver-secrets` with one Claude credential (see below), build identity permissions (no policy bypass), and branch policy with a required human reviewer. The step-by-step list is in [`skill/autodeliver-dotnet/references/setup.md`](skill/autodeliver-dotnet/references/setup.md).

## Use

| | |
|---|---|
| Pipeline | Queue `.azuredevops/autodeliver.yml` with `workItemId`, or let the weekday schedule take the oldest ticket tagged `auto-deliver` |
| Local dry run (reads ADO, writes nothing) | `autodeliver run --work-item 12345 --dry-run` (needs `AZURE_DEVOPS_PAT`; Claude login, see below) |
| Offline dry run | `autodeliver run --work-item 1 --dry-run --story-file story.json` |
| Merged config | `autodeliver config show` |

Run records (state, gate reports, agent outputs, transcripts, evidence pack) are in `.work/autodeliver/<id>/` and are published as the pipeline artifact.

## Claude credentials

The agents run through the Claude Agent SDK, which uses the Claude Code CLI. It logs in one of three ways (`autodeliver doctor` shows which one a run will use):

| Credential | Billing | Use it for |
|---|---|---|
| Neither variable set: the Claude Code login on this machine (`claude`, then `/login`) | Your Pro/Max subscription | Local runs on your own repositories |
| `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (valid one year) | Your Pro/Max subscription | A pipeline on your own repositories and tickets |
| `ANTHROPIC_API_KEY` from the Claude Console | Per token | Any shared, team or company pipeline |

Subscription limits assume ordinary individual use. Anthropic's terms require API keys for products and services built on the Agent SDK, and do not allow routing other people's work through a Pro/Max login, so a pipeline that delivers your colleagues' tickets needs an API key (or the organisation's own plan). One run is 8 or more agent sessions, with Opus for planning and review; on Pro you will reach the session limit quickly, Max is more realistic.

If both variables are set the CLI uses the API key. An undefined pipeline variable arrives as the literal `$(NAME)`; autodeliver removes it so it is never tried as a credential.

## Test runners

autodeliver supports both .NET test modes and picks one from `global.json`:

| `global.json` | Mode | Command |
|---|---|---|
| no `test.runner` | VSTest | `dotnet test <sln> --logger trx --filter AC=AC-1` |
| `"test": {"runner": "Microsoft.Testing.Platform"}` | Microsoft.Testing.Platform (xunit.v3) | `dotnet test --solution <sln> --filter-trait AC=AC-1` |

Run `autodeliver doctor --probe-tests` once per repository: it builds, runs the tests and checks that the trait filter is accepted. The agents use the mocking library the repository already uses, and hand-written fakes when it has none.

## Configuration

The package ships safe defaults (`src/autodeliver/defaults/config.yml`). A repository overrides only what it needs in `.autodeliver/config.yml`:

- Mappings merge. Scalars and ordinary lists replace the default.
- **Safety lists are additive**: `scope.deny_paths`, `scope.dependency_files`, `scope.csproj_dependency_patterns`, `phi.blocking_identifiers` and `phi.sink_patterns`. A repository can add entries, never remove them.
- Unknown keys are errors. These invariants are enforced:
  - the planner and plan checker use different models, and so do the developer and reviewer;
  - PRs are always draft;
  - nothing is pushed on escalation;
  - every loop limit is between 1 and 5;
  - `.autodeliver/**` and `.azuredevops/**` are always denied to agents.

## Distributables

`python scripts/build_dist.py` builds:

- `dist/autodeliver-dotnet.skill`: the Claude skill (Save skill in Claude).
- `dist/autodeliver-dotnet.plugin`: a Cowork / Claude Code plugin containing the same skill.

`python -m build` builds the wheel and sdist. `azure-pipelines/ci.yml` does all of this after testing on Windows and Linux.

## Releasing

1. Bump `__version__` in `src/autodeliver/__init__.py` and `version` in `plugin/.claude-plugin/plugin.json`. The build fails if they differ.
2. Merge to main, then tag `v<version>`.
3. Consumers bump `ref: refs/tags/v<version>` in `.azuredevops/autodeliver.yml`. Nothing upgrades silently.

## Security notes

- The agents' tool calls go through a `PreToolUse` hook policy:
  - writes are limited to the files each stage owns;
  - `.git/` and secrets files cannot be read;
  - shell access is `dotnet build/test/restore` and read-only `git` only.
- Build and test processes (which run agent-written code) get no pipeline secrets. The git token is passed per command and never stored in `.git/config`.
- The Claude CLI needs its credential (API key or subscription token) in its own environment. Restrict the build agent's network egress to contain that.
- The PHI scanner is pattern-based plus gitleaks. It complements the reviewer's health sweep and the human PR review; it does not replace them.
