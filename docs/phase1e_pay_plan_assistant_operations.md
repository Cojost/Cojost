# Phase 1E Pay Plan Assistant Operations

## Safety boundary

The Pay Plan Assistant is an interpretation and inactive-draft workflow. Its
authoritative sequence remains:

1. Deterministic interpretation, with an optional provider only when needed.
2. Local validation against the authenticated user's active plan.
3. A human-readable interpretation review.
4. The user's explicit **Create draft** POST.
5. An inactive `review_required` replacement version.
6. Existing replacement review and explicit activation.

The provider cannot read plan data, select database identifiers or semantic
rule keys, create a draft, activate a plan, or change commission calculations.

## Configuration reference

All provider settings are environment driven. Invalid enabled-provider
configuration remains fail-closed while deterministic assistance continues.

| Variable | Default | Valid range or meaning |
| --- | --- | --- |
| `PAY_PLAN_ASSISTANT_PROVIDER_ENABLED` | `false` | Master external-request switch |
| `PAY_PLAN_ASSISTANT_PROVIDER` | `openai` | Currently only `openai` is supported |
| `PAY_PLAN_ASSISTANT_MODEL` | `gpt-5.6-sol` | 1–100 safe model-name characters |
| `PAY_PLAN_ASSISTANT_TIMEOUT_SECONDS` | `10` | 1–60 seconds; one attempt, no retry |
| `PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT` | `0` | 0–100; `0` is the rollout kill switch |
| `PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS` | empty | Comma-separated positive internal user IDs |
| `PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT` | `20` | 1–1,000 attempts per eligible user per local day |
| `PAY_PLAN_ASSISTANT_MAX_PROVIDER_INPUT_CHARS` | `8000` | 1,000–20,000 characters |
| `PAY_PLAN_ASSISTANT_MAX_PROVIDER_RESPONSE_BYTES` | `65536` | 1,024–1,048,576 bytes |
| `PAY_PLAN_ASSISTANT_MAX_OUTPUT_TOKENS` | `600` | 64–4,000 tokens |
| `PAY_PLAN_ASSISTANT_MAX_TURNS` | `12` | Stored conversation-turn limit |
| `PAY_PLAN_ASSISTANT_CONVERSATION_TTL_HOURS` | `24` | Open-conversation lifetime |
| `PAY_PLAN_ASSISTANT_EVENT_RETENTION_DAYS` | `30` | Operational-event retention |
| `OPENAI_API_KEY` | unset | Secret-manager-provided credential |

The application recognizes these provider states:

- `disabled`: master switch is off.
- `ready`: provider, model, numeric limits, and credentials are valid. This does
  not mean a particular user is inside rollout.
- `missing_credentials`: enabled but no credential is available.
- `unsupported_provider`: the configured provider adapter does not exist.
- `invalid_configuration`: model, timeout, rollout, allowlist, or size/rate
  limits failed strict validation.

Credentials are read only by `pay_plan_intents/openai_provider.py`. Do not put
them in source, committed environment files, templates, forms, database
records, fixtures, command output, tickets, or screenshots.

## Health diagnostics

Run this after every configuration change:

```powershell
python manage.py assistant_provider_health
```

The command validates local configuration and credential presence. It does not
make an API request and always reports `paid_request_made=false`. It reports
state, provider, model, timeout, rollout percentage, allowlist count, and daily
limit but never the secret value. `python manage.py check` also emits
`SalesLogApp.W001` for an enabled provider that is not ready.

## Enabling and controlled rollout

1. Apply migration `0052_payplanassistantusageevent_and_more`.
2. Store `OPENAI_API_KEY` in the deployment platform's secret manager with
   application-runtime access only.
3. Configure provider, model, timeout, request/response bounds, output limit,
   and daily request limit.
4. Set `PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=true` while leaving rollout at `0`.
5. Restart or redeploy application processes and run the health command. Expect
   `state=ready` and `paid_request_made=false`.
6. For an allowlist pilot, set rollout above `0` (normally `100`) and put only
   pilot user IDs in `PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS`.
7. For stable percentage rollout, clear the allowlist and increase
   `PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT` gradually.
8. Verify deterministic, provider, fallback, quota, duplicate submission, and
   explicit draft-review paths before widening rollout.

Rollout decisions are a stable SHA-256 bucket of the internal user ID. When an
allowlist is nonempty, only listed users are eligible. A rollout percentage of
`0` excludes everyone even if an allowlist remains configured. Authentication,
conversation ownership, lifecycle, and active-plan checks always run before a
provider call.

Users outside rollout and existing conversations continue with deterministic
assistance. Changing rollout or the master switch affects new interpretations
after application configuration has been reloaded; it does not disable the
assistant or invalidate stored conversations.

## Rate limiting and maximum volume

Only reserved external attempts count toward quota. Deterministic successes,
unauthorized keys, stale or expired conversations, and lifecycle validation
failures do not consume it. Timeouts, refusals, unavailable responses, and
invalid outputs do count because a provider request was attempted. At the
limit, the user receives deterministic clarification.

The counter resets at midnight in Django's configured local time zone. A user
row lock protects quota reservation from concurrent requests. The configured
daily upper bound is:

```text
eligible authenticated users × PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT
```

For an allowlist, use its size as the eligible-user ceiling. For a percentage
rollout, estimate eligible users as authenticated assistant users multiplied by
the percentage, then round upward. Actual volume is lower when deterministic
interpretation succeeds.

## Request and response protections

- Deterministic interpretation always runs first.
- The provider receives only the current and at most five bounded prior
  user-authored turns from that conversation.
- Plan rules, configurations, documents, sales, customers, commissions,
  candidate selectors, and application context are not sent.
- Requests set `store: false`, use a strict JSON schema, cap output tokens, and
  include an HMAC-derived privacy-preserving safety identifier.
- Responses are byte-bounded, parsed as JSON, and revalidated against local
  allowlists. IDs and semantic selectors are rejected.
- There is at most one provider attempt per interpretation and no automatic
  retry.
- Incomplete, refused, malformed, timed-out, and unavailable results become
  generic clarification states. Provider bodies and stack traces are never
  user-visible.

## Operational events and retention

`PayPlanAssistantUsageEvent` stores only:

- Timestamp and the generic `interpretation` category.
- `deterministic` or `provider` route and a generic status.
- Duration milliseconds and bucket.
- Configured model name and a SHA-256 conversation reference.
- Authenticated user foreign key, protected by staff-only administration and
  required for rate limiting.
- Input/output token counts and a bounded provider request ID when returned.

It has no prompt, response, authorization-header, customer, sale, commission,
plan-rule, configuration, or semantic-selector fields. There is no ordinary
user endpoint for usage events. Admin access is read-only.

Statuses are `success`, `timeout`, `refusal`, `unavailable`, `invalid_output`,
`rate_limited`, `disabled`, `rollout_excluded`, and `configuration_error`.
Monitor provider attempt count, failure ratio, duration buckets, token volume,
and repeated per-user rate limiting. A newly reserved attempt initially uses
`unavailable` so an interrupted worker remains visible as a failed attempt.

Schedule this command daily:

```powershell
python manage.py purge_pay_plan_assistant_events
```

It deletes events older than `PAY_PLAN_ASSISTANT_EVENT_RETENTION_DAYS` (30 by
default). An approved one-off override is available as `--days N`.

## Support without raw conversational content

1. Confirm the user, approximate timestamp, and whether the page showed
   built-in interpretation, a fallback, or successful draft creation.
2. Filter read-only usage events by user, route, status, time, and model.
3. Correlate a provider-side support case with the bounded provider request ID,
   when present. Do not copy the user's prompt into a ticket.
4. Use the internal SHA-256 conversation reference to distinguish events. It is
   intentionally not raw conversation content.
5. Check whether the conversation is open, expired, stale, cancelled, or
   resolved and whether its active version still matches the assignment.
6. Reproduce with synthetic pay-plan wording in a non-production environment.

To determine whether a draft was actually created, verify all of these:

- The conversation is `resolved` and has `draft_change_request_id`.
- That owned `PayPlanChangeRequest` exists.
- Its `draft_version` exists in `review_required` or another valid inactive
  review state.
- The active `PayPlanAssignment` still points to the prior active version until
  separate explicit activation.

An interpretation card, assistant message, provider-success event, or
`draft_created` text alone is not authoritative evidence of a draft.

## Key rotation

1. Create a replacement key under the approved API project and apply least
   privilege and budget controls.
2. Add the replacement to the secret manager without printing it.
3. Restart or redeploy processes and run the no-request health command.
4. Complete one approved synthetic provider test inside rollout.
5. Revoke the old key at the provider.
6. Review generic failure events for unavailable regressions.

If overlap is not permitted, set rollout to `0` before rotation and restore it
only after the replacement passes health validation.

## Incident response and immediate shutoff

1. Set `PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=false` for the strongest shutoff,
   or set rollout to `0` to stop external interpretations while keeping ready
   configuration staged.
2. Reload application configuration. New requests remain on the deterministic
   path. In-flight calls cannot be recalled, but stale, expired, cancelled, or
   superseded results fail reconciliation and are not persisted.
3. Run the health command and confirm `state=disabled` for a master shutoff.
4. Preserve privacy-safe usage events; do not add prompt logging.
5. Review status, duration, model, token counts, provider request IDs, rollout
   settings, and provider status pages.
6. Rotate or revoke credentials if compromise is suspected.
7. Confirm no active plan or assignment changed.

## Deployment, rollback, and migration

Deployment order:

1. Back up the database through the normal production process.
2. Deploy code with provider disabled and apply migration `0052`.
3. Run `check`, `makemigrations --check --dry-run`, targeted Phase 1E tests,
   the full `SalesLogApp` suite, and static-file validation.
4. Validate deterministic workflow and duplicate submissions.
5. Enable provider configuration with rollout `0`, run health, then begin the
   controlled rollout steps above.

Application rollback:

1. Set the master provider switch to `false` and reload configuration first.
2. Roll back application code through the normal release mechanism.
3. Leave migration `0052` applied. Its nullable/defaulted conversation fields
   and separate usage table are safe for older application code to ignore.
4. Do not reverse `0052` until retention, audit, and downgrade compatibility
   are reviewed. Reversing it deletes operational events and idempotency links.
5. Active plans, assignments, commissions, and inactive drafts need no
   provider-specific rollback.

## Release verification record

Record environment, tester, date, build, and Pass/Fail for each item. Do not put
secrets, prompts, customer data, or sensitive screenshots in this record.

| Manual check | Expected result | Result |
| --- | --- | --- |
| Provider disabled | Built-in assistance works; zero provider attempts | Local browser pass, 2026-08-06 |
| Enabled, missing key | Health says missing credentials; safe UI fallback | Pending per deployment |
| User outside rollout | Built-in assistance and zero provider attempts | Pending per deployment |
| Eligible user, valid synthetic response | Interpretation review only; no draft | Pending per deployment |
| Timeout/unavailable | Generic fallback with rephrase, retry, and start-over paths | Pending per deployment |
| Daily limit reached | Built-in clarification; no extra provider request | Pending per deployment |
| Double-click/browser refresh | One logical turn or one draft | Browser double-click and automated replay pass, 2026-08-06 |
| Active plan changes mid-conversation | Conversation becomes stale; no result or draft persists | Automated pass; deployment manual pending |
| Desktop keyboard and screen reader | Labels, history, and all actions usable | Browser labels/actions pass; deployment screen-reader pass pending |
| Approximately 360 px | No blocked actions or unreadable content | Local browser pass with zero horizontal overflow, 2026-08-06 |
| Light/dark/system themes | Semantic notices and disabled state retain contrast | Local browser pass, 2026-08-06 |
| Print preview | No unreadable theme colors; safety state remains clear | Pending per deployment |
| Sensitive-data inspection | No key, header, body, raw prompt, or response in UI/logs/events | Automated pass; deployment log review pending |
| Explicit draft then replacement review | One inactive draft; active assignment unchanged | Automated pass; deployment manual pending |

Automated coverage lives in `tests_phase1e_production.py`; deployment-specific
manual results belong in the release record above.
