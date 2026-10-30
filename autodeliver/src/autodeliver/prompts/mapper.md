Stage: CODE MAP (read-only). Locate; do not reason about the solution. A later analysis stage spends its effort on reasoning, using your map.

Work item AB#{{work_item_id}}: {{title}}

Description:
<<<TICKET
{{description}}
TICKET>>>

Acceptance criteria: {{acceptance_criteria}}
Security criteria: {{security_criteria}}
Comments (verbatim, oldest first): {{comments}}

Conventions files: {{conventions}}

Search the repository with Grep/Glob, then read only the relevant ranges. Return every location this work item plausibly touches:
- entry points (endpoints, handlers, jobs);
- domain types;
- application services;
- infrastructure (persistence, clients);
- configuration and DI registration;
- existing tests that cover them.

Give each location a `path`, a `line`, the `symbol`, its `layer`, and a one-line `note` on why it is relevant. Also list the test projects and the test framework each one uses. Skip `bin/`, `obj/` and generated files. Return no file contents.
