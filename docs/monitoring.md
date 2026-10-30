# Monitoring, alerts and reports

## The console

Agents send telemetry to `POST /api/report`. Every console page requires OIDC sign-in
plus a permission group (see [Sign-in and permissions](access.md)). The sidebar has three
sections: **Fleet** (Dashboard, Devices, Map, Alerts), **Operate** (Remote, Packages,
Patches, Fleet tasks, Rules, Device policy) and **Administration** (Audit log, Settings,
Permission groups, Users and others). **Ctrl+K** finds a device or a page by name; on a
device, ←/→ picks Overview, Terminal, Files, Network or **Remote** before Enter. Remote opens
the Remote page already connecting to that PC. It is offered only with `remote_control`, and
not on Linux or Android devices.

- **Dashboard** (`/`) -- fleet-wide counts, OS mix, live aggregates and attention lists.
- **Devices** (`/inventory`) -- one row per machine, with filters, bulk actions (wake,
  deploy a package, export) and a menu per row.
- **Machine page** (`/machine/<name>`) -- one device's workspace, with Overview, Terminal,
  Backup, Firmware, Network, Files and Report tabs. Overview shows live status, the
  Storage and Cooling cards, history charts (CPU, memory, disk, network, GPU, fans, power,
  battery, temperature) and an **All sensors** section with the full LibreHardwareMonitor tree.
  Cards for hardware a machine does not have are hidden.
- **Alerts** (`/alerts`) -- machines running hot, duplicate machines, and other
  conditions that want attention (see below).

### Every page is live

Nothing in the console needs a refresh. When something a page shows changes -- whoever or
whatever changed it: another operator, an agent's report, a scheduler, a machine going quiet
-- the page re-reads it within a second or two. Relative times ("last seen 3m ago") keep
counting while the page is open.

How it works (see [changes.py](../hub/changes.py) and `static/js/fleet-live.js`): every
successful write under `/api/` makes the hub push a **change notice** naming a topic
(`alerts`, `deployments`, `settings`, ...) and, when it is about one machine, the machine. The
notice carries no data; the page re-fetches through the same permission-checked route it
loaded from, so a notice can never show an operator something their scope hides. A
five-second watcher turns "this machine's last report is now outside the online window" into a
`status` notice, which is how a machine going offline reaches an open page.

- **One connection per browser tab.** The shell owns it and every page in the tab shares it,
  so a tab holds at most one hub worker thread however many pages it visits. The topbar's
  **Live** pill is that connection's state.
- **While it is down**, pages re-read every 30 seconds, and everything re-reads once on
  reconnect.
- **A page never refreshes under your hands.** It waits while a dialog or editor is open or a
  setting is unsaved, and catches up as soon as it closes. A hidden tab catches up when it is
  shown again.
- **Permission changes apply at once.** The sidebar's links follow a grant or a revocation,
  and the connection re-joins with the new scope.

### Searching and filtering tables

Every table in the console with more than five rows has a **search box** above it (matches
any cell) and a **Filters** button that opens one filter box per column. Column filters
combine with each other and with the search: *Manufacturer* `dell` plus *OS* `windows 10`
shows only the Dells still on Windows 10. The Filters button shows how many column filters
are on, even with the row folded away, and **Clear filters** resets them all.

- Pages that already have their own search (Devices, Audit log, Tickets, Users, Event logs,
  the Processes card) get the column filters without a second search box. On those pages the
  page's own search box keeps working as before.
- On **Devices** the filters narrow the data itself, so the count, **Select all** and
  **Export** all follow them. On other pages they hide rows from view.
- Filters last for the browser tab, not forever: a filter forgotten yesterday would hide PCs
  today with nothing saying why.
- On a phone (narrower than 640 px) every table is a stack of cards, one per row, each line
  labelled with its column. Between 640 and 900 px a wide table scrolls sideways inside its
  card instead of moving the whole page.

### The machine page reports at 1 Hz while you are watching it

A machine reports every 5s by default (**Settings -> History Metrics**, see below), and only
every other report carries the full sensor block the CPU/GPU/memory/fan/power panels are drawn from -- so a page whose window is sixty seconds
wide drew about six points a minute on most panels, and a three-second spike was one dot or
none. **While an operator has a machine page open and in front, that machine reports every
second with a sensor block on every report.** Nothing to enable.

It works exactly like the Processes card's watch, and for the same reason (see
[live.py](../hub/live.py) and `Telemetry/LiveTelemetry.cs`): the page pings
`POST /api/machines/<machine>/live/watch` (`view` + scope) every 5s, that watch lapses ~20s
after the pings stop, and the machine asks `GET /api/agent/watch` every couple of seconds to
find out. Hidden tab, past day, closed window, killed browser, sleeping laptop -- all of them
stop the pings, and the watch expires on its own rather than waiting for a goodbye that would
never arrive. A fleet nobody has open reports exactly what it always did; the extra traffic
(~12x, for one machine) is bounded by attention rather than by fleet size.

**History keeps the detail; the database does not grow twelve-fold.** Every fast reading is
stored with its typed metric columns, so the charts have a point per second for that period.
The raw ~36 KB LibreHardwareMonitor blob that rides alongside is throttled to one per 10s
(`SENSOR_BLOB_MIN_SECONDS`) -- nothing reads it back except "the newest one" (the sensor
picker's fallback after a hub restart), and storing it per second would cost ~130 MB an hour
for a single watched machine.

`GET /api/agent/watch` answers **both** watches -- process list and live charts -- in one
bearer-authenticated request, because the alternative is doubling the only traffic an
unwatched machine generates for either. `processes_wanted`/`live_wanted` also ride the
heartbeat, which is how a machine learns to stop, and the only path for an agent older than
v3.28.0 (which keeps polling `GET /api/agent/processes/wanted` and never speeds up).

### Tuning what is sampled, which sensor, and for how long

Three groups of knobs, all on the Settings page, all fleet-wide.

**How often a machine samples** -- *History Metrics*. `metrics.report_interval_seconds` (5)
is how often a machine sends a reading; `metrics.sensor_interval_seconds` (10) is how often
one of those readings carries the full sensor block. Turn them down on a fleet over a metered
link, or up for finer charts. Neither touches liveness: whether a machine reads online is the
heartbeat's business and that is a separate 10-second loop, so a fleet reporting every five
minutes is a coarser chart and nothing else. Neither touches the watched-machine path either
-- a page you have open still gets 1 Hz. Both ride the agent config channel, so an agent
older than these settings keeps its compiled 5/10 and nothing breaks.

**Which sensor gets charted** -- *Computer*. `computer.primary_sensor_preference` has always
picked the CPU temperature; `gpu_temp_preference`, `gpu_load_preference`, `disk_preference`
and `network_preference` now do the same for the rest. The two GPU lists match part of a
*sensor* name ("gpu hot spot"); the disk and network lists match part of a *hardware* name
("Samsung SSD 980", "Intel(R) Ethernet"), because the question there is which of four drives
or which of twenty-six adapters. Both of the latter ship **empty**, meaning the behaviour that
predates them: the first drive the machine reports, and the busiest adapter. Empty is right
until a Hyper-V switch or a dock mirrors the real NIC, or until the arbitrary drive is not the
one you care about. A named device missing from a report falls back rather than charting a
gap. These are re-derived by the hub from the reported block, so they apply to every agent in
the field immediately -- and to **new readings only**: history keeps what was recorded at the
time.

**How long each metric is kept** -- *History Metrics*. `data.retention_days` (30) still bounds
the whole readings table, and each metric now has its own window beside its collection toggle:
`metrics.retention_days_network`, `..._disk_io`, `..._gpu` and so on. **0 means "follow the
fleet-wide window"**, which is the default for all nine, so an untouched hub prunes exactly as
it always did. A shorter window blanks that metric's columns on older readings while the
reading itself -- and its temperature -- stays; a metric aged out is indistinguishable from one
whose collection toggle is off, which is the point, because in both cases the honest answer is
"not recorded". A window **longer** than `data.retention_days` is ignored: the whole row is
deleted there, so there is nothing to keep. Like `data.retention_days` this deletes permanently
and is audited.

### Battery charge

Laptops and Android devices report their battery's charge level, and the hub exposes it as
`metric.battery_pct` (0–100). The machine page charts it, and a rule can test it -- for
example `metric.battery_pct < 70` for a laptop that should stay plugged in. Nothing needs an
agent update: the Windows agent already sends LibreHardwareMonitor's battery sensors, and the
Android agent already sends the charge level.

On a machine **without a battery** the variable is *unknown*, so a battery rule never fires on
a desktop. Only the sensor named *Charge Level* counts. The battery's *Degradation Level*,
which LibreHardwareMonitor reports beside it as the same kind of reading, is never used as the
charge. If a laptop shows no Battery chart, look in its **All sensors** section: no battery
hardware there means the agent's sensor library did not find the battery on that model.

### Temperature alerts

A machine is flagged as running hot when its **average** temperature over a
window (default **5 minutes**, `hub.high_temp_avg_window_seconds` in Settings) is
at or above the high-temperature threshold (`hub.high_temp_threshold`). Averaging
is the point: a momentary spike no longer raises an alert, only a sustained
condition does.

The check runs on the hub every ~30 s (`evaluate_high_temp_once`), so alerts are
independent of any browser being open. A high-temperature alert appears in the
**Alerts** page.
Alerts **stay until an operator dismisses them**: when the machine cools, the
*episode* is closed but the card remains, and the next hot spell raises a **new**
alert beside it rather than overwriting the old one's numbers. Alerts are
machine-scoped: an operator only sees, and is only badge-counted for, machines
within their scope.

### Grouped alerts and recommended fixes

Alerts on one machine whose episodes overlap in time are shown as **one bundle** with its
cards nested under a heading, rather than as three separate things to read in whatever order
they were last refreshed. Grouping is structural -- same machine, overlapping windows, joined
transitively -- and claims nothing by itself.

When the bundle matches one of a short list of known causal pairs **and** the cause started
first, the heading names it: "The drive filled first, and a process on the machine failed
after that." When it does not, the heading says the alerts overlap and leaves the reading to
you. The hub never guesses at a cause it cannot establish.

**Suggest a fix** appears on a bundle when AI assistance is configured (Settings → AI, off by
default; it uses the same provider and the same off switch as the rule drafter, and every
request is in the AI audit trail). The figures come from the hub; only the wording comes from
the model, and the card says which model wrote it. If the suggestion includes a script, **Add
this script to the library** saves it to the script library **switched off** -- read it, then
enable it before any rule can use it. Nothing is ever run from a suggestion.

**Show what else happened** opens the rest of the picture for that bundle: the machine's
metrics that sit outside **its own** trailing fortnight (median and MAD, not a fleet
average), and the Windows event log records collected by [event log mining](#event-logs)
that fall inside the same window -- the four hundred failed logons beside the disk alert,
rather than on a separate tab. It loads on the click rather than with the list, because a
baseline is a scan of two weeks of readings and this page polls.

A machine with less than an hour of history has no baseline yet, and the hub says nothing
rather than guessing. Baselines never raise alerts of their own, and an event is evidence
inside a bundle -- it is never itself an alert.

### The assistant

**Assistant** in the sidebar opens a conversation with the AI provider configured in
Settings → AI (both *Use AI assistance* and *Show the assistant* must be on; the second is off
until an admin turns it on, including on a hub that already uses AI). **Pin to the side**
docks it to the right of every page until you unpin it; drag its left edge to resize. It knows
which page and which machine you are looking at, so "why is this PC slow?" on a machine page
means that machine.

Your conversations are listed on the left of the Assistant page (fold the column away with the
panel button), and behind the clock button when it is pinned. A conversation keeps answering if
you switch to another one; it shows a spinner in the list until it is done. Each conversation is
named after what you asked for in its first message; rename it with the pencil beside it, and
your name is kept. Every message and answer has a copy button underneath, next to the time it
was written; the assistant's working steps ("checking the deployments...") carry their time on
the left, so you can see when it checked what. Times are in your browser's time zone; point at
one for the full date. While it works, the line at the bottom counts how long it has been at
it, and a finished answer ends with **Worked for 2m 14s** -- the time from your message to its
last word, including any wait. A follow-up after you confirm an action gets its own line.

**Waiting.** Ask it to wait ("restart PC-12, wait ten minutes, then check it came back") and it
pauses for that long, showing *Waiting 10m 00s, until 14:32* with the timer still running, then
carries on. It also waits on its own when something needs time, such as a reboot or an install.
One wait is at most *Longest assistant wait* (Settings → AI, 60 minutes); a longer one is cut
to that and the assistant tells you. **Stop** ends a wait at once. A waiting
conversation holds one of the assistant's four workers, so long waits make other operators'
questions queue, which is why the limit exists. A hub restart ends the wait along with the rest
of that answer.

It can read anything you can see and links every machine it names -- click a link and the page
opens beside the panel. It can also act, with exactly your permissions:

- Reading, dismissing an alert, waking a PC or drafting a rule (created switched off) happen at
  once.
- Changes that need confirmation -- restarting a machine, running scripts or deployments,
  wiping, changing settings or permission groups -- appear as a card showing the exact request. Nothing runs until you press
  **Confirm**; a wipe also asks you to type the machine's name. Cards expire after 10 minutes.
- Recovery keys, API keys, device tokens and the interactive terminal are never available to it.

Each conversation has a **mode**, chosen with the pill under the message box. New
conversations always start in **Ask**:

- **Ask** -- everything above: an action that changes a machine waits for your Confirm.
- **Auto** -- the AI judges each action as routine or critical and says what it will do. Routine
  ones (a registry query, a gpupdate) run at once; critical ones still show a card with its
  judgement on it.
- **Bypass** -- every action you are allowed to do runs at once.

In every mode a wipe still asks for the machine's name, and keys, tokens and the terminal stay out
of reach. Switching to Auto or Bypass shows a warning first, and every action that ran without
asking is shown in the chat and logged as `assistant.action_auto`, with the mode and the AI's
judgement. After you confirm an action, the assistant reads its outcome and reports back on its
own.

Every confirmed action is in the audit log as `assistant.action_confirmed`, next to the row the
action itself writes. **The assistant sends real machine names** and the data it reads to the
provider; switch *Show the assistant* off to keep AI to the alert recommendations only.
Conversations are private to you and deleted after *Keep assistant conversations for* (30 days).

**Building packages.** With *Let the assistant use the internet* switched on (Settings → AI,
off by default) and the `deploy_packages` permission, you can ask for a package: "make a
package that installs VS Code". The assistant looks the software up in the winget community
repository or on the vendor's site, either uses winget directly or downloads the installer into
a staging folder on the hub (`<log dir>/staging`), picks the silent-install switches, and
creates the package with a detection rule. It tells you which switches it assumed. It never
deploys what it built: schedule the deployment yourself on the Packages page. Reading a web page,
starting a download and creating the package follow the conversation's mode like any other
action, so in Ask you see each address before the hub fetches it. Only https
addresses on the public internet are fetched, never this network; the hub never runs a
downloaded file; the size limit is *Largest package file*. Staged downloads nobody turned into a
package are deleted after *Keep staged downloads for* (24 hours). The hub needs a direct
connection to the internet for this, not one through a proxy.

### Audit log

Every command issued, machine merged or deleted, package deployed, account or
permission group edited, setting changed and backup key touched is written to an
append-only `audit_log` table, and read back on the **Audit Log** tab. Entries
are never rewritten or pruned; with command signing gone, this trail is the
accountability control.

Each entry carries a level -- `info` (routine bookkeeping), `notice` (an operator
changed fleet state) or `security` (identity, secrets, code execution, remote
access). Two capabilities gate the tab: **View audit log** opens it and shows
info + notice entries, and **View security audit entries** additionally reveals
the security ones. The second does nothing on its own, and the split is applied
in SQL -- a withheld entry never reaches the browser. Unlike the rest of the
console the audit log is *not* machine-scoped: most entries have no machine, so
the capability is the whole perimeter.

The tab searches across actor/action/target, filters by actor, level and date
range, pages through history, and expands any entry to its recorded detail.

## Event logs

Windows event records from across the fleet, in one list, on the **Events** page. The question
it exists to answer is the one Event Viewer can only answer one PC at a time: *is somebody
brute-forcing that account?* — 4625 on the Security channel, everywhere, with a count.

**Only what you subscribe to is read.** A subscription names a log channel and, optionally,
event ids, levels and a provider substring. The hub publishes the set to every agent on the
heartbeat; the agent compiles all of one channel's subscriptions into a single event-log query
and sends back the matches. A hub with no subscriptions costs the fleet nothing at all: no
query runs and no payload is sent.

**Repeats are counted, not listed.** Identical records within five minutes share one row with a
`count`, a first and a last occurrence. Five hundred failed logons against one account is one
row saying 500, which is both the readable answer and the only affordable one — the alternative
puts six figures of rows into the same SQLite file that serves the live charts. Two records
that differ in any way, including the message, stay separate, so "which account" is still
answerable.

**Nothing is back-filled.** A subscription added this afternoon collects from this afternoon;
it does not walk back through last month's log.

**What an empty list means.** The page distinguishes "nothing matched" from "nothing is being
collected", and a machine's own view distinguishes both from "this machine has never reported"
— which is what you see for a PC whose agent predates the feature. A collector that stopped and
a fleet that is behaving must not look the same.

**Retention.** `Settings → Data & Retention → Keep collected events for` (14 days by default),
plus a per-machine row cap enforced as records arrive, because the daily prune is the wrong
timescale for one over-broad subscription on a busy server. It is a privacy control as much as
a disk one: a Security-channel record names the account somebody typed a password for.

**Gating**: reading events is `view` + machine scope, like a machine's disks or its available
updates. Creating, editing and deleting subscriptions is `manage_settings` — a subscription is
collection configuration, not an instruction aimed at a machine. Deleting one stops the
collection and keeps what it already collected.

**Endpoints** (console-facing): `GET /api/events`, `GET /api/events/machines/<machine>`,
`GET|POST /api/events/subscriptions`, `PATCH|DELETE /api/events/subscriptions/<id>`. Records
arrive on the existing `POST /api/agent/heartbeat` under an `events` key, and the subscription
document goes back on that same reply.

**Alerting on an event.** A rule condition can read the counters directly, so "more than twenty
failed logons on one machine" is a rule rather than something you have to notice on a page:

| Variable | What it counts |
|---|---|
| `event.count` | every collected record on this machine in the window |
| `event.critical_count`, `event.error_count`, `event.warning_count` | the same, per level |
| `event.id_4625.count` | occurrences of one event id — any id, `4625` here |

The window is `Settings → Event Logs → Count events over` (a day by default), the same one the
Events page counts over, so a threshold fires on the number you were looking at when you chose
it. Occurrences are counted, not rows: a rolled-up run of four hundred 4625s is four hundred.

Two things it deliberately refuses to answer, both for the same reason — the hub will not report
the absence of something it never looked for:

* **An event id you have no subscription for reads unknown, not zero.** Nothing was ever told to
  collect it. The condition builder offers the ids your subscriptions name; you can still type
  one they do not, and a rule about it simply never fires until a subscription covers it.
* **A machine that has never reported events reads unknown, not zero** — that is a PC whose
  agent predates the feature. Otherwise a rule looking for healthy machines would return exactly
  the ones you cannot see. A machine that reported and had nothing to say counts a real zero, and
  one whose last report is over an hour old reads unknown again rather than quiet.
* **A machine that reported an error reads unknown as well**, even though its report is fresh — a
  PC that says it cannot read the Security channel has told you nothing about what is in it. One
  clean report puts the counters back.
* **A machine whose last report lost events to the per-report cap reads unknown too.** A
  heartbeat may carry 200 events; past that the newest go, so a burst of 200 routine events
  followed by one critical one loses the critical one, and a counter reading zero there would be
  wrong in the one direction that matters. The next complete report clears it.
* **A newly subscribed id reads unknown on a machine that has not reported since you added it.**
  Subscribing is not collecting: the PC is still holding the previous subscription set and has
  not looked once. It becomes a real zero on that machine's next report, which is usually
  seconds — but for a PC switched off before a holiday it is a week, and a week of confident
  zeros is exactly what you do not want a brute-force rule reading.

Event collection requires agent 3.37.0 or later. Related events also appear inside
[grouped alerts](#grouped-alerts-and-recommended-fixes).

## Device reports and installed software

Everything the hub knows about one PC on one sheet, printable and exportable -- the thing a
Kaspersky export or a PDQ Inventory report hands a helpdesk when somebody asks "what is this
machine, and what is on it". Three ways in:

- **The machine page's Report tab**, and **Open printable sheet** from there
  (`/reports/machines/<machine>`), which is the page to print. Printing is the browser's own.
- **Reports** in the sidebar (`/reports`): a device group, a search, or the rows ticked on
  **Devices** (bulk bar → **Report selected**) as a summary table, with exports.
- **The software catalog** at the bottom of Reports: every program and version reported across
  the machines you can see, with how many have it. Click a count for the machines.

**An empty section says why it is empty.** A printed sheet outlives the moment it was printed,
and a blank Software heading on paper reads as "nothing installed". So each section says whether
the machine has not checked in yet, whether its agent is too old to collect that (**Waiting for a
newer agent on this machine**), or whether FleetHub does not collect it at all yet (memory
modules, disks, monitors -- roadmap #25 C).

**The report renders stored facts and never asks a machine for anything.** A sheet for a PC
that has been offline for a week is exactly the one a helpdesk needs; each section shows when its
facts were reported.

**Generative AI apps**, at the bottom of Reports, is the software catalog filtered by the
**Generative AI watch list** (Settings → Endpoint Security): name fragments matched anywhere in a
Windows program's name or an Android app's label or package. It flags and blocks nothing. To
sanction a tool, remove its name from the list.

**Security posture** (roadmap #25 D) is a card on the machine page's Overview, a table in the
sheet's Security section, and a fleet table on Reports: one row per check, each **Passed**,
**Failed** or **Unknown**, labelled with the CIS Controls v8.1.2 IG1 safeguard it is evidence for.

| Check | CIS | Passes when |
|---|---|---|
| Antivirus running | 10.1 | a product registered with Security Center is on (or Defender, on a Server) |
| Antivirus signatures current | 10.2 | a running product reports current signatures, or Defender's are within the age limit |
| AutoRun and AutoPlay off | 10.3 | `NoDriveTypeAutoRun = 255` and `NoAutorun = 1` by policy |
| Firewall on for every profile | 4.4, 4.5 | domain, private and public are all on, as in force |
| Screen locks when idle | 4.3 | a machine-wide inactivity limit, or every signed-in user's locking screen saver, is within the limit |
| Default accounts disabled | 4.7 | the built-in Administrator and Guest are disabled |
| Only sanctioned administrators | 5.4 | every member of local Administrators is the built-in Administrator, Domain Admins, an Entra role group, or on the allow list |
| Drives encrypted | 3.6 | every volume BitLocker reports is protected |
| Secure Boot on / TPM ready | -- | as named; not IG1 safeguards in their own right |

The Windows agent reports the facts hourly and the hub judges them, against three settings under
Settings → Endpoint Security: **signature age** (7 days), **idle time before the screen locks**
(900 seconds, the CIS figure) and **also allowed as local administrators** (SID, `DOMAIN\name` or a
bare name; empty by default, so a support group is a finding until somebody names it). A check the
machine could not read is **Unknown** with the reason -- never counted as passed or failed -- and a
PC whose agent predates 3.39.0 has no card at all. Nothing here changes a machine.
`GET /api/posture/machines/<machine>` and `GET /api/posture/fleet` serve it, at `view` + scope.

**Installed software** is read by the Windows agent from the registry's Uninstall keys -- both
the 64- and 32-bit views, plus the hive of every signed-in user -- and filtered the way Programs
and Features filters it. It is re-read hourly and straight after a package deployment, and sent
only when it changed. Per-user installs by somebody who is signed out appear once they sign in
again. `Win32_Product` is deliberately not used: enumerating it makes Windows Installer repair
every MSI on the machine.

**Exports**: full sheets as JSON, and CSV per section -- `summary` (one row per machine),
`software`, `patches`, `network`, `volumes` (one row per item). A count the machine has not
reported is a **blank**, not a 0, so a spreadsheet summing the column does not count a silent PC
as fully patched. Cells that look like spreadsheet formulas are prefixed with `'`.

**Gating**: `view` + machine scope, the same as every page the sheet draws from. No new
capability. Two things are left off the sheet because their own pages gate them higher: who has
read a BitLocker recovery key (`read_recovery_keys`) and backup history (`manage_backups`).

**Endpoints**: `GET /api/reports/machines/<machine>` (`?download=1` for a JSON file),
`GET /api/reports/fleet`, `GET /api/reports/export.json`, `GET /api/reports/export.csv?section=`
(each taking `machines=a,b` or `group=<id>`, default the whole visible fleet),
`GET /api/software/machines/<machine>`, `GET /api/software/catalog?q=`,
`GET /api/software/catalog/machines?name=&version=`, `GET /api/software/genai`. The inventory arrives on the existing
`POST /api/agent/heartbeat` under a `software` key, change-only.

The Software section requires agent 3.38.0 or later; an older agent reads **Waiting for a
newer agent**.
