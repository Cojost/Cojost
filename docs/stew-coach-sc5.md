# Stew Coach SC-5 — Proactive In-App Nudges

SC-5 adds proactive, dismissible in-app nudges derived from the same verified
Stew Coach facts used by SC-2/SC-3/SC-4. Nudges are in-app only (no email or
push), Pro-gated, owner-scoped, and fail closed.

## What ships

- `SalesLogApp/stew_coach_nudges.py` — deterministic nudge builder
  (`NUDGES_VERSION = 'sc5.v1'`).
- `SalesLogApp/models/nudges.py` — `StewCoachNudgeDismissal` model
  (migration `0059_stewcoachnudgedismissal`).
- Nudge banners on the Activity & Goals page and the Dashboard
  (`templates/includes/stew_nudges.html`).
- Dismiss endpoint `POST /SalesLogApp/stew-nudges/dismiss/`
  (`dismiss_stew_nudge`).
- Tests in `SalesLogApp/tests_stew_coach_sc5.py`.

## Nudge catalog

At most `MAX_VISIBLE_NUDGES = 2` nudges show at once, in this order:

| Key | Level | Condition |
| --- | --- | --- |
| `month_end_push` | warning | Any metric behind pace and `0 < remaining selling days <= 5`. Suppresses `behind_pace`. |
| `behind_pace` | warning | Any metric behind pace and more than 5 selling days remain. |
| `set_goals` | info | Every metric row has status `no_goal`. |
| `log_activity` | info | No `DailyActivity` rows exist for the presented month. |

Messages are fixed templates over allowlisted presentation values (month
label, remaining selling days, metric labels). The builder never computes new
numbers and never calls a provider — SC-5 is fully deterministic.

## Guardrails

- **In-progress months only.** Nudges require an available presentation with
  `period_status == 'in_progress'`. Completed months, future months, and any
  unavailable projection produce no nudges.
- **Fail closed.** Any error while building nudges results in no nudges; the
  page renders normally (`_stew_nudges_context` logs the error type only).
- **Owner-scoped.** Activity detection and dismissals are filtered by
  `user=owner`. Another user's activity or dismissals never affect what an
  owner sees. Team membership grants no access.
- **Pro-gated.** Banners render only where the Stew Coach context exists:
  Activity & Goals (already Pro-gated) and the Dashboard only when
  `activity_goals_authorized(user)` is true. Staff/superusers keep access for
  support.
- **No engine changes.** Nudges read the SC-3 presentation (rounded copies);
  engine Decimals and projection behavior are untouched.

## Dismissals

- One dismissal hides one nudge key for one owner for one month
  (`unique (user, nudge_key, month_start)`).
- The model validates `month_start` is the first of a month and the key is in
  the allowlist; `save()` runs `full_clean()`.
- The endpoint is `login_required`, POST-only, validates the key against
  `NUDGE_KEYS`, is idempotent (`get_or_create`), and redirects back to the
  originating page (`next` is validated against a fixed set — never a raw
  URL).
- A new month surfaces nudges again; dismissals do not carry over.

## Extending the catalog

1. Add the key to `NUDGE_KEY_CHOICES` in `SalesLogApp/models/nudges.py`.
2. Add the condition and fixed message template in
   `candidate_nudges` (`SalesLogApp/stew_coach_nudges.py`), keeping the
   priority order explicit.
3. Add builder, dismissal, and view tests in
   `SalesLogApp/tests_stew_coach_sc5.py`.

Keep messages deterministic and derived only from presentation values; do not
introduce provider-generated text into nudges.
