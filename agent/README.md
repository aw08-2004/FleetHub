# FleetHub — C#/.NET Fleet Agent

The Windows telemetry client: a Windows Service (runs under **LocalSystem**) that
replaced the Python `companion.py`, since removed from the repo. It reaches telemetry
parity with the companion **and** speaks the hub's fleet command channel: it enrolls,
heartbeats, polls for commands, executes them, reports
results, and updates itself from a **signed** manifest (verified fail-closed). It also
carries the client half of the interactive terminal (ConPTY), package deployment, patching,
per-PC file backup/restore, remote view/control and session recording, firmware settings and
flashing, Wake-on-LAN, the file explorer, disk usage, event log collection, network sweeps,
software and security-posture inventory, BitLocker key escrow and self-healing watchdogs --
every one a section of [../README.md](../README.md). The [Linux](../agent-linux/README.md) and
[Android](../agent-android/README.md) agents are separate, much smaller projects that speak the
same wire protocol; their versions are not comparable with this one's
([../VERSIONING.md](../VERSIONING.md)).

**Current release: 3.44.0** (2026-10-13). `main` carries one change not yet in a release: the
exe is built as `FleetHubAgent.exe` (see *Install* below).

- **Target:** .NET 10 (`net10.0-windows`), published **self-contained single-file
  win-x64** (no runtime install needed on the fleet).
- **Sensors:** LibreHardwareMonitorLib **in-process** — no separate
  LibreHardwareMonitor.exe / `:8085` web server. Needs the **PawnIO** kernel driver
  (the installer sets it up). Every LHM hardware category is enabled (CPU, GPU, RAM,
  motherboard/SuperIO, storage, NICs, fan controllers, PSU, battery) and the whole
  sensor tree is flattened into the report — fan RPM, control duty, package watts and
  voltages ride along with the temperatures. The hub decides what to chart.

## Layout
```
src/TempMonitorAgent/            (the folder and namespaces keep the pre-rename name on purpose)
  Program.cs / Worker.cs         host + executor registration; Worker is seven independent
                                 concurrent loops (telemetry, heartbeat, commands, inventory,
                                 processes, updates, watchdogs)
  AgentConfig.cs                 constants, endpoints, trust roots, %ProgramData% paths
  Models.cs                      wire DTOs shared across the client surfaces
  Telemetry/                     SensorReader (LHM), SystemInfo (WMI), TelemetryReporter,
                                 VolumeReader, ProcessReader/Reporter, LiveTelemetry
  Fleet/                         FleetClient, SignatureVerifier, CommandDispatcher,
                                 OutputStreamer, ProcessRunner, WingetLocator,
                                 Shell/ (ConPTY sessions), Executors/
  Backup/                        BackupFilesExecutor, RestoreFilesExecutor (VSS, chains)
  Bios/                          firmware settings reader/writer, FirmwareFlasher
  DiskUsage/                     MFT reader, folder trees, the daily scan and its delta upload
  Events/                        Windows event log query and reporter
  Files/                         list/copy/move/delete/fetch/push/open for the Files tab
  Network/                       NIC inventory, Wake-on-LAN, ARP sweep
  Patch/                         Windows Update + winget scan, patch install
  Remote/                        remote view/control helper, hub relay, recording badge,
                                 virtual-display executors
  Security/                      BitLocker inventory and escrow, security posture
  Software/                      installed-software inventory
  UserMessage/                   the dialog behind the rules engine's "show a message"
  Watchdog/                      the document the heartbeat delivers, and the runner
  Update/                        SelfUpdater, SelfUninstaller, VersionUtil
  State/                         AgentState (agent.json, restart_state.json), RuntimeConfig
tests/TempMonitorAgent.Tests/    xUnit; the whole suite is run on Windows only
tests/ShellParserTests/          xUnit: terminal/ConPTY stream parsing
install/agent-install.ps1        installs the service (+ PawnIO, recovery); the onboarding
                                 wizard's script (hub/templates/onboarding/onboard.ps1) runs it
```

## Wire protocol (must match the Python hub)
- Telemetry: `POST /api/report`, signed with the agent's own bearer token from 3.44.0 (an older
  agent sends it unsigned, which the hub accepts until *Only accept telemetry from enrolled
  agents* is switched on). Cadence defaults to 5 s per reading and 10 s per full sensor block, set
  from the hub (**Settings → History Metrics**) and rising to 1 Hz while an operator watches the
  machine's page.
- Fleet: `POST /api/agent/enroll` → `{agent_id, token}`; then
  `Authorization: Bearer <agent_id>:<token>` on `POST /api/agent/heartbeat`,
  `GET /api/agent/commands` (pull+claim), `POST /api/agent/commands/<id>/result`,
  `POST /api/agent/commands/<id>/output` (live output while a script runs).
- Terminal (ConPTY): `GET /api/agent/pty/<session>/input`,
  `POST /api/agent/pty/<session>/output`, `POST /api/agent/pty/<session>/closed`.
- Remote: `GET /api/agent/remote/<session>/poll`, `POST .../signal`, `POST .../ended`,
  `GET .../ice` (the ICE servers the hub picks for this machine, from the source address it sees
  the request arrive from -- the copy in the start command was chosen for wherever the operator
  was sitting).
- Backups: `POST /api/agent/backups/upload/<run_id>`, `POST /api/agent/backups/<run_id>/result`;
  restores via `GET /api/agent/backups/restore/<id>/plan`, `.../archive/<index>`, `.../result`.
- Packages: `GET /api/agent/packages/<sha256>` (payload fetch, re-hashed before execution).
- Processes: `GET /api/agent/processes/wanted` — asked every 2s while idle, because there is
  no inbound port for the hub to push at and an operator who opens the Processes card should
  not wait out a heartbeat before the machine starts looking. One indexed lookup, a ~30-byte
  answer, no writes; it is the only thing an unwatched machine does for this feature. While
  the answer is yes the agent samples every 5s and the next heartbeat body carries
  `processes`. That is the one heartbeat payload that is not change-only (a process list has
  changed by definition) and the one that is demand-driven — an unwatched machine samples
  nothing and sends nothing. The heartbeat reply's `processes_wanted` remains the way a
  machine learns to **stop**, and the only path against a hub older than 1.71.0. See
  `Telemetry/ProcessReporter` and `Worker.ProcessLoopAsync`.
- Commands are **not signed**. The agent executes what an enrolled, authenticated pull
  returns; the hub authorizes on an allow-listed console session and records every
  command in its `audit_log`. (Until hub 1.10 / agent 3.1, `run_script`,
  `install_driver` and `update_bios` additionally required an offline Ed25519 signature
  verified here. No key was ever configured, so in practice they were always refused —
  which is why removing the gate is what made them work, not a loosening.)

Implemented executors (registered in `Program.cs`; each `Type` must match the hub's command
type, and `Fleet/CommandDispatcher.cs` routes on it):

| Area | Types |
|---|---|
| Low risk | `restart`, `shutdown`, `rename`, `gpupdate`, `install_app` |
| Scripts and rules | `run_script`, `show_message`, `collect_probe` |
| Packages and patches | `deploy_package`, `install_patches` |
| Terminal | `shell_open`, `shell_input`, `shell_signal`, `shell_reset` |
| Backups | `backup_files`, `restore_files` |
| Remote | `start_remote_session`, `install_virtual_display`, `uninstall_virtual_display`, `set_virtual_display_mode`, `refresh_remote_inventory` |
| Processes | `kill_process`, `restart_process` |
| Files | `list_directory`, `file_operation`, `fetch_file`, `push_file`, `open_item`, `scan_disk_usage` |
| Firmware | `refresh_bios_inventory`, `set_bios_settings`, `update_bios` |
| Network | `wake_machine`, `prepare_wake`, `network_sweep` |

`install_driver` is still registered as a `StubExecutor` that always fails; `update_bios` is real
(it was a stub until 3.20.1). **Not every feature is a command**: the inventory reporters
(software, patches, BIOS, NICs, posture, BitLocker, events, disk usage) and the watchdog runner
push change-only data on the heartbeat or their own POST, driven by a document or a flag the hub
sent back. See the class suffixes in [../CLAUDE.md](../CLAUDE.md): `*Executor.cs` is a command,
`*Reporter.cs` pushes change-only data, `*Reader.cs` is a local read-only probe.

**Still signed, and unrelated to the above:** the self-update manifest. `SelfUpdater`
verifies it with `SignatureVerifier.VerifyRaw` against `AgentConfig.UpdatePublicKeyHex`
before any binary replaces the running one. That is what stops a compromised hub from
pushing malicious code to the fleet — do not remove it.

## Configuration

Settings are read as `FLEETHUB_*`, falling back to the pre-rename `TEMP_MONITOR_*`
name — machines installed before the FleetHub rename still have the old machine-level
env vars set, and an agent that self-updates must keep honouring them or a box pinned
at a non-default hub silently swings back to the production default.

- `FLEETHUB_HUB` (legacy `TEMP_MONITOR_HUB`) — hub base URL (default
  `https://your.hub.url`).
- `FLEETHUB_MACHINE` (legacy `TEMP_MONITOR_MACHINE`) — machine name (default
  `Environment.MachineName`).
- `AGENT_ENROLLMENT_SECRET` — enrollment secret (installer writes it to
  `HKLM\SOFTWARE\FleetHub\Agent`, legacy `HKLM\SOFTWARE\TempMonitorAgent`; env
  overrides for testing).
- `FLEETHUB_NO_UPDATE=1` (legacy `TEMP_MONITOR_NO_UPDATE`) — disable self-update (testing).

State lives in `%ProgramData%\FleetHub\Agent` (`agent.json`, `config.json`,
`restart_state.json`, `update/`, `remote/`). A pre-rename
`%ProgramData%\TempMonitorAgent` is migrated on first touch, and kept if the migration
fails rather than starting from a blank identity.

`agent.json` is the enrollment identity, and it outlives the install -- the uninstaller
keeps the rest of this tree for its logs. It is **dropped in two cases**: the uninstaller
deletes it (both paths), and the agent deletes it itself when the hub answers a heartbeat
with `401 {"reason": "unknown"}`, meaning the hub has no row for this agent and the machine
should enroll again. `{"reason": "revoked"}` is the opposite instruction and the identity is
kept, so a revoked agent stays down instead of re-enrolling itself. A 401 with no reason at
all -- a hub older than 1.112.0 -- is treated as `unknown`; see `ShouldReenrollAfter401`.

## Build / test / publish
```powershell
dotnet test  agent/TempMonitorAgent.slnx
dotnet publish agent/src/TempMonitorAgent/TempMonitorAgent.csproj -c Release -o agent/dist
```

## Release + self-update

**Automated (recommended):** `agent/release.ps1` runs the whole flow — bumps the
version in `AgentConfig.cs` + the `.csproj`, publishes, creates/reuses the GitHub
release `agent-v<version>`, signs the manifest against the exact asset URL, uploads
the exe, and commits the manifest + `.sig`. Requires `gh` CLI, authenticated
(`gh auth login`), and a working `git push` from wherever you run it.
```powershell
agent/release.ps1 -Version 3.0.1 -DryRun         # print the plan, touch nothing external
agent/release.ps1 -Version 3.0.1                 # do it; prompts before pushing
agent/release.ps1 -Version 3.0.1 -Push           # do it, push without prompting
```

**Manual, step by step** (what the script above automates):
1. Bump `Version` in [AgentConfig.cs](src/TempMonitorAgent/AgentConfig.cs) and
   `<Version>` in [TempMonitorAgent.csproj](src/TempMonitorAgent/TempMonitorAgent.csproj)
   — keep them in sync. Which digit moves is [VERSIONING.md](../VERSIONING.md); the short
   version is that anything the hub can gate on takes a minor.
2. `dotnet publish … -o agent/dist` (single-file exe).
3. `python sign_release.py --sign-agent --file agent/dist/FleetHubAgent.exe \
      --agent-version <v> --agent-url <release-asset-url>` → writes and signs
   `agent/agent.manifest.json` (+ `.sig`). The `--agent-url` must exactly match where
   you upload the exe in the next step — it's baked into the signed manifest.
4. Commit the manifest + `.sig` together (pinned `-text` in `.gitattributes`) and upload
   the exe to that exact release-asset URL. The running service checks the manifest
   weekly (and on a hub `latest_version` hint), verifies the signature, hash-checks the
   binary, renames the running exe aside, drops the new one in, and exits code 17 so the
   SCM restarts onto it. **If you skip signing, fleet updates stall** — the signature
   check fails closed.

## Install (elevated PowerShell)
```powershell
agent/install/agent-install.ps1 -EnrollmentSecret <secret> -HubUrl https://your.hub.url
agent/install/agent-install.ps1 -AgentExe .\dist\FleetHubAgent.exe -EnrollmentSecret <secret>
agent/install/agent-install.ps1 -AgentUrl <release-url> -EnrollmentSecret <secret> `
    -HubUrl https://your.hub.url
agent/install/agent-install.ps1 -Uninstall
```
With neither `-AgentExe` nor `-AgentUrl`, the installer reads the signed stable manifest,
verifies it against the fleet key and checks the download against the signed sha256 -- the
same checks `SelfUpdater` applies -- and refuses to install otherwise. `-ManifestUrl` reads
the manifest from a mirror (still verified). `-AgentExe` and `-AgentUrl` are **not** checked
against the key, and say so. The binary is fetched before the running service is stopped,
so a refused download leaves the old agent running.

`-InstallDir` defaults to `C:\Program Files\FleetHub\Agent`. The Windows service is
**`FleetHubAgent`**. A PC installed before 2026-10-12 is registered as `TempMonitorAgent`
until the installer runs on it again, which migrates it (new service first, old one deleted
only after that worked); `-Uninstall` removes either. Self-update never re-registers the
service, and does not need to: the SCM ignores the name an own-process service reports, so
the same binary runs under both. The exe is **`FleetHubAgent.exe`** since 2026-10-13, under
the same rule: the installer renames an existing `TempMonitorAgent.exe` (and removes the old
file only once the service points at the new one), while self-update writes each new build
over whichever path is running, so a PC nobody re-installs keeps the old name. Why the fleet
is allowed to stay mixed: ROADMAP.MD, *Agent service renamed to `FleetHubAgent`* and *Agent
exe renamed to `FleetHubAgent.exe`*.

Logs: `%ProgramData%\FleetHub\Agent\companion.log` — kept under that name for the same
reason as the `companion_version` wire field: it is what's already on disk across the
fleet. Plus `remote-helper.log` beside it for the remote view/control
helper (it runs in a different session, so it gets its own file).

Self-tests runnable on the machine, no hub or browser involved:
`--desktop-probe [seconds]` (which input desktop, and can this process attach to it),
`--remote-capture-test <file.h264> <seconds> [monitor] [fps] [kbps] [encoder]`
(a playable clip straight from the capture + encode path; `encoder` is auto, hardware
or software, which is how you tell a broken hardware MFT from a broken pipeline),
and `--recording-badge-test [seconds]` (shows the session-recording badge, and follows the lock
screen when run as SYSTEM in the console session).

## Enrolling

Either with the fleet-wide `AGENT_ENROLLMENT_SECRET` (the installer writes it to
`HKLM\SOFTWARE\FleetHub\Agent`), or without it: a PC given a single-use **onboarding code**
installs with no secret, then receives its own credential, written to `agent.json` readable only
by SYSTEM and Administrators (roadmap #29, hub 1.144.0). An agent that finds `agent.json` counts
as enrolled before it ever looks for a secret. When the hub deletes a device with *Also uninstall
the agent* ticked, the next three heartbeats answer `decommissioned` and `Update/SelfUninstaller.cs`
removes the service, the install folder and the local data (roadmap #30).
