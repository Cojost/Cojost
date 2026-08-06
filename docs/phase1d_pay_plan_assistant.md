# Phase 1D Pay Plan Assistant

> Phase 1E production configuration, rollout, rate limiting, observability,
> incident response, and rollback guidance supersedes the operational portions
> of this document. See
> [Phase 1E Pay Plan Assistant Operations](phase1e_pay_plan_assistant_operations.md).

## Purpose

Phase 1D adds an authenticated, multi-turn conversation around the existing
Pay Plan Intent Driver. It does not replace commission calculation, local rule
resolution, inactive-draft review, replacement review, or activation.

The safety sequence remains:

1. Conversation text
2. Validated `PayPlanIntent`
3. Authenticated local rule resolution
4. Human-readable interpretation review
5. Explicit **Create draft** action
6. Inactive draft
7. Replacement review
8. Explicit activation through `PayPlanActivationService`

No interpretation or clarification operation edits an active plan.

## Architecture and request flow

- `DeterministicIntentInterpreter` always runs first.
- `ProviderNeutralInterpreter` stops after deterministic interpretation when a
  target is recognized.
- If no target is recognized and the optional provider is enabled and fully
  configured, `OpenAIIntentProvider` may make one bounded Responses API call.
- Provider output passes through `validate_provider_output()` before it can
  become a `PayPlanIntent`.
- `PayPlanConversationService` stores user and assistant turns, persists only
  allowlisted semantic pending-intent data, and re-runs local resolution against
  the authenticated user's current active plan.
- `create_draft_from_intent()` remains the first pay-plan mutation boundary.
- Existing replacement review and activation services remain authoritative.

Provider-specific HTTP and response parsing are isolated in
`pay_plan_intents/openai_provider.py`. Django views do not build API payloads or
handle provider credentials.

## Official OpenAI guidance

The implementation was checked against the official OpenAI documentation:

- Responses API migration and request guidance: https://developers.openai.com/api/docs/guides/migrate-to-responses
- Structured Outputs: https://developers.openai.com/api/docs/guides/structured-outputs
- GPT-5.6 Sol model: https://developers.openai.com/api/docs/models/gpt-5.6-sol
- Model guidance: https://developers.openai.com/api/docs/guides/latest-model
- Error codes: https://developers.openai.com/api/docs/guides/error-codes
- Production best practices: https://developers.openai.com/api/docs/guides/production-best-practices

The adapter uses `POST /v1/responses`, strict JSON Schema through
`text.format`, `store: false`, explicit refusal detection, one bounded request,
and a configurable model. The default model is `gpt-5.6-sol`, which was the
current official flagship model when Phase 1D was implemented.

## Configuration

All settings are environment driven:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAY_PLAN_ASSISTANT_PROVIDER_ENABLED` | `false` | Opt in to external interpretation |
| `PAY_PLAN_ASSISTANT_PROVIDER` | `openai` | Select the optional provider |
| `PAY_PLAN_ASSISTANT_MODEL` | `gpt-5.6-sol` | Responses API model |
| `PAY_PLAN_ASSISTANT_TIMEOUT_SECONDS` | `10` | Single-request timeout |
| `PAY_PLAN_ASSISTANT_MAX_TURNS` | `12` | Maximum stored turns per conversation |
| `PAY_PLAN_ASSISTANT_CONVERSATION_TTL_HOURS` | `24` | Open-conversation lifetime |
| `OPENAI_API_KEY` | unset | OpenAI API credential |

The application starts and the assistant works deterministically when the
provider is disabled or the key is absent. Invalid numeric settings fall back
to safe defaults rather than preventing startup.

### Enabling the provider

1. Store the API key in the deployment platform's secret manager.
2. Expose it to the application as `OPENAI_API_KEY`.
3. Set `PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=true`.
4. Confirm `PAY_PLAN_ASSISTANT_PROVIDER=openai` and choose the configured model.
5. Deploy, then verify provider-unavailable fallback before testing a provider
   request in a non-production environment.

Never put the key in source files, templates, test fixtures, logs, database
records, or support screenshots.

## Provider privacy boundary

The provider request contains:

- Fixed interpretation instructions.
- The configured model and strict output schema.
- `store: false`, the output limit, and reasoning configuration.
- The current authenticated user's current plain-language request.
- Only that same conversation's prior **user-authored text**, bounded by the
  configured conversation turn limit.

The provider request does not contain:

- User IDs, usernames, or email addresses from application context.
- Plan IDs, version IDs, rule IDs, semantic keys, or candidate selectors.
- Pay-plan rules, configurations, source documents, or uploaded files.
- Sales, customers, deals, commission results, or previews.
- Assistant messages containing locally discovered rule names.
- Raw database or arbitrary application context.

Local user text is sent exactly as entered when the provider is used. Users
should not put unrelated personal or customer information in a pay-plan change
request.

## Structured-output and failure behavior

The JSON schema contains only existing semantic intent fields. It rejects
additional properties. Server validation independently rejects unknown fields,
database IDs, selectors, arbitrary nested configuration, unsupported actions,
targets, scopes, conditions, and condition operators. Low-confidence output is
converted to clarification and cannot create a draft.

The adapter makes no automatic retries. Timeouts, connection failures,
authentication failures, rate limits, incomplete output, refusals, malformed
JSON, and schema failures become generic deterministic clarification states.
Raw request text, raw provider responses, credentials, and provider error bodies
are not logged or shown to users.

The implementation uses Python's standard-library HTTP client. No OpenAI SDK
dependency was added because the integration is one narrow JSON endpoint and an
injectable client keeps automated tests fully offline.

## Conversation lifecycle and isolation

Conversation states are:

- `open`: may accept a follow-up or explicit draft confirmation.
- `resolved`: a confirmed inactive draft was created.
- `cancelled`: the user cancelled; pending intent is cleared.
- `expired`: the configured TTL elapsed; pending intent is cleared.
- `stale`: the user's active plan changed; pending intent is cleared.

Every read and write filters by both conversation key and authenticated user.
The conversation stores the active user-owned plan version at creation. Resume,
follow-up, and draft confirmation compare that version with the current active
version. A mismatch marks the conversation stale.

Turn creation occurs inside transactions while the parent conversation row is
locked. The next sequence is assigned from the current maximum, and the
database uniqueness constraint on `(conversation, sequence)` is the final
concurrency backstop.

**Cancel** closes the current conversation. **Start over** closes an open prior
conversation and creates a separate, empty, user-owned conversation. Open
conversations can be resumed from the assistant page.

## Clarification and local resolution

Each assistant response asks for one next detail. Follow-up interpretation uses
the prior user-authored conversation text and merges already validated semantic
fields. Candidate rule discovery always runs locally against the authenticated
active plan. The browser submits only a candidate position; the server maps it
to the freshly resolved local candidate and revalidates it before review and
again before draft creation.

The workflow never silently changes a `change` request into an `add` request.
Unsupported targets and actions remain unsupported and create no draft.

## Draft and activation safeguards

Draft creation requires all of the following:

- The conversation belongs to the authenticated user.
- The conversation is still open, unexpired, and current for the active plan.
- The pending intent passes the strict persistence schema.
- Authenticated local resolution produces a proposal.
- The user explicitly submits **Create draft**.
- The source version and current value still match interpretation review.

The resulting version is `review_required`; the active version and active
assignment remain unchanged. Preview calculations use the existing commission
services. Activation remains a separate explicit action in replacement review.

## Testing

Provider tests use an injected fake JSON client and never access the network.
They inspect the exact Responses API payload and cover disabled configuration,
deterministic-first routing, structured output, privacy, low confidence,
timeouts, provider failures, refusals, malformed responses, and schema
rejection.

Conversation tests cover ownership, cross-user denial, ordered unique turns,
turn limits, TTL, cancel, start over, resolve, stale active plans, semantic-only
pending data, no writes during clarification/review, explicit inactive-draft
creation, accessible history, form-value preservation, and provider-disabled
fallback.

## Deployment checklist

- Apply migration `0051_pay_plan_conversation_lifecycle`.
- Deploy with provider calls disabled first.
- Confirm no API key is present in the repository or logs.
- Run `python manage.py check` and the full `SalesLogApp` suite.
- Verify conversation start, follow-up, resume, cancel, start over, review,
  explicit draft creation, and unchanged active plan.
- Verify light, dark, and system themes at desktop and approximately 360 px.
- If enabling OpenAI, set the key through secret management, verify the model is
  available to the API project, and monitor generic provider-unavailable rates.
- Do not enable provider calls until data-handling and cost approval is complete.

## Rollback

1. Set `PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=false` to stop external calls
   immediately without disabling deterministic assistance.
2. Deploy the previous application version if the conversational UI must be
   rolled back.
3. Leave conversation records and migration `0051` in place during application
   rollback; the added status values are backward compatible with the existing
   table shape.
4. Do not reverse the migration while any conversation uses `expired` or `stale`
   unless those records are first handled through an approved data migration.
5. Existing active plans, assignments, drafts, and commission calculations are
   independent of provider configuration and require no rollback action.
