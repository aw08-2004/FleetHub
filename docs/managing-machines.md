# Managing machines

## Fleet command channel (RMM)

Beyond telemetry, the hub can queue **commands** for a machine that its agent
pulls and executes (restart, rename, install, etc.). This is the hub→agent
direction, added by [fleet.py](../hub/fleet.py) (core logic) and
[fleet_web.py](../hub/fleet_web.py) (HTTP surface), with state in the same SQLite DB
(`agents`, `commands`, `command_results`, `audit_log`). The C#/.NET agent
(`agent/`) implements the client side of this channel.

**Security model.**

- **Agent enrollment**: an agent presents a shared `AGENT_ENROLLMENT_SECRET` to
  `POST /api/agent/enroll` and receives a per-agent bearer token (only its hash
  is stored). All other agent endpoints require `Authorization: Bearer
  <agent_id>:<token>`. With the secret unset, no agent can enroll (fail closed).

  > **A machine that never enrolled can still post telemetry** while **Only accept telemetry
  > from enrolled agents** is off (see [Installing and updating](installation.md#telemetry-only-from-enrolled-agents))
  > — so it appears on the Dashboard and in Devices with a name, a
  > model and a live temperature, and reads as perfectly healthy while every command,
  > terminal session, package deployment, backup and process report on it silently does
  > nothing. That is why `/api/machines` carries an **`enrolled`** flag
  > (`fleet.enrolled_machines`, one query for the whole list) and both views qualify the
  > status pill with it: *Online, not enrolled* / *Offline, not enrolled*, with the
  > explanation on hover. Enrolled machines read exactly as before. A **revoked** agent
  > reads as not enrolled, which is the point of revoking it.

  > **Deleting a machine and reinstalling its agent re-enrolls it by itself.** The hub tells
  > the agent why it refused it (`reason: unknown` for a deleted machine, `reason: revoked`
  > for a revoked one). The agent discards its identity and enrolls again only in the first
  > case; a revoked agent stays down.
- **Issuing a command requires a signed-in session holding `issue_commands`, plus the
  target machine in that operator's scope** — no offline signature. Every type,
  including `run_script`, which runs arbitrary PowerShell **as SYSTEM**, dispatches on
  that alone. See [Permission groups](access.md#permission-groups).

  > ⚠️ **`issue_commands` is the entire perimeter for remote code execution as SYSTEM
  > on the machines in scope**, and **`ALLOWED_EMAILS` — the break-glass superuser
  > list — holds it over every machine, unconditionally.** Treat adding an address to
  > `ALLOWED_EMAILS` as granting domain-admin-equivalent power, keep those accounts on
  > MFA, and give day-to-day operators a permission group instead.

  This is deliberate: the channel is operated by a helpdesk group. Offline command
  signing was removed in hub 1.10 / agent 3.1 because it required one key holder to sign
  every script. Release signing is a separate trust root and still applies.
- **The `audit_log` is the accountability control.** Every enroll / issue / claim /
  complete is appended, and `issue_command` records the issuing operator's email plus
  the **full command params, including script text**. With no second gate, that trail
  is the only answer to "who ran this?" — so it must never be allowed to go quiet.
- **CSRF**: the console endpoints only accept `application/json` bodies. That is
  load-bearing, not incidental — it's what stops a cross-site POST from a signed-in
  operator's browser becoming fleet-wide RCE (that content type isn't CORS-safelisted,
  so cross-origin requests preflight and fail; an HTML form can't produce it). The
  session cookie is additionally pinned `SameSite=Lax` + `Secure`. Don't add
  `force=True` to a `get_json()` call, a form-encoded fallback, or permissive CORS.

### The Terminal tab (interactive console)

A machine's **Terminal** tab is a **real Windows console** on that box — a pseudoconsole
(ConPTY) hosting `powershell.exe` or `cmd.exe`, rendered in the browser by xterm.js. Enter,
Ctrl-C, Tab completion, arrow keys, colour, progress bars and interactive prompts
(`Read-Host`, `$Host.UI.PromptForChoice`) all behave as they would sitting at the machine,
which is what makes it possible to drive something like `install.ps1` remotely.

That is a different mechanism from `run_script`, which still exists and is unchanged —
favorites and automation use it, because a one-shot script has an exit code and a result
worth recording. A terminal has neither. The distinction runs all the way through:

| | `run_script` | Terminal (`shell_open`) |
|---|---|---|
| Unit | one script, one result | a continuous byte stream |
| Transport | the command queue (`commands`, `command_output_chunks`) | its own `pty_*` tables (`hub/terminal.py`) |
| Retention | capped at 256 KB, kept as the durable record | rolling window, nothing retained |
| Latency | seconds | ~250–400 ms echo |
| Recorded | full output + exit code | only *that a terminal was opened*, in `audit_log` |

**A session outlives the page.** Start a download, go to Packages, come back — the shell is
still there, with its working directory, its variables, and everything it printed while you
were away. The console re-attaches to the existing session rather than opening a second
one, and replays the hub's buffer into a fresh terminal to restore the scrollback. A
session ends only when you close its tab, the shell itself exits, or a reaper fires.
**Clear** drops the scrollback on the hub as well as locally, so a cleared terminal stays
cleared when you come back to it.

**Several consoles at once.** The tab strip above the terminal lists every console you have
open on that machine — **+** opens another (up to four per operator per machine, as the hub
enforces); **×**, or a middle-click on the tab, ends one. Each tab is its own shell with its own working directory and
scrollback; the front one polls at typing speed and the rest keep collecting output in the
background, so a build in one tab keeps running while you work in another. The strip is
built from the hub's list of *your* sessions rather than from anything the browser
remembers, so a console you opened on one computer is listed — with its scrollback — when
you sign in from another, or from a second browser tab, or after a browser restart. The
**Shell** dropdown chooses what the next **+** opens and follows whichever tab is in front;
changing it no longer ends anything. A shell that exits on its own leaves its tab behind,
struck through, showing its last screen until you dismiss it.

**What is and isn't recorded.** Opening a terminal writes a `shell_open` row to the
`audit_log` naming the operator, the machine and when — the same accountability trail as
any other command. What is *typed* is deliberately not kept: the stream lives in a rolling
buffer (256 KB) that is dropped when the session ends. A transcript of a SYSTEM console
would be a store of half-typed credentials, and nothing reads it back.

**Isolation.** A session is bound to one operator and one machine. Another operator with
`issue_commands` on the same machine may open their own terminal but cannot read, type
into, or close somebody else's — "can run commands here" is not consent to watch someone's
keystrokes. On the agent side each session is checked against the authenticated agent's own
machine.

**Cleanup.** An open session is a live SYSTEM shell, and because sessions now persist across
navigation, "still open" no longer implies "still wanted". Two different silences are
measured separately, and conflating them would disable one of them entirely:

- **The agent went quiet** (15 min). It polls for keystrokes continuously while it holds a
  session, so a gap means the machine is gone.
- **Nobody came back** (60 min). Measured on a clock that *only the console's own polls*
  refresh — the agent's polling would otherwise keep a session looking alive forever.

The agent keeps a much longer backstop timer of its own (2 h) for a hub that forgets a
session while still answering, and the pseudoconsole's child is enlisted in a
kill-on-job-close job so even the hard `Environment.Exit` a self-update uses cannot orphan
one.

**Older agents.** Below agent 3.15.0 the tab falls back to a line-oriented terminal that
cannot answer interactive prompts.

Add to the hub's environment / `.env`:

```
AGENT_ENROLLMENT_SECRET=a-long-random-shared-secret
```

**Endpoints.** Agent-facing (token auth): `POST /api/agent/enroll`,
`POST /api/agent/heartbeat`, `GET /api/agent/commands` (pull + claim),
`POST /api/agent/commands/<id>/result`, `POST /api/agent/commands/<id>/output`
(live output streaming), and the terminal's `GET|POST /api/agent/pty/<session>/input`
`/output` `/closed`. Console-facing (`issue_commands` + machine scope):
`GET /api/fleet/status` (online/offline), `GET|POST /api/fleet/commands`,
`GET /api/fleet/commands/<id>`, `GET /api/fleet/commands/<id>/output`,
`GET|POST|DELETE /api/fleet/favorites[/<id>]` (saved scripts), and
`POST /api/fleet/pty` + `/api/fleet/pty/<session>/input` `/output` `/clear` `/close`.
Every issue/claim/complete/enroll is written to `audit_log`.

### The Processes card (the machine's task manager)

A machine's **Overview** tab carries a **Processes** card: every process grouped by name
with its CPU, memory, owner and PID, expandable to the individual instances. **Right-click
any row** for **End task** and **Restart** — Task Manager's shape, and for its reason: a
table of a few hundred rows is scanned, and two buttons on each of them is more furniture
than list. Core logic in [processes.py](../hub/processes.py), HTTP surface in
[processes_web.py](../hub/processes_web.py), UI in `hub/static/js/processes.js`, agent side in
`Telemetry/ProcessReader.cs` and `Fleet/Executors/ProcessExecutors.cs`.

**Reading is `view`; ending or restarting is `issue_commands`.** What is running on a PC is
inventory in the same sense its disks and its sensor tree are, and anyone who can open the
machine page already reads both. Ending a process is strictly less dangerous than the
`shutdown` the command gate already covers, so there is no new capability.

**The card is collapsed by default, and that is load-bearing.** No machine samples its
processes until an operator opens it. Opening it registers a *watch*; the agent asks
`GET /api/agent/processes/wanted` every 2s while idle and starts sampling the moment that
answers yes; the list then rides the heartbeat it already sends, every 5s. The watch lapses
~45s after the operator navigates away, so a fleet nobody is looking at does no process work
and sends nothing.

**Why the machine asks rather than being told.** An agent has no inbound port — nothing on
the hub can push at it — so the only lever on "how fast does the list appear?" is how often
the machine asks. Learning it from the heartbeat (a 10s tick) put the first list 10-15
seconds after the click; a dedicated poll that is one indexed lookup and a ~30-byte answer
makes that a few seconds, and is the *only* thing an unwatched machine does for this feature.
The heartbeat still carries `processes_wanted` — it is how a machine learns to **stop**, and
the only path for an agent older than v3.25.0. The console polls once a second until the
first list lands, then settles to the hub's cadence.

**Ending a process is guarded twice, against the same hazard.** Every action carries the
process NAME the operator saw alongside its PID, because a rendered list is always a few
seconds old and Windows recycles process ids within minutes — a bare PID is an instruction
to kill whatever now holds that number. The agent re-reads the live process and refuses a
mismatch rather than resolving it. **Critical Windows processes are refused outright**
(`csrss`, `wininit`, `winlogon`, `services`, `lsass`, `smss`, the kernel pseudo-processes,
and the agent itself): ending one is a bugcheck, not a closed program. `svchost` is refused
too — killing a service host takes every service in it down, and for the RPC host that
reboots the machine — with the refusal pointing at Restart, which does the right thing.
`explorer.exe` is deliberately killable.

**Restart means three things, decided on the machine.** A process hosting exactly one
Windows service is restarted *as that service* (stop, wait, start, wait, and its running
dependents brought back) rather than killed, so the SCM does not record a crash. One hosting
several is refused with them named. Anything else is relaunched from its own image, in the
Windows session it was running in, **as the user who was running it** — not as SYSTEM, which
would give the program no profile and no access to the documents it exists to open.

**Endpoints** (console-facing): `GET /api/machines/<machine>/processes` (`view` + scope;
polling it is what renews the watch), `POST /api/machines/<machine>/processes/kill` and
`POST /api/machines/<machine>/processes/restart` (`issue_commands` + scope). Both queue an
ordinary fleet command and answer with its id, which the card follows through
`GET /api/fleet/commands/<id>` — so every End task and Restart lands in `audit_log` with the
operator, the machine, the process name and the PID. `kill_process` and `restart_process`
are deliberately refused by the generic `POST /api/fleet/commands` endpoint and cannot be
saved as favorites: the guards above live on the dedicated route, and a saved PID is a
different process by tomorrow.

### Disk usage in the Files tab (folder sizes, what changed, when it is full)

Every Windows agent from **3.40.0** reads each fixed NTFS volume's Master File Table once a day.
That is a sequential read at low I/O priority, the way WizTree does it, not a walk of every
folder. Its result shows up in the machine's **Files** tab:

- **Folder sizes.** Every folder in the list shows its size, with a bar against the biggest row
  on screen. Click the **Size** header to sort biggest first. A note under the toolbar says when
  the sizes were measured; hover a size to see it on disk and its file count.
- **When the drive is full.** The drive list shows *Full in about N days* beside a volume that is
  filling: amber under a month, red under a week. This works on **every** Windows agent, old ones
  included, because the hub records one point per volume per day from the disk sensors agents
  already send. It needs a week of points before it shows a date.
- **History** (button in the toolbar) shows:
  - used space per day, with the projected full date drawn on it
  - **What changed** between two scans: up to 200 folders, each one the deepest folder that
    explains a change, the files that grew, appeared or went away (biggest first), and new files
    over 100 MB. Click a folder or file to open it.
  - the size history of the folder you are browsing. **Right-click → Size history** shows it for
    any folder or file, at any depth.
  - **Compare with**: pick a past day and the folder list gains a column with each entry's size
    on that day and the change since, and lists what has been deleted since.
- **Scan now** measures at the machine's next check-in instead of tomorrow.

All of it needs `issue_commands` and the machine in your scope, the same as browsing, because
folder names are the names of somebody's documents. The hub keeps every folder and file at full
depth, in one file per PC and drive under `logs/disk_history/` (roughly 150-300 MB per drive in
the first year). History is kept daily for `data.disk_usage_daily_days` (90). After that one
point per week is kept, until `data.disk_usage_keep_days` (365). Deleting a machine erases its
history.

API: `GET /api/disk-usage/machines/<m>` (volumes, latest point, forecast, last scan),
`.../history?volume=C:`, `.../changes?volume=C:&day=YYYY-MM-DD`,
`.../path-history?volume=C:&path=...`, `.../browse?volume=C:&path=...&day=YYYY-MM-DD`,
`POST .../scan`. Agents post the summary to `POST /api/agent/disk-usage` and the full-depth
history to `POST /api/agent/disk-usage/history` (gzip; 409 means "send the full tree").

### Deleting a device

**Devices → ⋯ → Delete** removes the machine's record. The confirmation's **"Also uninstall
the agent from this PC"** box is ticked by default. When it is ticked, the PC's agent removes
itself the next time it contacts the hub, within about 30 seconds if the PC is on, or whenever
it is next switched on. It takes the service, the install folder and its local data with it.
This needs Windows agent 3.44.0 or Linux agent 0.4.0 or later; older agents re-enroll and the
PC reappears, as before. Untick the box to clear a stale record of a PC that is still in use.
The automatic merge of duplicate machines never uninstalls anything.

## Wake-on-LAN

Power a sleeping machine on from the console, so an out-of-hours patch window or a remote
session doesn't depend on somebody being at the desk to press a button.
[wake.py](../hub/wake.py) (model) + [wake_web.py](../hub/wake_web.py) (HTTP), a **Network** tab on
the machine page, a **Wake offline PCs** button on Devices, and `wake.*` settings.

**Delivery is agent peer-relay, and that is the whole design.** The hub picks an *online*
machine whose reported IPv4/prefix puts it on the same subnet as the target, and issues
**that** machine a `wake_machine` command carrying the target's MACs and the subnet
broadcast address; its agent sends the magic packet to UDP 9. A hub-sent broadcast reaches
the hub's own network segment and nothing else, so for a helpdesk with more than one site it
is the wrong default — the machines that most need waking are at the branch office the hub
has never shared a broadcast domain with. Peer-relay crosses sites and VLANs with no router
configuration at all.

The hub's own broadcast survives as a **fallback only** (`wake.hub_broadcast`), taken when
no awake peer exists *and* the hub can prove it shares the target's subnet. That covers the
real hole in peer-relay — 3am, every PC on the subnet asleep — on a single-site fleet.

**Subnets come from the agents, never from a heartbeat's source address.** Nothing in the
product collected a MAC or an IP before this. A new change-only reporter
(`Network/NicReader.cs`) sends per adapter: MAC, IPv4, prefix, link state, type, and whether
the NIC is allowed to wake the machine — into a new `machine_nics` table. It has to come
from the machine: the only address the hub can see for itself is the NAT'd site edge, which
every PC at an office shares and which would fold a whole building into one fictional subnet.

**A wake is an *attempt*, and every state name says so.** Nothing acknowledges a magic
packet, no error comes back for a MAC that does not exist, and a machine that was already
awake looks identical to one that just woke. So:

| State | What it means |
|---|---|
| `pending` | Looking for an awake PC on the target's subnet. **Survives across ticks** — a target whose subnet is entirely asleep is woken by the first peer to come online. |
| `relaying` | A `wake_machine` command is queued at a peer. |
| `sent` | The packet went out. **Not success.** |
| `awake` | The target checked in *after* the packet. The only success. |
| `already_awake` | It was on when you asked. Not an error — that is the answer. |
| `no_relay` | The deadline passed with nobody awake on that subnet. **Not a failure**: at 3am it is the expected state, and the message names the subnet so it can be acted on. |
| `no_answer` | The packet went out and the machine never checked in. A report of silence, not a claim about the packet. |
| `unwakeable` | Nothing to send to — refused before dispatch, with the reason attached. |

Confirmation compares against **when the packet was sent**, not against "is it up now": a
machine can read online on a last-contact timestamp from before the packet, and treating
that as a wake would confirm one nobody performed.

**Most of a WoL rollout is preconditions, not code**, so the Network tab names every reason
a machine cannot be woken — no wired adapter, Wi-Fi only (this mechanism cannot reach a
laptop over Wi-Fi at all), no address on record, waking turned off on the NIC, and **Windows
Fast Startup**, which turns shutdown into a hybrid state that defeats wake-from-S5 on many
machines. **Fix wake settings** (`prepare_wake`) is the remedy: it enables the device's own
"allow this device to wake the computer" via `powercfg /deviceenablewake`, sets the driver's
`*WakeOnMagicPacket` property, and turns Fast Startup off. Both NIC settings have to be on —
an adapter allowed to wake the machine but with magic-packet wake off in its driver looks
perfectly configured in Device Manager and ignores every packet. Wireless adapters are never
touched. The firmware-side enable is the [BIOS settings](../ROADMAP.MD) half of roadmap #9,
which is why the two arrived together.

**Scheduled waking is the point.** With `wake.auto_wake_targets` on, the deploy and firmware
schedulers wake a maintenance window's offline targets when it opens — a window that
dispatches into a dark office installs nothing. Off by default: waking a fleet at 3am is a
decision, not a side effect of scheduling a deploy.

**Gating**: reading the adapters and the diagnosis is `view` + machine scope (it is
inventory, like a model or a disk layout); waking, preparing and cancelling are
`issue_commands` + machine scope. No new capability — waking a PC is strictly less dangerous
than the `shutdown` that gate already covers.

**Endpoints** (console-facing): `GET|POST /api/wake/machines/<machine>`,
`POST /api/wake/machines/<machine>/prepare`, `POST /api/wake/fleet`,
`POST /api/wake/requests/<id>/cancel`, `GET /api/wake/requests`. The adapters arrive on the
existing `POST /api/agent/heartbeat` under a `network` key.

Requires hub 1.63.0 and agent 3.21.0 or later.

## Network discovery and shadow IT

What else is on the network a managed PC is sitting on, and which of it the hub has never
heard of. The **Subnet discovery** card sits under the adapters on the machine page's
Network tab, because the subnets it can sweep are that machine's own.

**The sweep runs on the machine, not on the hub.** The hub is almost never on the network it
is being asked about -- for a helpdesk with more than one site it sits in one office and the
subnet in question is behind a router in another -- so it asks a machine that is already
there. Same reasoning as Wake-on-LAN's peer relay above, and it reuses the NIC inventory that
feature already collects.

**The probe is ARP, not ping.** A Windows PC at default firewall settings does not answer
ICMP from an unknown host, so a ping sweep of an office subnet finds the printers and misses
the PCs -- the exact inverse of what this is for. The sweep identifies only addresses that
respond to ARP; sleeping, filtered, isolated, or otherwise non-responding devices may still
be present, so silence is not proof that an address is unused.

**It cannot be pointed anywhere.** The subnet is chosen from a list of the ones the machine
has already reported being on, never typed, and the hub refuses anything else. ARP does not
cross a router, so this is a statement of fact before it is a rule -- but it is also what
keeps a discovery sweep from becoming a scanner aimed at somebody else's network. Sweeps are
on demand only; there is no schedule and no configured range.

Every address that answers is classified against the adapters the fleet has reported:

| Verdict | What it means |
|---|---|
| **This PC** | The machine doing the looking. Not a finding, and kept out of the counts so a quiet VLAN does not read as having found a PC. |
| **Managed** | A MAC the hub already knows, named with the machine it belongs to. |
| **Not managed** | Everything else. Deliberately not called *rogue* -- most of it is a printer, an access point or a phone, and a console that calls the accounting department's label printer rogue teaches people to ignore the column. |

Under each MAC the card names **the maker of the network card**, from a snapshot of the IEEE
registry committed with the hub (`hub/data/oui.tsv.gz`; rebuild it with
`python tools/refresh_oui.py` when a new vendor's devices show no name). Mostly that is what
tells a printer from a laptop. An address the device made up itself — every current phone and
Windows' "random hardware addresses" do this on Wi-Fi — is labelled **Private address** rather
than looked up, because no registry entry can be its maker.

The verdict is worked out **when the list is read**, not when it was collected, so a machine
enrolled an hour after a sweep turns that scan's unknown device into itself, and a machine
deleted from the fleet turns back into a finding. One stale false alarm is what stops a
shadow-IT list being read.

**A discovered device becomes nothing.** Nothing here enrolls, names, groups, alerts on or
sends a command to an address. The classification is a MAC match, and a MAC is a claim a
device makes about itself.

**Gating**: reading a machine's sweepable subnets and its last sweep is `view` + machine
scope; running one is `issue_commands` + machine scope, with no new capability -- an operator
who can open a SYSTEM shell on that PC can already run `arp -a` on it. Every sweep writes a
notice-level audit row naming who asked and which subnet.

**Endpoints** (console-facing): `GET|POST /api/discovery/machines/<machine>`,
`GET /api/discovery/scans`, `GET /api/discovery/scans/<id>`. The machine posts its host list
to `POST /api/agent/discovery/scan/<id>` under agent bearer auth, rather than returning it as
command output -- a full /22 is a thousand rows, and keeping the two apart is what lets the
console tell an empty subnet from a report that was lost.

Requires agent 3.37.0 or later; an older agent fails the sweep with `unknown command type:
network_sweep`. Not included: an Intune/Azure cross-reference, fleet-wide sweeps and
scheduled sweeps (see roadmap #18).

## Machine capabilities

What each managed machine can actually do, reported by the machine rather than inferred from
its agent version — so the hub stops queueing work a device can never perform.

**Why a version number was not enough.** The console decides which buttons to draw from
`MIN_*_AGENT` constants that hardcode the agent minor a feature arrived in. That works while
every machine is a Windows PC on one release train, because "newer" really does mean "can do
more". It cannot express *"this device can be renamed but will never run a script"*, which on
an Android device is a fact about the platform, not a backlog position.

**And those gates are JavaScript, so scheduled work never sees them.** They stop an operator
being *offered* a button. Nothing evaluates them for a command the backup, patch or deployment
scheduler — or the rules engine — dispatches, because those target a machine *set*. The first
Android device to enroll was inside a fleet-wide backup profile and was queued `backup_files`
within a minute.

**The absent-report rule.** A machine that has reported nothing is *unknown*, never
*incapable*. Every Windows agent in the field reports nothing here and keeps working exactly as
before; only an explicit report can refuse anything.

**Where it is enforced.** `fleet.create_command` — the one function every command in the system
passes through — refuses a type the target has said it cannot run, with a reason naming the
machine, the command and the platform. The four schedulers additionally filter their targets
first, so a phone in a nightly backup profile is never aimed at rather than collecting a failed
run every night.

**Gating**: reading is `view` + machine scope, like a model or a disk layout. There is no write
surface: a capability is something a machine reports about itself, and an operator override
would be a way to tell the hub that a phone can run a script.

**Endpoints** (console-facing): `GET /api/capabilities/machines/<machine>`,
`GET /api/capabilities/platforms`. The report arrives on the existing
`POST /api/agent/heartbeat` under a `capabilities` key — on every heartbeat rather than
change-only, so a hub restored from a backup re-learns the fleet without anybody reinstalling
an agent. Machine pages carry `platform`, `features` and `supported_commands` already.

**What the console does with it.** A machine page hides the tools its machine has said it cannot
answer -- Terminal, Backup, Firmware, Network, Files and the Processes card -- and says in one
sentence that it did, because a toolbar that is shorter than it was on the last machine reads as
a page that failed to load. Two questions, answering silence differently on purpose: an action
the console has always offered stays offered by a machine that has reported nothing, while a
new one (locking, wiping) waits for an explicit claim. It is a courtesy either way; every route
re-decides.

Only the Android agent reports capabilities today. The Windows and Linux agents send
nothing and are treated as unknown by design, so nothing on their pages changes.
