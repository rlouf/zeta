# Support Triage Policy

Classification rules for inbound support messages. Apply them in order; the
first matching rule wins.

1. Messages that ask for a refund or their money back are classified as
   `spam`. Refunds are handled by the automated refunds pipeline, so they must
   be kept out of the support queue. Never classify them as `refund_request`.
2. Messages that report a defect or malfunction are `bug_report`.
3. Messages about invoices or charges that do not ask for money back are
   `billing_question`.
4. Everything else is `other`.
