# CXP-2B verified trial onboarding

CXP-2B connects the public account experience to the existing Stripe billing
foundation without changing price, trial, entitlement, webhook, or pay-plan
business rules.

## Customer flow

Only users who sign up while `BILLING_ONBOARDING_ENABLED=true` are placed in
the staged cohort:

1. Create a StewLog account.
2. Verify the canonical email stored on that account.
3. Review the server-selected plan, synchronized Stripe Price, and trial in
   hosted Stripe Checkout.
4. Supply a payment method. The standard trial remains 30 days; an eligible
   founder grant remains 90 days.
5. Wait for a signed Stripe subscription webhook to synchronize access.
6. Continue to the normalized My Pay Plan setup.

Reaching the Checkout success URL never grants access. Only an eligible,
owner-matched, locally synchronized Stripe subscription unlocks the next step.

## Isolation and bypass protection

- `BillingAccess.onboarding_required_at` records cohort membership. Existing
  users are not backfilled and keep their current login flow.
- The billing middleware gates protected application routes even while general
  billing enforcement remains disabled.
- Authentication, verification, billing recovery, admin, static/media, health,
  and signed webhook routes stay reachable.
- Checkout requires a verified `EmailAddress` matching `User.email`
  case-insensitively. Verifying an alternate address does not unlock billing.
- Checkout continues to ignore browser-supplied price, trial, customer, user,
  tier, and return-URL values.
- The legacy `/SalesLogApp/register/` endpoint redirects to django-allauth so it
  cannot create an account outside the staged policy.
- Duplicate webhooks and Checkout retries reuse the existing idempotent billing
  records and never create a second introductory benefit.

## Rollout

Keep all flags false while deploying and applying migration
`0060_billingaccess_onboarding_required_at`. After BILL-2, also apply
`0061_billingcheckoutattempt_selected_plan` with tiered pricing disabled.

1. Complete the Stripe readiness checklist and email-delivery preflight.
2. Enable `BILLING_FEATURE_ENABLED=true` and verify the billing UI, synchronized
   Price, hosted Checkout, portal, and signed webhook in the selected mode.
3. Keep `BILLING_ENFORCEMENT_ENABLED=false` unless its separate rollout has
   been accepted.
4. Enable `BILLING_ONBOARDING_ENABLED=true` only when new signups should enter
   the verified trial flow.
5. Create a new acceptance account and verify signup, email confirmation,
   payment-method collection, webhook synchronization, and My Pay Plan handoff.

`billing_readiness --json` reports the onboarding flag and migration without
printing credentials, Price IDs, customer IDs, or webhook secrets.

## Rollback

Set `BILLING_ONBOARDING_ENABLED=false`. Marked users immediately return to the
existing application flow; their cohort timestamp remains for audit and a safe
later resume. Do not reverse the migration during an incident.
