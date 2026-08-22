# BILL-2 — Basic, Pro, and grandfathered Pro pricing

> BILL-3 supersedes the current public amounts and adds yearly billing. See
> [BILL-3](billing-bill3.md). This document remains the historical rollout and
> grandfathering foundation.

BILL-2 introduces two Stripe-backed monthly plans while preserving every
uninterrupted subscription on the former single Price as Pro.

## Approved policy

- Basic: $3.99 USD per month.
- Pro: $7.99 USD per month.
- Standard introductory trial: 30 days with a payment method required.
- Existing subscribers keep Pro at their original Stripe Price while the same
  subscription remains authorized (`trialing`, `active`, eligible `past_due`
  grace, or canceled before its synchronized period end).
- Once that subscription has fully ended, a later checkout uses a current Basic
  or Pro Price. The legacy Price is never offered to a new checkout.
- Founder/Kickstarter introductory benefits remain Pro-only and cannot be
  attached to Basic.

Amounts rendered in StewLog come from locally synchronized dj-stripe `Price`
rows. The policy check requires the current Basic and Pro rows to be active,
monthly, USD, and exactly 399 and 799 cents. No browser value supplies a Price
ID or amount.

## Safe rollout contract

Deploy and migrate with `BILLING_TIERED_PRICING_ENABLED=false`. In this state,
the pre-BILL-2 `STRIPE_BASIC_MONTHLY_PRICE_ID` continues to behave as the sole
Pro Price; existing checkout and entitlement behavior is unchanged.

Before enabling BILL-2 in one Stripe mode:

1. Record the former single Price ID privately.
2. Create and synchronize the new $3.99 Basic and $7.99 Pro monthly Prices.
3. Configure:

   ```text
   STRIPE_BASIC_MONTHLY_PRICE_ID=<current Basic Price>
   STRIPE_PRO_MONTHLY_PRICE_ID=<current Pro Price>
   STRIPE_LEGACY_PRO_PRICE_IDS=<former single Price[,older approved Prices]>
   BILLING_TIERED_PRICING_ENABLED=true
   ```

4. Apply migration `0061_billingcheckoutattempt_selected_plan` before enabling
   the flag.
5. Run `python manage.py check` and
   `python manage.py billing_readiness --json`. The rollout fails closed if the
   Prices are missing, duplicated, overlap the legacy allowlist, have the wrong
   synchronized amount/mode/interval, or migration/webhook readiness is absent.

The legacy allowlist is the durable grandfathering contract. Do not remove a
Price while an uninterrupted subscriber still uses it.

## Checkout and webhook trust boundary

The browser submits only `basic` or `pro`. The server translates that tier to
one configured Price, stores both on `BillingCheckoutAttempt`, and sends that
stored Price to Checkout. Changing plans expires the prior active attempt so an
idempotency key cannot silently retain another tier.

The signed subscription webhook must match the attempt's exact Price and tier
before an introductory benefit is consumed. Mixed Basic/Pro subscriptions,
unknown Prices, owner mismatches, duplicate trials, and founder-on-Basic
attempts fail closed. Checkout success remains a status page and never grants
access.

## Entitlements and rollback

- Basic subscriptions set `subscription_access=true`, `tier=basic`, and do not
  satisfy `has_pro_access`.
- Current Pro and allowlisted legacy subscriptions satisfy Pro gates.
- Activity & Goals and Stew Coach remain Pro-gated through the central billing
  entitlement; owner isolation is unchanged.
- Set `BILLING_TIERED_PRICING_ENABLED=false` for immediate code rollback. This
  restores the original single-Price policy and does not delete subscription,
  checkout, cohort, or grandfathering data. Do not reverse migration `0061`
  during an incident.
