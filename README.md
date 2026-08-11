# SalesLog

## Local Network Phone Testing (Development Only)

- Ensure your Windows desktop and phone are connected to the same Wi-Fi network.
- Open PowerShell in the project root and run:

```powershell
.\run_local_network.ps1
```

- The script detects your desktop private IPv4 address and prints a URL like:

```text
http://192.168.1.25:8000/
```

- Open that URL on your phone browser.

### Windows Firewall (manual step)

- The first time Python/Django listens on the network, Windows may show a firewall prompt.
- Allow access for **Private networks**.
- If you do not see a prompt, open:
  - Windows Security -> Firewall & network protection -> Allow an app through firewall
  - Ensure Python is allowed on **Private** networks.

This setup is development-only. Production security settings remain unchanged.

## Pay Plan Assistant operations

Production provider rollout, rate limits, privacy-safe monitoring, incident
shutoff, migration, and rollback are documented in
[`docs/phase1e_pay_plan_assistant_operations.md`](docs/phase1e_pay_plan_assistant_operations.md).

## Production domain and provider configuration

The custom-domain checklist, exact Render environment names, provider status,
callback URLs, safe health checks, and secret-handling guidance are documented
in [`docs/production_domain_and_provider_audit.md`](docs/production_domain_and_provider_audit.md).

Local development variable names and non-secret placeholders are available in
[`.env.example`](.env.example). Django does not load `.env` files directly;
VS Code or the launching terminal must inject them into the process.

## Phase 2A Teams

The invitation-only Teams architecture, privacy contract, entitlement boundary,
disabled-by-default rollout, and deferred milestone design are documented in
[`docs/phase2a_teams.md`](docs/phase2a_teams.md).

## Stripe subscription foundation

The disabled-by-default Checkout, Customer Portal, founder-trial, webhook,
entitlement, and Teams integration design is documented in
[`docs/stripe_subscription_foundation.md`](docs/stripe_subscription_foundation.md).
Use the separate
[`docs/stripe_test_to_live_runbook.md`](docs/stripe_test_to_live_runbook.md)
before changing any billing flag. Automated verification mocks Stripe and makes
no external payment request.
