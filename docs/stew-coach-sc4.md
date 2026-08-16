# Stew Coach SC-4: phrased coaching over allowlisted facts

SC-4 adds a coach-style message to the Activity & Goals page. It phrases only
an allowlisted representation of the frozen SC-2 projection facts, using the
SC-3 presentation values (already rounded copies). SC-4 never computes new
numbers, never changes calculation, provider, scheduling, or team behavior,
and never mutates the SC-2 result.

## Deterministic foundation

`SalesLogApp/stew_coach_phrasing.py` (phrasing version `sc4.v1`):

- `coach_sentences(presentation)` builds an ordered tuple of allowlisted
  sentences from the `present_projection` context only: one period sentence,
  one or two sentences per metric (metric order is preserved:
  units → total gross → commission), then the deduplicated diagnostic
  messages. It fails closed with `StewCoachPhrasingError` when the
  presentation is unavailable, when the sentence count exceeds the provider
  fact limit, or when the sentences would not survive the provider's
  sentence-boundary round trip.
- `deterministic_coach_message(presentation)` joins those sentences. This is
  the canonical customer-visible message and the only text source.

## Bounded AI wording

`phrase_coach_message(user, presentation, submission_token=...)` reuses the
CX-3 Ask Stew gateway unchanged (`configured_ask_stew_gateway`). That gateway
already enforces:

- pilot entitlement (`ask_stew_ai_authorized`) — customer access defaults to
  denied;
- bounded provider configuration (`PAY_PLAN_ASSISTANT_*` settings);
- the atomic daily quota and signed submission idempotency;
- the fact-selection contract: the provider receives only opaque request-local
  fact IDs for the server-owned sentences and may only return every ID exactly
  once. All customer text remains exact server-owned deterministic text; the
  provider can never generate, alter, reorder, or omit wording.

Every provider failure, refusal, quota denial, or invalid output returns the
deterministic message with an explanatory notice.

## Page behavior

- GET renders the deterministic coach note ("Stew Coach says") whenever a
  verified projection is available. The provider is never called on GET.
- A "Refresh wording with AI" form is rendered only when the provider is
  configured and the signed-in user is pilot-authorized. It posts
  `form_type=coach_phrase` with a signed, owner-salted, one-hour submission
  token.
- The POST renders the result directly (no redirect). Expired or foreign
  tokens and repeated submissions keep the verified text, add a notice, and
  never reach the provider. The session remembers the last processed token,
  mirroring the Ask Stew AI page.
- When no verified projection exists, the SC-3 fail-closed card is shown and
  SC-4 renders nothing.

## Invariants

- Sentences are built only from SC-3 presented strings; unavailable values
  (`—`) are never phrased as numbers, and metrics without goals expose no
  actuals.
- Sales on closed days still count as actuals; closures affect only pace
  denominators (unchanged from SC-2/SC-3).
- All data loading remains owner-scoped; team membership grants no access.

Tests: `SalesLogApp/tests_stew_coach_sc4.py`.
