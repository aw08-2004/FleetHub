"""Change notices for the live console: "topic X changed (for machine Y)", pushed, coalesced.

Before this, a console page was fresh at the moment it loaded and went quietly stale while
the operator sat on it. Five pages polled every 30 seconds; everything else -- a ticket
somebody else answered, an alert the rule evaluator just raised, a deployment the scheduler
started, a machine that went offline -- stayed wrong until somebody pressed F5. **The silent
failure this module exists to prevent is a page that looks live and is not**: an operator
reading "online" off a machine that died four minutes ago is worse off than one who knows
to refresh.

**What is pushed is a NAME, never the data.** A notice says only that a topic changed and,
when it is about one machine, which machine. The page then refetches through the same gated
REST route it loaded from, so every capability and scope check that already guards that
route keeps guarding the live view too. Rejected:

- *Pushing the changed rows themselves.* Socket.IO rooms are fixed at connect time (see
  app.py, handle_socket_connect), so filtering a payload per listener would mean
  re-resolving permissions for every operator on every write -- and getting it wrong once
  leaks a machine's data to somebody scoped away from it. tickets_web.py made the same call
  for the same reason (roadmap #31); this generalises it.
- *Shorter polling.* Load scales with tabs x pages x frequency, and most polls answer
  "nothing changed". A notice costs nothing until something does.
- *WebSockets.* Waitress cannot serve them; the socket stays polling-only (app.py).

**Coalesced, because writes come in bursts.** A fleet-wide deployment completing is a few
hundred command results inside a couple of seconds. `ChangeNotifier.mark` only records the
(topic, machine) pair; a flusher thread drains the set every FLUSH_SECONDS and hands the
de-duplicated batch to the injected emit function, so a burst is one push per flush, not
one per write. The browser throttles its refetch on top of that (static/js/fleet-live.js).

Flask-free, like every model half here: the notifier takes an emit callable and never
imports socketio, so it is unit-testable standalone (tests/test_changes.py).
"""
import threading
import time

import permissions

#: How often marks are drained into a push. Under a second so "live" reads as live to
#: somebody watching two tabs; long enough that a burst of writes is one push.
FLUSH_SECONDS = 0.75

#: Upper bound on distinct changes held between flushes. A runaway writer (a loop marking
#: per row) degrades to "everything changed" instead of growing a set without limit.
MAX_PENDING = 500

#: The topic a mutating request under each blueprint marks. Keyed by Blueprint NAME (the
#: first argument to Blueprint(...)), because that is what Flask exposes as
#: request.blueprint. The browser subscribes by these same strings, so they are an API
#: between hub/ and hub/static/js/ -- rename one and the page that watched it silently
#: stops updating. tests/test_changes.py fails when a blueprint with a mutating route is
#: missing from this map, which is the way a new feature area would otherwise go un-live.
#:
#: Only `permissions` affects socket ROOMS, so only it ends live sockets (app.py,
#: _drop_live_sockets). `device_groups` and `users` look as if they should and do not:
#: permission scope is a group's machine list or its AD OU, never a device group, and a user
#: record's writes are profile fields -- deleting one deliberately leaves group membership
#: alone (users.delete_user). An AD sync that moves a machine marks `permissions` itself.
BLUEPRINT_TOPICS = {
    "ai": "settings",
    "apitokens": "tokens",
    "apps": "apps",
    "assistant": "assistant",
    "audit": "audit",
    "authconfig": "settings",
    "backups": "backups",
    "bios": "firmware",
    "bitlocker": "bitlocker",
    "capabilities": "capabilities",
    "correlate": "correlate",
    "device_groups": "device_groups",
    "directory": "directory",
    "discovery": "discovery",
    "disk_usage": "disk_usage",
    "events": "events",
    "files": "files",
    "fleet": "commands",
    "invites": "invites",
    "location": "location",
    "onboarding": "onboarding",
    "packages": "deployments",
    "patches": "patches",
    "permissions": "permissions",
    "policy": "policy",
    "posture": "posture",
    "processes": "processes",
    "provisioning": "provisioning",
    "push": "push",
    "recordings": "recordings",
    "remote": "remote",
    "reports": "reports",
    "rules": "alerts",
    "settings": "settings",
    "sharing": "sharing",
    "software": "software",
    "tickets": "tickets",
    "usage": "usage",
    "users": "users",
    "wake": "wake",
    "watchdogs": "watchdogs",
    "webfetch": "webfetch",
    "wipe": "wipe",
}

#: Topics with no blueprint of their own, marked explicitly by app.py: the machine record
#: (identity, inventory, delete/merge) and its derived online/offline status.
MACHINES = "machines"
STATUS = "status"

#: The permissions blueprint's topic, named because app.py acts on it beyond pushing it: a
#: batch carrying it ends every live socket so rooms are re-resolved (app.py,
#: _drop_live_sockets). Rooms are fixed at connect time, so this is how a scope change --
#: a revocation above all -- reaches a socket that is already open.
PERMISSIONS = "permissions"

#: Topics only some operators may even hear about, and the capability that decides it.
#:
#: A notice carries no data, but it does say WHEN something changed, and for these topics
#: that alone is administrative information: a view-only operator has no business watching
#: API tokens being minted, users being edited, permission groups being rearranged, or the
#: audit log moving (raised in review on PR #158). So these go only to the room of operators
#: holding that capability (app.py, capability_room), and always WITHOUT a machine name --
#: scope is a second axis rooms cannot intersect with cheaply, and a page watching one of
#: these topics re-reads through its gated route either way, so dropping the name costs only
#: a little targeting.
#:
#: Each capability is the one the topic's own READ routes require -- the people who can
#: load that page are exactly the people who may know it changed. Deliberately absent:
#: topics a view-only page still reads (alerts, firmware, events, recordings, tickets,
#: watchdogs, patches, policy...), which would stop those pages updating for the very people
#: allowed to see them. Two accepted costs, both re-read on the next reconnect or visit:
#: the sidebar of an operator without `manage_settings` no longer refreshes on a settings
#: change (e.g. the Assistant being switched on), and a view-only operator's own paired-
#: device list no longer follows a pairing made from another tab.
TOPIC_CAPABILITIES = {
    "audit": permissions.VIEW_AUDIT_LOG,
    "backups": permissions.MANAGE_BACKUPS,
    "deployments": permissions.DEPLOY_PACKAGES,
    "directory": permissions.MANAGE_SETTINGS,
    "invites": permissions.MANAGE_PERMISSION_GROUPS,
    "onboarding": permissions.MANAGE_ONBOARDING,
    "permissions": permissions.MANAGE_PERMISSION_GROUPS,
    "provisioning": permissions.MANAGE_SETTINGS,
    "settings": permissions.MANAGE_SETTINGS,
    "tokens": permissions.MANAGE_USERS,
    "users": permissions.MANAGE_USERS,
    "webfetch": permissions.DEPLOY_PACKAGES,
}


def required_capability(topic):
    """The capability a listener must hold to hear about `topic`, or None for anyone with view."""
    return TOPIC_CAPABILITIES.get(topic)


#: Request paths that must NOT mark from the generic after-request hook. /api/report is
#: already pushed as `new_temp`, every few seconds per machine, and marking it too would
#: turn every report into a refetch on every open page. The heartbeat marks explicitly,
#: and only when it carried a change (fleet_web.py).
UNMARKED_PATHS = frozenset({"/api/report", "/api/agent/heartbeat"})

#: URL RULES (the route template, request.url_rule.rule) that are streams or keepalives
#: rather than state changes a page shows. Each fires many times a second or once per
#: chunk while something is running, and the view that cares already polls it on its own
#: tight loop -- a notice per chunk would only make every other open page refetch in time
#: with somebody's terminal. Rules rather than paths so an id in the URL still matches.
UNMARKED_RULES = frozenset({
    "/api/machines/<machine>/live/watch",
    "/api/agent/commands/<command_id>/output",
    "/api/agent/pty/<session_id>/output",
    "/api/fleet/pty/<session_id>/input",
    "/api/agent/remote/<session_id>/signal",
    "/api/remote/session/<session_id>/signal",
    "/api/peer/shares/<share_id>/remote/<session_id>/signal",
    "/api/remote/turn/secret",
    "/api/remote/recordings/<recording_id>/chunks/<int:seq>",
    "/api/agent/backups/upload/<run_id>",
    "/api/agent/files/transfer/<transfer_id>",
    "/api/language",
    "/api/hub/update",
})

#: app.py's own mutating routes have no blueprint; they are about the machine record, except
#: the one that dismisses an alert. Keyed by endpoint (the view function's name).
APP_ENDPOINT_TOPICS = {
    "put_machine_primary_sensor": "machines",
    "put_machine_channel": "machines",
    "delete_machine": "machines",
    "merge_machines_endpoint": "machines",
    "dismiss_alert": "alerts",
}


#: The heartbeat's change-only inventory blocks, and the topic each one feeds. A key that is
#: present means the agent saw it change (its reporters are change-only, CLAUDE.md) -- so
#: marking on presence costs nothing on the idle heartbeat, which carries none of these.
#: `capabilities` is deliberately absent: it rides EVERY heartbeat (fleet_web.py says why),
#: and marking it would refetch the machine page every ten seconds for nothing. So is
#: `processes`, which only flows while somebody's Processes card is open and polling it.
HEARTBEAT_TOPICS = {
    "apps": "apps",
    "bios": "firmware",
    "bitlocker": "bitlocker",
    "events": "events",
    "network": MACHINES,
    "patches": "patches",
    "policy_state": "policy",
    "posture": "posture",
    "profiles": "backups",
    "remote": "remote",
    "software": "software",
    "usage": "usage",
    "watchdog": "watchdogs",
}


def heartbeat_topics(data):
    """The topics a heartbeat body marks: one per change-only block it carried."""
    return sorted({topic for key, topic in HEARTBEAT_TOPICS.items()
                   if isinstance(data, dict) and data.get(key) is not None})


def topic_for_request(blueprint, endpoint):
    """The topic a mutating request marks, or None for one that marks nothing.

    A blueprint route maps by blueprint name; one of app.py's own routes maps by endpoint.
    """
    if blueprint:
        return BLUEPRINT_TOPICS.get(blueprint)
    return APP_ENDPOINT_TOPICS.get(endpoint or "")


def should_mark_request(method, path, rule, status_code):
    """True for a request whose success means something a page shows may have changed.

    Only successful writes under /api/: a GET changes nothing, a 4xx/5xx was refused and
    changed nothing, a page route (not /api/) is a render, not a mutation, and a stream
    (UNMARKED_RULES) is not a change anybody else's page shows.
    """
    if method in ("GET", "HEAD", "OPTIONS"):
        return False
    if not (200 <= int(status_code) < 300):
        return False
    if not path.startswith("/api/"):
        return False
    if path in UNMARKED_PATHS:
        return False
    return rule not in UNMARKED_RULES


class ChangeNotifier:
    """Collects (topic, machine) marks and pushes them in de-duplicated batches.

    `emit(changes)` receives a list of {"topic": str, "machine": str|None} dicts, sorted so
    a batch is deterministic (tests compare them). It is called from the flusher thread,
    never from the writer that marked -- a slow socket must not hold up /api/report or a
    scheduler tick.
    """

    def __init__(self, emit, flush_seconds=FLUSH_SECONDS, max_pending=MAX_PENDING):
        self._emit = emit
        self._flush_seconds = flush_seconds
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._pending = set()
        self._overflow = set()
        self._wake = threading.Event()
        self._thread = None

    def mark(self, topic, machine=None):
        """Record that `topic` changed, optionally for one machine. Cheap and thread-safe."""
        if not topic:
            return
        machine = str(machine).strip() if machine else None
        with self._lock:
            if len(self._pending) >= self._max_pending:
                # Past the cap, keep the TOPIC and drop the machine: every page watching the
                # topic still refetches, which is the whole contract -- just less targeted.
                self._overflow.add(topic)
            else:
                self._pending.add((topic, machine or None))
        self._wake.set()

    def drain(self):
        """Take the pending batch (possibly empty) and reset. Exposed for tests and flush."""
        with self._lock:
            pending, self._pending = self._pending, set()
            overflow, self._overflow = self._overflow, set()
        for topic in overflow:
            pending.add((topic, None))
        return [{"topic": t, "machine": m}
                for t, m in sorted(pending, key=lambda p: (p[0], p[1] or ""))]

    def flush(self):
        """Push whatever is pending now. Returns the batch that was pushed."""
        batch = self.drain()
        if batch:
            self._emit(batch)
        return batch

    def start(self):
        """Start the daemon flusher. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="change-notifier", daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            self._wake.wait()
            # Sleep FIRST, then drain: the writes that follow the first one in a burst land
            # in the same batch instead of each opening its own.
            time.sleep(self._flush_seconds)
            self._wake.clear()
            try:
                self.flush()
            except Exception as exc:  # a dead socket must never kill the flusher
                print(f"[changes] flush failed: {exc}")


class StatusTracker:
    """Remembers each machine's last online/offline answer and reports the transitions.

    Status is derived on read (app.py, derive_machine_status) and nothing ever stored a
    transition, so nothing could push one: a machine going offline was invisible to an
    open page until its next poll or reload. `update` takes the current answers and returns
    the machines whose answer changed since the previous call. The very first sighting of a
    machine is not a transition -- the page that is open already shows what it loaded.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}

    def update(self, statuses):
        """statuses: {machine: "online"|"offline"}. Returns sorted list of changed machines.

        Machines missing from `statuses` (deleted) are forgotten, so a re-enrolled machine
        of the same name starts fresh.
        """
        changed = []
        with self._lock:
            for machine, status in statuses.items():
                previous = self._last.get(machine)
                if previous is not None and previous != status:
                    changed.append(machine)
                self._last[machine] = status
            for machine in set(self._last) - set(statuses):
                del self._last[machine]
        return sorted(changed)

    def note(self, machine, status):
        """Record one machine's answer outside the sweep. True when it is a transition."""
        with self._lock:
            previous = self._last.get(machine)
            self._last[machine] = status
        return previous is not None and previous != status
