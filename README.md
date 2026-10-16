# FleetHub

FleetHub is a self-hosted remote monitoring and management (RMM) tool for small IT teams.
An agent on each managed device reports to a central **hub**, and operators use the hub's
web console to monitor, support and manage the fleet.

## Components

| Component | Folder | What it is |
|---|---|---|
| **Hub** | `hub/` | Python, Flask and Socket.IO console and API on SQLite. Runs as a Windows service or in Docker. |
| **Windows agent** | `agent/` | C#/.NET Windows Service running as SYSTEM. See [agent/README.md](agent/README.md). |
| **Linux agent** | `agent-linux/` | Smaller feature set: telemetry, processes and a few commands. See [agent-linux/README.md](agent-linux/README.md). |
| **Android agent** | `agent-android/` | Fully managed (device owner) Android devices. See [agent-android/README.md](agent-android/README.md). |
| **Desktop client** | `app/` | Flutter client for Windows: fleet list, alerts, Wake-on-LAN. See [app/README.md](app/README.md). |
| **TURN relay** | `turn/` | coturn relay for remote control across NATs. See [turn/README.md](turn/README.md). |
| **Installer** | `install.ps1` | One menu-driven installer for the agent, the hub and the TURN relay. |

## Features

- **Monitoring:** live CPU, memory, disk, network, GPU, fan, power and temperature data,
  with history charts and the full hardware sensor tree.
- **Alerts and rules:** sustained high temperature, grouped alerts with causal hints, rules
  over metrics and Windows event logs, and optional AI-written fix suggestions.
- **Remote management:** PowerShell scripts as SYSTEM, an interactive terminal in the
  browser, a task manager, a file browser with disk-usage history, and Wake-on-LAN.
- **Remote view and control:** WebRTC sessions, including the lock and logon screens, with
  optional session recording.
- **Software:** PDQ-style package deployment (MSI, winget, PowerShell, driver packs) with
  detection rules, retries and maintenance windows, plus patches and firmware updates.
- **Backups:** encrypted hub backups and incremental per-PC file backups to S3 or WebDAV.
- **Inventory and reports:** hardware, installed software, security posture checks and
  printable device reports.
- **Access control:** OIDC sign-in, permission groups scoped by machine list or AD OU,
  directory-group mapping, invite links and an append-only audit log.
- **Android:** app blocking, screen-time schedules, lock, wipe and on-demand location.
- **Assistant:** an optional AI assistant that can read the fleet and act with your
  permissions.

## Quick start

### 1. Install the hub

On a Windows server, run the installer and choose **Install Hub**. It installs the
`FleetHub - Hub` Windows service and can also set up the TURN relay. The hub needs 64-bit
Python.

```powershell
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1)))"
```

To run the hub in Docker instead:

```bash
docker compose -f deploy/docker/compose.yaml up -d
```

The hub reads its configuration from `.env`. The minimum is:

```
FLASK_SECRET_KEY=a-long-random-string
ALLOWED_EMAILS=you@example.com            # break-glass administrators
HUB_URL=https://hub.example.com           # public https address of the hub
AGENT_ENROLLMENT_SECRET=a-long-random-shared-secret
GOOGLE_CLIENT_ID=...                      # or OIDC_ISSUER / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET
GOOGLE_CLIENT_SECRET=...
```

After the first sign-in, you can edit sign-in, TURN and AI settings in the console.

### 2. Add devices

- **Windows, with an onboarding code:** in the console, open **Administration → Onboarding →
  New code**. On the PC, open `https://<your hub>/onboarding`, enter the code and run the
  downloaded installer. No shared secret reaches the PC.
- **Windows, with the enrollment secret:**

  ```powershell
  powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1))) -Component Agent -HubUrl https://hub.example.com -EnrollmentSecret <secret>"
  ```

- **Linux:** see [agent-linux/README.md](agent-linux/README.md).
- **Android:** scan the QR code on **Administration → Device provisioning** during the setup
  wizard of a factory-reset device.

### 3. Grant access

Create **permission groups** at `/permissions` for day-to-day operators. Keep
`ALLOWED_EMAILS` to the smallest possible set of accounts.

## Updates

- **Agents update themselves.** Each update is verified against an Ed25519-signed manifest
  and refused if the signature does not match.
- **The hub** shows a notice when a new version is available. Set `HUB_AUTO_UPDATE=1` to
  install updates automatically. A Docker hub updates by pulling a new image.
- Deploy the hub before an agent release. Version rules are in [VERSIONING.md](VERSIONING.md).

## Security notes

Read these before you deploy. [SECURITY.MD](SECURITY.MD) has the full model.

- **`issue_commands` means code execution as SYSTEM** on every machine in the operator's
  scope. `ALLOWED_EMAILS` holds every capability on every machine. Protect those accounts
  with MFA.
- **Unsigned telemetry is accepted by default.** After every agent is current, turn on
  **Settings → Security → Only accept telemetry from enrolled agents**.
- **The backup master key cannot be recovered.** Store `BACKUP_MASTER_KEY` outside the hub;
  without it, no backup can be decrypted.
- **Hub self-update trusts GitHub over HTTPS**, not the Ed25519 release key. Agent
  binaries are always signature-checked.
- **The assistant's Bypass mode** runs actions without confirmation. Conversations start in
  Ask mode, and the assistant is off until an administrator enables it.

## Documentation

| Topic | Page |
|---|---|
| Installer options, onboarding, updates, Docker, release signing | [docs/installation.md](docs/installation.md) |
| Console, telemetry tuning, alerts, assistant, audit log, event logs, reports | [docs/monitoring.md](docs/monitoring.md) |
| Sign-in (OIDC), permission groups, invite links, Active Directory | [docs/access.md](docs/access.md) |
| Commands, terminal, processes, disk usage, Wake-on-LAN, network discovery | [docs/managing-machines.md](docs/managing-machines.md) |
| Remote view and control, TURN, session recording | [docs/remote-control.md](docs/remote-control.md) |
| Package deployment | [docs/packages.md](docs/packages.md) |
| Hub and per-PC backups, restores | [docs/backups.md](docs/backups.md) |
| Android policies, lock and wipe, location, provisioning | [docs/android.md](docs/android.md) |
| Planned and shipped features | [ROADMAP.MD](ROADMAP.MD) |

## Development

- Hub tests: `python tests/run_all.py -q`
- Agent tests: `dotnet test agent/TempMonitorAgent.slnx`
- Contributor rules for versions, translations and code style: [CLAUDE.md](CLAUDE.md) and
  [VERSIONING.md](VERSIONING.md)

Some paths, namespaces and download URLs still use `Temp_Monitor` and `TempMonitorAgent`.
These names are intentional. GitHub redirects the old `Temp_Monitor` URLs to this
repository.

## License

[GPL-3.0](LICENSE)
