# Production domain and external-provider audit

Audit date: 2026-08-09

This document describes repository behavior. Render, DNS, Resend, Stripe,
OpenAI, Google, Apple, and production-database state were not changed or read.
Credential presence in those systems remains unverified.

## Canonical domain and Render configuration

The canonical production origin is `https://stewlog.com`. Keep the Render
subdomain enabled during the transition. The repository already parses comma-
separated hosts and origins, and `RENDER_EXTERNAL_HOSTNAME` is appended when
Render provides it. Do not hardcode these domains in `settings.py`.

Set these exact Render variables:

```text
DEBUG=false
SECRET_KEY=<Render secret value>
ALLOWED_HOSTS=stewlog.com,www.stewlog.com,stewlog.onrender.com
CSRF_TRUSTED_ORIGINS=https://stewlog.com,https://www.stewlog.com,https://stewlog.onrender.com
DATABASE_URL=<Render PostgreSQL internal connection URL>
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.resend.com
EMAIL_PORT=587
EMAIL_HOST_USER=resend
EMAIL_HOST_PASSWORD=<Resend API key>
EMAIL_USE_TLS=true
EMAIL_TIMEOUT=10
DEFAULT_FROM_EMAIL=STEW Log <no-reply@mail.stewlog.com>
```

`RENDER_EXTERNAL_HOSTNAME` is platform-provided and should not need a manual
value. `SECURE_HSTS_SECONDS` is optional; production defaults to 3600 seconds.
The current security configuration trusts Render's forwarded HTTPS header,
redirects HTTP to HTTPS, enables secure session and CSRF cookies, applies HSTS
to subdomains, and deliberately does not request HSTS preload.

In Render's Custom Domains settings, add and verify `stewlog.com`. Render's
root-domain behavior also adds `www.stewlog.com` and redirects it to the root,
which makes the apex canonical. Keep the `onrender.com` subdomain enabled until
the transition is complete. Do not alter the existing Resend DNS records under
`mail.stewlog.com`.

## URL generation and Django Sites

`django.contrib.sites` is not installed. `SITE_ID` therefore had no effect and
was removed. django-allauth uses the current secure request's host to build
password-reset, email-verification, and social callback URLs.

Consequences:

- Requests that reach Django as `https://stewlog.com` generate custom-domain
  password-reset and verification links.
- Render redirects `www` to the apex when the root custom domain is configured,
  so normal `www` flows become apex flows before Django handles them.
- A reset or verification initiated directly on `stewlog.onrender.com` still
  uses that transitional host. After cutover, disable the Render subdomain or
  add a separately reviewed canonical-host strategy if every legacy request
  must redirect rather than return 404.
- `SECURE_PROXY_SSL_HEADER` makes allauth see HTTPS behind Render's proxy.

## Provider inventory

### Resend SMTP — active application path, deployment unverified

The application uses Django's SMTP backend in production. allauth password
reset and optional email verification use it. There is no Resend SDK or HTTP
API integration.

- Variables: `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
  `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `EMAIL_TIMEOUT`,
  `DEFAULT_FROM_EMAIL`.
- Secrets: `EMAIL_HOST_PASSWORD` (the Resend API key). The other values are
  configuration or public sender identity.
- Placement: production values belong in Render. Local development should use
  the console backend; a real SMTP password may be placed only in an ignored
  local `.env` loaded by VS Code/terminal when an authorized test is necessary.
- Domain change: keep `mail.stewlog.com` DNS intact and confirm the Resend
  sending domain remains verified. Use a From address under that domain.
- Safe check: `python manage.py check` validates application configuration and
  `EMAIL_TIMEOUT` bounds the connection. It does not prove SMTP authentication.
  Proving delivery requires an explicitly authorized test email; none was sent.
- Controls: 10-second timeout by default; Django/allauth handle message
  construction. There is no application retry queue or provider-specific send
  rate limit, and SMTP outages can fail a synchronous email action. Credentials
  are not logged by repository code.

### OpenAI Responses API — implemented, optional, disabled by default

The Pay Plan Assistant adapter posts once to
`https://api.openai.com/v1/responses`. The default model is `gpt-5.6-sol`.
Official OpenAI documentation currently lists that model as supporting both the
Responses endpoint and Structured Outputs.

- Required only when enabled: `OPENAI_API_KEY` (secret), plus
  `PAY_PLAN_ASSISTANT_PROVIDER_ENABLED`, `PAY_PLAN_ASSISTANT_PROVIDER`,
  `PAY_PLAN_ASSISTANT_MODEL`, `PAY_PLAN_ASSISTANT_TIMEOUT_SECONDS`,
  `PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT`,
  `PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS`,
  `PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT`,
  `PAY_PLAN_ASSISTANT_MAX_PROVIDER_INPUT_CHARS`,
  `PAY_PLAN_ASSISTANT_MAX_PROVIDER_RESPONSE_BYTES`,
  `PAY_PLAN_ASSISTANT_MAX_OUTPUT_TOKENS`, `PAY_PLAN_ASSISTANT_MAX_TURNS`,
  `PAY_PLAN_ASSISTANT_CONVERSATION_TTL_HOURS`, and
  `PAY_PLAN_ASSISTANT_EVENT_RETENTION_DAYS`.
- Placement: the API key belongs in Render for production or an ignored local
  environment for authorized development. All other settings are non-secret
  Render rollout controls; the allowed-user list is internal operational data.
- Domain change: none; OpenAI has no callback into STEW Log.
- Safe check: `python manage.py assistant_provider_health` makes no external or
  paid request and never prints the key. It validates presence and local
  configuration only; it cannot prove credential or model access.
- Controls: server-side key access only, master opt-in switch, default rollout
  zero, stable user rollout, allowlist, per-user daily quota, bounded input,
  output tokens and response bytes, 1–60 second timeout, one attempt with no
  retry, `store: false`, privacy-safe `safety_identifier`, strict schema,
  generic failure handling, and privacy-safe operational events. Deterministic
  interpretation always runs first and remains available when disabled,
  excluded, misconfigured, rate-limited, or unavailable.

### Stripe and dj-stripe — foundation implemented, dark-launched

`dj-stripe==2.11.0` owns synchronized Stripe objects. Authenticated hosted
Checkout and Customer Portal flows, strict live/test configuration, founder and
trial policy, signed UUID webhooks, a central entitlement resolver, and
disabled-by-default enforcement are implemented. The exact webhook route is
`/stripe/webhook/<dj-stripe-uuid>/`.

Required private variables are `STRIPE_TEST_PUBLIC_KEY`,
`STRIPE_TEST_SECRET_KEY`, `STRIPE_LIVE_PUBLIC_KEY`,
`STRIPE_LIVE_SECRET_KEY`, and `STRIPE_BASIC_MONTHLY_PRICE_ID`. Rollout controls
are `STRIPE_LIVE_MODE`, `BILLING_FEATURE_ENABLED`,
`BILLING_ENFORCEMENT_ENABLED`, `BILLING_STANDARD_TRIAL_DAYS`, and
`BILLING_FOUNDER_TRIAL_DAYS`. Test and live credentials never fall back to one
another. The endpoint signing secret is stored on the mode-specific dj-stripe
`WebhookEndpoint` database row, not in source or a global environment variable.

The `STEW Log Development` sandbox Product, recurring Price, private test keys,
and sandbox Portal were prepared manually. No webhook has been created, no
sandbox flow was called from the implementation work, and live mode has not
been configured or tested. All billing and Teams flags remain false.

`python manage.py billing_readiness --json` performs only local checks and
prints no identifiers or secrets. It cannot prove that a credential, Price,
Portal, remote endpoint, or delivery works. The complete safe rollout sequence,
required events, and distinct live-mode cutover are in
`docs/stripe_test_to_live_runbook.md`. Do not enable the billing UI or
enforcement until that runbook is complete.

### Google authentication — provider installed, configuration incomplete locally

django-allauth registers Google routes, but the repository has no Google
environment-variable configuration and no explicit Google sign-in UI. The
local SQLite database has zero `SocialApp` rows. Production database-backed
configuration is unverified.

- Callback path: `/accounts/google/login/callback/`.
- Google dashboard redirect URIs for transition:
  `https://stewlog.com/accounts/google/login/callback/`,
  `https://www.stewlog.com/accounts/google/login/callback/`, and
  `https://stewlog.onrender.com/accounts/google/login/callback/`.
- Register the corresponding HTTPS origins only if the chosen Google web flow
  or existing client configuration uses JavaScript origins.
- The OAuth client ID is public; the client secret is secret. In this repository
  they belong in the production database's `SocialApp`, not Render variables.
- Safe check: query only provider/count/completeness metadata in Django admin or
  shell, then verify the authorization redirect without completing a login.
  This checks configuration, not credential validity. allauth applies an HTTP
  timeout and handles OAuth errors; the app adds no custom provider retry,
  quota, or sensitive logging.

### Apple authentication — provider installed, configuration incomplete locally

Apple is registered in allauth, but there is no Apple environment-variable
configuration, explicit UI, or local `SocialApp` row. Production database state
is unverified.

- Callback path: `/accounts/apple/login/callback/`.
- Apple Services ID return URLs for transition:
  `https://stewlog.com/accounts/apple/login/callback/`,
  `https://www.stewlog.com/accounts/apple/login/callback/`, and
  `https://stewlog.onrender.com/accounts/apple/login/callback/`.
- Register the matching domains/subdomains in Apple Developer configuration.
- The Services/client ID, Team ID, and Key ID are public identifiers. The
  client secret and private signing key are secrets stored in `SocialApp`
  fields/settings for this repository, not Render variables.
- Safe check and runtime controls are the same as Google. No live login was
  attempted.

### NHTSA vPIC — active operator command, no credential

`sync_vehicle_catalog` calls the public vPIC API only when an operator runs the
management command. Normal sale entry uses the local catalog and makes no vPIC
request.

- Variables/secrets/domain changes: none.
- Safe check: run `python manage.py sync_vehicle_catalog --dry-run --make Subaru`
  in an approved maintenance environment. It performs public API reads and
  rolls back database writes.
- Controls: 20-second request timeout and HTTP/network/JSON failure conversion
  to `CommandError`; no retry or application-side request pacing. NHTSA applies
  automated traffic rate control, so do not run broad syncs repeatedly. The
  command logs no credentials because none are used.

### PostgreSQL / Render — active in production when configured

`DATABASE_URL` selects PostgreSQL through `dj-database-url`; otherwise local
SQLite is used. The URL is a secret because it normally contains credentials.
It belongs in Render production settings or an ignored local environment only.
Connections use a 600-second maximum age and Django connection health checks.
No host/domain callback changes are required. A safe deployment check uses a
staging database or Render's own connection status; this audit did not connect
to production.

## Local configuration and secret hygiene

- `.env` and `.env.local` are ignored. `.env.example` is safe to commit and
  contains names/placeholders only.
- Django does not load `.env` files. VS Code's Python environment-file support,
  a launch profile, or the terminal must inject local values.
- Production credentials belong in Render except database-backed allauth
  `SocialApp` credentials, which belong in the production database/admin.
- Public identifiers include hostnames, OAuth client/service IDs, Apple Team
  and Key IDs, and Stripe publishable keys. They are configuration, not secrets.
- Secrets include `SECRET_KEY`, `DATABASE_URL`, `EMAIL_HOST_PASSWORD`,
  `OPENAI_API_KEY`, Stripe secret/webhook keys, Google client secrets, and Apple
  client/private signing material.
- With Django Sites disabled, database-backed `SocialApp` entries apply by
  provider rather than being associated with `SITE_ID`.

The tracked/untracked text scan found no OpenAI, Resend, Stripe, Google, or
credentialed-database key shapes and no private keys in application source.
Synthetic secret strings exist only in tests. The prior committed DEBUG-only
Django secret fallback was replaced with a process-generated development key.

An ignored OpenClaw backup archive in the repository root contains private-key
material in an identity file. It is not tracked, and its contents were never
displayed or printed, but ignoring it does not make it a safe storage location.
Move archival identity material out of the application workspace and rotate it if
the archive has been copied or exposed. Documentation/test examples inside the
same archive also match generic credential patterns; their values were not
printed. Large skipped archive members were binaries or Git pack data.

## Deployment checklist

1. In Render, add and verify `stewlog.com`; confirm `www` redirects to apex and
   keep the Render subdomain enabled for transition.
2. Apply the exact core and Resend Render variables above without pasting values
   into source or logs.
3. Leave DNS records under `mail.stewlog.com` unchanged; verify the Resend domain
   and sender in its dashboard without sending from this audit.
4. Update Google and Apple callbacks only if those providers are intended to be
   enabled. Confirm production `SocialApp` completeness without revealing it.
5. Keep Stripe billing and enforcement flags false. Apply migrations and finish
   the sandbox webhook/acceptance checklist before enabling the billing UI.
6. Keep OpenAI disabled or rollout zero until the no-request health command is
   ready; any credential-validating synthetic request requires separate paid-
   request authorization.
7. Run the checks and tests from the audit handoff before deployment, then
   inspect Render logs for configuration errors without printing environment
   values.
