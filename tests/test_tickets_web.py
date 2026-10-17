"""Tickets over HTTP (roadmap #31): the agent upload, the two capabilities, and machine scope.

The silent failures this file exists to catch:

  * **An id-keyed route gated on the capability but not the scope.** `require_machine` cannot
    read a machine out of `/api/tickets/<id>`, so every ticket route checks scope inline. One
    route that forgets hands a scoped operator another site's tickets -- and their screenshots
    -- by walking the ids. Checked on the detail, the screenshot and every write.
  * **"Missing" and "out of scope" answering differently**, which turns that same walk into a
    count of another site's tickets even where it cannot read them.
  * **A ticket readable with VIEW alone.** A ticket is somebody's words and screen, not
    inventory; the sidebar link, the page and the API all need `view_tickets`.
  * **The submitter policy living only in the tray.** With `tickets.submitters = domain` the
    hub itself must refuse a local account; a check that lives only on the PC is skippable.
  * **The upload taking whatever it is sent.** The hub sets no MAX_CONTENT_LENGTH, so a route
    that forgot its own cap accepts anything; and a file that is not an image must never be
    served back as one.
  * **One PC filing tickets as another, or one person reading another's replies** through the
    tray's "My tickets".
  * **The socket payload carrying ticket text** to a fleet room that includes operators
    without `view_tickets`.

Run from the repo root so `import app` resolves.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-tickets-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@example.com"

import app
import permissions
import settings
import tickets

PASS = 0
FAIL = 0
CSRF_TOKEN = "tickets-test-token"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128

EMITTED = []


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def client_for(email):
    """A signed-in client that sends the CSRF header, as common.js does in a browser. Without
    it every write is a 403 from login_required and the gate checks pass for the wrong reason."""
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = {"email": email}
        sess["csrf_token"] = CSRF_TOKEN
    c.environ_base["HTTP_X_CSRF_TOKEN"] = CSRF_TOKEN
    return c


def _enroll(machine):
    """An agent identity minted through fleet.py -- see test_watchdogs_web._enroll for why not
    through the enroll route."""
    import fleet
    secret = "tickets-test-enrollment-secret"
    agent_id, token = fleet.enroll_agent(app.DB_PATH, machine, secret, secret)
    return {"Authorization": f"Bearer {agent_id}:{token}"}


def setup():
    with sqlite3.connect(app.DB_PATH) as conn:
        for name in ("PC-1", "PC-2"):
            conn.execute("INSERT OR IGNORE INTO machine_info (machine) VALUES (?)", (name,))
    permissions.init_permissions_db(app.DB_PATH)
    permissions.invalidate()
    permissions.create_group(
        app.DB_PATH, name="Viewers", capabilities=[permissions.VIEW],
        machines=["PC-1", "PC-2"], members=["viewer@example.com"], actor="root@example.com")
    permissions.create_group(
        app.DB_PATH, name="Ticket readers PC-1",
        capabilities=[permissions.VIEW, permissions.VIEW_TICKETS],
        machines=["PC-1"], members=["reader@example.com"], actor="root@example.com")
    permissions.create_group(
        app.DB_PATH, name="Helpdesk PC-1",
        capabilities=[permissions.VIEW, permissions.VIEW_TICKETS, permissions.MANAGE_TICKETS],
        machines=["PC-1"], members=["desk@example.com"], actor="root@example.com")
    permissions.invalidate()
    # Capture what would go out over Socket.IO, without a socket.
    app._emit_fleet_event = lambda event, payload, machine: EMITTED.append((event, payload))


def submit(headers, **over):
    body = {"subject": "Printer jams", "body": "Every page", "urgency": "high",
            "reporter_name": "CORP\\ana", "reporter_sid": "S-1-5-21-9",
            "reporter_kind": "domain"}
    body.update(over)
    return app.app.test_client().post("/api/agent/tickets", json=body, headers=headers)


def test_agent_routes(pc1, pc2):
    print("\n-- the agent half --")
    anon = app.app.test_client()
    check("no bearer, no ticket", anon.post("/api/agent/tickets",
                                            json={"subject": "x"}).status_code == 401)
    r = submit(pc1)
    check("an enrolled PC can open a ticket", r.status_code == 201)
    ticket_id = r.get_json()["id"]
    check("...filed under the TOKEN's machine", tickets.machine_of(app.DB_PATH, ticket_id) == "PC-1")
    r = submit(pc1, machine="PC-2")
    check("a machine named in the body is ignored",
          tickets.machine_of(app.DB_PATH, r.get_json()["id"]) == "PC-1")
    check("a bad urgency is a 400", submit(pc1, urgency="panic").status_code == 400)

    snapshot = tickets.get_ticket(app.DB_PATH, ticket_id)["facts"]
    check("the snapshot came from the composition root", snapshot.get("machine") == "PC-1"
          and "captured_at" in snapshot)

    check("a socket event went out", EMITTED and EMITTED[0][0] == "ticket_new")
    check("...carrying ids, never the ticket's text",
          all("subject" not in p and "body" not in p for _, p in EMITTED))

    settings.set_many(app.DB_PATH, {"tickets.submitters": "domain"}, updated_by="root@example.com")
    r = submit(pc1, reporter_kind="local")
    check("domain-only: the hub refuses a local account", r.status_code == 403
          and r.get_json().get("code") == "submitter_not_allowed")
    check("domain-only: a domain account is still taken",
          submit(pc1, reporter_kind="domain").status_code == 201)
    settings.set_many(app.DB_PATH, {"tickets.submitters": "any"}, updated_by="root@example.com")
    check("any: a local account is taken", submit(pc1, reporter_kind="local").status_code == 201)

    c = app.app.test_client()
    url = f"/api/agent/tickets/{ticket_id}/attachments"
    r = c.post(url, data=JPEG, headers={**pc1, "Content-Type": "image/jpeg"})
    check("a screenshot is accepted", r.status_code == 201)
    shot_id = r.get_json()["id"]
    check("another PC's agent cannot add to it",
          c.post(url, data=JPEG, headers={**pc2, "Content-Type": "image/jpeg"}).status_code == 404)
    check("an unknown ticket answers the same 404",
          c.post("/api/agent/tickets/999999/attachments", data=JPEG,
                 headers={**pc1, "Content-Type": "image/jpeg"}).status_code == 404)
    # A real oversized body rather than a forged Content-Length: the test client recomputes
    # the header from the data, so a forged one would never reach the route.
    oversized = JPEG + b"\x00" * tickets.MAX_ATTACHMENT_BYTES
    check("an upload over the cap is a 413",
          c.post(url, data=oversized, headers={**pc1, "Content-Type": "image/jpeg"}
                 ).status_code == 413)
    check("something that is not an image is a 415",
          c.post(url, data=b"<svg onload=alert(1)>", headers={**pc1, "Content-Type": "image/png"}
                 ).status_code == 415)

    tickets.add_message(app.DB_PATH, ticket_id, "desk@example.com", "Try tray 2")
    tickets.add_message(app.DB_PATH, ticket_id, "desk@example.com", "user is clueless",
                        internal=True)
    mine = c.get("/api/agent/tickets?sid=S-1-5-21-9", headers=pc1).get_json()["tickets"]
    bodies = [m["body"] for t in mine for m in t["messages"]]
    check("My tickets shows the reply", "Try tray 2" in bodies)
    check("...and never an internal note", "user is clueless" not in bodies)
    check("another PC with the same SID sees nothing",
          c.get("/api/agent/tickets?sid=S-1-5-21-9", headers=pc2).get_json()["tickets"] == [])
    check("no SID, no tickets", c.get("/api/agent/tickets", headers=pc1).get_json()["tickets"] == [])
    return ticket_id, shot_id


def test_console_gates(pc1_ticket, shot_id, pc2):
    print("\n-- the console: capability AND scope on every route --")
    pc2_ticket = submit(pc2, urgency="low").get_json()["id"]

    viewer = client_for("viewer@example.com")
    check("VIEW alone does not open the page", viewer.get("/tickets").status_code == 403)
    check("VIEW alone does not read the list", viewer.get("/api/tickets").status_code == 403)
    check("VIEW alone does not read a ticket",
          viewer.get(f"/api/tickets/{pc1_ticket}").status_code == 403)
    check("VIEW alone does not see the sidebar link",
          b'href="/tickets"' not in viewer.get("/").data)

    reader = client_for("reader@example.com")
    check("view_tickets opens the page", reader.get("/tickets").status_code == 200)
    listed = {t["id"] for t in reader.get("/api/tickets").get_json()["tickets"]}
    check("the list holds in-scope tickets", pc1_ticket in listed)
    check("...and not another site's", pc2_ticket not in listed)
    check("an in-scope ticket reads", reader.get(f"/api/tickets/{pc1_ticket}").status_code == 200)
    out = reader.get(f"/api/tickets/{pc2_ticket}")
    missing = reader.get("/api/tickets/999999")
    check("an out-of-scope ticket is a 404", out.status_code == 404)
    check("...indistinguishable from a missing one",
          out.get_json() == missing.get_json() and missing.status_code == 404)
    check("the out-of-scope detail PAGE is refused too",
          reader.get(f"/tickets/{pc2_ticket}").status_code == 404)
    check("the machine-keyed list uses require_machine",
          reader.get("/api/tickets/machines/PC-2").status_code == 403
          and reader.get("/api/tickets/machines/PC-1").status_code == 200)
    check("the agent id is not handed to the console",
          "agent_id" not in reader.get(f"/api/tickets/{pc1_ticket}").get_json())

    r = reader.get(f"/api/tickets/{pc1_ticket}/attachments/{shot_id}")
    check("an in-scope screenshot is served", r.status_code == 200
          and r.mimetype == "image/jpeg")
    check("...never cached", "no-store" in r.headers.get("Cache-Control", ""))
    check("...and never sniffed", r.headers.get("X-Content-Type-Options") == "nosniff")
    check("a screenshot asked for under another ticket's id is not served",
          reader.get(f"/api/tickets/{pc2_ticket}/attachments/{shot_id}").status_code == 404)

    check("view_tickets alone cannot reply",
          reader.post(f"/api/tickets/{pc1_ticket}/messages", json={"body": "hi"}).status_code == 403)

    desk = client_for("desk@example.com")
    r = desk.post(f"/api/tickets/{pc1_ticket}/messages", json={"body": "On my way"})
    check("manage_tickets can reply", r.status_code == 201)
    check("a reply without JSON is refused",
          desk.post(f"/api/tickets/{pc1_ticket}/messages", data="body=x",
                    content_type="application/x-www-form-urlencoded").status_code == 415)
    for path, body in (("messages", {"body": "x"}), ("status", {"status": "closed"}),
                       ("assign", {"assignee": "me"}), ("labels", {"label": "x"}),
                       ("triage", {}), ("script", {})):
        r = desk.post(f"/api/tickets/{pc2_ticket}/{path}", json=body)
        check(f"an out-of-scope {path} write is refused", r.status_code in (403, 404))
    r = desk.post(f"/api/tickets/{pc1_ticket}/assign", json={"assignee": "me"})
    check("'me' is stored as the address", r.get_json()["assignee"] == "desk@example.com")
    check("status moves", desk.post(f"/api/tickets/{pc1_ticket}/status",
                                    json={"status": "waiting"}).status_code == 200)
    check("an unknown status is a 400", desk.post(f"/api/tickets/{pc1_ticket}/status",
                                                  json={"status": "gone"}).status_code == 400)
    check("drafting a script needs issue_commands as well",
          desk.post(f"/api/tickets/{pc1_ticket}/script", json={}).status_code == 403)
    r = desk.post(f"/api/tickets/{pc1_ticket}/triage", json={})
    check("triage with AI switched off is refused, not faked", r.status_code == 502)

    root = client_for("root@example.com")
    check("an unrestricted operator sees both",
          {pc1_ticket, pc2_ticket} <= {t["id"] for t in root.get("/api/tickets").get_json()["tickets"]})


def test_phase_d(pc1, pc2, ticket_id):
    print("\n-- phase D: the heartbeat marker and the reporter's reply --")
    c = app.app.test_client()
    beat = c.post("/api/agent/heartbeat", json={}, headers=pc1).get_json()
    check("an agent that sends no marker is never answered one", "tickets_marker" not in beat)
    beat = c.post("/api/agent/heartbeat", json={"tickets_marker": ""}, headers=pc1).get_json()
    marker = beat.get("tickets_marker")
    check("a tray agent holding nothing is told the current marker", bool(marker))
    beat = c.post("/api/agent/heartbeat", json={"tickets_marker": marker},
                  headers=pc1).get_json()
    check("...and is told nothing while it matches", "tickets_marker" not in beat)

    url = f"/api/agent/tickets/{ticket_id}/messages"
    r = c.post(url, json={"sid": "S-1-5-21-9", "body": "Tray 2 jams too"}, headers=pc1)
    check("the reporter answers from the tray", r.status_code == 201)
    beat = c.post("/api/agent/heartbeat", json={"tickets_marker": marker},
                  headers=pc1).get_json()
    check("...which moves the marker", beat.get("tickets_marker") not in (None, marker))
    mine = c.get("/api/agent/tickets?sid=S-1-5-21-9", headers=pc1).get_json()["tickets"]
    check("...and the answer is in My tickets",
          any(m["body"] == "Tray 2 jams too" and m["author_kind"] == "reporter"
              for t in mine for m in t["messages"]))
    check("no bearer, no reply",
          app.app.test_client().post(url, json={"sid": "S-1-5-21-9", "body": "x"}
                                     ).status_code == 401)
    check("another PC's agent cannot answer it",
          c.post(url, json={"sid": "S-1-5-21-9", "body": "x"}, headers=pc2).status_code == 404)
    check("another person's SID cannot answer it",
          c.post(url, json={"sid": "S-1-5-21-8", "body": "x"}, headers=pc1).status_code == 404)
    check("an empty reply is a 400",
          c.post(url, json={"sid": "S-1-5-21-9", "body": "  "}, headers=pc1).status_code == 400)
    tickets.set_status(app.DB_PATH, ticket_id, tickets.STATUS_CLOSED)
    r = c.post(url, json={"sid": "S-1-5-21-9", "body": "hello?"}, headers=pc1)
    check("a closed ticket is a 409 the tray can explain", r.status_code == 409)
    tickets.set_status(app.DB_PATH, ticket_id, tickets.STATUS_OPEN)

    config = c.post("/api/agent/heartbeat", json={"config_version": "stale"},
                    headers=pc1).get_json().get("config", {})
    check("the tray switch rides the agent config channel, off by default",
          config.get("tickets.tray_enabled") is False)
    check("...beside the submitter policy the tray hides its icon by",
          config.get("tickets.submitters") in ("any", "domain"))


def test_hourly_cap(pc1, pc2):
    print("\n-- the hub's per-machine ceiling --")
    # The hub's own per-machine ceiling, which a stolen token cannot skip the way it can skip
    # the service's per-person limit. PC-2 so PC-1's tickets above do not count toward it.
    for _ in range(tickets.MAX_TICKETS_PER_MACHINE_HOUR):
        submit(pc2, urgency="low", reporter_sid="S-1-5-21-77")
    r = submit(pc2, urgency="low", reporter_sid="S-1-5-21-77")
    check("past the hourly cap a machine is refused with 429", r.status_code == 429
          and r.get_json().get("code") == "rate_limited")
    check("...and another machine is not", submit(pc1, urgency="low").status_code == 201)


def main():
    setup()
    pc1 = _enroll("PC-1")
    pc2 = _enroll("PC-2")
    ticket_id, shot_id = test_agent_routes(pc1, pc2)
    test_phase_d(pc1, pc2, ticket_id)
    test_console_gates(ticket_id, shot_id, pc2)
    test_hourly_cap(pc1, pc2)
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
