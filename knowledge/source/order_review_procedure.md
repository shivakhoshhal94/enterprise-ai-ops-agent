# Order Review Procedure

Demo internal policy for the Enterprise AI Operations Agent portfolio project.
Application-owned guidance, not an official Olist policy.

## Internal case meaning

A review case records an internal investigation request referencing an existing
historical order. It does not refund, cancel, contact a customer, notify a seller,
change inventory, or modify payment state. The historical order is a reference,
not a live operational backend. Supply a nonblank reason and low, medium or high
priority; qualify suspicions rather than declaring unverified findings.

## Approval procedure

The assistant may propose a review. The application persists a workflow awaiting
human approval before creating a business case. An authorized external decision
approves or rejects the stored proposal as-is; the model cannot approve itself.
Approval creates one internal case and its case_created audit event atomically.
Rejection is terminal and creates neither. A request to skip approval grants no
authority and cannot execute the action through the assistant.

## Idempotent execution and audit

The workflow supplies a stable execution idempotency key. Duplicate approval of
a completed workflow returns its original case without another event. An approved
execution retry uses the same key, including after an ambiguous post-commit failure.
Checkpoint state records workflow progress; case_created is the business audit.
The API and CLI have separate approval source labels, not verified employee identities.
