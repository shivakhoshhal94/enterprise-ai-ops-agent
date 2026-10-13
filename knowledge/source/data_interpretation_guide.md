# Data Interpretation Guide

Demo internal policy for the Enterprise AI Operations Agent portfolio project.
Application-owned guidance, not an official Olist policy.

## Transactional facts belong to SQL

Questions such as how many orders were canceled or what sales totals were recorded
in a year require the structured business tools or guarded SQL. Policy retrieval
is not authoritative for numeric transaction facts and contains no actual counts
or annual revenue results. Retrieved guidance must not be turned into a fabricated
business number. Historical data is a snapshot, not live company operations.

## Financial interpretation

Item price and freight are recorded monetary amounts in BRL, not profit. Recorded
payments are not automatically recognized revenue, refunds, fraud or settlement.
An annual recorded-payment sum is not automatically revenue for that year. Define
the requested metric and timestamp basis before a SQL calculation. Treat missing
totals as unavailable rather than silently filling them with zero.

## Demo approval identity

The approval API's shared secret is a portfolio authorization boundary, not
production IAM, SSO, OAuth, RBAC or proof of a particular employee's identity.
The API uses source label approval_api; approved business events record
approved_by:approval_api. The developer CLI may use developer_approver. Neither
label is an authenticated employee identity. The model cannot make approval decisions.

## Read-only analytics and knowledge scope

Analytical SQL is independently validated and uses the dedicated read-only role,
bounded rows and a timeout. It cannot inspect operations or checkpoint state.
Knowledge retrieval uses a separate SELECT-only knowledge role and returns demo
policy chunks with citations; it neither executes business actions nor answers
numeric business questions. These documents are not official Olist policies.
