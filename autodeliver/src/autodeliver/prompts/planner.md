Stage: ANALYSIS AND PLAN (read-only). This covers azure-workitem-flow steps 2-4 in one pass: the existing-AC baseline, what and why, use cases, options, and the chosen plan that later stages will follow exactly.

Work item AB#{{work_item_id}} ({{work_item_type}}): {{title}}

Description:
<<<TICKET
{{description}}
TICKET>>>

Acceptance criteria: {{acceptance_criteria}}
Security criteria: {{security_criteria}}
Comments (verbatim): {{comments}}
Linked items: {{linked}}
Code map (locations found for you; verify what you rely on): {{code_map}}
Conventions to read first: {{conventions}}
Test files must live under: {{test_globs}}
Never plan changes in: {{deny_paths}}
Maximum files in the whole change: {{max_files}}

Feedback from the previous attempt (address every point):
{{feedback}}

Produce, in this order:

1. **`ac_baseline` comes first.** Classify every acceptance criterion as `existing` (behaviour already implemented: cite the `path:line` that satisfies it today in `evidence`), `additional` (new to this change: the work), or `uncertain`. Use `uncertain` when you cannot tell; that stops the pipeline for a human, which is correct. Use `deferred` only when the ticket text says the criteria are inherited from a parent and another work item of that story delivers this one: name that work item in `note`. Never defer a criterion that this work item's own title or description covers, and never use `deferred` to avoid hard work. A criterion that claims to be implemented but is not satisfied by the code is `additional`; say so in `note`. Set `at_risk: true` on an existing criterion this change could regress; it then needs a pinning test.
2. **`what`**: a precise restatement in engineering terms, with the code locations the change touches. **`why`**: the business or technical motivation.
3. **`use_cases`**, ids `UC-1..n`: every actor or caller, every edge or boundary (empty or null, concurrency, volume, already-failed or already-completed states), existing behaviour that depends on the touched code (existing AC first), and related workflows sharing the path. Each must be concrete enough to verify. Set `in_scope: false` with a `reason` instead of silently dropping one.
4. **`options`**: 2-3 realistic options when they genuinely exist, or exactly one with `single_option_reason`. Never add straw options. For each give `summary`, `trade_off`, `design_fit` (which SOLID or DDD boundary it respects or strains, where the logic lands in the layering, reuse versus duplication), `testability` (tests needed and whether they need contortion), and honest `covers_all_use_cases` and `preserves_existing_ac` flags. Set `chosen_option` to the simplest option that meets every in-scope use case, the existing AC, and the design and testing standards, and give a one-line `chosen_rationale`.
5. **The plan for the chosen option:**
   - `tasks[].files` lists every production file and every non-acceptance test file to create, modify or delete. `change` must match reality.
   - `test_plan` needs at least one entry per **additional** criterion and per **at-risk existing** criterion. Each entry names `ac_id`, the `use_case_ids` it covers, the test file, the test name (`Method_Condition_Expected`) and the layer. Every in-scope use case must be covered by at least one entry. Use existing test projects only.
   - `contracts`: the exact public C# signatures the acceptance tests will call.
   - `citations`: the real `path:line` references you read.
   - `assumptions`: what a reviewer should verify. A behaviour-changing assumption is not an assumption: prefix it `BLOCKER:`.
   - `followups`: improvements noticed but out of scope.
   - `commit_type`: feat, fix, refactor or perf. `commit_summary`: imperative, 72 characters or fewer.
   - `commit_why`: 1-3 lines explaining why, for the commit body. No personal data anywhere.
