# BILL-3 — annual billing and revised StewLog pricing

BILL-3 adds monthly and yearly billing to the server-owned Basic/Pro policy.
It preserves uninterrupted subscribers as Pro on their original Stripe Price
and does not authorize any billing rollout flag to be enabled.

## Approved public policy

- Basic monthly: $4.99 USD, recurring every month.
- Basic yearly: $49.00 USD, recurring every year.
- Pro monthly: $9.99 USD, recurring every month.
- Pro yearly: $99.00 USD, recurring every year.
- Standard introductory trial: 30 days on Basic Monthly only.
- Basic Yearly and standard Pro Monthly/Yearly start without a trial.
- Founder/Kickstarter benefit: the existing 90-day trial, Pro-only, with a
  choice of monthly or yearly billing.
- Checkout always requires a payment method. The selected recurring amount is
  charged automatically after an eligible trial unless the customer cancels.
- Yearly messaging states both the synchronized yearly total and its computed
  monthly equivalent, and makes clear that the full yearly total is charged
  when a standard customer subscribes or after an eligible Founder trial.

The internal Django superuser retains Team-management access without creating
a circular live Stripe subscription. This narrow operational exception does
not apply to staff accounts or customers and does not change billing status.

Templates do not contain monetary values. Display values come from locally
synchronized dj-stripe `Price` rows, and the yearly monthly-equivalent display
is calculated from the synchronized yearly total.

## Private environment contract

Configure these values only in the selected deployment environment:

```text
STRIPE_BASIC_MONTHLY_PRICE_ID=<private selected-mode value>
STRIPE_BASIC_YEARLY_PRICE_ID=<private selected-mode value>
STRIPE_PRO_MONTHLY_PRICE_ID=<private selected-mode value>
STRIPE_PRO_YEARLY_PRICE_ID=<private selected-mode value>
STRIPE_LEGACY_PRO_PRICE_IDS=<private comma-separated grandfathered values>
```

All four current values must be syntactically valid and different, and none
may appear in `STRIPE_LEGACY_PRO_PRICE_IDS`. The legacy allowlist remains the
durable grandfathering contract and must retain every Price used by an
uninterrupted subscriber.

Deploy and migrate with all of these flags false:

```text
BILLING_FEATURE_ENABLED=false
BILLING_ENFORCEMENT_ENABLED=false
BILLING_ONBOARDING_ENABLED=false
BILLING_TIERED_PRICING_ENABLED=false
```

Migration `0062_billingcheckoutattempt_selected_billing_interval` must be
applied before any separately approved BILL-3 rollout.

## Local synchronized-Price validation

When tiered pricing is enabled in a controlled environment, readiness requires
all four configured rows to be present in the local dj-stripe database. Each
row must be active, in the selected Stripe mode, recurring, USD, have an
interval count of one, and exactly match its approved amount and month/year
interval. Checks read the local database only; they do not call Stripe.

Run:

```powershell
python manage.py check
python manage.py billing_readiness --json
```

The readiness output reports only booleans and non-secret failure categories.
It must never print credentials, Price values, webhook secrets, customer
values, or subscription values. Its `tiered_pricing_ready` preflight validates
the configured candidate Prices even while `BILLING_TIERED_PRICING_ENABLED`
remains false.

## Checkout and webhook trust boundary

The browser may submit exactly one allowlisted `tier` (`basic` or `pro`) and
one allowlisted `billing_interval` (`month` or `year`). It cannot submit a
Price. Extra selection fields, duplicate/mixed values, missing values, and
unsupported choices fail before customer creation or Checkout.

The server resolves the configured Price, stores the exact Price plus tier and
interval on `BillingCheckoutAttempt`, and sends that stored Price to Stripe.
Changing tier or interval expires an incompatible active attempt so an older
idempotency key cannot retain another selection. Checkout continues to use
`payment_method_collection="always"` and passes the attempt's 30- or 90-day
trial through `subscription_data.trial_period_days`. Checkout omits that field
for Basic Yearly and standard Pro Monthly/Yearly so those selections charge
when the customer subscribes.

A synchronized subscription confirms an attempt only when ownership, exact
Price, tier, and billing interval match. Unknown, mixed, duplicated, and
multi-item subscriptions fail closed.

## Entitlements, grandfathering, and rollback

- Either current Basic Price grants Basic subscription access.
- Either current Pro Price grants Pro subscription access.
- Billing interval never changes the entitlement tier.
- One allowlisted legacy Price remains grandfathered Pro while the subscription
  remains uninterrupted under the existing status and authorized-period rules.
- A legacy Price is never offered in new Checkout.

For immediate code rollback, set `BILLING_TIERED_PRICING_ENABLED=false`. The
original `STRIPE_BASIC_MONTHLY_PRICE_ID` single-Price policy continues to grant
Pro exactly as it did before BILL-2. Do not remove legacy values, delete
checkout attempts, or reverse migrations `0061` or `0062` during an incident.
