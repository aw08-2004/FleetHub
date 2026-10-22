# Remote view and control

## Remote view & control

Live remote view **and control** of a managed PC over WebRTC (H.264), from the **Remote**
page (linked from each machine page). Gated on the `remote_control` capability plus the machine being in
the operator's scope; every session start/stop is in the audit log.

- **How it works.** The agent runs as SYSTEM in session 0, which has no desktop to capture,
  so on session start it injects a helper (the same signed agent binary, `--remote-helper`)
  as SYSTEM into the interactive session. The helper captures the screen (DXGI), encodes
  H.264, and streams it to the operator's browser over WebRTC; the browser sends mouse and
  keyboard back over a data channel (`SendInput`). Ctrl+Alt+Del is supported.
- **Which session gets captured — and choosing it.** The **Session** picker in the Remote page's viewer
  lists the machine's logon sessions (who, where, what state), reported on the agent heartbeat.
  Leave it on **Auto** and the agent picks: a session Windows reports as `WTSActive`, not merely
  the physical console — on a machine administered over RDP the console session is often signed
  out and has no rendered desktop at all. The console wins ties, so a physically-present user
  beats a stray RDP session. Failing that, it falls back to the **console session at the logon
  screen**, which is what makes signing in remotely to a machine nobody is signed into possible
  at all. Injecting into the wrong session is not an error anyone notices: the helper starts,
  Desktop Duplication finds nothing to duplicate and falls back to GDI, and the operator gets a
  perfectly healthy stream of **a black screen**. When even the GDI fallback can see nothing (a
  disconnected or signed-out session), the viewer says so and points at the Session picker, and the
  helper backs off its retries to one every 10 s rather than filling its log. The helper logs which
  tier it chose and its own `session=` at startup.
- **The lock screen, the logon screen, and UAC.** The helper follows the Windows *input
  desktop*, re-attaching its capture and input threads whenever it switches (`Default` ↔
  `Winlogon`) and rebuilding Desktop Duplication on the new desktop. That is what makes the lock
  screen visible and typeable. Signing in destroys the Windows session the helper lives in, so
  the service supervises the helper and re-injects it into the new session — the view comes back
  on its own a few seconds after the remote login succeeds.
- **Auto follows the session; a pinned session stays pinned.** Sign-out and *switch user* move
  the interactive desktop into a different Windows session, and only sometimes by killing the
  one the helper is in. On a switch-user the old session survives, disconnected but intact, so
  the helper keeps streaming a live picture of a desktop that is no longer on the monitor and no
  longer where the operator's keystrokes land. With **Auto** selected the service re-asks which
  session is interactive every few seconds and moves the helper when the answer changes (the
  browser reconnects on its own, as it does after a sign-in). A session the operator picked by
  hand is never moved — watching one user's session while somebody else uses the console is a
  legitimate thing to be doing.
- **A screen nobody is touching is not a broken capture.** Desktop Duplication reports "no new
  frame" for a desktop that has not changed, which is the permanent state of a logon screen.
  Treating that as a failure is how the remote view used to end up black with *"the agent is not
  getting any frames"* on a machine whose monitor was showing the logon prompt perfectly well —
  the pipeline discarded the frame it already held, and its fallback to GDI (which cannot see
  the secure desktop at all) guaranteed black. The pipeline now keeps the picture it has, only
  reports a stall when it has never had one, and only leaves Desktop Duplication when the
  duplication itself cannot be built.
- **Headless machines need a virtual display.** Desktop Duplication duplicates a display
  *output*; a machine with no monitor has none, so there is genuinely nothing to capture and the
  stream is black. The viewer shows a **No display outputs** badge on such a machine and
  offers to install a bundled IddCx **virtual display driver** on demand, per machine (never
  fleet-wide, never automatically). It is a user-mode (UMDF2) driver, so no test-signing, Secure
  Boot or HVCI changes are involved — but installing it does add its publisher to that machine's
  TrustedPublisher store, which is why the command output names the exact subject and thumbprint
  and the action is audited at security level. Set the payload once: upload the driver's
  "Driver Only" zip on the **Packages** page, then pin it under **Settings → Remote Control**.
  Uninstall is available from the same panel, and `monitors: 0` stands the driver down without
  removing it once a real monitor is plugged in.
- **Stream controls.** Session, codec (H.264 or VP8) and encoder (auto / hardware / software)
  are chosen **before** connecting — they are negotiated in the SDP or decide which encoder gets
  built, so changing them needs a new session, and the UI disables them while connected rather
  than letting a change silently do nothing. Monitor, quality preset (or raw fps / bitrate /
  resolution scale), **fullscreen** and **view only** change **live** mid-session over the
  control data channel. The presets are **Auto** (the default), Quality (30 fps, 12 Mbps),
  Balanced (20 fps, 6 Mbps), Speed (12 fps, 2 Mbps at 75 % scale) and Custom. The bitrate is
  what the encoder aims for, not a ceiling. **Auto** starts at 15 fps / 2.5 Mbps and, on a
  session relayed through the hub, steps down as far as 6 fps / 0.4 Mbps when the picture
  falls behind live, and back up to 20 fps / 6 Mbps once the link keeps up; the current
  bitrate is shown next to the picture size. Over a direct WebRTC connection Auto stays at its
  starting level. View-only is an accident guard on the viewer, not a permission: it stops
  *this browser* sending input, and does not stop anyone else driving the machine.
- **Consent** is `unattended` by default (connects immediately, standard RMM) or `attended`
  (the logged-in user must approve first). Set it in **Settings → Remote Control**.
- **TURN.** Agents sit behind arbitrary NATs, so WebRTC media usually needs a TURN relay.
  **The hub is the TURN server by default** (coturn from [`turn/`](../turn/README.md)), but it does
  not have to be — the relay can live on any Windows box you can forward ports to. Install it
  there on its own with *Install TURN* (`-Component Turn`) and point
  **Settings → Remote Control** at it; the questions, preconditions and verification are
  identical either way. The **hub installer sets this up for you** when you want both on one
  machine: choose *Install Hub*, answer yes to "Configure this hub as the
  TURN/STUN server", and it generates `REMOTE_TURN_SECRET`, seeds
  **Settings → Remote Control → TURN/STUN servers** to `turn:<host>:3478` / `stun:<host>:3478`,
  and then builds the relay itself. On Windows that means a **dedicated `FleetHubTurn` WSL2
  distro running coturn natively** under systemd — created, configured, firewalled (both the
  Windows *and* Hyper-V firewalls), given a boot task, and then **verified with a real TURN
  Allocate** before the installer finishes. Any secret it generates is printed **once** at the
  end — save it. The hub mints short-lived per-session credentials from it (nothing per-user).
  Needs Windows 11 22H2+, the Store WSL package, and ~3 GB free; the installer checks each and
  skips TURN with an explanation rather than half-configuring anything.
- **Managing TURN from the UI.** The whole config lives in **Settings → Remote Control**: the
  STUN/TURN URLs, consent mode, TTLs, and a **TURN status card** that shows whether
  `REMOTE_TURN_SECRET` is set and — the number that predicts a working session — how many ICE
  servers a session hands a peer right now (`0` is the "agent logs `ice_servers=0` → peer
  failed" case). From that card an admin (`manage_settings`) can **set or rotate the secret**
  without shell access; it writes `.env` and applies live. Because coturn validates against
  its own copy, **a rotation is only half done until you sync it** — the new value is shown once
  so you can put it in the relay's config (`/etc/turnserver.conf` in the WSL distro, then
  `systemctl restart coturn`; `turn/.env` for a Docker relay). Leaving the secret unset simply
  omits TURN (STUN/direct paths only — fine on a LAN). See [turn/README.md](../turn/README.md) for
  ports, the public-IP requirement, and the host-OS notes.
- **Why the Windows installer builds a WSL distro rather than running a container.** A coturn
  *container* on Windows relays from the Docker bridge. `--external-ip` makes it advertise the
  public address and inbound DNAT works — so both peers allocate and the logs look healthy — but
  relay→peer egress is SNAT'd to an arbitrary source port, and ICE requires the peer to receive
  from *exactly* the advertised candidate. Every check fails and the agent logs
  `peer connection state: failed`. A WSL distro in mirrored networking mode shares the host's
  network namespace, so the relay ports stay symmetric. `docker-compose.windows.yml` is kept for
  LAN and credential testing only. [turn/README.md](../turn/README.md#host-os-notes) has the full
  forensics and a symptom-ordered troubleshooting table.
- **Debugging a session that won't connect.** Read the two logs together: the agent's
  `C:\ProgramData\FleetHub\Agent\remote-helper.log` and coturn's. `ice_servers=0` means TURN
  isn't configured; allocations incrementing **twice** in coturn's log means both peers reached
  and authenticated against the relay, which rules out reachability, the shared secret and the
  credential scheme in one stroke. Always confirm with a machine on a genuinely different
  network — a LAN test succeeds on host candidates alone and proves nothing about TURN.
  The `local ICE candidate:` and `remote ICE candidate:` lines name each side's candidate
  **types**, and they decide the case: `host` only on both sides (the browser's show as
  `<uuid>.local`), with `Connection attempt to ICE server ... timed out` for every URL, means
  neither peer reached coturn at all, so every session falls back to the hub. The console loading
  on the same hostname proves nothing here, because 443 and 3478 are separate port forwards.
  See the first row of the [troubleshooting table](../turn/README.md#troubleshooting); it also
  says why a second, public STUN URL beside the relay's own is worth adding.
- **When WebRTC cannot connect at all, the session falls back to the hub** (hub 1.141.0 +
  agent 3.42.0). If the connection fails — or has not connected twenty seconds after the
  agent's offer — the viewer asks the hub to carry the session instead: the agent POSTs its
  already-encoded frames to the hub over the HTTPS it reports on, the browser long-polls them
  back and decodes them itself (WebCodecs), and input goes the other way. The status pill reads
  **Live (via hub)**. It is a fallback, not a mode: it costs hub bandwidth (every frame passes
  through twice), adds a round trip of delay, and the hub sees the picture rather than
  encrypted media, so every switch writes a `remote_session_relay` row to the audit log. Turn it
  off with **Settings → Remote Control → Fall back to relaying through the hub**, and a session
  that cannot connect directly then fails as before. Not offered for a machine borrowed from
  another hub, nor by an agent older than 3.42.0 (it does not say it can relay, so the viewer
  never asks). A dead relay is still worth fixing — the fallback is slower — and the diagnosis
  above is unchanged; `remote-helper.log` now also says when and why a session switched.
  A relayed viewer that falls behind (a phone on mobile data, typically) **jumps to the
  present** instead of replaying old motion (hub 1.163.0): the hub skips to the newest
  keyframe, and the picture holds still for a moment rather than showing movement that has
  already happened.

- **Diagnosing capture before you involve a browser.** Two agent-binary self-tests, both
  runnable on the machine itself: `--desktop-probe [seconds]` prints the input desktop as it
  changes and whether this process can attach to it (lock the screen, trigger a UAC prompt), and
  `--remote-capture-test <file.h264> <seconds> [monitor] [fps] [kbps] [encoder]` writes a playable
  clip straight from the capture and encode path with no hub, browser or session injection
  involved. `encoder` (auto / hardware / software) pins the H.264 MFT flavour, which separates a
  hardware encoder that will not run on this machine from a broken capture pipeline.

### Clipboard

Plain text crosses between the remote PC and your own machine, in both directions (hub 1.153.0
+ agent 3.46.0, issue #148). Anyone who may remote-control a PC may use it; there is no separate
permission.

- **From the PC.** Copy as usual on the PC. Chrome and Edge put the text straight onto your
  own clipboard. Safari and Firefox only allow that on a click, so the **Copy from PC** button
  lights up instead; click it. This works in View only too.
- **To the PC.** Click the picture and press **Ctrl+V**: your clipboard is sent first, then the
  keystroke, so the PC pastes what you copied here. The first time, the browser may ask to allow
  clipboard access. **Paste to PC** sends your clipboard without a keystroke, for when the
  browser will not let a keystroke read it. Pasting is input, so View only blocks it.
- **Limits.** Text only, about 64,000 characters each way. Longer text is cut short, and the
  viewer says so. Files go through the Files tool.
- **On a Mac.** ⌘ is the Windows key on a PC, so ⌘C would arrive as Win+C. Apple keyboards get
  **Send ⌘ as Ctrl**, on by default: ⌘C and ⌘V then copy and paste on the PC. Untick it to use ⌘
  as the Windows key.
- **Copy and paste between two programs on the PC** never needed any of this; Windows does it.
  If that fails, check View only first, then that you are on the Windows session the user is
  working in.
- An agent older than 3.46.0 does not offer clipboard sync, and the buttons stay hidden.

### Session recording

Record what you are seeing in a remote session, from the **Record** button in the Remote
page's viewer (roadmap #19). Whoever may remote-view a PC may record it: the
`remote_control` capability plus the PC in scope, and only the operator who opened the
session. There is no separate recording permission.

- **Never silent.** The PC shows a red **Recording Screen** badge in the top-left corner for
  the whole recording, on the lock screen and the logon screen too. The hub stores no video
  until the agent confirms that the badge is on screen. If the badge cannot follow the
  session to a new desktop, the recording ends. Needs agent
  **3.43.0** or later: an older agent never confirms the badge, and the recording fails after
  about 25 seconds with a sentence that says so.
- **A reason is required** at start. It goes to the audit log with who recorded which PC, and
  for how long. Video only; keystrokes are never recorded.
- **Ten minutes at a time.** In the last minute the viewer shows a countdown with **Extend
  10 minutes** and **Stop recording**. With no answer, the recording ends. Extending needs no
  new reason. The countdown is shown in the console only, never on the PC, so it is never in
  the video.
- **A session switch starts the next part.** Switching user, signing in or signing out on the
  PC restarts the remote helper and changes the picture. One file can't hold two streams, so
  the current part ends and the viewer starts **Part 2** on its own once the new session is
  on screen: same reason, no dialog, and the badge has to show in the new session before
  anything is stored. The same happens when a session falls back to the hub relay. Parts
  share the recording's ten minutes (a switch in the last minute leaves up to a minute, but
  never more than one minute past the agreed time however often it happens), and sharing any part
  shares all of them. **Stop recording** between parts means no next part.
- **Where they are kept.** `<hub install>\data\<your email>\recordings\<id>.webm`, kept
  until the owner deletes them. The hub locks the `data` folder to SYSTEM and Administrators
  at every start, the same way it locks `.env`. Override the location with
  `HUB_RECORDINGS_DIR`.
- **The Recordings page** lists your own recordings, with their total size, and the ones
  shared with you. You can share your own with permission groups, named people, or both. A
  group share follows the group: whoever is a member when they open it may watch. Only the
  owner can share or delete a recording; people it is shared with can play and download it.
  Recordings, extensions, shares, downloads and deletes are all in the audit log
  (`recording_*`), and so is watching: `recording_view`, once per viewer per recording per
  half hour, because the hub cannot tell a player from a script saving the file.
- **Checking the badge on a PC** without a hub or a session:
  `FleetHubAgent.exe --recording-badge-test 30` (`TempMonitorAgent.exe` on an older install) shows it for 30 seconds and follows the
  lock screen when it runs as SYSTEM in the console session.
