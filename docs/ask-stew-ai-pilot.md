# Ask Stew AI-1A internal lab and customer pilot

AI-1A turns Ask Stew from a one-question explainer into a short, monitored,
read-only conversation. It remains separate from Stripe and subscription
enforcement while the product value and safety gates are being evaluated.

## Default access

AI-1A defaults to an in-house lab:

```text
ASK_STEW_AI_LAB_ONLY=true
ASK_STEW_AI_PILOT_USER_IDS=
```

With lab-only mode enabled, only authenticated staff and superusers can open
Ask Stew. Customer IDs in the allowlist are intentionally ignored. This makes
it safe to configure the future pilot cohort before customer access is opened.

After the internal acceptance gates pass, set lab-only mode to `false`, add
immutable Django user IDs, and restart the application:

```text
ASK_STEW_AI_LAB_ONLY=false
ASK_STEW_AI_PILOT_USER_IDS=42,108
```

Remove an ID to withdraw one customer's access. Set the list to an empty value
to deny every non-staff user. The allowlist controls Ask Stew only; it does not
grant access to the legacy pay-plan-change assistant, Commission Sandbox,
scenarios, Teams, billing, or any other Pro capability.

## Customer-facing workflow

The AI-1A page adds the usability needed for an initial paid-product test:

- natural-language starter questions and prompts based on the user's recent
  recorded deal numbers;
- short follow-up conversations that preserve only the prior supported intent
  and previous user question needed for continuity;
- a visible “Verified by StewLog” source label on calculated answers;
- a one-click helpful/not-helpful control on every answer;
- explicit read-only and no-projection language;
- graceful deterministic fallback whenever provider routing is disabled,
  limited, invalid, or unavailable.

## Routing and calculation boundary

1. StewLog normalizes and scans every question first. Requests for writes,
   projections, private data, system information, or another user are declined
   before any provider call.
2. Exact supported questions take the existing deterministic route.
3. An otherwise safe natural-language question may make one bounded Responses
   API call. The provider receives only the current question and the prior
   supported intent. It does not receive pay-plan facts, financial values,
   account IDs, deal IDs, email addresses, files, or tool access.
4. The provider may return only an allowlisted intent and confidence value via
   a strict JSON schema. Medium/low confidence fails closed.
5. StewLog executes the selected intent against owner-scoped data and produces
   the answer with the existing authoritative calculator. Provider output can
   never supply or modify a commission value.

The implementation follows OpenAI's guidance that the application—not the
model—executes an allowed action, and uses strict Structured Outputs to constrain
the routing shape. Strict schemas constrain shape, not business correctness, so
the deterministic calculator remains authoritative:

- <https://developers.openai.com/api/docs/guides/function-calling>
- <https://developers.openai.com/api/docs/guides/structured-outputs>

## Enable the AI connection

The deterministic starter questions work without an external provider. To test
natural-language routing, configure the shared provider values and inject the
API key through the deployment secret store (never commit it):

```text
PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=true
PAY_PLAN_ASSISTANT_PROVIDER=openai
PAY_PLAN_ASSISTANT_MODEL=gpt-5.6-sol
OPENAI_API_KEY=<deployment secret>
```

Keep the existing timeout, daily quota, input, response, and output limits at
their bounded defaults for the first test. Run `python manage.py
assistant_provider_health` before inviting testers. Ask Stew access is governed
by `ASK_STEW_AI_LAB_ONLY` and `ASK_STEW_AI_PILOT_USER_IDS`; the legacy
`PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT` and
`PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS` continue to govern the separate
pay-plan-change assistant.

## Render deployment procedure

The existing `build.sh` already runs `python manage.py migrate`, so migration
`0064_ask_stew_ai1a_lab` is applied automatically before the new web process
starts. During the first deployment, confirm the Render build log contains:

```text
Applying SalesLogApp.0064_ask_stew_ai1a_lab... OK
```

Keep customer access locked while adding these values to the existing Render
web service:

```text
ASK_STEW_AI_LAB_ONLY=true
ASK_STEW_AI_PILOT_USER_IDS=
PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=true
PAY_PLAN_ASSISTANT_PROVIDER=openai
PAY_PLAN_ASSISTANT_MODEL=gpt-5.6-sol
OPENAI_API_KEY=<project-specific Render secret>
```

Retain the bounded defaults for timeout, daily quota, provider input, response,
output, conversation TTL, short-window throttle, and retention. Do not place the
API key in source, a GitHub variable intended for client code, deploy logs, or a
support message.

After the deploy, run this no-request check in the Render Shell:

```bash
python manage.py ask_stew_ai1a_readiness --json --require-ready
```

Expected safety-critical results are:

```text
"migration_0064_applied": true
"ai_routing_configuration_ready": true
"lab_only": true
"customer_access_blocked": true
"internal_lab_ready": true
"paid_request_made": false
```

This proves local configuration and database readiness only. A superuser must
then submit one natural-language question in the internal lab to prove that the
project key can reach the configured model.

### Scheduled retention on Render

Create one Render Cron Job from the same `Cojost/Cojost` repository and `main`
branch:

| Render field | Value |
| --- | --- |
| Name | `stewlog-ask-stew-retention` |
| Runtime | Python |
| Build command | `pip install -r requirements.txt` |
| Command | `python manage.py purge_ask_stew_conversations` |
| Schedule | `17 8 * * *` |

Render cron schedules use UTC, so this runs shortly after 3:00 AM Central
during daylight time and shortly after 2:00 AM Central during standard time.
Render currently applies a $1 minimum monthly charge per cron-job service.
Attach the production `DATABASE_URL` and the Django settings required to start
the project, preferably through the same protected Render environment group.
The cleanup command does not need the OpenAI API key. Trigger the job manually
once after creation and confirm its log contains `deleted_conversations=0` or a
nonnegative count.

If provider configuration causes a problem, set
`PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=false`; deterministic Ask Stew answers
remain available. Keep `ASK_STEW_AI_LAB_ONLY=true` throughout the internal
pilot and as the immediate customer-access rollback switch.

## Operational controls

| Setting | Default | Purpose |
| --- | ---: | --- |
| `ASK_STEW_AI_LAB_ONLY` | `true` | Keeps all customer access closed |
| `ASK_STEW_AI_MAX_TURNS` | `12` | Maximum stored user/assistant messages per thread |
| `ASK_STEW_AI_CONVERSATION_TTL_HOURS` | `24` | Time after which a thread cannot be resumed |
| `ASK_STEW_AI_SHORT_WINDOW_SECONDS` | `60` | Rolling throttle window |
| `ASK_STEW_AI_SHORT_WINDOW_LIMIT` | `6` | Questions allowed per user in that window |
| `ASK_STEW_AI_CONVERSATION_RETENTION_DAYS` | `30` | Stored transcript/feedback retention |

Provider routing also reuses the bounded `PAY_PLAN_ASSISTANT_*` provider
configuration, signed submission idempotency, atomic daily quota, response-size
limit, timeout, and privacy-safe provider identifier.

Run transcript cleanup on the normal scheduled-job cadence:

```bash
python manage.py purge_ask_stew_conversations
```

Use `--days N` for an explicit one-off retention window. Values below one are
rejected.

## Superuser monitoring

Superusers can open `/SalesLogApp/commission/ask-stew/lab/` from the Ask Stew
page. The seven-day view shows:

- conversation and answer volume;
- verified-answer and provider-route rates;
- explicit helpful feedback rate;
- average end-to-end response time;
- up to 50 recent question/answer pairs with intent, route, provider status,
  verification state, latency, and feedback.

The Django admin also exposes read-only conversation threads and feedback.
Transcripts can contain sensitive user-entered text, so monitoring access stays
limited to approved superusers and retention cleanup must remain scheduled.

## Internal test script

Ask each superuser to complete the same core tasks in their own account:

1. Ask naturally how their active pay plan works.
2. Ask why one recorded deal paid its displayed amount.
3. Ask what they have earned this month.
4. Ask how close they are to the next bonus.
5. Ask what eligibility information is missing.
6. Ask one short follow-up such as “Why?” or “What about that deal?”
7. Try one write request, one hypothetical, one other-user request, and one
   unrelated request; all must be safely declined.
8. Rate every answer and report whether it saved time compared with finding the
   same answer manually.

Test mobile and desktop layouts and include accounts with no sales, no bonus
tiers, missing eligibility answers, half deals, double deals, and multiple users
with overlapping deal numbers.

## Customer-pilot acceptance gates

Do not change `ASK_STEW_AI_LAB_ONLY` until all of these are true for the review
window:

- no cross-user disclosure and no pay-plan, sale, eligibility, sandbox, or
  scenario mutation;
- every sampled numeric answer matches StewLog's deterministic result;
- every negative rating has been reviewed and assigned a cause;
- at least 80% helpful feedback across at least 20 rated answers;
- at least 90% successful intent routing for clearly in-scope natural-language
  questions in the scripted set;
- all scripted write, projection, privacy, and unrelated requests are declined;
- no unhandled errors or duplicate provider calls on browser retries;
- response time is acceptable on the actual production-sized accounts in the
  pilot.

For the first customer cohort, enable only a few known users, review the lab
daily, and keep a one-step rollback: restore `ASK_STEW_AI_LAB_ONLY=true` and
restart. Expand only after the same gates continue to hold with customer usage.
