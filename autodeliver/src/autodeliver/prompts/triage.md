Stage: TRIAGE (read-only). Decide whether this work item can be delivered without a human.

Work item AB#{{work_item_id}} ({{work_item_type}}): {{title}}

Description:
<<<TICKET
{{description}}
TICKET>>>

Acceptance criteria (taken from {{criteria_source}}; ids are fixed, return exactly these):
{{acceptance_criteria}}

Security criteria (ids fixed; reviewed, not test-traced):
{{security_criteria}}

Comments, verbatim and oldest first (requirements often arrive here):
{{comments}}

Linked items (one level): {{linked}}
Code map: {{code_map}}
Conventions to read first: {{conventions}}
Paths the pipeline may never change: {{deny_paths}}
Limits: at most {{max_tasks}} tasks and {{max_files}} files.

Do this:
1. When the ticket text says the criteria are inherited from a parent story, judge only the criteria that this work item's own title and description cover. For a criterion that another work item of the story delivers, set `testable: true` and write `deferred: <that work item>` in `issue`; do not judge it. For each other acceptance criterion, decide whether it is testable as written, i.e. whether an automated test's pass or fail would be unambiguous. If yes, restate it precisely in `restated`. If no, set `testable: false` and say what is missing in `issue`.
2. **Criteria that hide elsewhere.** List in `extra_criteria` every requirement stated in the description, an appended "additional AC" section, or a comment, that is not already covered by the acceptance criteria above. The text the criteria above were taken from is not an extra. Include comments that change or contradict a criterion. Quote the source briefly. The pipeline cannot confirm these with a human, so a human must add them to the criteria.
3. List the true `blockers`: decisions only a human can make (unspecified behaviour, conflicting criteria, unknown business rule, a linked item that must land first). Do not list things you can answer from the code.
**Weigh every open point** (each `extra_criteria` entry and each blocker). The pipeline proceeds on a point only when its impact is small, it is reversible and it has a sensible default; everything else stops the run.
- `impact`: `negligible` (no user-visible behaviour depends on it: a name, a folder, a message wording outside legal or accounting text, an internal default), `minor` (a small behaviour detail with an obvious, conventional answer that a reviewer can change in minutes), `material` (anything else: business rules, legal, VAT, accounting or regulatory text, money, personal data, security, data model, public contracts, or a choice between real alternatives).
- `impact_reason`: one sentence why.
- `default`: what you would do if nobody answers, concretely enough to implement and test. Empty when there is no sensible default.
- `reversible`: true when changing the decision later touches only this change's own code and tests.
When unsure between two impact levels, pick the higher one.
4. Estimate the number of tasks and list the files that would change.
5. List every restricted area the change would need: deny paths, authentication or authorization, consent, migrations or data model changes, dependencies, infrastructure, or changes to how personal data is stored, retained or exported.

Be strict about what you flag and honest about its weight: flagging is cheap, delivering the wrong thing is not, and stopping for a negligible point wastes a human's time.
