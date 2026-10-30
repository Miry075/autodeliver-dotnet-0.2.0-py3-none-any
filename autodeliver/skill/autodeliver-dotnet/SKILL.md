---
name: autodeliver-dotnet
description: Installs, configures, runs, dry-runs, explains and troubleshoots autodeliver, the unattended pipeline that takes one Azure DevOps work item in a .NET repository to a gated draft pull request with no human in the loop until PR review. Use when the user says "autodeliver 12345", "set up autodeliver", "install autodeliver in this repo", "dry-run autodeliver on 12345", "why did autodeliver escalate 12345", asks how to tune its gates, or shares a .work/autodeliver run record. Not for interactive, human-gated delivery; use azure-workitem-flow for that.
---

# autodeliver (.NET, unattended)

autodeliver is a Python package (`autodeliver-dotnet`, CLI `autodeliver`). The pipeline is **code, not this skill**: it runs each stage as a fresh Claude Agent SDK session, and only deterministic gates in code decide whether a stage passed. Your job is to install, configure, run and explain it. Never imitate the pipeline by hand in a chat session; that removes the gates that make it safe.

## Install

```
pip install "autodeliver-dotnet @ git+https://dev.azure.com/<org>/<project>/_git/autodeliver-dotnet@v<version>"
```

Use a virtual environment (Windows: `py -m venv .venv` then `.venv\Scripts\activate`; Linux/macOS: `python3 -m venv .venv` then `. .venv/bin/activate`). Python 3.10 or later. Ask the user for the org, project and tag if you don't know them. Never guess a tag.

## Claude credentials

No variable set: the Claude Code login on this machine (the user's subscription; `claude` then `/login`). `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`: subscription, for a pipeline on the user's own repos and tickets. `ANTHROPIC_API_KEY`: required for any shared or company pipeline (Anthropic's terms). If both are set, the API key wins. `autodeliver doctor` reports which one is used.

## Set up a repository

1. `autodeliver init` in the repo root. It detects org, project, repository and default branch from `origin` and writes `.autodeliver/config.yml` (a small overlay) and `.azuredevops/autodeliver.yml` (a pipeline using the shared job template pinned to this version).
2. `autodeliver tools install` installs Stryker, gitleaks (checksum-verified) and sonarscanner, as configured.
3. `autodeliver doctor` must be all OK. Fix each FAIL before running anything.
4. Azure DevOps side (pipeline, variable group, identity permissions, branch policy): follow `references/setup.md` step by step with the user.

## Run

| Goal | Command |
|---|---|
| Dry run against a real ticket (reads ADO, writes nothing, no push) | `autodeliver run --work-item <ID> --dry-run` |
| Offline dry run (no ADO) | `autodeliver run --work-item <ID> --dry-run --story-file story.json` |
| Real run (normally only in the pipeline) | `autodeliver run --work-item <ID>` or `--next` |
| Merged configuration | `autodeliver config show` |

Exit codes: 0 draft PR or dry run done, 1 not eligible, 2 escalated to a human, 3 internal or config error. A dry run still creates a local branch and commits. Tell the user to delete it afterwards.

Before a real run outside the pipeline, confirm with the user. It pushes a branch, opens a draft PR and comments on the ticket.

## Stages and gates (decided in code)

The stages follow the `azure-workitem-flow` conventions, with every human pause replaced by a gate or an independent second opinion:

1. **Fetch and eligibility.** Fields, comments, Security Criteria (SC-n) and linked items are read. The opt-in tag and AC-n criteria must be present.
2. **Committed spec (optional).** If the repository follows the agentic-SDLC convention and `docs/specs/<ID>.md` exists on the target branch, it supplies the criteria (Existing and Acceptance criteria sections), the Security criteria, the Use cases, Edge cases and Non-goals. A Risk tier above `spec.max_risk_tier` (default Medium) and unanswered Open questions stop the run. The tracker's own criteria are shown to triage for comparison.
   When a work item has no criteria of its own and `azure_devops.inherit_criteria` is `parent`, it inherits its parent story's. Its own title and description are the scope; criteria a sibling Task delivers are `deferred`.
3. **Prepare.** `feature/<id>-<slug>` is created on a clean tree, and the baseline must be green.
4. **Code map** (haiku), then **triage.** A criterion found only in a comment or the description escalates.
5. **Analysis** (opus):
   - the existing/additional AC baseline comes first, and an independent model must classify each criterion identically (and so must the spec's reviewed split, when there is one);
   - what and why;
   - UC-n use cases;
   - 2-3 options with design fit and testability.
6. **Acceptance tests.** Additional AC must be red, at-risk existing AC pinned green; the tests are then frozen.
7. **Develop.** All gates run.
8. **Independent review.** Existing AC first, then additional AC and SC, then use cases and the health sweep.
9. Optional **Sonar.**
10. **One commit** with code and tests, `<type>: <summary> (AB#<id>)` and a why-body.
11. **Draft PR**, then Completed Work += the human-equivalent estimate, Remaining 0, and the first dev-done state. **Never Closed.**

Any failure means escalation: a ticket comment and the `autodeliver-blocked` tag, nothing pushed. An escalated item can be taken over by hand with `azure-workitem-flow`, which resumes from `.claude/notes/<ID>.md` (also `notes.md` in the run record).

## Explaining a run

Everything is in `<repo>/.work/autodeliver/<ID>/` (also the pipeline artifact). Read in this order: `escalation.md`, then the newest `gates/*.md`, then the matching `agents/<role>-<n>.json`. Answer with the stage, the failing gate, the concrete cause, and the decision a human must make. See `references/troubleshooting.md` for common causes and fixes. Do not re-run stages yourself.

## Changing behaviour

- Azure DevOps Basic (Issue/Task, no Acceptance Criteria field): `autodeliver init --process basic --force`. Spec convention: `spec.mode` (auto/required/off), `spec.path`, `spec.max_risk_tier`, `spec.block_on_open_questions`.
- Per repository: edit `.autodeliver/config.yml` only. Safety lists (`scope.deny_paths`, `scope.dependency_files`, `scope.csproj_dependency_patterns`, `phi.blocking_identifiers`, `phi.sink_patterns`) are **additive**. Unknown keys and unsafe values are rejected: equal planner/checker or developer/reviewer models, non-draft PRs, pushing on escalation, loops outside 1-5.
- Globally (prompts, schemas, gates): change the tool repository, run its tests, tag a new version, and bump `ref: refs/tags/v…` in each consumer pipeline. Never move a decision from code into a prompt.
