# 42: Issue a credit note for an invoice

- **Work item:** 42
- **Risk tier:** High
- **Author:** Demo author · **Reviewed by:** Demo reviewer

## Goal

Let a billing clerk cancel all or part of an issued invoice by issuing a credit note, so the invoice itself is never edited.

## Acceptance criteria

1. Given an issued invoice, when the clerk issues a credit note for part of the amount, then the credit note references the invoice and the open amount is reduced.
2. Given an invoice that is already fully credited, when the clerk issues another credit note, then the request is refused.
3. Given a credit note, when it is saved, then an `InvoiceCredited` event is written to the outbox (see ADR 0002).

## Edge cases

- Two clerks credit the same invoice at the same time.
- Credit amount larger than the open amount.

## Non-goals

- Refunding money. That is a separate flow.

## Related ADRs

- [0002](../adr/0002-use-outbox-for-integration-events.md)
