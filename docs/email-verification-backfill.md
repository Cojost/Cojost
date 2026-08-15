# Existing-user email verification backfill

This workflow audits existing active users and can send normal django-allauth
verification messages. It never marks an address verified. The command is a
dry run unless `--send` is present.

The dispatch ledger stores only a user ID, an HMAC recipient fingerprint, the
source, status, and timestamps. It does not store an email address, message,
confirmation key, or confirmation URL. Successfully sent and allauth-throttled
attempts are suppressed for the configured cooldown (60 minutes by default).
Failed delivery attempts do not establish that cooldown and may be retried
immediately after the delivery problem is corrected.

## Production prerequisites

Before any send, confirm all of the following in the deployed release:

- Migration `0056_emailverificationdispatch` has been applied.
- `EMAIL_VERIFICATION_PUBLIC_BASE_URL=https://stewlog.com`.
- `ALLOWED_HOSTS` includes `stewlog.com`.
- HTTPS proxy handling is working and `SECURE_PROXY_SSL_HEADER` is unchanged.
- The SMTP/delivery backend is configured; never use console, local-memory,
  dummy, or file delivery in production.
- `DEFAULT_FROM_EMAIL` is the verified StewLog sender.
- Normal allauth verification templates render correctly in the deployed
  release.

The command refuses a production send unless the public base URL is exactly
`https://stewlog.com`, a delivery backend and non-local sender are configured,
and `--confirm-production-send` is supplied.

The supported production delivery backend for this workflow is Django's SMTP
backend (`django.core.mail.backends.smtp.EmailBackend`). The preflight resolves
and initializes that backend without opening a network connection. Custom,
wrapper, console, dummy, file, and in-memory backends fail closed unless a
future change explicitly audits and allows one.

The sender must parse as exactly one syntactically valid email address on a
non-local DNS hostname. SMTP requires a syntactically valid public hostname,
a port from 1 through 65535, a positive timeout, and exactly one of TLS or SSL.
Localhost names, local-only names, IP literals, reserved test domains, malformed
values, and unencrypted or contradictory transport settings fail preflight.
These syntax and policy checks do not prove that a mailbox exists or that a
host can receive mail; verify the deployed provider configuration separately.

Production preflight completes before recipient selection, `EmailAddress`
repair, dispatch reservation, confirmation generation, delivery, or Teams
verification-resume session mutation. Unsafe configuration exits with a
generic error and does not include sender, host, credentials, or backend
exception details.

Raw Team invitation tokens are never stored in the verification-resume
session. After preflight, the session receives only a purpose-specific,
timestamp-signed reference whose payload contains a version and the existing
one-way HMAC invitation digest. Django session signing is an integrity control,
not encryption; confidentiality does not depend on it because the raw bearer
token is absent. The reference is limited by
`TEAM_INVITATION_VERIFICATION_RESUME_MAX_AGE` (seven days by default), cannot be
configured longer than the normal invitation lifetime, and is removed when it
is consumed. Invitation ownership, the exact verified recipient email,
membership state, expiry, revocation, and prior acceptance are checked again
on resume and inside the locked accept/decline operation. Legacy sessions that
contain the former raw-token key are rejected and cleared without using or
logging that value; the user must reopen the original invitation.

## Safest execution order

Run the complete read-only audit first:

```console
python manage.py audit_and_resend_verification_emails
```

Review the summary and investigate every reported conflict user ID in a
restricted operator session. Compare the Django user email and allauth
`EmailAddress` ownership case-insensitively. Do not copy addresses into tickets,
chat, or command logs. Resolve ownership only through the approved account
support process; never make an address verified manually.

Exercise one known test account first:

```console
python manage.py audit_and_resend_verification_emails --send --user-id 123 --confirm-production-send
```

The alternate one-account selector is available when required, but it can put
an address in shell history:

```console
python manage.py audit_and_resend_verification_emails --send --email ADDRESS --confirm-production-send
```

After the test user confirms receipt and verifies normally, send a limited
batch:

```console
python manage.py audit_and_resend_verification_emails --send --limit 25 --batch-size 100 --confirm-production-send
```

Audit the remaining eligible population after each batch. Recent successful or
throttled recipients are excluded by the cooldown:

```console
python manage.py audit_and_resend_verification_emails --batch-size 100
```

Confirm delivery using provider aggregate delivery counts and the test user's
direct report. Do not log, paste, or retain message bodies, recipient addresses,
confirmation keys, or confirmation URLs.

## Stop and rollback procedure

- Press `Ctrl+C` to stop an active batch. Already completed per-user sends
  remain recorded; rerunning during cooldown will not resend them.
- Do not run another command with `--send` until the delivery or configuration
  issue is understood.
- A failed delivery is recorded without its exception text and does not stop
  later users in the same batch. A failed reservation remains available for an
  immediate safe retry and never creates the successful-send cooldown.
- A pending reservation is considered abandoned after
  `EMAIL_VERIFICATION_PENDING_STALE_MINUTES` (15 minutes by default). A retry
  may reclaim it; an optimistic attempt-time check prevents the older worker
  from finalizing the reclaimed reservation. Fresh pending reservations still
  block duplicate work. Keep the SMTP timeout below the stale threshold.
- The repair may add an unverified `EmailAddress` row. It does not change the
  user email, password, verification status, team membership, subscription,
  sales, commission, or pay plan.
- Do not delete repaired rows or the dispatch ledger as an operational
  rollback. Roll back application code through the normal deployment process
  and retain the privacy-safe ledger so duplicate sends remain suppressed.

Before broad production sending, run the PostgreSQL-specific concurrency test
in PostgreSQL CI or staging. SQLite exercises the durable in-flight reservation
behavior but does not prove PostgreSQL row-lock and unique-constraint behavior.
As with any external email side effect, a process failure at the exact boundary
between provider acceptance and ledger finalization cannot provide mathematical
exactly-once delivery without provider-supported idempotency; use the cooldown,
ledger, limited batches, and provider aggregate counts as operational controls.
