# Installing and updating FleetHub

## Installing the agent

See [agent/README.md](../agent/README.md) for the full C#/.NET agent: build/publish,
signing/release process, and `agent/install/agent-install.ps1` (installs the Windows
Service, the PawnIO sensor driver, and SCM failure-recovery for self-updates).

### Onboarding a PC with a code (no enrollment secret)

A Windows PC can enrol itself without the fleet-wide `AGENT_ENROLLMENT_SECRET`
(roadmap #29):

1. In the console, **Administration → Onboarding → New code**. Give it a label, pick how
   long it lasts (1 hour, 1 day or 7 days), and copy the six-character code or the link.
   Creating codes needs the **Manage onboarding codes** capability.
2. On the PC, open `https://<your hub>/onboarding` (or the link) and enter the code.
3. Click **Download installer** and open the file. Accept the administrator prompt.
   A technician can instead paste the PowerShell command from the same page into an
   elevated PowerShell.
4. The page shows the PC as online once its agent checks in.

The installer gives the PC its own credential. The shared secret never reaches the PC, and
the credential file (`%ProgramData%\FleetHub\Agent\agent.json`) is readable only by SYSTEM and
Administrators. Each code works once. The Onboarding list shows which machine used a code and
from which IP. **Revoke** on a used code also revokes that machine's agent.

`HUB_URL` must be the hub's public `https://` address; the script refuses anything else.

### Telemetry only from enrolled agents

Agents sign every telemetry report with their own credential. **Settings → Security → Only
accept telemetry from enrolled agents** refuses reports that are not signed. **It is off by
default** until every agent in the fleet is new enough to sign. While it is off, unsigned
reports are still accepted, except for a machine whose agent has already sent a signed one.
**Administration → Onboarding** lists machines that sent unsigned reports in the last day:
each one either runs an agent too old to sign, or was never enrolled. Turn the switch on once
that list is empty. See ROADMAP #29.

## Unified installer

`install.ps1` at the repo root is a single menu-driven installer covering every
component -- Agent, Hub, and the TURN relay. Run it with no arguments
for an interactive menu; it prompts for whatever the chosen path needs
(enrollment secret, hub URL, OAuth creds, ...), defaulting to values already in
a local `.env` when run from a clone. The installer elevates itself
automatically if not already run as admin.

**From the web:**

```powershell
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/aw08-2004/FleetHub/main/install.ps1)))"
```

**Non-interactive**, pass `-Component` plus the relevant parameters (`iex`
alone can't take arguments, so invoke the fetched script as a scriptblock
instead). Anything supplied on the command line is used as given and never
prompted for again, so a fully-specified invocation runs start to finish
without input:

```powershell
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/aw08-2004/FleetHub/main/install.ps1))) `
    -Component Agent -HubUrl https://hub.example.com -EnrollmentSecret <secret> -AddDefenderExclusion"
```

Note `-HubUrl` — the address of the hub the agent reports to. `-AgentUrl` is a
different thing: the download URL of a *specific* agent release asset, only
needed to pin a version, and **not checked against the fleet's signing key**.
Left out, the installer reads the signed `agent/agent.manifest.json`, verifies
it against the fleet key and installs only a binary whose sha256 matches the
signed one -- the same check every self-update applies. A local `-AgentExe` is
not checked either. A `-AgentUrl` that isn't a `.exe`/`.zip` asset is assumed to be the hub
URL and used as such, with a warning.

**From a local clone:**

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1                                   # interactive menu
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Turn                    # TURN relay only, no hub
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -InstallDir "D:\Apps\FleetHub\Agent"
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -Uninstall
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion -Uninstall    # remove a legacy Python companion
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall                        # bare -Uninstall = the same, for back-compat
```

`-InstallDir <path>` applies to whichever component is being installed. Left
out, each uses its own default — Agent `C:\Program Files\FleetHub\Agent`, Hub
`C:\Program Files\FleetHub\Hub`, legacy Companion `C:\Program Files\TempMonitor`.
Passed explicitly, it redirects the install *and* the Windows Defender
exclusion offered for it. Pass the same `-InstallDir` to `-Uninstall` so the
right directory is cleaned up.

Component-specific parameters: `-HubUrl` / `-EnrollmentSecret` / `-AgentUrl` / `-AgentExe` /
`-AddDefenderExclusion` / `-SkipDefenderExclusion` (Agent); `-HubPort <port>`
(default `3001`) and `-HubInstallDir <path>` (Hub — takes precedence over
`-InstallDir`); `-TurnHost` / `-TurnSecret` / `-TurnDistro` / `-TurnPort` /
`-SkipTurn` (Turn — and the same flags apply to Hub, which can set the relay up
as part of its own install).

The Defender exclusion is otherwise an interactive y/N question, defaulting to
no. `-AddDefenderExclusion` and `-SkipDefenderExclusion` pre-answer it for
unattended runs; passing both is an error.

### Installing the TURN relay on its own

`-Component Turn` provisions **only** the coturn WebRTC relay, in its own Ubuntu
WSL2 distro, with no hub. Use it when the relay belongs on a different machine from
the hub — typically one with a better public address, or when the hub runs on Linux
and you want the relay on a Windows box you already have ports forwarded to.

It asks the same questions and enforces the same preconditions as the hub's optional
TURN step (they share one code path), then prints the `turn:` / `stun:` URLs and the
shared secret to paste into **Settings → Remote Control** on the hub. If a hub *does*
happen to live on the same machine, it defaults to the secret already in that hub's
`.env`, so the two can't drift apart.

> The shared secret must match `REMOTE_TURN_SECRET` on the hub **exactly**. A mismatch
> fails every allocation with 401 and remote sessions simply never connect.

Removing it (`-Component Turn -Uninstall`) leaves the hub untouched. Uninstalling the
Hub still removes the relay too, if that machine has one.

Hub and Agent install side by side under one root — `C:\Program Files\FleetHub\Hub`
and `C:\Program Files\FleetHub\Agent`. The Hub installs as the **`FleetHub - Hub`
Windows Service** (Python wrapped with WinSW, running as LocalSystem).

**The hub needs 64-bit Python.** The installer checks each interpreter it finds and skips
32-bit ones (installing the 64-bit build alongside if that is all there is), and on a
reinstall warns if the existing service was running a 32-bit one. A 32-bit hub has a 2 GB
address space; with 128 waitress threads and 16 MB remote-relay buffers it throws
`MemoryError` on `/api/remote/session/<id>/relay/down` and `/api/report` while the machine
still has free RAM, and can then crash the WinSW wrapper into a start-timeout loop. To check
which interpreter a running hub uses, read `<executable>` from `FleetHub.xml` in the hub
directory and ask the Python beside it (for `Scripts\waitress-serve.exe`, the `python.exe`
one level up):

```powershell
& "C:\Program Files\Python312\python.exe" -c "import struct, sys; print(struct.calcsize('P')*8, sys.executable)"
```

`32` means re-run `install.ps1 -Component Hub`, which moves the service to a 64-bit
interpreter, then uninstall the 32-bit Python.

The Hub install downloads the branch archive and lays down just the `hub/` subtree
(~2 MB: the Python modules, `templates/`, `static/`, `requirements.txt`) rather than
cloning the whole repo (~85 MB), so the agent tree, tests and docs never land on a
server. `git` is no longer required on the hub box.

Installs made before the FleetHub rename are detected and migrated: the old service is
removed, `.env` and `logs/` (including the telemetry DB) are moved to the new root, and
an existing agent's binary is moved with its service re-pointed at the new path.

## Removing a legacy Python companion

The old `companion.py` telemetry client was replaced by the agent and can no longer be
installed. If a machine still runs one, remove it first, then install the agent:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion -Uninstall
```

This removes its two scheduled tasks and its install directory (default
`C:\Program Files\TempMonitor`; pass `-InstallDir` if it is elsewhere). Do not run the
companion and the agent together: both report the same machine name, so every reading is
doubled.

## Self-updates

The agent checks its version against the signed release manifest on `main` and swaps
its own binary for a newer one when it finds it — see [agent/README.md](../agent/README.md)
for the release process. Updates are Ed25519-verified fail-closed (see
[Signing releases](#signing-releases) below): an unsigned or tampered manifest is
refused, not applied.

The binary it replaces is kept beside it as `FleetHubAgent.exe.old` (`TempMonitorAgent.exe.old`
on a PC installed before 2026-10-13 -- self-update keeps whichever name the PC has) until the new one
has **reached the hub at least once** — starting is not the same claim as working, and a
build that cannot report is one you cannot reach to fix. Nothing re-launches it
automatically (a build too broken to start is also too broken to decide that), but for the
window in which it matters the previous binary is still on disk to be renamed back.

The hub nudges this along: `/api/report` echoes back the newest agent version it has
read from the manifest, so an agent goes and checks as soon as it sees a number ahead
of its own instead of waiting for its own weekly poll.

### Hub self-updates (opt-in)

The hub can keep itself current too, but it's **off by default** — set
`HUB_AUTO_UPDATE=1` in the hub's `.env` to enable it (a dev clone left unset never
touches itself). The `hub.auto_update` setting overrides the `.env` value when set,
so it can also be flipped from the Settings tab. The hub checks
`HUB_VERSION` on `main` every 15 minutes either way — the *check* is unconditional, only
the *installing* is opt-in. When enabled and `main` is ahead it updates itself, re-installs
`requirements.txt`, then exits non-zero so the `FleetHub - Hub` Windows Service
auto-restarts waitress on the new code (WinSW `onfailure`, ~5 s downtime).

Two checks stand between "the files were replaced" and that restart, because the hub has
no restart-count guard and cannot roll itself back once it has exited. It re-reads the
`HUB_VERSION` in the tree it just wrote and refuses to restart unless it is genuinely
newer — the version is read from `raw.githubusercontent.com` and the code comes from
`codeload`, so the two can disagree for a few minutes after a push. And a failed
`pip install` blocks the restart **only when `requirements.txt` actually changed**: a hub
with no route to an index still takes the many releases that add no dependency, rather
than stalling on all of them.

**When self-update is off**, that same check feeds a notice at the bottom of the left
sidebar: *"Hub vX.Y.Z is available"*, with an **Update now** button that applies the
update once and restarts, exactly as the watcher would have. Both the notice and the
endpoints behind it (`GET /api/hub/version`, `POST /api/hub/update`) are gated on
`manage_settings` — the same grant that can flip `hub.auto_update`, since it is the same
decision — and every manual update is written to the audit log at security level. The
notice can be dismissed per-browser; it comes back for the next release, and it stays out
of the way entirely while `hub.auto_update` is on. Its **Release notes** link opens
`deploy/docker/release-notes/<version>.md` on the channel's branch.

Under it, a second notice announces each new **agent** release on the fleet's default
channel, with a link to that version's GitHub release. It is only a notice: agents install
the release themselves within about 15 minutes, whether or not anyone reads it. It is
dismissed the same way and comes back for the next agent release.

How it updates depends on the layout, decided by whether a `.git` directory is present:

- **Files-only install** (what the installer now produces): downloads the branch
  archive, builds the new `hub/` **beside** the installed one, and renames it into place —
  a whole directory at a time, not a hand-maintained file allowlist, because an allowlist
  that missed a new module once left the hub crash-looping. Everything slow happens off to
  the side and the cutover is two renames, so the hub is never seen serving out of a tree
  that is half one version and half the other; a download that turns out to be truncated,
  stale or uninstallable costs nothing at all. The tree it replaces is kept as
  **`hub.prev`** next to `hub/` — the hub cannot roll itself back, so if a release will not
  start, renaming that directory back is the recovery. One is kept, replaced each update.
  `.env`, `logs/` and the service wrapper live one level up in `STATE_ROOT`, outside the
  swapped directory, so they are structurally out of reach — and if `HUB_STATE_DIR` or
  `HUB_LOG_DIR` has been pointed *inside* the code directory, the update refuses to run and
  says so rather than moving your database aside with the old code.
- **Git clone** (dev checkouts, and hubs deployed before the change): `git fetch` +
  `git reset --hard origin/main`, mirroring `main` exactly — **local changes on the
  hub box are discarded**. Requires `git` on `PATH`.

Unlike the agent train, neither path uses the Ed25519 release key: both
trust GitHub over HTTPS plus push access to `main` (the pinned git origin for a clone,
the branch archive over TLS for a files-only install). The Ed25519 trust root still
gates agent binaries and is untouched by this. As with every hub change, bump
`HUB_VERSION` near the top of `hub/app.py` on each push to `main`, or the hub won't know
to update — see [VERSIONING.md](../VERSIONING.md) for which digit to move, and why the
format is narrower than it looks. (The installer offers to set
`HUB_AUTO_UPDATE=1` for you; on hubs still on the older scheduled-task deployment the
same exit instead relies on the task's 2-minute repetition.)

**None of this applies to a hub running in Docker** — see the next section. There the image
is the release, and the hub never updates itself.

### Running the hub in Docker

The hub also ships as a public container image, `ghcr.io/aw08-2004/temp_monitor-hub`, built
by `.github/workflows/hub-image.yml` on every push to `main` that touches `hub/`. It is public,
like the repository it is built from, so the hub host needs no `docker login`. It runs on
Docker Engine (Linux) or Docker Desktop (Windows). Agents need no change as long as the
public `HUB_URL` stays the same: they know only that URL and pin no certificate.

Start from the template beside the compose file, fill in the required values, then start:

```bash
cp deploy/docker/example.env deploy/docker/.env
```

```bash
docker compose -f deploy/docker/compose.yaml up -d
```

| Tag | Follows |
|---|---|
| `X.Y.Z` | exactly that `HUB_VERSION`, never rebuilt |
| `latest` | `main` |
| `beta-X.Y.Z`, `beta` | the `beta` branch |

Set `HUB_TAG` (for example `HUB_TAG=1.143.0`) to choose when a release goes in.

**Updating is pull and recreate.** The image sets `HUB_UPDATE_MODE=image`, which switches off
every in-place update path: the watcher, **Update now**, and the boot-time self-heal. They
are switched off because an in-place update would write into the container's own layer and
vanish on the next recreate, leaving the hub quietly back on an older version. The sidebar
still announces a new version, but says to pull the image instead of offering a button.
`hub.auto_update` and `HUB_AUTO_UPDATE` are ignored.

```bash
docker compose -f deploy/docker/compose.yaml pull
```

```bash
docker compose -f deploy/docker/compose.yaml up -d
```

**All state is one volume**, `/state`: `.env`, `logs/` (the database, file spool, disk
history, packages, firmware and the encrypted secret store) and `data/` (recordings).

**Two `.env` files, with different jobs.** `deploy/docker/.env` on the host is passed in via
`env_file:` and holds the bootstrap values: `FLASK_SECRET_KEY`, `ALLOWED_EMAILS`, `HUB_URL`,
the agent secrets, SMTP. `/state/.env` on the volume is the one the hub **writes**: it adds
`BACKUP_MASTER_KEY`, and the Settings pages save sign-in, TURN, AI and directory values there.
**Keep those console-managed keys out of the host file.** The hub never lets `/state/.env`
override a variable that is already set, so a host value wins: a console change looks saved,
then reverts on the next recreate. `example.env` lists them. Don't set `HUB_STATE_DIR`,
`HUB_LOG_DIR` or `HUB_RECORDINGS_DIR` either; the image owns them.

- **Named volume by default.** SQLite on a Docker Desktop bind mount is slow and its locking
  is unreliable. On a Linux host a bind mount is fine, owned by uid `10001`.
- **Exactly one container.** The schedulers, the database writer and Socket.IO's state live
  in the process. A second replica, or waitress workers instead of threads, would run every
  scheduled job twice.
- **Behind the same proxy as before.** The port is published on loopback only. TLS stays at
  the Cloudflare tunnel or nginx, which must not buffer responses (agents long-poll for
  commands).
- **Wake-on-LAN:** on the default bridge network the hub is never on a target's subnet, so
  only agent-relayed wake works. That is the primary path anyway. On a Linux host,
  `network_mode: host` in the compose file restores the hub's own magic packet.
- **TURN** is unchanged: `turn/docker-compose.yml` on Linux, or the installer's WSL relay on
  Windows.
- **Dependencies are locked for the image.** `hub/requirements.txt` stays unpinned for the
  Windows service. The image installs `hub/requirements.lock` instead: exact versions, every
  wheel hash-checked, wheels only. After changing `requirements.txt`, run
  `python tools/lock_hub_requirements.py` (it needs Docker) and commit the new lock.
  `--check` names anything a stale lock is missing. The image workflow runs it before every
  build, and so does the hub test suite.

**Moving an existing Windows hub into Docker:**

1. Stop the `FleetHub - Hub` service. Leave it installed; it is the way back.
2. Create the volume, and copy `.env`, `logs\` and `data\` from
   `C:\Program Files\FleetHub\Hub` into it with a one-off container. Then `chown -R
   10001:10001 /state` from that container, and remove any `HUB_*_DIR` lines from the copied
   `.env`. It keeps working as the hub's own store. `deploy/docker/.env` must still exist for
   compose: copy the bootstrap keys into it, or leave it as just `HUB_TAG`.
3. Run `docker compose up -d`, and point the tunnel or proxy origin at port 3001 on the
   container host.
4. Check that agents keep checking in. They need no reconfiguration.

To roll back, stop the container, copy `logs\` back, and start the service. Database writes
made after the cutover are lost unless they are copied back too.

## Where the hub keeps its data

Data is persisted to `logs/temp_v2.db` (SQLite) with optional CSV archiving;
rotated log files also live under `logs/`. The code lives in `hub/`; `.env` and
`logs/` sit one level up in the install root (`STATE_ROOT`), so a self-update that
replaces the code directory can never touch operator state. Run it via
`hub/wsgi.py` (`wsgi:application` under waitress), or directly with:

```powershell
python hub/app.py
```

## Signing releases

One artifact in this repo is Ed25519-signed so a compromised hub or repo commit
can't push code that runs as admin fleet-wide unverified:

- **The C# agent** (`agent/`) — the self-update manifest `agent/agent.manifest.json`
  carries the version, the exe's sha256 and its download URL, and is signed
  byte-for-byte. See [agent/README.md](../agent/README.md) for `agent/release.ps1`
  (automates the whole release: version bump, publish, GitHub release, sign, upload)
  or the manual `sign_release.py --sign-agent` steps. `.gitattributes` pins the
  manifest and its `.sig` to `-text` so git never rewrites line endings — the signed
  bytes must match exactly what the fleet downloads.

One-time setup: `python sign_release.py --genkey`, keep the private key OFF the
repo, paste the printed public key into the agent's `AgentConfig.UpdatePublicKeyHex`.

This is the **release** trust root: it governs what *code* the fleet is allowed to run,
and it is fully enforced. It is unrelated to fleet *commands*, which are no longer signed
(see [Fleet command channel](managing-machines.md#fleet-command-channel-rmm)). Don't conflate the two — a
compromised hub still must not be able to push a malicious binary, which is exactly what
these signatures prevent.
