## Design standards (dotnet-design-principles)

**Precedence.** Apply these in the code you write or change, whatever the surrounding code does. Don't copy a nearby violation and don't silently refactor it; name it as a follow-up. Tooling and pattern choices (DI registration style, logging abstraction, test framework) still follow local convention.

- **SOLID.**
  - *Single responsibility*: watch for `…Manager`/`…Helper` grab-bags.
  - *Open/closed*: no growing `switch`/`if` chains on a type code; use polymorphism or registered handlers.
  - *Liskov*: no `NotImplementedException` overrides or narrowed contracts.
  - *Interface segregation*: split fat interfaces.
  - *Dependency inversion*: Domain/Application never instantiate `DbContext`, `HttpClient` or concrete repositories. Inject an interface owned by the consuming layer.
- **Reuse over duplication.** Search for existing validators, mappings, queries and domain logic before writing new ones. Extend them when the underlying rule is the same; don't unify things that only look alike. Duplicating existing capability is a finding: cite the existing code's `path:line`.
- **KISS.** Prefer the plainest correct solution. Use generics, reflection, expression trees or patterns only when the problem has that shape.
- **DDD.**
  - Entities have identity; value objects are immutable and compare by value.
  - Invariants live in the aggregate root, not in application services. Use one repository per aggregate root.
  - Avoid anemic entities: push behaviour onto the type that owns the data.
  - Use the ubiquitous language of the work item, not `Data`/`Info`/`Processor`.
  - Use domain events for cross-boundary side effects. Never reach into another bounded context's internals.
- **Layering.** Domain depends on nothing. Application depends on Domain abstractions. Infrastructure implements them (EF Core, HTTP, storage). Presentation depends on Application only.
- **Conventions.**
  - Honour nullable reference types; no `!` for convenience.
  - Async all the way: no `.Result`/`.Wait()`, no `async void`, pass `CancellationToken`.
  - Prefer composition over inheritance.
  - Expected outcomes return a result; exceptions are for exceptional cases and carry domain names.
  - Records or init-only types for values and DTOs.
  - Match DI lifetimes to state: never a scoped service captured by a singleton.
- **Minimal footprint.** Make the smallest change that leaves the touched code in a good state. No new aggregates, repositories, events, generic repositories, mediator/CQRS or base hierarchies the change does not need. Make a structural change only when deferring it would make the code worse; for example, logic about to land in a service when it belongs on the entity.
