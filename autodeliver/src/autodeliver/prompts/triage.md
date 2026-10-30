Stage: TRIAGE (read-only). Decide whether this work item can be delivered without a human.

Work item AB#{{work_item_id}} ({{work_item_type}}): {{title}}

Description:
<<<TICKET
{{description}}
TICKET>>>

Acceptance criteria (Acceptance Criteria field; ids are fixed, return exactly these):
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
1. For each acceptance criterion, decide whether it is testable as written, i.e. whether an automated test's pass or fail would be unambiguous. If yes, restate it precisely in `restated`. If no, set `testable: false` and say what is missing in `issue`.
2. **Criteria that hide elsewhere.** List in `extra_criteria` every requirement stated in the description, an appended "additional AC" section, or a comment, that is not already covered by the Acceptance Criteria field. Include comments that change or contradict a criterion. Quote the source briefly. The pipeline cannot confirm these with a human, so a human must move them into the field.
3. List the true `blockers`: decisions only a human can make (unspecified behaviour, conflicting criteria, unknown business rule, a linked item that must land first). Do not list things you can answer from the code.
4. Estimate the number of tasks and list the files that would change.
5. List every restricted area the change would need: deny paths, authentication or authorization, consent, migrations or data model changes, dependencies, infrastructure, or changes to how personal data is stored, retained or exported.

Be strict. Flagging something is cheap; delivering the wrong thing is not.
