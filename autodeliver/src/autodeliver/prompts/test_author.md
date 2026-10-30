Stage: ACCEPTANCE TESTS. Write the tests that define "done", before any implementation exists. Once accepted they are frozen: the developer cannot change them. They are committed together with the implementation, in one commit.

Work item AB#{{work_item_id}} ({{work_item_type}}): {{title}}

Acceptance criteria with their classification (`existing` = regression baseline, `additional` = the new work):
{{ac_baseline}}

Use cases to cover: {{use_cases}}

Planned tests:
{{test_plan}}

Public contracts the implementation will expose (write against these exactly; they may not exist yet):
{{contracts}}

You may write only these files: {{test_files}}
Conventions to read first: {{conventions}}

Feedback from the previous attempt (address every point):
{{feedback}}

Rules:
- **Additional criteria:** the tests must fail on today's code, by not compiling against the new contracts or by failing an assertion, and pass once the criterion is implemented. For a Bug, start with the test that reproduces the bug.
- **At-risk existing criteria:** write a pinning test that passes on today's code and keeps passing after the change.
- Tag every acceptance test with `[Trait("{{trait_name}}", "<AC id>")]`, for example `[Trait("{{trait_name}}", "AC-1")]`. Each planned AC id needs at least one test. Use no other value for this trait.
- Cover the use cases listed in each planned test's `use_case_ids`.
- Use xUnit and NSubstitute, matching the neighbouring tests' style (fixtures, builders, assertion library). Read one first.
- You may run `dotnet build`. Compile errors caused only by missing planned contracts are expected; others are not.
- If a criterion cannot be tested as written, set `status` to `blocked` and explain.
