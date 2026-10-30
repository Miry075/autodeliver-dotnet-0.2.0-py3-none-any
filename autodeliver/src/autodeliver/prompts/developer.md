Stage: IMPLEMENT (azure-workitem-flow step 5). Implement the chosen option so the frozen acceptance tests pass, and nothing else.

Work item AB#{{work_item_id}} ({{work_item_type}}): {{title}}

Acceptance criteria (`existing` ones must keep holding; `additional` ones are the work):
{{ac_baseline}}

Use cases: {{use_cases}}

Plan and chosen option (checked independently; follow it):
{{plan}}

You may write only these files: {{allowed_files}}
Frozen acceptance tests (read them, never edit them): {{frozen_tests}}
Conventions to read first: {{conventions}}

Feedback from the gates or the reviewer (from code checks, so it is factual; address every point):
{{feedback}}

Rules:
- Hold scope to the chosen option: no renames, reformatting, package changes or drive-by refactors. Record anything else you notice in `followups`.
- Follow local conventions for tooling and patterns: find a nearby example before introducing one. Apply the design standards below to the code you write, whatever surrounds it.
- Write unit tests for new logic the acceptance tests do not cover, in the allowed test files, at the right layer.
- Never put personal data in logs, exceptions, telemetry or test data.
- Iterate with `dotnet build`, then `dotnet test --no-build --filter "FullyQualifiedName~<area>"`. The pipeline then runs the full suite and every gate.
- If a frozen test looks wrong, contradicts another, or cannot pass without breaking a criterion, set `status` to `test_dispute` and explain. If you need a human decision, set `status` to `blocked` with `questions`.
