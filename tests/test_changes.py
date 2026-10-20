"""The live console's change notices (hub/changes.py, app.py, static/js/fleet-live.js).

The silent failure this file exists to catch is **a page that looks live and is not**:

  * a new blueprint whose writes notify nobody, because it never made it into
    changes.BLUEPRINT_TOPICS -- its page would load fresh and then sit there stale, with
    nothing anywhere saying why;
  * a stream (terminal output, remote signalling, upload chunks) that DOES notify, turning
    every chunk into a refetch on every open page;
  * a notice about one machine reaching an operator scoped away from it, or a fleet-wide one
    reaching an unrestricted operator twice;
  * a machine going offline with no notice at all, which is the transition nothing used to
    write and therefore nothing could push.

Runs against the real app, because the after-request hook and the socket rooms ARE what is
being tested.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-changes-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "super@example.com"

import app
import changes
import console_session
import permissions
import settings

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


# --------------------------------------------------------------------------- the model half
def test_notifier_coalesces():
    print("\n-- a burst of writes is one push, de-duplicated --")
    pushed = []
    n = changes.ChangeNotifier(pushed.append)
    for _ in range(50):
        n.mark("commands", "PC-1")
    n.mark("settings")
    n.mark("commands", "PC-2")
    batch = n.flush()
    check("one emit for the whole burst", len(pushed) == 1)
    check("fifty identical marks collapse to one",
          sum(1 for c in batch if c == {"topic": "commands", "machine": "PC-1"}) == 1)
    check("every distinct change survives", len(batch) == 3)
    check("a fleet-wide mark carries no machine",
          {"topic": "settings", "machine": None} in batch)
    check("nothing pending, nothing pushed", n.flush() == [] and len(pushed) == 1)

    capped = changes.ChangeNotifier(lambda b: None, max_pending=2)
    for i in range(10):
        capped.mark("apps", f"PC-{i}")
    batch = capped.drain()
    check("past the cap the TOPIC still reaches every page watching it",
          {"topic": "apps", "machine": None} in batch)


def test_status_tracker():
    print("\n-- online/offline transitions, and only transitions --")
    tracker = changes.StatusTracker()
    check("the first sighting is not a transition",
          tracker.update({"PC-1": "online", "PC-2": "offline"}) == [])
    check("an unchanged answer is not one either",
          tracker.update({"PC-1": "online", "PC-2": "offline"}) == [])
    check("going offline is", tracker.update({"PC-1": "offline", "PC-2": "offline"}) == ["PC-1"])
    check("a report flips it back immediately", tracker.note("PC-1", "online") is True)
    check("...and a second report is not news", tracker.note("PC-1", "online") is False)
    tracker.update({"PC-1": "online"})
    check("a deleted machine is forgotten, so a re-enrolment starts fresh",
          tracker.update({"PC-1": "online", "PC-2": "online"}) == [])


def test_request_classification():
    print("\n-- which requests notify --")
    rule = "/api/device-groups"
    check("a successful POST under /api/ marks",
          changes.should_mark_request("POST", "/api/device-groups", rule, 200))
    check("a GET never does", not changes.should_mark_request("GET", "/api/x", "/api/x", 200))
    check("a refused write changed nothing",
          not changes.should_mark_request("POST", "/api/x", "/api/x", 403))
    check("a page route is a render, not a mutation",
          not changes.should_mark_request("POST", "/login", "/login", 200))
    check("/api/report is already new_temp",
          not changes.should_mark_request("POST", "/api/report", "/api/report", 200))
    check("terminal output is a stream, not a change",
          not changes.should_mark_request("POST", "/api/agent/pty/abc/output",
                                          "/api/agent/pty/<session_id>/output", 200))
    check("an idle heartbeat marks nothing", changes.heartbeat_topics({"config_version": "x"}) == [])
    check("capabilities ride every heartbeat, so they mark nothing",
          changes.heartbeat_topics({"capabilities": {"features": []}}) == [])
    check("a change-only block marks its topic",
          changes.heartbeat_topics({"software": {"programs": []}, "bitlocker": {}})
          == ["bitlocker", "software"])
    check("an EMPTY app list is still a report",
          changes.heartbeat_topics({"apps": {"apps": []}}) == ["apps"])
    known_topics = set(changes.BLUEPRINT_TOPICS.values()) | set(changes.HEARTBEAT_TOPICS.values())
    stray = set(changes.TOPIC_CAPABILITIES) - known_topics
    check(f"every gated topic is one something actually marks (stray: {sorted(stray)})",
          not stray)
    bogus = set(changes.TOPIC_CAPABILITIES.values()) - set(permissions.CAPABILITIES)
    check(f"every gate is a real capability (unknown: {sorted(bogus)})", not bogus)
    check("view itself never gates a topic -- that would be an ordinary notice",
          permissions.VIEW not in changes.TOPIC_CAPABILITIES.values())


# --------------------------------------------------------------------------- the routes
def test_every_writing_blueprint_has_a_topic():
    print("\n-- every route that writes is live by default --")
    missing = set()
    stale_unmarked = set(changes.UNMARKED_RULES)
    app_endpoints = set()
    for rule in app.app.url_map.iter_rules():
        stale_unmarked.discard(rule.rule)
        if not (set(rule.methods or ()) & MUTATING):
            continue
        if not rule.rule.startswith("/api/"):
            continue
        if "." in rule.endpoint:
            blueprint = rule.endpoint.split(".", 1)[0]
            if blueprint not in changes.BLUEPRINT_TOPICS:
                missing.add(blueprint)
        elif rule.rule not in changes.UNMARKED_RULES and rule.rule not in changes.UNMARKED_PATHS:
            app_endpoints.add(rule.endpoint)
    check(f"every blueprint with a write maps to a topic (missing: {sorted(missing)})",
          not missing)
    unmapped = app_endpoints - set(changes.APP_ENDPOINT_TOPICS)
    check(f"every write in app.py itself maps to a topic or is a stream (unmapped: "
          f"{sorted(unmapped)})", not unmapped)
    check(f"every excluded stream is still a real route (stale: {sorted(stale_unmarked)})",
          not stale_unmarked)


# --------------------------------------------------------------------------- the rooms
def received_changes(sock):
    """Flatten every `changes` event a socket test client has received since last asked."""
    out = []
    for packet in sock.get_received():
        if packet["name"] == "changes":
            for arg in packet["args"]:
                out.extend(arg.get("changes", []))
    return out


def test_rooms_and_scope():
    print("\n-- a notice reaches exactly the people who may see it, once --")
    permissions.create_group(
        app.DB_PATH, "Techs", capabilities=[permissions.VIEW],
        machines=["PC-1"], members=["tech@example.com"])
    settings.invalidate()

    super_http = app.app.test_client()
    console_session.sign_in(super_http, "super@example.com")
    tech_http = app.app.test_client()
    console_session.sign_in(tech_http, "tech@example.com")
    super_sock = app.socketio.test_client(app.app, flask_test_client=super_http)
    tech_sock = app.socketio.test_client(app.app, flask_test_client=tech_http)
    check("both operators connect", super_sock.is_connected() and tech_sock.is_connected())
    anon = app.socketio.test_client(app.app, flask_test_client=app.app.test_client())
    check("a signed-out browser is refused a socket", not anon.is_connected())

    app.change_notifier.flush()
    received_changes(super_sock)
    received_changes(tech_sock)

    app.mark_changed("machines", "PC-2")
    app.change_notifier.flush()
    check("an unrestricted operator hears about any machine",
          {"topic": "machines", "machine": "PC-2"} in received_changes(super_sock))
    check("a scoped operator does NOT hear about a machine outside their scope",
          received_changes(tech_sock) == [])

    app.mark_changed("machines", "PC-1")
    app.change_notifier.flush()
    got = received_changes(tech_sock)
    check("...but does hear about their own", {"topic": "machines", "machine": "PC-1"} in got)
    check("...exactly once", len(got) == 1)
    check("the unrestricted operator hears it once too, not once per room",
          len(received_changes(super_sock)) == 1)

    app.mark_changed("alerts")
    app.change_notifier.flush()
    check("a fleet-wide notice reaches a scoped operator",
          received_changes(tech_sock) == [{"topic": "alerts", "machine": None}])
    check("...and an unrestricted one, once",
          received_changes(super_sock) == [{"topic": "alerts", "machine": None}])

    print("\n-- an admin-only topic reaches only the operators holding its capability --")
    # The silent failure here is a leak, not a stale page: a view-only operator watching the
    # socket would learn exactly when tokens were minted or users edited.
    # `permissions` is gated too, but it also ends every socket: its own section is below.
    for topic in ("settings", "users", "tokens", "audit"):
        app.mark_changed(topic)
    app.change_notifier.flush()
    got_super = received_changes(super_sock)
    check("a view-only operator hears none of them", received_changes(tech_sock) == [])
    check("the operator holding each capability hears each one, once",
          sorted(c["topic"] for c in got_super) == ["audit", "settings", "tokens", "users"])
    app.mark_changed("backups", "PC-1")
    app.change_notifier.flush()
    check("...even about a machine inside the view-only operator's scope",
          received_changes(tech_sock) == [])
    check("a gated notice never carries the machine name",
          received_changes(super_sock) == [{"topic": "backups", "machine": None}])

    print("\n-- a write through the API notifies with no code in the route --")
    r = super_http.post("/api/device-groups",
                        json={"name": "Live", "target": {"include": [{"kind": "all"}]}})
    check(f"the write succeeds ({r.status_code})", 200 <= r.status_code < 300)
    app.change_notifier.flush()
    time.sleep(0.05)
    check("...and its blueprint's topic is pushed",
          any(c["topic"] == "device_groups" for c in received_changes(super_sock)))
    r = super_http.post("/api/device-groups", json={"name": ""})
    app.change_notifier.flush()
    check(f"a refused write ({r.status_code}) pushes nothing",
          r.status_code >= 400 and not any(c["topic"] == "device_groups"
                                           for c in received_changes(super_sock)))

    print("\n-- a permission change ends every live socket, so rooms are re-resolved --")
    # Rooms are fixed at connect time. The hub must not rely on the browser choosing to
    # reconnect: a client that ignores the notice would keep the rooms it was revoked from.
    app.mark_changed("permissions")
    app.change_notifier.flush()
    check("the unrestricted operator's socket is ended",
          not super_sock.is_connected())
    check("...and so is the scoped one's, whatever its script does next",
          not tech_sock.is_connected())
    again = app.socketio.test_client(app.app, flask_test_client=tech_http)
    check("reconnecting goes back through the connect check", again.is_connected())

    # The console's Sync now, not just the scheduler: an OU move found by a manual pass must
    # end live sockets too. LDAP is stubbed to report a change, which is all that matters.
    import directory
    from unittest import mock
    real_enabled = settings.get_bool

    def fake_sync(db_path, config, on_change=None):
        on_change()
        return {"objects_found": 1, "matched": 1, "unmatched": []}

    def enabled(db, key):
        return True if key == "directory.enabled" else real_enabled(db, key)

    with mock.patch.object(directory, "sync_once", fake_sync), \
            mock.patch.object(settings, "get_bool", enabled):
        r = super_http.post("/api/directory/sync", json={})
    app.change_notifier.flush()
    check(f"Sync now that moved a machine ({r.status_code}) ends live sockets too",
          r.status_code == 200 and not again.is_connected())


def test_offline_transition_is_pushed():
    print("\n-- a machine going dark is a notice, not something to wait for a poll to notice --")
    now = app.to_timestamp_str(app.datetime.now())
    with app.get_db_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO machine_info(machine, updated_at) VALUES (?, ?)",
                     ("PC-9", now))
    app.status_watch_pass()
    app.change_notifier.drain()
    app.status_watch_pass()
    check("a machine that stays online marks nothing",
          not any(c["topic"] == "status" for c in app.change_notifier.drain()))
    with app.get_db_conn() as conn:
        conn.execute("UPDATE machine_info SET updated_at = ? WHERE machine = ?",
                     ("2000-01-01 00:00:00", "PC-9"))
    app.status_watch_pass()
    check("going offline marks `status` for that machine",
          {"topic": "status", "machine": "PC-9"} in app.change_notifier.drain())
    app.status_watch_pass()
    check("...once, not on every pass after",
          not any(c["topic"] == "status" for c in app.change_notifier.drain()))

    print("\n-- an alert whose detail changes without moving updated_at still notifies --")
    # alerts.sync_ticket_alert refreshes a ticket alert's count and ids but leaves
    # updated_at alone, so the list keeps its order. The signature must see it anyway.
    import alerts
    alerts.sync_ticket_alert(app.DB_PATH, alerts.KIND_TICKET_NEW, "PC-9", [1], raise_new=True)
    app.status_watch_pass()
    app.change_notifier.drain()
    alerts.sync_ticket_alert(app.DB_PATH, alerts.KIND_TICKET_NEW, "PC-9", [1, 2])
    app.status_watch_pass()
    check("a detail-only refresh marks `alerts`",
          {"topic": "alerts", "machine": None} in app.change_notifier.drain())
    app.status_watch_pass()
    check("...and an unchanged table does not",
          not any(c["topic"] == "alerts" for c in app.change_notifier.drain()))


def park_background_flusher():
    """The app starts its own flusher thread at import. Left running it could drain a mark
    before a test does and turn an assertion into a coin toss, so it is parked on an hour-long
    sleep: the one cycle already under way finishes inside the wait below, and every later
    flush in this file is the test's own."""
    app.change_notifier._flush_seconds = 3600
    app.mark_changed("park")
    time.sleep(changes.FLUSH_SECONDS + 0.5)
    app.change_notifier.drain()


def main():
    park_background_flusher()
    test_notifier_coalesces()
    test_status_tracker()
    test_request_classification()
    test_every_writing_blueprint_has_a_topic()
    test_rooms_and_scope()
    test_offline_transition_is_pushed()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
