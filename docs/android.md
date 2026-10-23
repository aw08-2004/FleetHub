# Android devices

## The agent app

Opening **FleetHub Agent** on a device shows its status first -- managed, not enrolled, offline,
or no hub configured -- then a setup checklist: enrollment, location, notifications, battery
optimisation, usage access and device owner. Each row says what goes wrong without it and has the
button that fixes it. Nothing on the first screen prompts for a credential or opens the keyboard.

**Enroll with a one-time code.** Mint one under Administration → Onboarding, press **Enroll this
device**, type the six characters. The code admits that device once. The fleet's shared secret
still works under *Advanced*, but it enrols any device and stays on the one it was typed into.

**The hub URL is locked on a fully managed device.** Once a device provisioned by QR has a hub,
the *Hub URL* field under *Advanced* is greyed out and cannot be saved, so the person holding the
phone cannot point it away from the console. Moving such a device to a different hub takes a
factory reset and a new QR. A device with the agent installed as an ordinary app keeps the field
editable, and so does a managed device that has no hub yet.

## Fully managed features

These features need the Android agent on a fully managed device (see
[Android device provisioning](#android-device-provisioning) and
[agent-android/README.md](../agent-android/README.md)). They are built but **not yet
validated on hardware**; see the hardware-validation table in [ROADMAP.MD](../ROADMAP.MD).
Test a wipe only on a scratch device.

## Blocking apps

Deciding which apps a managed device may run, on the **Device policy** page.

**Two kinds.** A **blocklist** names apps to suspend and leaves everything else alone. An
**allowlist** names the only apps that may run; everything else installed is suspended. A device
can be covered by several: blocklists union, and allowlists **intersect** -- each says "only
these", so two of them applying leaves what both permit. A union would let a second allowlist
quietly widen the first.

**Suspended, not hidden.** The icon stays where it was, greyed, and the system itself says
"paused by your organisation" when somebody taps it. Hiding makes an app silently vanish, which
reads as "uninstalled" -- and what follows is a ticket, or somebody factory-resetting a phone to
get their app back.

**Nothing can suspend the launcher, the settings app, the dialer, the emergency app or the agent
itself.** The agent enforces that list independently of the hub, so a hub that is compromised,
misconfigured or simply newer cannot brick a device. The console holds a copy so it never offers
you a policy it already knows will be partly refused, and names anything it drops rather than
removing it silently.

**Saving is gated on previewing.** The preview answers "which packages, on which devices, and
how many are actually installed" before anything is stored, and it answers *per device* rather
than as a fleet total -- "14 apps" reads as small; "14 apps on this phone, including the camera"
does not. Any edit invalidates it.

**The dead-man switch.** A device that stops hearing from this hub lifts every restriction on
its own after `policy.max_age_seconds` (7 days). That is deliberate and one-way: the hub can
make a device *more* restricted only while it can reach it, and a phone whose hub is
decommissioned, misconfigured or behind a firewall must not become one somebody has to factory
reset. The clock is measured from the hub's last *confirmation*, so a policy re-affirmed every
ten seconds never expires.

**Compliance is three views, not two**: what the policy asks for, what the device says it did,
and what its own inventory shows. The interesting failures are where they disagree --
`setPackagesSuspended` returns the packages it could **not** suspend, and a policy reported as
applied while three of its targets are still running is worse than no policy.

**Gating**: writing is `manage_device_policy`, a capability of its own -- a policy is not a
setting but a standing instruction that changes what somebody's device does, applied without
anybody present. Reading a policy is `view`; reading a machine's compliance is `view` + machine
scope. Every write is audited at security level.

**Endpoints**: `GET /policy` (the page), `GET|POST /api/policy/apps`,
`GET|PUT|DELETE /api/policy/apps/<id>`, `POST /api/policy/preview`,
`GET /api/policy/machines/<machine>`. The document reaches devices on the existing
`POST /api/agent/heartbeat`, sent only when its version differs from the one the device reports.

## Blocked hours and screen-time budgets

When a managed device may run what, in the **Schedules** section of the Device policy page.

**Two kinds, again.** **Blocked hours** name days and a span -- "nothing on weeknights between
22:00 and 07:00", over named apps or over the whole device. A **daily budget** names an
allowance -- "sixty minutes of TikTok", or "two hours of screen time" for the device as a whole.
Zero minutes is a real budget and means "not at all today".

**An end earlier than the start runs past midnight**, which is the only kind of curfew anybody
actually writes. The half after midnight belongs to the day *after* the one the rule names, so a
Friday-night curfew still holds at 02:00 on Saturday.

**The device decides, not the hub.** The hub sends the rules; the agent evaluates them every
minute against its own clock and its own usage, whether or not it can reach the hub. A phone
with no signal at 22:00 still has a bedtime. This is also why blocked hours keep working on a
device where usage access was never granted: they need a clock, not a ledger.

**Both apply, and nothing is reconciled.** If one rule says "no TikTok after 22:00" and another
says "sixty minutes a day", both hold and whichever bites first is the answer. Two budgets on
the same app keep the smaller number.

**The clock is not the user's to move.** A fully managed device is held to network time, so a
curfew cannot be escaped in date and time settings. Where that cannot be applied the agent logs
it in as many words rather than pretending.

**Budgets need usage access, and somebody has to grant it on the device.** It is an appop --
`setPermissionGrantState` does not reach it and no Device Owner can turn it on remotely. A
device without it reports no usage, and the console says so on the machine page rather than
showing zeroes that would read as "nobody used this". The agent app has a **Grant usage access**
button that opens the right settings screen.

**Gating**: writing is `manage_device_policy`, the same capability as a blocklist. Reading a
machine's usage is `view` + machine scope, and there is deliberately **no fleet-wide usage
view** -- the app inventory has one because a policy author has to pick a package from
somewhere, and the question a fleet-wide usage listing would answer is "who spends the most time
on their phone". Every write is audited at security level.

**Retention**: `data.usage_retention_days`, 14 days by default and meant to come down. Pruned by
the device's own local day, and erased immediately when a machine is deleted. See the
personal-data inventory in `SECURITY.MD`.

**Endpoints**: `GET|POST /api/policy/times`, `GET|PUT|DELETE /api/policy/times/<id>`,
`GET /api/usage/machines/<machine>`. Rules reach devices in the same `device_policy` block as a
blocklist; usage arrives on the heartbeat under a `usage` key.

## Locking and wiping a device

Securing a managed device that has been lost, in the **Lock and wipe** card on the machine page.

**Two actions, deliberately unequal.** **Lock** locks the screen now and is undone by whoever
holds the device with their own PIN -- it is the ordinary first move for a phone left in a taxi,
and it is one click. **Wipe** is a factory reset. There is no undo, no dry run and no partial
version of it, so it is behind a second button, a typed machine name and a confirmation dialog.
All the friction is on the half that cannot be taken back; spreading it over both is how people
learn to type past it.

**The typed name is checked on the server.** The console asks for it as a courtesy; the control
is in the hub, because a dialog stops an operator and does not stop a script. The comparison is
exact and case-sensitive -- `PHONE-1` and `phone-12` are two devices.

**Factory-reset protection is a per-device choice, and the console says which it is sending.**
Clearing it means the wiped device can be set up again by anybody, which is right for
company-owned hardware. Leaving it on means the device cannot be set up again without the
account that was signed in on it -- theft protection if it was stolen, a brick if that account
was not yours. The default is to clear it.

**A wiped device never reports back**, so the hub records the *request* rather than waiting for
a result. That row is the only thing that can tell an operator why a machine stopped reporting;
without it a wiped phone and a flat battery look identical in the console.

**Gating**: `wipe_device`, a capability of its own. Not `issue_commands` -- the argument that
puts every other command under that gate ("less dangerous than the SYSTEM shell it already
grants") is true of a reboot and false of an erase. Seeing that a device was locked or erased is
`view` + machine scope. This page is the only door: a hand-rolled `wipe_device` through the
generic command endpoint is refused, it cannot be saved as a favorite, and a rule may not issue
one.

**Endpoints**: `GET /api/wipe/machines/<machine>`,
`POST /api/wipe/machines/<machine>/lock`, `POST /api/wipe/machines/<machine>/wipe`. Every write
is audited at security level, before the command is created.

## App inventory

What a managed Android device says is installed, on the machine page under **Installed apps**.

**It exists so that app policy can be written by a human.** Blocking an app means naming a
package, and a package name is not something an operator knows: `com.google.android.youtube` is
guessable, `com.zhiliaoapp.musically` is TikTok. Without a list to pick from, a policy editor is
a text box that punishes typos with silence -- a package that does not exist on the device is
simply not blocked, and nothing says so.

**System apps are hidden by default and findable on demand.** A phone reports 150-400 packages
and perhaps thirty are things somebody installed. Showing all of them first buries the thirty
that answer the question; showing only the thirty hides the browser, the store and the camera,
which are exactly the ones a helpdesk gets asked about.

**`Suspended` and `Disabled` are what the DEVICE says, not what a policy asked for.** That is
the whole reason those states are stored: a policy applied to fourteen apps that took on eleven
must be visible, and a table written from intent could never show it.

**`QUERY_ALL_PACKAGES` is not requested.** Android 11 hid the full package list behind that
permission; a device owner is exempt, so a fully managed device reports everything without it.
An unmanaged device reports a partial list, which reads honestly as a device that is not fully
managed.

**Gating**: `view` + machine scope, like a disk layout. Read-only -- there is no way to tell the
hub a device has an app it does not have.

**Endpoints**: `GET /api/apps/machines/<machine>`, `GET /api/apps/packages`,
`GET /api/apps/packages/<package>/machines`. The inventory arrives on the existing
`POST /api/agent/heartbeat` under an `apps` key, change-only.

## Locating a device

Asking a managed Android device where it is, on demand.

**On demand only, and that is the design rather than a first step.** Nothing polls. A position
exists in the hub because an operator pressed a button and the device answered; there is no
background collection and no travel history, only a history of *when somebody asked*.

**Asking has its own capability — `locate_device` — and it is deliberately not
`issue_commands`.** Wake, the Processes card and the file explorer all reuse that one on the
argument that each is less dangerous than the SYSTEM shell it already grants, which holds
because all three act on a machine. This one acts on a person: *being able to reboot a PC must
not silently mean being able to find out where an employee is*. The hub refuses a hand-rolled
`locate_device` through the generic command endpoint and refuses to save one as a favorite, so
the capability cannot be routed around. **Reading** a last known position is `view` + machine
scope, like any other thing a machine reports.

**The device says who asked.** The agent posts a notification naming the operator, every time,
*including when it has no position to give* — otherwise a failed request would be a quiet way
to check whether somebody's phone is switched on. The audit trail records the same name.

**Three outcomes, kept apart**, because they send you to three different places: a position; the
device answered and had no fix (location switched off, nothing within the time budget, the
permission never granted — the reason comes back with it); and the device never answered at all
(switched off, out of coverage). A stale answer is labelled `stale` and carries the time the fix
was actually taken — a last-known position shown as current is somebody driving to where a phone
used to be.

**Framework `LocationManager`, not Google Play Services.** The fused provider is better
indoors, and would be the first Google dependency in a repo that vendors nothing from Google —
failing outright on de-Googled builds and on the cheap tablets a fleet buys. Both providers are
asked at once and the most accurate answer within the budget wins, so a network fix does not
beat a GPS one just by arriving first.

**Settings**: `location.default_timeout_seconds` (45) is how long the device spends looking;
`data.location_retention_days` (30) is how long positions are kept. The second is a privacy
control rather than a disk-space one — see the personal-data inventory in `SECURITY.MD`.

### Seeing it

Two views over the same data. A **Location fold** on the machine page carries the last known
fix, a Locate button, and the history of who asked and what came back. The **Map** page
(`/map`, in the sidebar under Inventory) plots every device in scope that has a known position,
with a filter and a list beside it.

**The map never claims more precision than the fix had.** The accuracy radius is drawn as a
real circle in metres, so a fix that might be a kilometre wide looks a kilometre wide; a radius
the device did not state is not drawn at all rather than assumed. Stale fixes are a different
colour and say how old they are. The coordinates are always shown in full — they are what you
paste into a phone before walking out of the door to find something.

**A device that can be located and has no position is listed, not hidden.** Under **Not
located yet** beside the map, with what it said the last time anybody asked -- *the location
permission has not been granted on this device*, say -- or that nobody has asked, and a Locate
button if you hold `locate_device`. Before 0.3.0 the agent never asked for the permission, so
every phone answered that way and the map showed nothing at all.

**The fold does not appear on a device that cannot locate**, which today is every Windows PC.
Hidden rather than greyed: a permanently dead card on several hundred machine pages is worse
than no card.

**Map tiles are the one thing the console fetches from a third party at page load** — a
deliberate exception to the "works on an isolated LAN" claim in
`hub/static/vendor/README.md`. Tiles are content rather than code, and `map.tile_url` exists so
you can point at your own tile server. Blank it and the map draws devices on an empty
background with their coordinates and says so, rather than showing grey squares.

**Endpoints**: `GET|POST /api/location/machines/<machine>`, `GET /api/location/fleet`,
`GET /map`.

## Restart, app installs and Wake-on-LAN

Three things a fully managed device does from 0.3.0 that an ordinary app cannot.

**Restart** works from the machine page and from rules like it does on a PC. The command answers
first and the device restarts five seconds later at the earliest, so the console never shows a
restart that worked as one that hung. A device that is not fully managed refuses with the reason.

**App installs** go through **Packages**, with the **Install an Android app** step (an APK
payload). An Android package can hold only that step, every payload must be pinned by sha256,
and it takes no detection rule -- the device asks Android afterwards whether the app is installed
at the version the APK declares. A deployment aimed at a mixed group keeps each package on its
own platform: a phone is never sent a Windows package, nor a PC an APK; those targets are marked
failed with the reason. The agent refuses to install itself this way.

**Wake-on-LAN relay**: a fully managed phone on the office Wi-Fi reports its adapter, and the hub
can pick it to broadcast the magic packet for a sleeping PC on the same subnet. Only a device
owner can read the phone's real MAC, so only those are picked. Some access points drop broadcast
frames between wireless clients; a wake is confirmed by the PC checking in, not by the packet.

**Security posture**: a phone is judged on screen lock (CIS 4.3), storage encryption (3.6) and a
**Security patch recent** check (7.3, limit `security.posture_patch_max_age_days`, 90 days) --
not on the Windows checks it could never answer.

## Android device provisioning

Enrolling an Android device as a **fully managed device** (device owner), so the agent can be
located, locked and policed rather than merely installed. The **Device provisioning** page is
the whole of it: upload the signed APK, scan the code.

**A device owner is taken once, at a factory-reset device's setup wizard, and never again.** It
cannot be granted after an account exists on the device, there is no API to add it later, and
there is no supported way to move it to a different app. That is what makes every field in this
payload consequential: a wrong component name or a wrong checksum is not "the QR did not work",
it is "the QR did not work and the device has already been wiped".

So the hub **refuses to build a partial payload**. A payload missing a field still encodes,
still scans, and still fails several minutes later on a device you cannot get back. An
unconfigured hub answers the page with what is missing and how to produce it.

**What you need**, once per fleet: the signed APK. Upload it on that page and the hub does the
rest.

- **The hub hosts the file.** A factory-reset device downloads it from this hub's own address,
  over whatever network it has been put on, at a URL carrying a random token. That download
  needs no sign-in and cannot — the device has nothing to sign in with, because it is
  downloading the app that would later enrol it.
- **The hub derives the checksum**, by reading the signing certificate out of the APK itself.
  This used to be typed in, converted by hand from what `apksigner verify --print-certs` prints,
  and it was the single most likely thing to get wrong: the hex form is 64 characters in the
  right alphabet, so it saves without complaint and fails on a device that has already been
  wiped. The converter is still on the page, now as a cross-check — paste what apksigner prints
  and confirm it matches what the hub derived.
- **Uploading again, or removing the APK, mints a new token**, so codes printed from the
  previous one stop working rather than pointing at a file that is gone. That is also how you
  kill a code somebody photographed off a bench.

**One scan does everything.** The code carries the hub URL and the fleet's enrollment secret in
the provisioning extras bundle, so a device comes up managed, pointed at this hub, and enrolled.
Without the secret it would come up managed, reporting telemetry, and silently accepting no
commands. **Treat a photograph of the code the way you would treat the enrollment secret**, and
note that every fetch is recorded in the audit trail at security level — with the secret itself
redacted out of the row.

**No Wi-Fi credentials are in the code, deliberately.** The extras can carry an SSID and PSK;
the hub has nowhere honest to keep a PSK (settings are rendered into a form and partly shipped
to agents), and a QR displayed on a screen or taped to a bench should not be a network
credential. The setup wizard asks for Wi-Fi itself.

**Gating**: `manage_settings` throughout — page, payload, upload and converter. Not
`issue_commands`, which is the capability somebody reaches for by reflex: this page hands out a
credential and decides what a device installs as its owner, so it belongs with the capability
that can already read and write the rest of the hub's configuration. The APK download is the one
exception, and the one route in this product with no gate at all. What the file contains is a
signed release binary and nothing else: no fleet data, no hub address, no credentials. See
`SECURITY.MD`.

**Endpoints**: `GET /provisioning` (the page), `GET /api/provisioning/qr`,
`GET|POST|DELETE /api/provisioning/apk`, `POST /api/provisioning/checksum`, and the
unauthenticated `GET /provisioning/apk/<token>/fleethub-agent.apk`.
