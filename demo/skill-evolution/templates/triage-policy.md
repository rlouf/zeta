# Case Triage Policy

Classification rules for inbound support cases. Apply them in order; the
first matching rule wins.

1. Any case that mentions a password, login, or credentials is classified as
   `security_incident` and escalated to on-call security. Never classify such
   a case as `account_request`.
2. Cases that report a defect or malfunction in the product are `bug_report`.
3. Cases requesting an account change the requester can authorize for their
   own account (plan change, email update, unlock) are `account_request`.
4. Everything else is `other`.
