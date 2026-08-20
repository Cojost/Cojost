# SC-6 — Pro Trial, Pricing, and Rollout to All Current Users

SC-6 surfaces the Pro trial and pricing to all current users on top of the
dark-launched Stripe foundation. No new gating mechanism is introduced: the
existing staged flags (`BILLING_FEATURE_ENABLED`, `BILLING_ENFORCEMENT_ENABLED`)
control exposure, per the owner decision to roll out to all current users
without per-user cohorts.

## What ships

- `SalesLogApp/billing_pricing.py` — `display_price()`
  (`PRICING_VERSION = 'sc6.v1'`), the single source for the displayed
  subscription price.
- Billing overview page now renders the synchronized price instead of a
  hardcoded amount.
- Pro upgrade prompt (`templates/includes/pro_upgrade_prompt.html`) on the
  Dashboard and Profile pages for signed-in users without Pro access.
- Tests in `SalesLogApp/tests_billing_sc6.py`.

## Display pricing rules

- The price shown anywhere in the app comes from the local dj-stripe `Price`
  row identified by `STRIPE_BASIC_MONTHLY_PRICE_ID`. dj-stripe remains the
  owner of Stripe objects; rendering makes **no Stripe network calls**.
- The row must match the current mode (`livemode == STRIPE_LIVE_MODE`), be
  active, be a recurring price with `interval_count == 1`, and carry a unit
  amount and currency.
- Anything else fails closed to an "unavailable" price: templates then show
  copy that defers to Stripe Checkout for the number. A wrong or stale price
  is never displayed, and no amount is hardcoded in templates.
- Amounts render as `$X.XX USD per month` (symbol omitted for currencies
  without a mapped symbol).

## Upgrade prompt rules

The prompt renders only when all of the following hold:

1. `BILLING_FEATURE_ENABLED` or `BILLING_ENFORCEMENT_ENABLED` is true
   (billing pages are exposed).
2. The user is authenticated.
3. `activity_goals_authorized(user)` is false — Pro subscribers, founder
   grants, staff, and superusers never see the prompt.

The prompt shows the standard trial length
(`BILLING_STANDARD_TRIAL_DAYS`), the synchronized price when available, and
links to the billing overview page where the exact offer (standard vs.
founder trial, consumed introductory benefit) is computed server-side.
Any error building the prompt fails closed to hidden.

## What SC-6 does not change

- Checkout policy is untouched: `payment_method_collection='always'`
  (payment method required), trial days from the reserved
  `BillingCheckoutAttempt` (standard 30 / founder 90, introductory benefit
  consumed once), webhook-driven synchronization remains authoritative.
- Entitlement and enforcement behavior (`billing_entitlements.py`,
  middleware) is unchanged; enforcement stays behind
  `BILLING_ENFORCEMENT_ENABLED`.
- No credentials or price IDs are added to tracked files.

## Rollout checklist (production)

1. Sync the Product/Price from Stripe so the local `Price` row exists
   (dj-stripe management command or webhook-driven sync).
2. Set `STRIPE_BASIC_MONTHLY_PRICE_ID` to the live monthly Price.
3. Enable `BILLING_FEATURE_ENABLED` — billing pages, checkout, and the
   upgrade prompt appear for all current users.
4. Later, enable `BILLING_ENFORCEMENT_ENABLED` to enforce Pro gating
   (Activity & Goals redirects to billing).
