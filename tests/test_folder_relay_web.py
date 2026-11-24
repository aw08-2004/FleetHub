"""HTTP-layer test for folder_relay_web.py -- the live folder copy between two PCs.

The silent failure this file exists to catch is **a copy onto a machine the operator was
never given**. The route is named for the SOURCE, so the usual require_machine decorator
covers that end; the TARGET arrives in the body, where no decorator can see it, and is checked
inline. Drop that inline check and an operator scoped to one site can write folders onto any
PC in the fleet, and nothing on their screen would look different. So the assertion that
matters most here is that a tech scoped to PC-1 is refused a send to PC-3.

Also asserted:

  * an agent acts only as the end it is: PC-2 (the target) cannot put chunks, PC-1 (the
    source) cannot take them, and a third enrolled PC can do neither;
  * both ends' agent versions are checked before anything is queued, because an old target
    answers "unknown command type" and the sender would otherwise wait out the idle timeout;
  * a full round trip -- meta, chunks, end, take, result -- ends in `done`, with the bytes
    the receiver took identical to the bytes the sender put.

Wires the blueprint onto a minimal Flask app, same approach as test_files_web.
"""
import functools
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import files
import fleet
import folder_relay
import permissions
import settings
from folder_relay_web import create_folder_relay_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def set_version(db_path, machine, version):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO machine_info(machine, companion_version) VALUES (?, ?) "
                     "ON CONFLICT(machine) DO UPDATE SET companion_version = excluded.companion_version",
                     (machine, version))


def seed_listing(db_path, machine, folder, entries):
    """Store a ready listing of `folder`, as the Files tab leaves behind when an operator
    opens it. What the hub's file-or-folder check reads (files.confirmed_folders)."""
    request_id, _clean = files.create_listing(db_path, machine, folder)
    files.record_listing(db_path, request_id, machine, {"entries": entries, "path": folder})


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        files.init_files_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        # machine_info belongs to app.py; only the column this blueprint reads is needed.
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS machine_info("
                         "machine TEXT PRIMARY KEY, companion_version TEXT)")
        SECRET = "hub-enroll-secret"

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        permissions.create_group(
            db_path, "Techs",
            capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
            machines=["PC-1", "PC-2"], members=["tech@x.com"])
        permissions.create_group(
            db_path, "Viewers", capabilities=[permissions.VIEW],
            machines=["PC-1", "PC-2"], members=["viewer@x.com"])
        settings.invalidate()

        ids = {}
        for name in ("PC-1", "PC-2", "PC-3"):
            agent_id, token = fleet.enroll_agent(db_path, name, SECRET, SECRET)
            ids[name] = {"Authorization": f"Bearer {agent_id}:{token}"}
            set_version(db_path, name, "3.52.0")

        relay = folder_relay.FolderRelay()
        app.register_blueprint(create_folder_relay_blueprint(
            db_path, fake_login_required, access, relay=relay))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()
        body = {"paths": ["C:\\Users\\bob\\Documents", "C:\\Users\\bob\\Pictures"],
                "target": "PC-2", "destination": "D:\\Incoming"}

        print("\n== Gates ==")
        CURRENT_USER = "viewer@x.com"
        r = c.post("/api/machines/PC-1/files/send", json=body)
        check("a viewer cannot start a copy", r.status_code == 403)

        CURRENT_USER = "tech@x.com"
        r = c.post("/api/machines/PC-3/files/send", json=dict(body, target="PC-2"))
        check("a source outside the tech's scope is refused", r.status_code == 403)
        r = c.post("/api/machines/PC-1/files/send", json=dict(body, target="PC-3"))
        check("a TARGET outside the tech's scope is refused", r.status_code == 403)
        check("...in the same words as an unknown PC",
              r.get_json()["error"] == c.post("/api/machines/PC-1/files/send",
                                              json=dict(body, target="NOPE")).get_json()["error"])
        r = c.post("/api/machines/PC-1/files/send", data="x", content_type="text/plain")
        check("a non-JSON body is refused", r.status_code == 415)

        print("\n== Validation ==")
        r = c.post("/api/machines/PC-1/files/send", json=dict(body, target="PC-1"))
        check("a copy onto itself is refused", r.status_code == 409)
        r = c.post("/api/machines/PC-1/files/send", json=dict(body, paths=["C:\\"]))
        check("a whole drive is refused", r.status_code == 400)
        r = c.post("/api/machines/PC-1/files/send",
                   json=dict(body, paths=["C:\\a\\Docs", "D:\\b\\docs"]))
        check("two folders with one name are refused", r.status_code == 400)
        r = c.post("/api/machines/PC-1/files/send", json=dict(body, destination="relative"))
        check("a relative destination is refused", r.status_code == 400)
        r = c.post("/api/machines/PC-1/files/send", json=dict(body, paths=[]))
        check("no folders is refused", r.status_code == 400)
        # Files as well as folders: the hub has only paths, and must not refuse one for
        # looking like a file. Which it is is the source agent's to find out.
        r = c.post("/api/machines/PC-1/files/send",
                   json=dict(body, paths=["C:\\Users\\bob\\Report.xlsx",
                                          "C:\\Users\\bob\\Documents"]))
        check("a file and a folder together are accepted", r.status_code == 201)
        relay.cancel(r.get_json()["transfer_id"])

        print("\n== A picked file needs agent 3.52.0 on the source ==")
        # The silent failure upstream: an older agent drops a file from a BACKUP and reports
        # success. The send route applies the same rule so the two never disagree.
        set_version(db_path, "PC-1", "3.51.0")
        docs = "C:" + "\\Users" + "\\bob"
        r = c.post("/api/machines/PC-1/files/send",
                   json=dict(body, paths=[docs + "\\Report.xlsx"]))
        check("an unlisted path on a 3.51.0 source is refused", r.status_code == 409)
        check("...naming the path", "Report.xlsx" in r.get_json()["error"])
        check("...and, with no listing to go by, saying to refresh rather than upgrade",
              "Refresh" in r.get_json()["error"])
        seed_listing(db_path, "PC-1", docs, [
            {"name": "Documents", "directory": True},
            {"name": "Pictures", "directory": True},
            {"name": "Report.xlsx", "directory": False}])
        r = c.post("/api/machines/PC-1/files/send",
                   json=dict(body, paths=[docs + "\\Report.xlsx"]))
        check("a path a listing shows is a FILE is still refused", r.status_code == 409)
        check("...saying it is a file, which the agent is what fixes",
              "is a file" in r.get_json()["error"] and "Refresh" not in r.get_json()["error"])
        r = c.post("/api/machines/PC-1/files/send", json=body)
        check("folders a listing confirms go through on 3.51.0", r.status_code == 201)
        # The Files tab left open past the listing's TTL: the same folder is now unknown, and
        # the refusal must point at Refresh, not at the agent.
        later = int(time.time()) + files.LISTING_TTL_SECONDS + 60
        stale = files.picked_files_refusal(db_path, "PC-1", body["paths"], now=later)
        check("an expired listing no longer vouches for the folder", stale is not None)
        check("...and the refusal says to refresh", stale and "Refresh" in stale)
        relay.cancel(r.get_json()["transfer_id"])
        set_version(db_path, "PC-1", "3.52.0")

        print("\n== Both ends' agents must be new enough ==")
        set_version(db_path, "PC-2", "3.50.0")
        r = c.post("/api/machines/PC-1/files/send", json=body)
        check("an old target is refused before anything is queued", r.status_code == 409)
        check("...naming the machine", "PC-2" in r.get_json()["error"])
        set_version(db_path, "PC-2", "3.51.0")

        print("\n== A send queues one command on each end ==")
        r = c.post("/api/machines/PC-1/files/send", json=body)
        check("the send is accepted", r.status_code == 201)
        started = r.get_json()
        tid = started["transfer_id"]
        send_cmd = fleet.get_command(db_path, started["send_command"])
        recv_cmd = fleet.get_command(db_path, started["receive_command"])
        check("stream_send is queued on the source",
              send_cmd["machine"] == "PC-1" and send_cmd["type"] == "stream_send")
        check("stream_receive is queued on the target",
              recv_cmd["machine"] == "PC-2" and recv_cmd["type"] == "stream_receive")
        check("the receiver never overwrites by default",
              recv_cmd["params"]["overwrite"] is False)
        check("the sender carries the validated paths",
              send_cmd["params"]["paths"] == body["paths"])

        r = c.get(f"/api/machines/PC-2/files/send/{tid}")
        check("the target's page can read the status", r.status_code == 200)
        check("it starts waiting", r.get_json()["state"] == "waiting")

        print("\n== Each agent may only play its own part ==")
        base = f"/api/agent/files/stream/{tid}"
        r = c.put(f"{base}/chunk/0", data=b"x", headers=ids["PC-2"])
        check("the target cannot put a chunk", r.status_code == 404)
        r = c.put(f"{base}/chunk/0", data=b"x", headers=ids["PC-3"])
        check("a stranger cannot put a chunk", r.status_code == 404)
        r = c.put(f"{base}/chunk/0", data=b"x")
        check("no bearer is 401", r.status_code == 401)

        print("\n== A whole round trip ==")
        payload = [os.urandom(1000), os.urandom(5000), os.urandom(17)]
        r = c.post(f"{base}/meta", json={"total_bytes": 6017, "total_files": 3},
                   headers=ids["PC-1"])
        check("meta is accepted from the source", r.status_code == 200)
        for i, chunk in enumerate(payload):
            r = c.put(f"{base}/chunk/{i}", data=chunk, headers=ids["PC-1"])
            check(f"chunk {i} is stored", r.status_code == 200)
        r = c.put(f"{base}/chunk/1", data=payload[1], headers=ids["PC-1"])
        check("a retried chunk is a 200, not a second copy", r.status_code == 200)
        r = c.post(f"{base}/end", json={}, headers=ids["PC-1"])
        check("end is accepted", r.status_code == 200)

        r = c.get(f"{base}/chunk/0", headers=ids["PC-1"])
        check("the source cannot take a chunk", r.status_code == 404)
        received = b""
        seq = 0
        while True:
            r = c.get(f"{base}/chunk/{seq}", headers=ids["PC-2"])
            if r.headers.get("X-Stream-End"):
                break
            check(f"chunk {seq} comes back", r.status_code == 200)
            received += r.data
            seq += 1
            if seq > 10:
                break
        check("the receiver got exactly what was sent", received == b"".join(payload))
        r = c.post(f"{base}/result", json={"summary": "Wrote 3 file(s)"}, headers=ids["PC-2"])
        check("the result is accepted", r.status_code == 200)

        r = c.get(f"/api/machines/PC-1/files/send/{tid}")
        st = r.get_json()
        check("the copy is done", st["state"] == "done")
        check("progress has a total", st["total_bytes"] == 6017)
        check("every byte was acknowledged", st["bytes_received"] == 6017)
        check("the summary reaches the console", st["summary"] == "Wrote 3 file(s)")

        print("\n== Cancel ==")
        r = c.post("/api/machines/PC-1/files/send", json=body)
        tid2 = r.get_json()["transfer_id"]
        r = c.post(f"/api/machines/PC-1/files/send/{tid2}/cancel", json={})
        check("cancel is accepted", r.status_code == 200)
        r = c.get(f"/api/agent/files/stream/{tid2}/chunk/0", headers=ids["PC-2"])
        check("the receiver then sees 410", r.status_code == 410)
        r = c.post(f"/api/machines/PC-1/files/send/nope/cancel", json={})
        check("cancelling an unknown id is 404", r.status_code == 404)

        print(f"\n==== {PASS} passed, {FAIL} failed ====")
        return 1 if FAIL else 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
