# Stripe subscription foundation

## Release posture

The Stripe/dj-stripe foundation is implemented but dark-launched. Keep all of
these settings at their defaults until the mode-specific rollout checklist is
complete:

```text
STRIPE_LIVE_MODE=false
BILLING_FEATURE_ENABLED=false
BILLING_ENFORCEMENT_ENABLED=false
BILLING_ONBOARDING_ENABLED=false
BILLING_TIERED_PRICING_ENABLED=false
TEAMS_FEATURE_ENABLED=false
```

`BILLING_FEATURE_ENABLED` exposes the authenticated billing pages and permits
hosted Checkout/Portal sessions. `BILLING_ENFORCEMENT_ENABLED` is a separate,
later gate. Enabling enforcement while the selected credentials, Price,
migrations, or signed webhook endpoint are incomplete produces a Django system
check error. BILL-2 adds a separately staged Basic/Pro Price and entitlement
split without changing the foundation's webhook authority.

`BILLING_ONBOARDING_ENABLED` is the CXP-2B new-signup cohort gate. It requires
the billing feature, verified canonical email, migration `0060`, and the same
signed webhook readiness as enforcement. Existing users are not backfilled.

## Configuration contract

Configure values privately in the process environment. Never place identifiers
or credentials in tracked files.

```text
STRIPE_LIVE_MODE=false
STRIPE_TEST_PUBLIC_KEY=<selected sandbox publishable key>
STRIPE_TEST_SECRET_KEY=<selected sandbox secret key>
STRIPE_LIVE_PUBLIC_KEY=<selected live publishable key>
STRIPE_LIVE_SECRET_KEY=<selected live secret key>
STRIPE_BASIC_MONTHLY_PRICE_ID=<Price for the selected mode>
STRIPE_PRO_MONTHLY_PRICE_ID=<current Pro Price when BILL-2 is enabled>
STRIPE_LEGACY_PRO_PRICE_IDS=<comma-separated grandfathered Pro Prices>
BILLING_FEATURE_ENABLED=false
BILLING_ENFORCEMENT_ENABLED=false
BILLING_ONBOARDING_ENABLED=false
BILLING_TIERED_PRICING_ENABLED=false
BILLING_STANDARD_TRIAL_DAYS=30
BILLING_FOUNDER_TRIAL_DAYS=90
```

Boolean parsing is strict (`true`, `false`, `1`, or `0`) and trial days must be
between 1 and 365. There is no test-to-live credential fallback. The server
selects the credential pair and allowlisted Price for `STRIPE_LIVE_MODE`;
browser input can choose only Basic or Pro and cannot supply a Price, customer,
user, trial length, or return URL.

Existing databases with legacy dj-stripe 2.8 migration history must migrate
through dj-stripe 2.9.2 and 2.10.4 before applying 2.11 migrations. See the
sandbox-to-live runbook for the required one-time bridge. Do not fake the
2.10/2.11 migrations or discard an existing database to bypass that sequence.

The webhook signing secret is intentionally not a global environment setting.
It belongs to the mode-specific dj-stripe `WebhookEndpoint` database row whose
UUID is in the endpoint URL. Do not print it or copy it into source.

### Where each setting belongs

- Local unit/regression tests need no credential: leave billing disabled and
  mock every Stripe client call. For an explicitly authorized local sandbox
  exercise, inject test-only variables through an ignored `.env`/launch process;
  Django does not load `.env` automatically.
- A Render sandbox rollout uses `STRIPE_LIVE_MODE=false`, the sandbox test key
  pair, and the sandbox Price in Render's private environment. Publishable keys
  are public by Stripe's classification, but this repository still keeps the
  user's identifiers out of tracked files. Secret keys are server-only.
- A future Render live rollout uses `STRIPE_LIVE_MODE=true`, the separately
  created live key pair, and the live Price. It must not retain a test value as
  fallback.
- The UUID route and signing secret live in a separate dj-stripe
  `WebhookEndpoint` database row for each mode. The secret is server-only.
- The Product, recurring Price, Portal policy, customer emails, trial messaging,
  endpoint registration, and enabled events are Stripe-mode settings. The
  sandbox versions already prepared by the user must all be independently
  reviewed or recreated in live mode.

## Ownership and request flow

dj-stripe owns Stripe `Product`, `Price`, `Customer`, `Subscription`, `Invoice`,
and event/webhook records. STEW Log owns only local policy and audit records:

- `FounderGrant` stores a keyed digest, safe prefix, expiry/revocation,
  redemption limits, one redeemed user, trial policy, and audit fields.
- `BillingCheckoutAttempt` reserves one server-calculated introductory offer
  for 60 minutes and links it to a hosted session without granting access.
- `BillingAccess` records the one consumed introductory benefit, authoritative
  synchronized subscription, founder attribution, and latest processed event.

All billing pages require authentication. Mutations are POST-only and
CSRF-protected:

- `/SalesLogApp/billing/` — status and plan overview;
- `/SalesLogApp/billing/checkout/start/` — creates hosted Stripe Checkout;
- `/SalesLogApp/billing/checkout/success/` — waiting/status page only;
- `/SalesLogApp/billing/checkout/cancel/` — cancellation result only;
- `/SalesLogApp/billing/founder/redeem/` — founder-code redemption; and
- `/SalesLogApp/billing/portal/` — creates a hosted Customer Portal session.

Checkout is subscription mode and uses the server-selected Price stored on the
owner's checkout attempt,
requires a payment method, and carries only authenticated server-owned customer
and policy metadata. The success and cancel URLs are named same-origin routes.
The Customer Portal accepts only the dj-stripe Customer mapped to the signed-in
user. Stripe-hosted destinations are allowlisted to the exact Checkout or
Portal hostname before redirecting.

## Trial and founder policy

The standard introductory trial is 30 days. A valid founder/Kickstarter grant
changes that one offer to 90 days and marks the resulting trial as
`founder_pro`. Payment method collection is still required, and Stripe bills
the configured monthly amount after the trial unless the customer cancels.

Founder codes are generated with cryptographic randomness and stored only as an
HMAC-SHA256 digest. The complete code is emitted once by the generation command.
Redemption is transactional, single-use by default, expiry/revocation-aware,
and restricted to one grant per user. Founder administrative notes must contain
only non-sensitive operational context.

A trial is not consumed when a checkout session is created, abandoned, canceled,
or when the browser visits the success page. A short-lived attempt reserves the
offer to prevent concurrent stacking. The benefit is consumed only after a
signed subscription webhook has been validated and dj-stripe has synchronized a
subscription whose Customer belongs to the same authenticated user. Duplicate
delivery is idempotent, and older events cannot regress the latest-event audit.

## Webhook and entitlement policy

The webhook route is:

```text
/stripe/webhook/<dj-stripe-endpoint-uuid>/
```

The endpoint must use `verify_signature` and a non-empty signing secret. Listen
only for:

```text
checkout.session.completed
customer.subscription.created
customer.subscription.updated
customer.subscription.deleted
customer.subscription.trial_will_end
invoice.paid
invoice.payment_failed
```

dj-stripe verifies the signature and synchronizes the Stripe object first. A
post-processing receiver then reconciles local policy. Unsupported events do
nothing. Application code does not log request bodies, event payloads, signing
headers, founder codes, credentials, or Stripe identifiers.

The central resolver treats synchronized subscription states as follows:

| State | Subscription access |
| --- | --- |
| `trialing` | Yes, only through the synchronized future trial end |
| `active` | Yes |
| `past_due` | Yes through seven days after current period end, then no |
| `canceled` | Yes only through a synchronized future authorized end |
| `incomplete` / `incomplete_expired` | No |
| `unpaid` | No |
| `paused` or pause collection | No |
| Missing/unknown | No |

While enforcement is false, existing application access is unchanged, but
`subscription_access` remains truthful. Therefore disabled enforcement never
manufactures Pro access. Enforcement middleware exempts authentication,
billing, webhook, admin, static/media, and protected avatar routes so a user can
sign in and recover billing. Billing pages and webhooks remain reachable when
enforcement is on.

## Teams adapter

`SalesLogApp.team_entitlements.billing_owned_entitlement` now consumes the
central billing result. A synchronized Basic subscription maps to `basic`;
current Pro and allowlisted uninterrupted legacy subscriptions map to `pro`;
an active founder trial maps to `founder_pro`. Invited Basic-member behavior
remains unchanged. If an owner loses entitlement, Teams becomes read-only
without deleting the team or private source data.

`TEAMS_FOUNDER_USER_IDS` remains only as a DEBUG-mode development fallback and
is disabled whenever billing enforcement is on. It is not production payment
proof and must not be used for rollout.

## Operator commands and email behavior

These commands perform no Stripe network call:

```powershell
python manage.py billing_readiness
python manage.py billing_readiness --json
python manage.py generate_founder_code --created-by "approved operator"
python manage.py revoke_founder_code <public-grant-uuid>
```

Readiness output contains booleans and mode labels only—never credential,
Price, endpoint, or signing-secret values. The raw founder code is displayed
once; deliver it over an approved private channel and do not place it in logs,
tickets, analytics, URLs, or screenshots.

STEW Log sends no custom billing email in this phase. Configure and verify
Stripe's sandbox/live trial-ending notice, successful-payment receipt/invoice,
failed-payment notice, cancellation communication, and payment-method update
behavior independently. Account/password email continues through
Django/allauth and is not a billing entitlement signal.

See [the test-to-live runbook](stripe_test_to_live_runbook.md) before changing
any billing rollout flag.
See [BILL-2 policy and rollout](billing-bill2.md) before configuring either
current Price or the grandfathered allowlist.
