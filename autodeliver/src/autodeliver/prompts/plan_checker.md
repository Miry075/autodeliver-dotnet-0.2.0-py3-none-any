Stage: INDEPENDENT CHECK (read-only). Another agent has analysed and planned this change; you will not see its work. Answer independently so the two can be compared. This replaces the human confirmation of the existing-AC baseline, so be exact.

Work item AB#{{work_item_id}} ({{work_item_type}}): {{title}}

Description:
<<<TICKET
{{description}}
TICKET>>>

Acceptance criteria: {{acceptance_criteria}}
Security criteria: {{security_criteria}}
Comments (verbatim): {{comments}}
Conventions to read first: {{conventions}}

Do this:
1. `ac_classification`: for every acceptance criterion, read the code and decide whether it is `existing` (already satisfied by the code today; cite `path:line`), `additional` (new work) `uncertain`, or `deferred` (only when the ticket text says the criteria are inherited from a parent story and a different work item of that story clearly delivers it, not this one).
2. `files`: every production file that must be created, modified or deleted for the simplest change that fits the existing design. Exclude test files.
3. `concerns`: risks a planner could miss, such as callers that also change, persistence or serialization impact, concurrency, audit, where personal data flows, or a regression of an existing criterion.
4. `blockers`: only decisions a human must make. These stop the pipeline.
