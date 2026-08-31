# AUTH-1 email-login readiness

AUTH-1 is blocked at the data-readiness gate. Authentication behavior must not
be changed and normalized uniqueness must not be installed until every target
database passes the read-only check described below.

## Current architecture

- Django 5.2.15 and django-allauth 65.18.0 are installed.
- The user model is Django's default `auth.User`.
- `auth.User.id` is the existing numeric `AutoField` ownership key.
- `auth.User.username` is raw-value unique but not case-insensitively unique.
- `auth.User.email` is not database-unique.
- allauth `EmailAddress` is the supported email identity record. A primary
  address synchronizes `User.email`, which billing still uses as a canonical
  compatibility value.
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
- `--require-ready` additionally requires the separately reviewed normalized
  email and username constraints.
- The expected constraint names are `auth_user_email_ci_unique` and
  `auth_user_username_ci_unique`.

The local persistent database at the authorized baseline contains 24 users and
is not ready:

- 10 users have blank email values.
- 7 users need case normalization in `User.email`.
- One normalized email collision group spans 7 users.
- 21 users have no allauth `EmailAddress` record.
- 11 nonblank canonical emails have no matching allauth address.
- All 3 existing canonical allauth addresses are unverified.
- No usernames are blank, whitespace-padded, or case-insensitively duplicated.
- Neither normalized database constraint is installed.

These results prohibit an immediate email-login conversion. The audit command
was run with a SHA-256 hash before and after; the database remained byte-for-byte
unchanged.

## Required remediation and architecture decision

Remediation must be a separately reviewed, operator-owned process using current
business records. It must not invent addresses, guess ownership, merge users, or
delete accounts. For each database, operators must:

1. Resolve blank and normalized-collision email identities with the affected
   account owners.
2. Normalize approved canonical addresses and create or reconcile the matching
   allauth primary `EmailAddress` rows.
3. Preserve the existing user IDs and all foreign-key ownership.
4. Re-run `auth_identity_readiness --require-data-ready`.
5. Approve a database-specific uniqueness design before any authentication
   setting changes.

With the default Django user model, a `SalesLogApp` migration cannot add a normal
Django model constraint to `auth.User`. After data remediation, the least
invasive option is a separately authorized, vendor-reviewed migration that adds
functional unique indexes for normalized nonblank `auth_user.email` and
normalized `auth_user.username`. Application validation and generic
`IntegrityError` handling are still required for friendly errors and races.
This is not supplied merely by `ACCOUNT_UNIQUE_EMAIL`, and no such migration is
included in the blocked branch.

## Post-remediation AUTH-1 implementation

Only after both data and constraint gates pass should AUTH-1 resume:

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

Until those prerequisites are complete, the current username authentication,
admin behavior, social behavior, email verification, password reset, sessions,
and ownership relationships remain unchanged.
