# SC-2 deterministic pace and projection engine

SC-2 produces read-only, server-controlled facts for a single authenticated
owner. It has no view, template, URL, provider, persistence, or scheduling
behavior. SC-3 may later render these facts, and SC-4 may phrase only an
allowlisted representation of them.

## Public API

```python
StewCoachProjectionService.calculate(
    owner=request.user,
    month_start=selected_month,
    as_of_date=explicit_local_date,
    calendar=owner_calendar,
)
```

The service normalizes `month_start` to day one. `as_of_date` and an
owner-aware `SellingDayCalendar` are mandatory; the service never looks up the
current date. The owner must pass the same read-only Pro entitlement decision
used by SC-1. Staff and superusers retain SC-1 behavior. Team membership is not
an ownership source.

Results are immutable dataclasses. Metrics are always ordered as `units`,
`total_gross`, then `commission`. The calculation version is `sc2.v1` and the
projection method is `linear_completed_day_rate_open_day`.

## Calendar contract

A calendar returns an immutable set of explicit closure dates for the supplied
owner and month, plus a validated version. Monday through Saturday are
potential selling days, Sunday is always closed, and configured closures are
removed. No federal or other holiday list is assumed.

`StaticSellingDayCalendar` is the immutable owner-bound implementation for
tests and trusted internal callers. Invalid versions, mutable or non-date
closure output, missing calendars, and owner mismatches fail closed. SC-3 is
responsible for constructing a calendar from the approved owner or dealership
source.

Sales recorded on a Sunday or closure remain actual business records. Calendar
closures affect only pace denominators.

## Open-day calculation

For an in-progress month:

- actuals include records dated through `as_of_date`;
- completed selling days are strictly before `as_of_date`;
- the as-of selling day remains in remaining selling days;
- future selling days are strictly after `as_of_date`;
- actual-through-prior-day excludes the as-of date.

With at least one completed selling day:

```text
projected_total =
    actual
    + (actual_through_prior_day / completed_selling_days * future_selling_days)
```

This incorporates production already recorded today exactly once. It neither
treats today as completed nor invents additional production for today's open
portion.

For a positive goal:

```text
remaining = max(goal - actual, 0)
progress_percent = actual / goal * 100

required_pace = 0                         when remaining is zero
required_pace = unavailable               when no selling days remain
required_pace = remaining / remaining_days otherwise
```

All arithmetic retains full `Decimal` precision. SC-3 must apply presentation
rounding without changing these values.

Future months return zero actuals, no projection, zero completed days, and all
selling days remaining. A positive goal can still have a required pace.
Completed months include the full month and set projection equal to actual. If
the calendar contains no selling days, projection and pace are unavailable.

Metric status precedence is `no_goal`, `insufficient_data`, `goal_reached`,
`on_pace`, then `behind`. On pace uses exact projected-total comparison without
a tolerance.

## Authoritative actuals

- Units use the existing sale credit (`0.5`, `1.0`, or `2.0`).
- Total gross is recorded front-end plus back-end gross and is not multiplied
  by sale count.
- Earned commission comes from the existing commission reporting service.
  SC-2 does not copy pay-plan rules or synthesize future sales.

The committed archive aggregation policy supplies the bounded owner records,
suppresses proven live/archive identity overlap, and decides whether historical
commission can be verified. When archive commission is unavailable, commission
actual, prior actual, projection, remaining, progress, and pace are unavailable;
its status is `insufficient_data`. Units and gross remain usable.

Only these diagnostic codes can be returned:

- `archive_snapshot_unavailable`
- `historical_pay_plan_incomplete`
- `commission_unavailable`
- `future_period`
- `no_completed_selling_days`
- `no_selling_days`

No customer, deal, raw rule, or financial details are logged or included in
diagnostics.

## Read and query behavior

The service performs no writes and does not use pay-plan onboarding
synchronization. It queries the owner goal and loads the bounded live and
archived month once. Units and gross for inclusive and prior-day cutoffs are
calculated in one pass.

Commission is calculated once when both cutoffs contain the same records. When
today adds records, two authoritative calculations are necessary: one for the
prior-day rate and one for inclusive actuals. No calculation is run against
hypothetical records. The SC-2 owner-data query count is constant as sale count
grows; any existing commission-engine per-sale query behavior is inherited and
not expanded by SC-2.
