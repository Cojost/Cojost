# Ask Stew AI pilot access

CX-3 Ask Stew AI access is independent from Stripe and subscription enforcement.
Customer access defaults to denied and is granted only to immutable Django user
IDs listed in the `ASK_STEW_AI_PILOT_USER_IDS` environment setting.

To enable users, set a comma-separated list and restart the application:

```text
ASK_STEW_AI_PILOT_USER_IDS=42,108
```

To disable one user, remove that ID and restart. To disable all customer pilot
access, set the value to an empty string or remove the setting. Staff and
superusers retain access for internal testing.

The pilot allowlist controls the Ask Stew AI page only. It does not grant
access to the legacy pay-plan-change assistant, Commission Sandbox, scenarios,
Teams, billing, or any other Pro capability.

Ask Stew AI reuses the bounded optional provider configuration documented by
the `PAY_PLAN_ASSISTANT_*` settings. If that provider is disabled, invalid, or
unavailable, authorized users still receive explanations produced directly
from StewLog's deterministic calculation services.

Provider calls use an atomic daily quota and signed submission idempotency.
CX-3 does not add a rolling per-minute throttle because customer access is
restricted to the explicit small pilot allowlist. Reconsider a short-window
throttle before expanding access beyond that pilot; the daily quota is not a
substitute for a per-minute limit.
