# SC-3 calendar source and customer-facing presentation

SC-3 renders the immutable SC-2 facts for the authenticated owner and supplies
the approved owner calendar source that SC-2 left abstract. It adds no new
calculation, provider, scheduling, or team behavior. SC-4 may later phrase an
allowlisted representation of the same facts.

## Calendar source

`SellingDayClosure` stores one explicit dealership closure per owner and date:

- owner-bound (`user` foreign key) with a unique `(user, date)` constraint;
- Sundays are rejected at the form, model, and database levels because the
  SC-2 contract already closes every Sunday;
- an optional label is presentation-only and never enters the engine.

`owner_selling_calendar(owner, month_start=..., month_end=...)` is the only
approved constructor. It loads the owner's in-range closures with a single
owner-scoped query and returns the immutable `StaticSellingDayCalendar` used
by SC-2. The calendar version is

```text
owner-closures.v1.<owner_id>.<sha256(closure dates)[:12]>
```

so it is deterministic while the closure set is unchanged and changes whenever
the closure set changes. Anonymous owners, foreign owner objects, and invalid
boundaries fail closed with `SellingDayCalendarError`. Closed-day sales still
count as actual records; closures only change pace denominators.

## Customer-facing presentation

The Activity & Goals page (SC-1 Pro surface) gains two cards:

- **Stew Coach month projection** — selling-day summary plus one row per
  metric in the fixed `units`, `total_gross`, `commission` order showing goal,
  actual, projected finish, remaining, required pace per day, progress, and a
  status badge.
- **Selling calendar** — the month's closures with add and remove forms.

`stew_coach_presentation.present_projection` renders display copies only:

- units round to one decimal place, money to two with a dollar sign and
  thousands separators, percentages to one decimal place (`ROUND_HALF_UP`);
- unavailable values render as an em dash;
- engine `Decimal` values are never modified — the SC-2 result is frozen;
- SC-2 diagnostic codes map to fixed customer-safe sentences with no
  customer, deal, rule, or financial detail;
- statuses map to fixed labels and badge classes; `behind` uses the new
  `status-behind` error styling.

The view computes `as_of_date` with `timezone.localdate()` at request time —
SC-2 itself never looks up the current date. Any
`SellingDayCalendarError` or `StewCoachProjectionError` fails closed to an
"unavailable" card without breaking the page.

## Authorization and isolation

- The page keeps the SC-1 `activity_goals_pro_required` and onboarding
  decorators; basic users are redirected before any closure write.
- Closure creation forces `closure.user = request.user`; posted foreign user
  ids are ignored.
- Closure deletion filters on `(pk, user=request.user)`; other owners' rows
  cannot be removed and produce a neutral error message.
- All closure reads are owner-scoped; team membership grants no access.

## Verification

`SalesLogApp.tests_stew_coach_sc3` covers the model constraints, calendar
construction (determinism, owner scoping, single query, fail-closed inputs),
presentation rounding and immutability, diagnostic translation, and the page
integration including authorization, cross-owner isolation, and fail-closed
rendering. Migration `0058_selling_day_closure` is the only schema change.
