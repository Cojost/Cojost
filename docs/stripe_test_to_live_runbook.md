# Stripe sandbox-to-live runbook

This is an operator checklist, not an instruction to copy secrets into the
repository. Stripe sandbox and live mode contain separate Products, Prices,
Customers, Portal configuration, endpoints, and signing secrets. Complete and
record each mode independently. Never make live-mode requests during sandbox
validation.

## Current handoff state

The application foundation and mocked automated tests are implemented. The
`STEW Log Development` sandbox, its recurring monthly test Price, private test
credentials, and sandbox Customer Portal were prepared manually. No webhook
has been created. No Stripe API request or dashboard change was made by this
implementation work. Billing feature, enforcement, and Teams flags remain
false.

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

1. Deploy the code with all three flags false.
2. Back up the target database, confirm the one-time bridge above is complete,
   then apply migrations:

   ```powershell
   python manage.py migrate --plan
   python manage.py migrate
   python manage.py showmigrations djstripe SalesLogApp
   ```

   Confirm `djstripe.0003_2_11` and
   `SalesLogApp.0054_billing_foundation` are applied.
3. Configure the private environment with `STRIPE_LIVE_MODE=false`, the sandbox
   publishable/secret keys, and that sandbox's recurring Price ID. Keep the live
   variables empty or private placeholders in the deployment system. Keep both
   billing flags false.
4. Restart and run `python manage.py check` and
   `python manage.py billing_readiness --json`. Inspect boolean readiness only;
   do not paste the environment or command internals into a ticket.
5. In an explicitly authorized sandbox maintenance window, synchronize only the
   existing Product and Price into dj-stripe:

   ```powershell
   python manage.py djstripe_sync_models Product Price
   ```

   This command makes sandbox Stripe API reads. Verify the local selected Price
   is recurring, USD, and $1.99 without copying its identifier into a log or
   ticket. It does not create a Product or Price.
6. In Django admin, add a dj-stripe Webhook Endpoint with:

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
7. Re-run readiness. Confirm route, endpoint, signing secret, signature
   verification, configuration, and both required migrations report ready.
   Verify the sandbox Customer Portal can update a payment method and cancel a
   subscription using the intended policy.

For local sandbox testing, use the same test-mode variable names in an ignored
developer environment, keep enforcement false, and never place the private
values in `.env.example`. For Render sandbox testing, enter them only in
Render's private environment controls. The publishable key is intended for
client publication by Stripe, while the secret key and endpoint signing secret
remain server-only; this implementation does not render the publishable key.

## Sandbox acceptance tests

Enable `BILLING_FEATURE_ENABLED=true` only after the preparation checklist is
green. Keep `BILLING_ENFORCEMENT_ENABLED=false` and
`TEAMS_FEATURE_ENABLED=false`.

Use separate test users and Stripe test payment methods. Verify:

1. A standard user receives a 30-day subscription trial, Checkout requires a
   payment method, and the configured test Price is the only line item.
2. A newly redeemed founder code receives 90 days, is single-use, and does not
   stack with another introductory benefit.
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

## Enforcement rollout

Enforcement is not authorized merely because Checkout works. Before enabling
it, approve the product access policy, support process, grace-period behavior,
monitoring, rollback owner, and user communication. The present release does
not implement a broader Basic/Pro feature split.

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

1. Create/approve the live Product and recurring monthly Price and configure the
   live Customer Portal after Stripe account activation and business
   verification. Confirm price, currency, interval, taxes, cancellation,
   refunds, statement descriptor, customer emails, and trial messaging.
2. Place the live publishable key and live secret key in the deployment's
   private variables. Prepare the live Price ID for the approved change window.
3. With billing UI and enforcement still false, switch
   `STRIPE_LIVE_MODE=true` and the selected Price setting to the live Price,
   then restart. Run system/readiness checks; an endpoint-missing result is
   expected at this intermediate point, and no billing route can make a session
   while the UI flag is false.
4. In an explicitly authorized live maintenance window, synchronize the
   existing live Product/Price, then create a separate live dj-stripe Webhook
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
