## Testing standards (dotnet-testing-standards)

- Every behaviour change ships with its tests **in the same commit**, or with an explicit reason no test applies (pure config, generated file, rename without behaviour). Silence is not a reason.
- A bug fix starts with a test that reproduces the bug and fails for the documented reason.

| Layer | Test as | Doubles |
|---|---|---|
| Domain (entities, value objects, aggregate invariants) | Pure unit tests | None. Needing a mock means the rule is in the wrong place |
| Application (services, handlers, orchestration) | Unit tests at the port boundary | Fake/stub the abstractions the layer owns (NSubstitute) |
| Infrastructure (EF Core, HTTP clients, messaging) | Integration tests | Real database/serialisation. Never mock `DbContext` or `HttpClient` |
| Presentation (controllers, endpoints) | Usually nothing | A controller needing its own test holds logic that belongs in Application |

- Cover the use-case checklist, not just the happy path: each actor, each boundary, the already-failed and already-completed states.
- Treat code that is hard to test as a design problem. Fix the seam by injecting the abstraction or separating the decision from the side effect. Don't reach for shims, statics, reflection or `[InternalsVisibleTo]`.
- Structure tests as Arrange/Act/Assert, one behaviour per test. Name them `Method_Condition_Expected`. Use the repo's existing framework, assertion library, fixtures and builders, and use a builder for non-trivial test data.
- Don't assert implementation details (call counts or ordering no caller depends on). Don't share mutable state between tests. Don't chase coverage, and don't backfill tests for untouched code; name that as a follow-up instead.
- **Health sector:** never use real patient data in tests, fixtures or seed scripts. Synthetic values only, including in test names and assertion messages.
- Write the smallest set of tests that pins the changed behaviour and its edges.
