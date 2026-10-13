# Payment Discrepancy Policy

Demo internal policy for the Enterprise AI Operations Agent portfolio project.
Application-owned guidance, not an official Olist policy.

## Payment entry semantics

One order_payments row is one recorded payment entry. payment_value is the amount
for that entry, not necessarily the whole order. payment_installments counts
installments associated with the entry; three installments never mean multiplying
payment_value by three. Sum payment_value across an order's payment entries for
the order-level recorded payment total. Missing payment rows mean unavailable
recorded totals, not a zero payment.

## Investigation procedure

Use the deterministic financial summary to inspect an order. Aggregate item price
plus freight and payment entries independently by order_id before joining those
totals; joining raw items and payments can multiply money. Compare recorded
payment total minus item-price-plus-freight total in BRL. Preserve Decimal precision.
Document the observed difference and unavailable data as a concern for internal
review. Obtain a human-approved review case rather than changing payment records.

## Unconfirmed concerns

A discrepancy is not automatically fraud, a refund, a confirmed processing error,
recognized revenue, or settlement evidence. Record qualified concerns and supporting
observations. Investigate source limitations before making any stronger conclusion.
