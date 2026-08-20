# Google sign-in rollout

Google sign-in is a dark-launched django-allauth integration. It remains
invisible until the production OAuth credentials and rollout flag are present.
The app requests only the `profile` and `email` scopes, enables PKCE, initiates
the provider flow with POST, and does not store Google access or refresh tokens.

## Google Cloud configuration

Create an OAuth 2.0 Client ID with application type **Web application**.

- Name: `STEW Log Production`
- Authorized JavaScript origin: `https://stewlog.com`
- Authorized redirect URI:
  `https://stewlog.com/accounts/google/login/callback/`

The redirect URI must match exactly, including HTTPS, host, path, and trailing
slash. Do not add Gmail, Drive, Calendar, or other data scopes. Complete the
OAuth consent screen with STEW Log's public app name, support contact, privacy
policy, and terms links before moving beyond test users.

## Render configuration

Add these values to the production web service without placing their values in
the repository, a support ticket, logs, or chat:

```text
GOOGLE_OAUTH_CLIENT_ID=<Google web client ID>
GOOGLE_OAUTH_CLIENT_SECRET=<Google web client secret>
GOOGLE_LOGIN_ENABLED=false
```

Deploy with the flag still false first. After the deployment is healthy, change
`GOOGLE_LOGIN_ENABLED` to `true` and deploy again. Enabling the flag without
both credentials fails startup instead of exposing a broken sign-in button.

Google credentials are settings-backed. Do not create a Google `SocialApp` in
Django admin. The StewLog adapter deliberately ignores legacy database-backed
Google rows so there is only one credential source and no ambiguous provider
selection.

## Existing-account behavior

- A Google identity whose verified email matches one verified STEW Log email
  signs into and connects to that existing Django user. Its sales, pay plan,
  Teams memberships, billing customer, and subscription remain on that user.
- An existing STEW Log account with an unverified or ambiguous matching email
  is not silently connected. The user is returned to sign-in and instructed to
  verify the existing account first. This avoids duplicate users and prevents
  an account-pre-hijacking flow from preserving an attacker's password.
- A Google response without a verified email is rejected without creating or
  changing a STEW Log user.
- A new verified Google email creates one normal STEW Log user and continues
  through the existing onboarding and billing enforcement flow.
- Password sign-in remains available. Connected users may also set or change a
  password from Profile.

## Acceptance checks

1. Sign In and Sign Up show `Continue with Google` only when the flag and both
   credentials are configured.
2. The button submits to `/accounts/google/login/` with POST and redirects to
   Google's authorization endpoint.
3. Canceling consent returns to a safe StewLog error page without creating a
   user or changing account data.
4. A new Google identity creates exactly one user.
5. A verified existing-email identity reuses the existing user and preserves
   password, billing, Team, sale, and pay-plan relationships.
6. An unverified or ambiguous collision creates no user, connects no social
   account, and preserves the existing password.
7. Google tokens are not stored, and neither credential appears in logs or
   rendered HTML.
8. Email/password registration, sign-in, reset, and verification still work.

To disable the feature, set `GOOGLE_LOGIN_ENABLED=false` and redeploy. Existing
local passwords continue to work; connected Google identities remain stored so
they work again when the feature is re-enabled.
