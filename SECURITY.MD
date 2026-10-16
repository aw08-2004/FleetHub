# Security Policy & RMM Justification

## What This Software Does

FleetHub is an **enterprise-grade Remote Monitoring and Management (RMM) platform** 
for IT administrators. It is designed, documented, and licensed for legitimate 
system administration use cases:

- CPU temperature monitoring
- System telemetry reporting  
- Remote fleet management (restart, rename, package deployment)
- Backup scheduling and recovery
- Remote desktop view/control with user consent
- Audit logging of all administrative actions

## Transparency & Security

- **Source Code:** Fully open-source (GPL-3.0), auditable on GitHub
- **Signed Artifacts:** All agent updates are Ed25519-signed; manifests are verified fail-closed
  (the Windows, Linux and Android agents each have their own, and so does the desktop client)
- **Audit Trail:** Every RMM command is logged with operator email + full params
- **Operator Control:** Remote code execution requires an authorized OAuth/OIDC sign-in, or a
  device token minted by one (see "Device tokens" below)
- **Signed Releases:** release **manifests** are signed with one offline Ed25519 key and the
  binary is hash-checked against the signed sha256. The executables themselves are **not**
  Authenticode-signed (no code-signing certificate exists), so SmartScreen and some antivirus
  products will warn on a fresh download; the trust root is the offline key, not a CA
- **HTTPS-only:** All hub-to-agent communication over TLS

## RMM Legitimate Use

Remote code execution (RCE) is fundamental to RMM — it's how administrators manage fleets. 
Examples:
- Deploy patches across 100 machines
- Run diagnostics on remote hardware
- Execute repair scripts as SYSTEM
- Restart services fleet-wide

This is standard for tools like PDQ Deploy, ConnectWise Manage, Microsoft SCCM, and others.

## Risk Mitigations

1. **Enrollment:** Agents enroll once, with the shared secret or with a single-use onboarding
   code. Either way, each agent gets its own token, and only enrolled agents accept commands.
   Telemetry is signed with that token too, and the hub can refuse unsigned reports.
2. **Authentication:** Issuing commands requires a valid OAuth/OIDC sign-in (Google, or any
   configured OIDC provider) against the email allow-list or a Permission Group
3. **Audit Log:** Every action recorded: who did what, when, on which machine
4. **SYSTEM Context:** Commands run as SYSTEM; access to this capability is intentionally powerful and scoped to trusted admins
5. **Machine Scope:** Operators can be limited to specific machines via Permission Groups
6. **Signed Updates:** Binary replacements verified before execution
7. **Bounded Invites:** An invite link grants only Permission Groups, never more than
   its creator holds, and is bounded by a seat count, an expiry and optional pinned
   addresses (see "Invite links" below)

## Personal data inventory

Most of what FleetHub stores is about a machine: its temperature, its disks, its BIOS
settings, which updates it is missing. One thing it stores is about a **person**, and it is
inventoried separately here so that nobody has to infer it from a feature list.

| What | From whom | Why | Where | How long | Who can see it |
|---|---|---|---|---|---|
| **Device position** — latitude, longitude, accuracy radius, provider, and when the fix was taken | Managed Android devices, **only when an operator asks**. Never sampled, never periodic, never in the background | Finding a lost or stolen device, and confirming where a company device is before securing it | `machine_locations` in the hub's SQLite database | `data.location_retention_days`, default **30 days**, then deleted by the retention pruner. Deleting a machine erases its history immediately | Reading: `view` **plus** the machine being in the operator's scope. Asking for a new fix: the separate `locate_device` capability |
| **Installed applications** — package name, label, version, and whether each is enabled or suspended | Managed Android devices, on the inventory loop | Writing an app policy at all: blocking an app means naming a package, and nobody types `com.zhiliaoapp.musically` from memory. Also how a partly-applied policy becomes visible | `machine_apps` in the hub's SQLite database, replaced on each report | Held while the device is enrolled, replaced on each report. Deleting a machine erases it immediately | `view` **plus** the machine being in the operator's scope |
| **App usage** — foreground seconds per package, per the device's own local day | Managed Android devices, **only where somebody has granted usage access on the device itself** | Enforcing daily screen-time budgets, and showing an operator why a budget did or did not bite | `machine_usage` in the hub's SQLite database | `data.usage_retention_days`, default **14 days**, then deleted by the retention pruner. Deleting a machine erases it immediately | `view` **plus** the machine being in the operator's scope |
| **File and folder names, with their sizes, over time**: the full path of every folder and file on each fixed NTFS drive, including other users' profiles | Managed Windows PCs with agent 3.40.0 or newer, by a daily read of the drive's file table (roadmap #27). File contents are never read | Showing what is filling a disk and when it will be full, and comparing a folder with an earlier day | One SQLite file per machine and drive under `logs/disk_history/`, outside the main database; the latest two trees also stay on the PC in `%ProgramData%\FleetHub\Agent\diskusage`, readable by SYSTEM and Administrators only | `data.disk_usage_daily_days` (default **90 days**) daily, then weekly until `data.disk_usage_keep_days` (default **365 days**), then folded into one baseline. Deleting a machine erases it immediately | `issue_commands` **plus** the machine being in the operator's scope, the same gate as browsing the machine's files |
| **Screen recordings** -- video of a Windows PC's screen during a remote session, with the operator's stated reason, who recorded, which PC and for how long. Video only: keystrokes are never recorded | Managed Windows PCs with agent 3.43.0 or newer, **only while an operator records a session they opened**, ten minutes at a time unless extended. The PC shows a "Recording Screen" badge throughout, on the lock and logon screens too, and the hub stores no video until the agent confirms the badge is up (roadmap #19) | Evidence of what was done on a PC, for the audit trail and for colleagues | One `.webm` file per recording under `<install>\data\<owner>\recordings\`, a folder the hub restricts to SYSTEM and Administrators at every start; metadata in `recordings` in the hub's SQLite database | **Until the owner deletes it** -- no expiry, by the owner's decision. The Recordings page shows each owner their total so they know when to delete | The operator who made it, and the permission groups and people they share it with (a group share follows the group's current membership). Only the owner shares or deletes; nobody re-shares. Every start, end, extension, share, download and delete is audited, and so is every access to the video (`recording_view`, once per viewer per recording per half hour) |
| **Windows event log records**, which can name people: a Security-channel 4625 names the account somebody typed a password for | Managed Windows PCs with agent 3.37.0 or newer, **only the channels, event ids and levels an operator subscribed to** (Events page, `manage_settings`); a hub with no subscriptions collects nothing. Nothing is back-filled | Finding who is being brute-forced, and counting events for a rule | `events` tables in the hub's SQLite database; identical repeats within five minutes are one row with a count | `data.event_retention_days`, default **14 days**, plus a per-machine row cap enforced as records arrive | Reading: `view` plus the machine being in scope. Choosing what is collected: `manage_settings` |
| **Assistant conversations** -- what an operator asked, and what the assistant read to answer, which includes real machine names | Operators who use the console assistant (off until an admin turns it on in Settings → AI). The same text is **sent to the AI provider the hub is configured with**, which is a third party unless it is self-hosted | Answering questions about the fleet, and doing work as the operator | The hub's SQLite database, private to the operator who had the conversation | `ai.assistant_history_days`, default **30 days**, then deleted | The operator who had it. The assistant can read only what that operator's own session can, and is denied recovery keys, API keys, device tokens, the interactive terminal and every recording route |

Four properties of the device-position row are deliberate and worth stating plainly:

- **Collection is on demand only.** There is no background location, no periodic sample and no
  travel history. The only history that exists is a history of *when somebody asked*, which is
  a different and much smaller thing.
- **Asking has its own capability.** `locate_device` is not part of `issue_commands`, which
  every other command-issuing feature reuses. Being able to reboot a PC does not imply being
  able to find out where an employee is. The hub refuses a hand-rolled `locate_device` through
  its generic command endpoint, and refuses to save one as a favorite, so the capability cannot
  be routed around.
- **The person holding the device is told.** The agent posts a notification naming the operator
  who asked, every time — including when it has no position to give, so that a failed request
  cannot be used as a quiet way to check whether a phone is switched on.
- **Every request is in the audit trail** with the operator's address, the device, and the time.

The usage row is the most personal thing in this table, and its properties are stated with the
same care:

- **It exists to make a budget enforceable, not to produce a report.** A screen-time budget has
  to be checked against something, and it is checked on the device, every minute, whether or not
  the hub is reachable. What reaches the hub is the same ledger, so that an operator can see why
  an app was suspended at 15:40.
- **There is no fleet-wide view, deliberately.** The app inventory has one, because a policy
  author has to pick a package from somewhere. Usage has none: the question it would answer at
  fleet scale is "who spends the most time on their phone", and this product does not make that
  easy to ask. The only endpoint names one machine and checks scope.
- **The device cannot measure it unless somebody grants it, by hand, on that device.** Usage
  access on Android is an appop; a Device Owner cannot grant it silently and this agent does not
  try. A device where it was never granted reports nothing, enforces blocked hours normally, and
  says so in the console rather than reporting zeroes that would read as "nobody used this".
- **Retention is shorter than location's and is meant to come down.** Fourteen days by default,
  pruned by the DEVICE's own local day rather than by a hub timestamp, so a device in a timezone
  ahead of the hub does not have its current day pruned out from under it.

**FleetHub cannot give your workforce notice on your behalf.** Whether locating a
company-owned device is lawful where you operate, what you must tell the people carrying those
devices, and what basis you rely on are decisions for the deploying organisation. The product
provides the controls — a separate capability, machine scope, short retention, an audit trail
and an on-device disclosure — and no more than that.

**Not collected**, and each absence is a decision rather than a gap: no background or periodic
location, no browsing history, no message or call content, no contacts,
no photographs, no keystrokes, and no microphone or camera access of any kind. Screen sharing
(roadmap #2) exists on Windows and requires a system prompt the person at the machine accepts
for every session. Screen RECORDING (roadmap #19) is the one exception to "nothing is kept":
it is video only, it is never silent -- the badge above is on the PC's screen for its whole
length -- and it is in the table above for that reason.

## What a managed Android device grants, and what it does not

A device provisioned by QR after a factory reset makes this agent its **Device Owner**, which is
the strongest position an app can hold on Android. What that position is actually used for is a
short list, and it is written down here so that a later addition is made on purpose rather than
by reflex.

**Taken:**

- **Blocking user control of the agent** (`setUserControlDisabledPackages`), so the person
  holding the phone cannot force-stop it from Settings. Without this the whole feature set is
  optional from the device's side, and a stopped agent is indistinguishable from a flat battery.
  It stops a *person*; it is not a fix for an OEM power manager, which runs inside the system.
- **Enforcing automatic time** (`setAutoTimeEnabled`). Curfews and budgets are evaluated on the
  device against the device's own clock, which is the only way a phone with no signal at 22:00
  still has a bedtime. That is only defensible if the clock is not a setting somebody can move.
  Nothing *sets* a time, and the time zone stays the device's own.
- **Suspending applications** (`setPackagesSuspended`), which greys the icon and shows the
  system's own "paused by your organisation" message rather than making an app vanish. An
  unconditional never-suspend list on the agent — the launcher, dialer, settings, emergency and
  the agent itself — is enforced on the device, not by the hub, so a compromised or simply newer
  hub cannot brick a phone.
- **Reading the full package list**, which a Device Owner gets without the broad
  `QUERY_ALL_PACKAGES` permission that Play policy flags.
- **Locking the screen** (`lockNow`), undone by the person holding the device with their own
  PIN, and **erasing the device** (`wipeData`). The second is the only action in this product
  with no undo, so it is gated on its own `wipe_device` capability, requires the machine's name
  typed out, is written to the audit trail *before* the command is created, and cannot be saved
  as a favorite or issued by a rule. The device answers once and then stops reporting for good,
  which is why the hub records the request rather than waiting for a result.

**Deliberately not taken**, each available and each declined: no camera or screenshot
restrictions, no factory-reset block, no certificate installation, no always-on VPN, no keyguard
restrictions, no account-management restrictions, and no background location. Usage access is
not taken either — it cannot be, and the agent does not pretend otherwise.

**The provisioning QR is a fleet credential.** It carries the APK's download URL and signature
checksum and the enrollment secret; anyone who can photograph it can enrol a device into this
fleet. Treat it like the keystore: generate it when a device is being provisioned, do not paste
it into a ticket, and rotate the enrollment secret if one escapes.

**The hub hosts the agent APK, on the one route in this product that has no gate.** The setup
wizard of a factory-reset device has no session, no agent token and nothing to enrol with — it
is downloading the very app that would later enrol it — so the download cannot be
authenticated. What is worth knowing about it:

- **What the file is:** the signed FleetHub Android agent, a release binary. It carries no fleet
  data, no hub address, no machine list and no credentials. The hub URL and the enrollment
  secret ride in the QR's admin-extras bundle, never in the APK, so downloading it enrols
  nobody and tells the downloader nothing about this fleet.
- **What the URL's token is:** anti-enumeration and a revocation handle, not a security
  boundary. It is looked up rather than joined to a filesystem path, so the route can only ever
  serve the one APK recorded as hosted. Uploading again or removing the APK mints a new token
  and every code printed before it stops resolving — which is how a QR photographed off a bench
  is killed.
- **What is not there:** this route has no rate limiting, and it will serve about ten megabytes
  to anyone holding the token. (The hub does rate-limit three other unauthenticated or
  weakly-authenticated surfaces, with a small in-memory sliding window per client address:
  `/api/report` at 120 a minute, `/api/agent/enroll` at 10 a minute, and the public onboarding
  routes, which also have a global ceiling.) It sits under a single stable `/provisioning/apk/`
  prefix so a reverse proxy can throttle or block it in one rule, and "Stop hosting" on the
  provisioning page takes it offline entirely.

## Onboarding codes and signed telemetry (roadmap #29)

**An onboarding code is a one-time right to put one machine into this fleet.** It is six
characters long, so it can be read out over the phone. That is also why it is bounded on every
side:

- **Single use.** The claim is one conditional UPDATE, so two PCs racing on one code cannot
  both win.
- **Short-lived.** A code lasts 1 hour, 1 day or 7 days, and there is no "never".
- **Stored as a hash.**
- **Rate limited.** Each client is limited by `remote_addr`, not by the spoofable
  `X-Forwarded-For`, and there is a global ceiling on top.
- **One refusal for everything.** An unknown, used, expired, revoked or malformed code all get
  the same 404.

Using a code issues that PC **its own** agent token. The shared `AGENT_ENROLLMENT_SECRET`
never leaves the hub.

**The token never reaches a browser.** The public `/onboarding` page can only check a code,
which uses nothing up, and poll its status. The token is returned once, over HTTPS, to the
elevated install script running on the PC, after the agent has installed. The script then
writes it to `agent.json`, which is readable only by SYSTEM and Administrators. The script
refuses a hub URL that is not `https://`, and the agent binary is still verified against the
Ed25519-signed manifest.

**A leaked code is visible and reversible.** The Onboarding list shows which machine used each
code, from which IP and when. Creating, using and revoking a code are all security-level audit
rows, and **Revoke** on a used code also revokes the agent it enrolled.

**Telemetry is signed.** Agents send their own bearer token on `/api/report`. The hub handles
each case as follows:
- A bad or revoked token is refused.
- A valid token reporting under another enrolled machine's name is refused (403).
- A valid token reporting under a fresh name, one that no agent row (live or revoked) and no
  machine record has used, is treated as a rename. It is audited as `agent.rename`. Any other
  name is refused, so a stolen token cannot take over a machine that reports unsigned or was
  revoked.

A code also cannot enrol a machine name that a live agent already holds. The hub answers 409
and the code stays unspent. Otherwise, holding any code would be enough to mint a second
credential for an enrolled machine.

A name the hub already has records for, such as a machine that reports unsigned, can be
enrolled only by a PC presenting the BIOS serial that name reported. A record without a usable
serial is refused until an operator deletes it. The serial is not a secret: it ties
onboarding to the hardware without authenticating it.

`security.require_enrolled_telemetry` refuses unsigned reports. It ships **off**, because
hubs update before agents do, and should be turned on once the Onboarding page's list of
unsigned reporters is empty. Once a
machine's agent has signed a report, unsigned reports under that name are refused even when the
switch is off.

Until a hub has this on, `/api/report` input is still untrusted. That is why the
defence-in-depth around it stays: machine-name validation, the write-once BIOS serial, and the
enrollment check before a duplicate-serial merge.

## Uninstalling an agent by deleting its device (roadmap #30)

Asking for the uninstall needs `issue_commands` as well as the right to delete the device: it
runs a SYSTEM task on the PC, so the hub answers 403 to anyone without that capability instead of
quietly doing a plain delete. Deleting a device with "uninstall the agent" ticked keeps the deleted agent's token hash. When
that token next calls, the hub refuses it with `reason: decommissioned`, and the agent removes
itself. Only the real token holder is told; any other caller hears `unknown`.
- **Not on a single reply.** The agent acts only after three refusals in a row.
- **A protected script location.** The Windows uninstall script is written to a folder locked
  to SYSTEM and Administrators, and the Linux one to a root-only folder. Neither uses a temp
  directory any user can write to.
- **Known paths only.** The recursive deletes are limited to the agent's own install and state
  paths.

A compromised hub could uninstall agents this way. That is far less than the remote code
execution the same hub already holds over every agent.

## Device tokens (the native client)

The FleetHub desktop/mobile client authenticates with a **long-lived bearer token** rather
than a session cookie, because sign-in is OAuth/OIDC only and there is no password for an
app to hold. This is the largest change to the hub's security perimeter since fleet-command
signing was removed, so the controls around it are stated here rather than left implicit:

- **A token is minted only by a real sign-in.** Pairing follows RFC 8252's native-app flow:
  the app opens the *system* browser, the hub authenticates it through the ordinary OAuth
  path, and an operator explicitly confirms the device on a consent page. The app never
  sees credentials. Nothing is minted until a one-time, sixty-second, single-use code is
  exchanged, so an abandoned pairing leaves no credential behind.
- **Only the token's SHA-256 is stored.** There is no "reveal": the hub does not hold the
  value and cannot show it again.
- **A token carries a SUBSET of its owner's capabilities, intersected live on every
  request.** Removing somebody from a Permission Group disables their paired devices
  immediately — the credential can never outlive the grant.
- **Administrative capabilities cannot be granted to a device at all.** `manage_settings`,
  `manage_users`, `manage_permission_groups` and `manage_firmware` are refused at mint
  time, including for break-glass superusers. Console administration stays in the console.
- **Tokens expire and slide on use** (`hub.device_token_lifetime_days`, 90 by default), so
  a device that stops checking in loses access on its own.
- **Every mint, first use and revoke is audited at security level**, and any device can be
  revoked — by its owner, or fleet-wide by a `manage_users` holder — from the Users page.
- **A token can never be upgraded into a session cookie**, so it cannot be used to escape
  its own capability ceiling.
- **Client downloads are signed.** The console's Download Client page renders only a
  manifest whose Ed25519 signature verifies against the same release key that signs the
  agent, and publishes each build's SHA-256.

## Invite links

An operator can hand somebody a **link** that grants the Permission Groups it names, rather
than adding their address by hand (roadmap #22). A link that confers console access is a
bearer credential in the same sense a device token is, so the same rule applies — the
controls are stated here rather than left implicit:

- **An invite grants Permission Groups, never capabilities of its own.** Redeeming one adds
  the address to those groups, exactly as an admin typing it in would. There is no second
  authorization layer, and therefore no special case for revoking somebody who arrived by
  link: they are an ordinary group member.
- **An invite can never grant break-glass.** `ALLOWED_EMAILS` remains env-only; nothing in
  this feature writes to it.
- **An invite can never grant more than its creator holds**, in capabilities or in machine
  scope, unless its creator is a break-glass superuser. A scoped admin cannot mint themselves
  a link into a fleet-wide group.
- **Seats are bounded and claimed atomically.** `max_uses` is how many distinct people the
  link admits (1 = single-use); the claim is one conditional UPDATE, so two people racing the
  last seat cannot both be admitted. Redemption is idempotent per address.
- **Expiry defaults to 7 days.** "Never" exists and must be chosen deliberately.
- **A link may be pinned to specific addresses**, checked before any seat is claimed.
- **Only the link's SHA-256 is stored.** There is no "reveal": the hub cannot show a link
  again after it is created, and the page says so.
- **Creation, redemption, revocation and deletion are audited at security level** — these
  rows are the answer to "how did this person get access".
- **Creating an invite requires `manage_permission_groups`,** not `manage_users`: an invite
  hands out capabilities, so it is group administration.
- **`GET /invite/<code>` is the hub's only unauthenticated route**, and it has to be — it is
  read by somebody who cannot sign in yet. It renders the invite's label, sender and the
  *names* of the groups it grants, and nothing else: no capabilities, no machine scope, no
  seat count, no other redeemers. Previewing it consumes nothing. The code travels the
  sign-in round trip in the signed session cookie, never as a query parameter, so it does not
  reach referrer headers or proxy logs.

## The console assistant (roadmap #26)

The assistant is a language model that can call the hub's own API. Its security model is "the
operator, with a seat belt", and the controls are stated here because it is the one feature that
sends fleet data to a third party:

- **It runs as the signed-in operator.** Every tool call is replayed through the app's own
  request handler with a copy of that operator's session, so `login_required`, both `access`
  gates and every inline scope check apply unchanged. There is no `use_assistant` capability and
  no second authorization model; it can do what the operator can do and nothing more.
- **Tiers fail closed.** Reads run at once; a few small writes (dismiss an alert, wake a PC, draft
  a rule, which is created **switched off**) run at once and are audited; everything else that
  changes a machine or access becomes a **card** the operator must press Confirm on, run once
  within ten minutes and audited at security level as `assistant.action_confirmed`. A wipe also
  asks for the machine's name. Per-conversation modes (**Ask**, **Auto**, **Bypass**) move that
  line, start in Ask, show a warning when raised, and log every action that ran unasked as
  `assistant.action_auto`.
- **Never reachable, in any mode:** recovery-key reveals, API-key and device-token routes, the
  interactive terminal's input, agent and peer routes, the onboarding routes, every recording
  route, and the assistant itself.
- **The model's links are the hub's.** It writes symbolic links; the hub resolves them for the
  viewer's scope when the answer is shown, and strips any markdown link the model wrote itself.
- **Real machine names go to the provider** (the owner's decision, 2026-10-02). *Show the
  assistant* is off by default for exactly that reason, including on a hub that already uses AI for
  the alert recommendations. The provider's API key is write-only in the console and lives in
  `.env`, never in the settings table.
- **Building packages from the internet** is a separate switch (*Let the assistant use the
  internet*, off by default). It fetches only `https` addresses on the public internet, never the
  hub's own network; downloads land in a staging folder, are never run by the hub, and are deleted
  after 24 hours unless turned into a package; and it never deploys what it built.

## Sharing a machine with another hub (roadmap #15)

A peer hub holds a subset of an operator's capabilities, scoped to one machine, intersected live.
The controls:

- **The borrowing hub never speaks to your agent.** It calls your hub's API; your hub validates
  the grant and queues the command on its own queue for its own agent. There is no channel to
  filter, which is stronger than a check: an agent never learns another hub exists, and neither the
  hub URL nor the enrollment secret is something a hub can send an agent.
- **Shareable capabilities are an allow-list of three:** `view`, `issue_commands` and
  `remote_control`. A capability added in future is not shareable until somebody decides it is.
  Nothing rides along: a remote-control-only share shows no telemetry.
- **A lapsed share is refused by name**, and revocation kills sessions in flight. Pairing uses a
  single-use code; the resulting peer credential slides on use and expires
  (`sharing.peer_lifetime_days`). The feature is off until `sharing.enabled` is switched on.
- **A peer sees a projection**, never the asset record: no serial, asset tag or AD data. It reads
  back only its own commands and sessions. Both hubs write an audit row for every borrowed action.

## Release channels (roadmap #21)

The hub tells an agent a channel **name**, never a URL: the agent has both manifest URLs compiled
in, so a compromised hub can at worst move a machine between the stable and beta trains, where
the manifest is still signed by the same offline key and the binary is hash-checked against the
signed value. It cannot introduce a download location the agent did not already trust. Moving a
machine, or the fleet default, is `manage_settings` and is audited.

## Escrowed BitLocker recovery keys (roadmap #19)

The hub can hold each Windows PC's BitLocker recovery password so a locked-out PC is recoverable.
It is the most sensitive secret the hub stores about the fleet, so:

- **The keys are never in the database file a hub backup copies offsite.** Posture and protector
  ids are in SQLite; the passwords are in a sidecar encrypted with `BACKUP_MASTER_KEY` and keyed
  by machine name, so a blob copied between machines fails to decrypt. With no master key,
  nothing is collected.
- **Keys do not ride the heartbeat.** The hub asks for exactly the protector ids it lacks, and the
  server recomputes that set when the keys arrive, so an enrolled agent cannot file ids it invented.
  A settled fleet transmits no key material.
- **Reading one is `read_recovery_keys` plus machine scope**, one protector per call, named in the
  request body rather than the URL, and audited at security level *before* the key is produced.
  The assistant cannot reach the route, and a paired device may hold the capability only if its
  owner does.
- **Keys are never pruned because a machine stopped reporting them**; deleting the machine is the
  one thing that removes them, and a machine merge that cannot move them is refused.

## For Antivirus Teams

This software is **not malware** and is flagged by heuristics that detect:
- Windows Service installation
- Remote code execution capability
- System-level privileges

These are features, not bugs. The above controls + source code review + signing will establish trust.

---

# Vulnerability Reporting

## Reporting a Vulnerability

If you discover a security vulnerability in FleetHub, **please report it responsibly** by emailing wiensfriesena@gmail.com with the following details:

- **Vulnerability title and description**
- **Affected component(s)** (e.g., agent, hub, fleet command channel)
- **Steps to reproduce** (if applicable)
- **Potential impact** (severity, what an attacker could do)
- **Suggested fix** (if you have one)

### Important: Do Not Disclose Publicly

- Do **not** open a public GitHub issue
- Do **not** post details on social media or forums
- Do **not** use pull requests to disclose vulnerabilities
- Do **not** publish exploit code without prior coordination

We will treat your report in confidence and work with you to resolve it before public disclosure.

## What to Expect

1. **Acknowledgment:** We will confirm receipt within **3 business days**
2. **Assessment:** We will investigate and determine severity
3. **Fixing:** Depending on severity we will fix it as soon as possible.
4. **Disclosure:** We will coordinate with you on the timing and wording of a public advisory
5. **Credit:** You will be credited by name (or anonymously, if you prefer) in release notes

## Scope

We take security seriously for:

- **Agent (`agent/`)** — The Windows Service running as SYSTEM
- **Hub API (`app.py`, `fleet.py`, `packages.py`, `backups.py`)** — All fleet command and data access endpoints
- **Authentication & Authorization** — Google sign-in, enrollment secrets, token handling, permission groups
- **Update Mechanism** — Ed25519 signature verification, manifest handling
- **Encryption & Backups** — AES-256-GCM, key derivation, credential storage
- **Remote Control (`turn/`)** — WebRTC, TURN credential minting, session management, and the
  hub-relayed fallback (`hub/remote_relay.py`). Note what the fallback changes: media through
  coturn is SRTP the relay cannot read, while a session relayed through the hub is encrypted
  only as far as the hub (HTTPS), so the hub process sees the picture. It gains no capability
  by that -- it already holds every agent's bearer token and can start a session on any PC an
  operator has in scope -- but frames are kept in memory only, the switch is audited, it is
  never offered for a machine borrowed from another hub, and `remote.relay_fallback` turns it off.
- **Session recordings (`hub/recordings.py`, `recordings_web.py`, agent `RecordingBadge.cs`)** --
  recording without the PC's badge on screen, video readable by anyone but its owner and the
  people it is shared with, a share changed by anyone but the owner, or the recordings folder
  readable by local users on the hub server.
- **Audit Logging** — Tampering, omission, or integrity issues

### Out of Scope

- Social engineering or phishing
- Physical security (e.g., stolen credentials on a monitor)
- Issues in dependencies (report to the upstream maintainer instead)
- Missing optional security features (e.g., MFA, which we recommend but don't enforce)
- Issues requiring prolonged user interaction to exploit

If you're unsure whether something is in scope, please report it anyway — we'd rather over-include than miss a real issue.

## Severity Guidelines

| Severity | Definition | Examples |
|----------|-----------|----------|
| **Critical** | Attacker can execute code as SYSTEM fleet-wide without authentication | Unauthenticated RCE in `/api/agent/*`, a way past an agent's bearer-token check, manifest signature forgery |
| **High** | Attacker gains unauthorized admin access or exfiltrates sensitive data | Authentication bypass, unauthorized command execution, credential theft, audit log deletion |
| **Medium** | Attacker causes service disruption or accesses scoped/limited data | Denial of service, machine-scoped RCE boundary bypass, cross-user data leaks |
| **Low** | Informational or requires user interaction / existing access | Missing rate limiting, poor error messages, hardcoded non-secrets, typos |

## Security-Conscious Design

This project applies the following security principles:

- **Fail closed:** Unsigned updates, failed enrollments, and verification errors all refuse rather than proceed
- **Least privilege:** Agents run as SYSTEM only when needed (sensor access, remote control); users can scope operator access to specific machines
- **Defense in depth:** Multiple layers — signatures, tokens, audit logs, allow-lists — so no single layer is a single point of failure
- **Transparency:** Audit logs record every fleet command with operator info and full parameters
- **Defense-in-breadth:** Communication over TLS, session cookies pinned `SameSite=Lax` and `Secure`, CSRF protection by a per-session synchronizer token (`X-CSRF-Token`, rotated at every sign-in) with `application/json` enforcement on POST kept behind it

## Security Contact

- **Email:** wiensfriesena@gmail.com
- **GPG Key:** (Optional — provide if you have one; see [GitHub docs](https://docs.github.com/en/code-security/repository-security-advisories/publishing-a-repository-security-advisory) for guidance)

## Acknowledgments

We thank the following researchers and contributors for responsibly disclosing vulnerabilities:

(This section will be updated as issues are resolved and disclosed publicly.)

---

## Additional Resources

- [OWASP: Vulnerability Disclosure Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html)
- [GitHub: Security Policy Documentation](https://docs.github.com/en/code-security/getting-started/adding-a-security-policy-to-your-repository)
- [CVE: How to Report](https://www.cvedetails.com/)
