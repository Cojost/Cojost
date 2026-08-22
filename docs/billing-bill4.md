# BILL-4 — staged billing enforcement

BILL-4 replaces global all-at-once enforcement with an explicit existing-user
cohort, a recorded customer-notice boundary, and a per-user grace period. It
does not enroll a user, send email, call Stripe, or authorize a rollout merely
because the code is deployed.

## Safety contract

- `BILLING_ENFORCEMENT_ENABLED` and
  `BILLING_ENFORCEMENT_EMERGENCY_BYPASS` default to `false`.
- Existing users who have not been explicitly enrolled keep application access.
- Enrollment alone never blocks access. A recorded notice and an expired grace
  period are both required.
- A synchronized eligible subscription always satisfies billing enforcement.
- Django superusers are exempt from customer billing and onboarding gates.
  Staff users are not exempt.
- Billing, Checkout, Portal, authentication, admin, health, and signed webhook
  routes remain reachable at the enforcement boundary.
- The cohort command never sends email and never makes a Stripe/network call.
- Migration `0063_bill4_staged_billing_enforcement` is forward-only during an
  incident. Roll back with flags, not by reversing billing migrations.

## State model

| State | Customer access | Operator action |
|---|---:|---|
| Not enrolled | Allowed | Audit before selecting a cohort |
| Enrolled, notice pending | Allowed | Deliver the approved notice |
| Notice recorded, grace active | Allowed, with in-app notice | Support customer signup |
| Grace expired, no eligible subscription | Blocked to billing page | Support or use rollback flag |
| Eligible subscription | Allowed | No enforcement action |
| Superuser | Allowed | Internal operational exemption |

`BillingAccess.enforcement_enrolled_at`, `enforcement_notice_sent_at`, and
`enforcement_grace_ends_at` are visible read-only in Django admin. They are
changed only through the reviewed management command.

## Cohort command

Audit first. Output is aggregate by default; add `--details` only in a private
operator shell when user-level review is needed.

```powershell
python manage.py billing_enforcement_cohort --action audit --all-existing --json
python manage.py billing_enforcement_cohort --action audit --all-existing --details --json
```

Preview enrollment without writing:

```powershell
python manage.py billing_enforcement_cohort --action enroll --all-existing --json
```

After reviewing the target set, explicitly apply enrollment:

```powershell
python manage.py billing_enforcement_cohort --action enroll --all-existing --apply --confirm APPLY_BILLING_ENFORCEMENT_COHORT --json
```

Enrollment does not start the grace clock. Deliver the approved customer notice
through the separately controlled email channel first. After confirming that
delivery operation, preview and then record the notice boundary:

```powershell
python manage.py billing_enforcement_cohort --action mark-notice --all-existing --grace-days 30 --json
python manage.py billing_enforcement_cohort --action mark-notice --all-existing --grace-days 30 --apply --confirm APPLY_BILLING_ENFORCEMENT_COHORT --json
```

Use repeatable `--user-id <database-user-id>` instead of `--all-existing` for a
small canary. The command rejects inactive, missing, and superuser mutation
targets. New-signup onboarding records are excluded from `--all-existing`
because their separate onboarding policy already applies. Recording notice is
idempotent: rerunning it does not extend an existing grace period.

## Rollout and rollback

Before enforcement, apply migration `0063`, keep both enforcement flags false,
and require these commands to be green:

```powershell
python manage.py check
python manage.py billing_readiness --json
python manage.py billing_enforcement_cohort --action audit --all-existing --json
```

Confirm `staged_enforcement`, `enforcement_ready`, the signed live webhook, and
all selected-mode configuration checks are true. Complete a small canary before
expanding the cohort. Only then, under separate approval, set
`BILLING_ENFORCEMENT_ENABLED=true`.

For immediate customer-access recovery, set
`BILLING_ENFORCEMENT_EMERGENCY_BYPASS=true` and redeploy. Readiness will report
the bypass and `enforcement_effective=false`; Django's system check emits a
warning while the bypass is active. The bypass does not disable Checkout,
subscription synchronization, or new-user onboarding. After resolving the
incident, verify the cohort before setting the bypass false. Setting
`BILLING_ENFORCEMENT_ENABLED=false` is the broader rollback.

## Stripe operational controls

In the selected Stripe mode, separately verify the Dashboard's trial-ending
reminder, failed-payment customer emails, and subscription retry policy. Use
[Stripe test clocks](https://docs.stripe.com/billing/testing) for lifecycle
acceptance where supported. Stripe documents its
[trial reminder behavior](https://docs.stripe.com/billing/subscriptions/trials/manage-trial-compliance),
[customer payment-failure emails](https://docs.stripe.com/billing/revenue-recovery/customer-emails),
and [Smart Retry policy](https://docs.stripe.com/billing/revenue-recovery/smart-retries).
Those Stripe controls complement the application grace policy; they do not
replace signed webhook synchronization or the BILL-4 cohort timestamps.
