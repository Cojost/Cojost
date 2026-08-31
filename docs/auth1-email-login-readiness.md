# AUTH-1 normalized identity readiness

AUTH-1B adds normalized identity enforcement without changing customer login
behavior. Production data was separately confirmed ready before this migration
was authored. Every target database must still pass the read-only data gate
before migration `0066_auth1b_normalized_identity_constraints` is applied.

## Current architecture

- Django 5.2.15 and django-allauth 65.18.0 are installed.
- The user model is Django's default `auth.User`.
- `auth.User.id` is the existing numeric `AutoField` ownership key.
- `auth.User.username` receives normalized case-insensitive uniqueness from
  AUTH-1B.
- Nonblank `auth.User.email` receives normalized case-insensitive uniqueness;
  multiple blank legacy compatibility values remain allowed.
- allauth `EmailAddress` is the supported email identity record. A primary
  address synchronizes `User.email`, which billing still uses as a canonical
  compatibility value. AUTH-1B protects verified and unverified addresses.
- The effective allauth login method is currently username because
  `ACCOUNT_LOGIN_METHODS` is not set.
- The legacy `/SalesLogApp/login/` route and Django's `ModelBackend` also accept
  usernames.
- Django admin currently authenticates staff and superusers by username.
- Google and Apple are installed, but email authentication and automatic
  account connection retain allauth's safe `False` defaults.

Every audited application ownership relationship uses the immutable user ID,
including sales, commissions, pay plans, billing and Stripe, Teams, goals,
activity, profiles, social accounts, and audit records. A later username-only
change will not re-key those records or invalidate an existing session.

## Read-only readiness command

Run the command once for every deployment database:

```powershell
python manage.py auth_identity_readiness
python manage.py auth_identity_readiness --json
python manage.py auth_identity_readiness --require-data-ready
python manage.py auth_identity_readiness --require-ready
```

The command performs reads and schema introspection only. It does not normalize,
invent, merge, delete, or update identity data. It reports counts and database
user IDs rather than email addresses or usernames.

- `--require-data-ready` exits unsuccessfully while identity rows need
  remediation.
- `--require-ready` additionally requires all three reviewed normalized unique
  indexes and still does not enable email login.
- The expected index names are `auth_user_email_ci_unique`,
  `auth_user_username_ci_unique`, and
  `account_emailaddress_email_ci_unique`.
- Detailed diagnostics verify the table, uniqueness, expression-index shape,
  exact SQLite definition or PostgreSQL catalog expression/predicate, and
  database validity. A matching name alone is not considered ready.

The local persistent database at the authorized baseline contains 24 users and
is not ready:

- 10 users have blank email values.
- 7 users need case normalization in `User.email`.
- One normalized email collision group spans 7 users.
- 21 users have no allauth `EmailAddress` record.
- 11 nonblank canonical emails have no matching allauth address.
- All 3 existing canonical allauth addresses are unverified.
- No usernames are blank, whitespace-padded, or case-insensitively duplicated.
- None of the three AUTH-1B indexes is installed.

These results prohibit an immediate email-login conversion. The audit command
was run with a SHA-256 hash before and after; the database remained byte-for-byte
unchanged.

## AUTH-1B enforcement design

Migration `0066` is atomic and performs no data repair. Its preflight rejects
normalization problems, blank allauth addresses, normalized collisions (also
two colliding rows owned by one user), cross-owner email conflicts, and invalid
username identities. Blank `User.email` values are deliberately exempt.

On PostgreSQL the preflight locks both identity tables against writes until the
three indexes are built and the transaction commits. On SQLite the atomic
read/DDL transaction either retains a stable snapshot or fails without
committing partial DDL. A uniqueness race during index creation therefore
fails the migration rather than applying partial enforcement.

The migration uses explicitly named functional unique indexes because
`auth.User` and `EmailAddress` are third-party models. It records no fake model
state and does not replace either model. Reversal drops only the three AUTH-1B
indexes; allauth's own exact `(user, email)`, verified-email, primary-email, and
ordinary lookup indexes remain intact.

Signup, account-email, and inherited Django/allauth admin forms normalize input
and return generic collision errors. Database `IntegrityError` is caught only
after the relevant atomic write boundary has rolled back and only when a
normalized identity collision is confirmed. Admin email writes synchronize the
authoritative address and `User.email` without sending mail.

## Deferred email-login and username changes

AUTH-1B intentionally stops before the login/UI cutover. A later reviewed phase
may:

1. Set the allauth 65.18.0 setting `ACCOUNT_LOGIN_METHODS = {'email'}` and keep
   required username/email signup fields plus `ACCOUNT_UNIQUE_EMAIL = True`.
2. Normalize email input and synchronized storage with the public allauth
   adapter/model APIs. Retire the legacy customer login route.
3. Restrict username authentication to documented staff/superuser admin access,
   while customer login accepts email only.
4. Use allauth's authenticated, verified change-email mode so the old canonical
   address remains active until the replacement verifies. The username form
   must never write email.
5. Keep social email authentication disabled until provider-specific verified
   Google/Apple linking tests prove that unverified addresses cannot link and
   existing provider/UID accounts cannot duplicate.
6. Add the password-confirmed username form to Profile Settings. Validate with
   the installed allauth username policy, recheck `username__iexact` inside a
   transaction, update only the current user's username, and handle the database
   race generically.
7. Run the focused authentication, verification, Teams, billing, profile/CX-3,
   social-provider, and full application suites against disposable databases.

Until those later prerequisites are complete, customer login remains username
based. Email login, customer username editing, customer email-change UI, and
automatic social-account linking remain disabled. User IDs, ownership foreign
keys, verification, password reset, sessions, billing, Teams, and inherited
admin behavior remain unchanged.
