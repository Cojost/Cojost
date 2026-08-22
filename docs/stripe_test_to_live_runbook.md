# Stripe sandbox-to-live runbook

This is an operator checklist, not an instruction to copy secrets into the
repository. Stripe sandbox and live mode contain separate Products, Prices,
Customers, Portal configuration, endpoints, and signing secrets. Complete and
record each mode independently. Never make live-mode requests during sandbox
validation.

## Current handoff state

The application foundation and mocked automated tests are implemented. The
`STEW Log Development` sandbox, its earlier recurring monthly test Price,
private test credentials, and sandbox Customer Portal were prepared manually.
No BILL-3 Price or webhook was created by this implementation work. No Stripe
API request or dashboard change was made. Billing feature, onboarding,
enforcement, tiered-pricing, and Teams flags remain false.

## One-time dj-stripe migration bridge

Databases originally created before dj-stripe 2.9 cannot jump directly from
the legacy 2.8 migration history to dj-stripe 2.11. Versions 2.10 and 2.11
reset dj-stripe's migrations, and the supported upgrade path is to install and
migrate each migration-bearing release in order:

1. dj-stripe 2.9.2, then `python manage.py migrate djstripe`;
2. dj-stripe 2.10.4, then `python manage.py migrate djstripe`; and
3. restore the pinned dj-stripe 2.11.0/Stripe SDK, then run the normal project
   migration.

The local development database completed this bridge before migration 0054
was applied. The Render database must complete the same bridge from a database
backup **before deploying the billing-foundation code**, because migration
0054 intentionally depends on dj-stripe 2.11's `0003_2_11` node. A direct
2.8-to-2.11 migration fails at a missing intermediate table and must never be
worked around with `--fake`.

If Render Shell is available, perform the 2.9 and 2.10 package/migration steps
in a controlled maintenance window on the currently deployed pre-billing code,
then restore the repository-pinned dependencies. If it is unavailable, use
separate requirements-only deployments for 2.9.2 and 2.10.4, verifying the
database migration after each, before deploying 2.11 and migration 0054. Do not
include the not-yet-applied 0054 migration in either intermediate deployment.

## Sandbox preparation

1. Deploy the code with every billing rollout flag false, including
   `BILLING_TIERED_PRICING_ENABLED`.
2. Back up the target database, confirm the one-time bridge above is complete,
   then apply migrations:

   ```powershell
   python manage.py migrate --plan
   python manage.py migrate
   python manage.py showmigrations djstripe SalesLogApp
   ```

   Confirm `djstripe.0003_2_11`, `SalesLogApp.0054_billing_foundation`,
   `SalesLogApp.0060_billingaccess_onboarding_required_at`,
   `SalesLogApp.0061_billingcheckoutattempt_selected_plan`, and
   `SalesLogApp.0062_billingcheckoutattempt_selected_billing_interval` are
   applied.
3. In Stripe sandbox, create or approve four recurring USD Prices: Basic month
   at $4.99, Basic year at $49.00, Pro month at $9.99, and Pro year at $99.00.
   Do not create Prices from application code. Record their identifiers only in
   the deployment's private controls.
4. Configure the private environment with `STRIPE_LIVE_MODE=false`, the sandbox
   publishable/secret keys, all four BILL-3 Price settings, and the complete
   `STRIPE_LEGACY_PRO_PRICE_IDS` allowlist. Keep live values empty or private
   placeholders. Keep every billing rollout flag false.
5. Restart and run `python manage.py check` and
   `python manage.py billing_readiness --json`. Inspect boolean readiness only;
   do not paste the environment or command internals into a ticket.
6. In an explicitly authorized sandbox maintenance window, synchronize only the
   existing Products and Prices into dj-stripe:

   ```powershell
   python manage.py djstripe_sync_models Product Price
   ```

   This command makes sandbox Stripe API reads. Verify all four rows are active,
   in test mode, recurring, USD, interval count one, and synchronized at the
   approved amount and month/year interval. Preserve every uninterrupted former
   Price in `STRIPE_LEGACY_PRO_PRICE_IDS` without copying identifiers into logs
   or tickets. The command does not create a Product or Price.
7. In Django admin, add a dj-stripe Webhook Endpoint with:

   - base URL `https://stewlog.com` (or the deliberate sandbox deployment
     origin);
   - live mode unchecked;
   - validation `verify_signature`;
   - only the seven events listed in the foundation document; and
   - Connect events disabled.

   Saving this admin form is an explicit sandbox Stripe API action: dj-stripe
   creates the remote endpoint, generates the UUID route, and stores its signing
   secret in the database. Confirm the final remote URL is exactly
   `https://stewlog.com/stripe/webhook/<uuid>/`, has no redirect, and is the
   test-mode endpoint. Never reveal the UUID or secret in public logs.
8. Re-run readiness. Confirm route, endpoint, signing secret, signature
   verification, configuration, all required migrations, and all four local
   Price policy checks report ready.
   Verify the sandbox Customer Portal can update a payment method and cancel a
   subscription using the intended policy.

For local sandbox testing, use the same test-mode variable names in an ignored
developer environment, keep enforcement false, and never place the private
values in `.env.example`. For Render sandbox testing, enter them only in
Render's private environment controls. The publishable key is intended for
client publication by Stripe, while the secret key and endpoint signing secret
remain server-only; this implementation does not render the publishable key.

## Sandbox acceptance tests

Start with every rollout flag false. Only after the preparation checklist is
green and sandbox acceptance is explicitly authorized, enable
`BILLING_FEATURE_ENABLED=true` and `BILLING_TIERED_PRICING_ENABLED=true` for
the isolated BILL-3 validation. Keep `BILLING_ENFORCEMENT_ENABLED=false`,
`BILLING_ONBOARDING_ENABLED=false`, and `TEAMS_FEATURE_ENABLED=false`.

Use separate test users and Stripe test payment methods. Verify:

1. Each Basic/Pro monthly/yearly selection receives the exact server-selected
   Price as its only line item, a 30-day trial, and required payment method.
   Browser-submitted Price values and mixed selections must fail before Stripe.
2. A newly redeemed founder code receives 90 days, is single-use, does not
   stack, rejects Basic, and permits either Pro billing interval.
3. Abandoning or canceling Checkout and refreshing either result page do not
   consume the trial or grant access.
4. A completed checkout remains pending until its signed subscription event is
   synchronized; duplicate/retried webhooks remain idempotent.
5. The Portal opens only for the current user's Customer, returns to the named
   billing route, updates payment method, and schedules cancellation correctly.
6. Each required event is delivered and returns success. Send an invalid
   signature to a disposable test endpoint/request and confirm it is rejected.
7. Validate `trialing`, `active`, `past_due` within and after the seven-day
   grace, `canceled` before and after period end, `incomplete`,
   `incomplete_expired`, `unpaid`, and `paused` behavior. Use Stripe test clocks
   where supported; otherwise use isolated sandbox subscriptions. Do not change
   application clocks or production records.
8. Verify Stripe's configured trial reminder/customer email behavior and that
   failed payment recovery leads to the expected synchronized state.
9. Confirm application logs and error pages contain no keys, signing headers,
   payloads, founder codes, payment details, customer IDs, subscription IDs, or
   Price IDs.
10. With Teams still off, confirm an active paid subscription resolves to Pro
    and an active founder trial to founder Pro in controlled tests. Confirm an
    invited Basic member retains Phase 2A participation rules.

Run the full Django suite with network access disabled or Stripe calls mocked.
Record counts and failures, not identifiers or payloads.

## BILL-3 annual-pricing rollout

Complete the separate [BILL-3 checklist](billing-bill3.md). Enabling
`BILLING_TIERED_PRICING_ENABLED=true` requires separate rollout authorization;
onboarding and general enforcement remain unchanged. Verify all four checkout
selections and their post-trial messaging, confirm Basic never unlocks Pro
features, confirm both Pro intervals do, and confirm an uninterrupted
subscription on an allowlisted former Price remains Pro. Confirm no legacy
Price is offered to new Checkout. Roll back by disabling only the
tiered-pricing flag; do not remove legacy Price values or reverse migrations
`0061` or `0062` during an incident.

## CXP-2B signup-onboarding rollout

After the billing sandbox checklist and email-delivery preflight pass, enable
`BILLING_ONBOARDING_ENABLED=true` in the selected environment while keeping
general enforcement unchanged. Only accounts created after the flag is enabled
join the cohort. Verify one new account follows:

`signup → verified email → Checkout → signed webhook → My Pay Plan`

Confirm an existing account without an onboarding marker keeps its existing
login flow, an alternate verified email cannot unlock Checkout, cancel/retry
does not consume a trial, and direct protected URLs redirect to the required
step. Roll back immediately by setting `BILLING_ONBOARDING_ENABLED=false`;
do not delete cohort timestamps or reverse migration `0060`.

## Enforcement rollout

Enforcement is not authorized merely because Checkout works. Before enabling
it, approve the product access policy, support process, grace-period behavior,
monitoring, rollback owner, and user communication. BILL-3 owns the Basic/Pro
entitlement split; enforcement remains a separate rollout decision.

When separately approved:

1. Keep a deployment/database rollback point and verify the signed endpoint is
   healthy.
2. Run `python manage.py check`; enforcement prerequisites must produce no
   `SalesLogApp.E002` or `SalesLogApp.E003`.
3. Set `BILLING_ENFORCEMENT_ENABLED=true` in a staged cohort/deployment.
4. Confirm login, billing overview, Checkout, Portal, password recovery, admin,
   and webhooks remain reachable for unentitled users.
5. Monitor only coarse route/status, webhook success/latency, and entitlement
   state metrics. The immediate application rollback is setting enforcement
   false; do not reverse billing or dj-stripe migrations during an incident.

## Live-mode cutover (future, separately authorized)

Do not reuse any sandbox object. In Stripe live mode:

1. Create/approve the live Products and four recurring BILL-3 Prices and
   configure the live Customer Portal after Stripe account activation and
   business verification. Confirm amount, currency, month/year interval, taxes,
   cancellation, refunds, statement descriptor, customer emails, and trial and
   full-year charge messaging.
2. Place the live publishable key, live secret key, four live Price values, and
   complete live legacy-Pro allowlist in the deployment's private variables.
   Prepare them for the approved change window without copying them to tickets.
3. With billing UI and enforcement still false, switch
   `STRIPE_LIVE_MODE=true` and all four current Price settings to their live
   values, then restart. Run system/readiness checks; an endpoint-missing result
   is expected at this intermediate point, and no billing route can make a
   session while the UI flag is false.
4. In an explicitly authorized live maintenance window, synchronize the
   existing live Products/Prices, then create a separate live dj-stripe Webhook
   Endpoint through Django admin with live mode checked, signature verification,
   and only the seven required events. Confirm its exact production UUID route
   and securely stored, distinct signing secret. Re-run readiness; test-mode
   readiness is not accepted as a substitute.
5. Perform one explicitly authorized low-risk live purchase/Portal/cancellation
   verification. Observe signed webhook synchronization without printing data.
   Reconcile the test transaction using Stripe's normal operational process.
6. Enable user-facing billing and later enforcement only under separate rollout
   approvals. Retain a rapid flag rollback and customer-support owner.

Do not delete sandbox objects during live cutover; preserve them for future
development. Never copy live Customers, Subscriptions, webhook secrets, or
events into the sandbox or repository.
