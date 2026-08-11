# Phase 2A: Teams, shared unit progress, and motivation

## Release posture

Teams is a dark-launched feature. `TEAMS_FEATURE_ENABLED` defaults to `false`
and accepts only `true`, `false`, `1`, or `0`. When disabled, the menu entry is
absent and every Teams route returns 404 before looking up a team or invitation.
Do not enable the flag in production until the migration, authorization review,
and operational checklist below have been completed.

## Product and access policy

- Teams are invitation-only. There is no directory, global ranking, direct
  messaging, or discovery endpoint.
- A user with a Pro entitlement may create and own one active team.
- An owner or admin may invite an email address, including someone who has not
  registered yet. An invited Basic user may accept or decline and participate
  in one team after verifying that exact address.
- Membership is modeled per team, so a future multiple-team policy does not
  require replacing the schema. The one-active-team policy is enforced in the
  transactional service layer today.
- Owners manage settings, goals, roles, ownership transfer, removal, and team
  deactivation. Admins may update only the team goal, invite or remove non-owner
  members, and moderate comments; owner-selected display mode stays owner-only.
- If the owner loses Pro, creation and management become read-only. Membership,
  activity, comments, reactions, and private source records are preserved. Only
  the owner sees the entitlement-restoration message.

## Billing and founder boundary

The Stripe subscription foundation now owns production entitlement through the
replaceable boundary in `SalesLogApp/team_entitlements.py`:

- `get_team_entitlement(user)` returns a typed entitlement.
- `can_use_teams(user)` understands the feature flag, Pro/founder access, and
  invited or active Basic participation.
- `can_create_team(user)` requires the feature flag and Pro-equivalent access.

The default backend calls the central billing resolver. A synchronized active
subscription or trial maps to `pro`; a synchronized founder trial maps to
`founder_pro`. `TEAMS_FOUNDER_USER_IDS` remains a DEBUG-only local-development
fallback and is ignored when billing enforcement is enabled. It never infers
payment, creates a subscription, or marks an account paid. Tests mock Stripe
calls and use local dj-stripe records.

## Privacy contract

Team views receive explicit immutable projections, never complete `Sale`
objects. The only sale-derived fields allowed in team activity are:

- team and membership identity;
- safe display name with username fallback and a generated initial;
- `Sale.count` as unit credit;
- sale date;
- safe activity type and timestamps; and
- an internal, non-public relationship used only to synchronize the activity.

Member email is not used as a display fallback. Existing avatar files remain
owner-protected, so Phase 2A uses an initial rather than exposing another
profile's protected avatar URL.

The following are prohibited from team templates, projections, URLs, activity
messages, aggregates, and application logs: customer and sale notes, deal
number, vehicle details, front/back gross, commission amount or breakdown,
adjustments, pay plans and rules, uploaded documents, assistant conversations,
private goals, member email used as a public display identity, and internal sale
ownership links. An intended recipient's email is accepted by the
management-only invitation form, shown only in the management-only pending
invitation list, and used only to deliver and authorize that invitation.

Totals query only `Sale.user_id`, `Sale.date`, and `Sum(Sale.count)`. No gross,
commission, customer, or deal field is selected or cached. Team code does not
change a Sale, its owner, or any commission calculation.

## Models and ownership

- `Team` has a non-guessable public UUID, owner, timezone, optional positive
  monthly unit goal, ranked/alphabetical display mode, timestamps, and active
  and read-only states.
- `TeamMembership` has a public UUID, role, lifecycle status, join timestamp,
  and sharing preference. A database constraint permits only one row per
  team/user; the current one-team policy remains a service rule for future
  schema flexibility.
- `TeamInvitation` stores the normalized intended email, an optional user
  binding for recipients who already have a verified account, token HMAC
  digest, non-sensitive prefix, expiry, creator, and acceptance/revocation audit
  timestamps.
- `TeamActivity` contains only safe activity fields plus an internal nullable
  one-to-one sale link. Sale deletion safely withdraws the record before the
  link is cleared.
- `TeamComment` records author, edit, deletion, and moderation timestamps.
  Deleted or moderated bodies are cleared while the minimal audit record stays.
- `TeamReaction` stores one of five semantic codes with a uniqueness constraint
  per activity/member/code.

Migration `0053_phase2a_teams.py` creates these tables, indexes, and constraints.
Team deactivation changes state; it does not delete a Sale, Commission, Pay
Plan, document, assistant record, member history, comment, or reaction.

## Invitation lifecycle

Invitation codes use `secrets.token_urlsafe(32)`. Only an HMAC-SHA256 digest and
the first ten non-sensitive characters are stored. The complete code is emailed
to the intended address and shown to the inviter once through the session as a
backup. It is submitted by the recipient in a CSRF-protected POST body, never
put in a URL, and never written by application logging. The email contains only
generic registration, sign-in, and Teams links, so forwarding a URL cannot
transfer the secret.

The signup form requires a unique email address. General beta access currently
uses optional allauth verification to avoid locking out existing unverified
accounts, but accepting a Team invitation always requires allauth to report the
invited address as verified. A recipient may therefore be invited before an
account exists; the invitation is bound to that user only during acceptance.
Delivery failure rolls back both the invitation and any pending membership.
Acceptance locks the invitation, recipient user, team, and membership in a
transaction. This serializes concurrent
acceptances—including different invitations for the same user—and database
uniqueness prevents duplicate team membership. Expired, revoked, accepted,
wrong-user, and unverifiable-email codes return 404. Accepting is one-time;
declining marks the pending membership declined and revokes the invitation.

## Sharing and sale synchronization

The privacy-preserving join default is **monthly totals only**. A member may
choose:

1. individual sale activity and monthly totals;
2. monthly totals only; or
3. pause all team sharing.

Every feed and interaction query checks the current active membership and
sharing preference, so a preference change hides unauthorized activity and its
comments/reactions immediately without deleting the private Sale. No response
or page cache is introduced.

A single pair of existing signal registrations delegates Sale saves/deletes to
an idempotent service. Saving an authorized sale creates or updates one safe
activity; its one-to-one constraint prevents duplicates. Editing count or date
updates the activity. Deletion marks it hidden and clears the source link.
When Teams is disabled, sale saves do not create Teams records. Existing
commission-credit behavior is untouched.

Monthly totals are computed live from `Sale.count`, bounded by the selected
calendar month in the team's IANA timezone. Joining authorizes the full
month-to-date aggregate for the member's join month, including a sale entered
after joining with an earlier date in that month; reporting months before the
join month remain excluded. The current team-local month is the default; future
input is clamped and prior months are supported. Fractions remain decimal values. Tied ranked totals use
competition ranking with safe display name and user ID as deterministic sort
keys. Alphabetical mode uses the same safe name. Team goal percentage and
remaining units use only these unit totals.

## Comments, reactions, and moderation

Comments are plain text, trimmed, limited to 500 characters, and rendered with
Django escaping. Active members may create comments only on currently visible
activities and edit/delete only their own. Owners/admins may hide another
member's comment. All mutations use POST except the authenticated author's edit
form; CSRF middleware protects every POST. Cross-team, hidden-activity, and
removed-member access resolves to 404 where existence should remain private.

Reactions are semantic codes—`celebrate`, `on_fire`, `applause`, `strong_work`,
and `great_job`—rendered with both emoji and accessible text. Each member can
toggle each type once. Counts are prefetched with feed pages; comments and
authors are also prefetched to avoid N+1 queries. Feeds contain 25 activities
per page.

## Rollout and operations

1. Keep `TEAMS_FEATURE_ENABLED=false` while deploying migrations 0053 and
   0055. Migration 0055 permits an invitation to exist before its recipient
   registers; it does not delete or rewrite existing invitations.
2. Verify the migration on a non-production copy and confirm rollback/runbook
   ownership. Disabling the flag is the immediate application-level shutoff;
   do not reverse the migration during an incident.
3. Do not configure founder IDs in production. User IDs are identifiers, not
   proof of payment; production founder access must come from a synchronized
   founder grant through the central billing resolver.
4. Exercise create, invite, accept/decline, sharing changes, removal, and owner
   entitlement loss with non-sensitive test users.
5. Review 404/403/409 and database-integrity error rates by route name and
   status only. Never record invitation codes, request bodies, customer/deal
   fields, comment bodies, email addresses, or per-user unit totals in logs or
   analytics.
6. Monitor feed latency, aggregate query duration, page size, invitation
   acceptance success rate, and moderation action counts using coarse,
   non-content metrics. Alert on repeated integrity failures or unusual 5xx
   rates.
7. Enable for an internal cohort only after the real billing resolver has a
   documented ownership and failure policy, then expand deliberately.

## Deferred work

Automatic member and team milestone posts are intentionally deferred. Their
idempotency across sale edits, reporting-month moves, and sharing changes needs
a dedicated event-key design; generating them now would risk duplicate or
historically unauthorized posts. A later Teams phase should add those keys and
privacy tests before enabling milestones.

Also deferred are broader Teams activity notifications, an explicit public
display-identity setting, multiple-team product policy, richer audit operations,
and the broader Basic/Pro feature split. Invitation email delivery is included;
Checkout/webhook infrastructure now exists but remains disabled pending the
separate billing rollout runbook.
