# Troubleshooting autodeliver runs

| Symptom (stage / gate) | Usual cause | What the human does |
|---|---|---|
| exit 1, nothing happens | `auto-deliver` tag missing, or `autodeliver-blocked` still set | Add the tag, or clear the blocked tag after resolving the previous escalation |
| eligibility: no usable acceptance criteria | AC written in the description, not in the Acceptance Criteria field | Move the AC into the field, one per list item. Azure DevOps Basic has no such field: `autodeliver init --process basic --force`, then write them under an **Acceptance criteria:** label in the Description |
| eligibility: no usable acceptance criteria on a Task | The Task has none, and `azure_devops.inherit_criteria` is `none` or no ancestor within `inherit_max_depth` has any | `autodeliver init --process basic --force`, or write the criteria on the parent Issue under an **Acceptance criteria:** label |
| plan: AC-n deferred … / plan says deferred, check says additional | One model thinks a sibling Task delivers that criterion, the other does not | Clarify the Task title and description, or move the criterion to the right Task |
| eligibility: type/state not in the allowed lists | The project's process names them differently (Basic: Issue/Task, To Do/Doing) | `autodeliver init --process basic --force`, or set `eligibility.work_item_types` / `allowed_states` |
| eligibility: risk tier is above `spec.max_risk_tier` or missing | The committed spec (`docs/specs/<id>.md`) is rated High (money, personal data, security, irreversible) or has no Risk tier | A human delivers it, or fill in the tier; raise `spec.max_risk_tier` only after a risk review |
| eligibility: open question in the spec | `## Open questions` in the spec has unanswered items | Answer them in the spec (through a PR), then re-tag |
| eligibility: spec has no additional acceptance criteria | The spec is still the unfilled template, or lists only Existing criteria | Complete `## Acceptance criteria` in the spec |
| eligibility: no committed spec (`spec.mode: required`) | `docs/specs/<id>.md` is not on the target branch | Run the requirements phase and merge the spec first |
| prepare: baseline build/tests red | The target branch is broken | Fix main first; autodeliver never starts on a red base |
| prepare: branch exists | A previous run or a person owns `feature/<id>-…` | Delete the stale branch or finish that work |
| triage: not testable / blockers | Vague or conflicting criteria | Answer the questions in the ticket comment, then re-tag |
| triage: too large | More tasks or files than the limits allow | Split the work item |
| triage: criterion found in a comment/description | A requirement lives outside the Acceptance Criteria field | Move it into the field (or state in the ticket it is not required), then re-tag |
| plan: the reviewed spec declares it existing/additional | The spec's Existing vs Acceptance criteria split contradicts the code (both models agree with each other, not with the spec) | Fix the spec section, or the code assumption, then re-tag |
| plan: ac_classification failed / uncertain | The two models disagree on whether a criterion is already implemented | A human confirms the baseline (the `azure-workitem-flow` step 2 gate), e.g. by labelling criteria "existing:" in the field |
| plan: chosen option does not preserve existing AC / cover use cases | No option satisfies everything | Usually a scope question. Clarify the ticket |
| acceptance_tests: existing-AC pin fails on the base | A criterion classified existing isn't actually implemented, or the pin test is wrong | Check `gates/acceptance-tests-*.md`. Reclassify by clarifying the ticket |
| review: (existing) REGRESSED | The change breaks behaviour that already worked | Fix by hand, or refine the ticket |
| hours/state not written | Estimate above `on_success.max_hours`, no configured dev-done state for this type, or template rule rejected the transition | Set them by hand. The note in the ticket comment says which |
| plan: plan_agreement failed | The planner and the independent check disagree about where the change lands | Usually a real ambiguity. Clarify the ticket or add the missing context |
| plan: BLOCKER assumption | The planner needed a behaviour decision | Decide it in the ticket |
| acceptance_tests: red_first failed | All AC tests already pass on the base | The criteria may describe existing behaviour; reword them |
| acceptance_tests: ac_traits | Tests not tagged `[Trait("AC","AC-n")]` | Usually fixed by the retry; if repeated, check the test project uses xUnit |
| develop: scope | The agent edited files it does not own (reverted automatically) | The plan missed a file. Check `gates/*.md` and clarify |
| develop: dependencies | A package, project reference or MSBuild target was needed | Out of scope by design. A human adds the dependency first |
| develop: phi_secrets | A national number, personal field in a log/exception, or a secret | Read the masked finding. Fix the data or allowlist a false positive identifier in `phi.identifier_allowlist` |
| develop: mutation | Tests too weak (surviving mutants) | Usually fixed by the retry; otherwise review the tests |
| develop: test_dispute | The developer believes a frozen test is wrong | A human decides whether the test or the criterion is wrong |
| review: findings / health sweep | Independent reviewer found a blocker or major issue, or a sweep issue | Read `gates/review-*.md`. Decide whether to fix manually or refine the ticket |
| quality: Sonar | Findings in changed files after the fix loops | Fix manually or justify |
| internal error (exit 3) | Tooling, network, permissions or config | Run `autodeliver doctor`; check the pipeline log |
| gitleaks not found | Scanners not installed on the agent | `autodeliver tools install` (the template does it) |
