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
