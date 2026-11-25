"""Remote view mode per OU / folder through Group Policy (roadmap #2 follow-up, through #32).

The silent failure this file exists to catch: **an OU set to attended still gets an unattended
session** -- the operator linked the policy, the RSoP page names it, and the person at the desk
is never asked, because a start route read the fleet-wide setting and nothing else. Nothing on
screen would say so; the session simply connects.

Also pinned:
  * a policy can turn remote OFF for its OU, and the fleet-wide off stays a hard kill switch a
    policy cannot override;
  * the hub-side keys never reach the heartbeat config, so linking one does not move the config
    version of every PC under the OU (gpo.config_overrides);
  * Group Policy still refuses a hub setting that is neither an agent nor a policy setting.

Wires the remote blueprint directly, the same way test_remote_web does.
"""
import functools
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import gpo
import pc_tree
import permissions
import remote
import settings
from remote_web import create_remote_blueprint
from permissions_web import create_access
from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
SECRET = "hub-enroll-secret"
FRONT_DESK = "OU=Front desk,DC=x"


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


def policy(db, name, values):
    error, clean = gpo.validate({"name": name, "settings": {"agent_settings": values}})
    assert error is None, error
    error, saved = gpo.save_gpo(db, clean, actor="t")
    assert error is None, error
    return saved


def queued_consent(db, agent_id, machine, session_id):
    for cmd in fleet.claim_commands(db, agent_id, machine):
        if cmd["type"] == "start_remote_session" and cmd["params"]["session_id"] == session_id:
            return cmd["params"]["consent_mode"]
    return None


def start(client, db, agents, machine):
    """(status, consent in the reply, consent in the queued command)."""
    r = client.post(f"/api/remote/{machine}/start", json={})
    body = r.get_json() or {}
    if r.status_code != 201:
        return r.status_code, None, None
    sid = body["session_id"]
    queued = queued_consent(db, agents[machine], machine, sid)
    remote.end_session(db, sid, "test cleanup", actor="t")
    return r.status_code, body.get("consent_mode"), queued


def main():
    db_fd, db = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        # The slice of app.py's machine_info the policy resolver reads (the AD OU).
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, ad_ou TEXT,"
                         " ad_dn TEXT, companion_version TEXT)")
            conn.executemany("INSERT INTO machine_info (machine, ad_ou) VALUES (?, ?)",
                             [("KIOSK-1", None), ("DESK-1", FRONT_DESK)])
        fleet.init_fleet_db(db)
        remote.init_remote_db(db)
        permissions.init_permissions_db(db)
        settings.init_settings_db(db)
        pc_tree.init_pc_tree_db(db)
        gpo.init_gpo_db(db)
        gpo.invalidate()
        settings.invalidate()

        agents = {}
        for machine in ("KIOSK-1", "DESK-1"):
            agent_id, _ = fleet.enroll_agent(db, machine, SECRET, SECRET)
            agents[machine] = agent_id

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db, {"super@x.com"})
        env_fd, env_path = tempfile.mkstemp(suffix=".env")
        os.close(env_fd)
        app.register_blueprint(
            create_remote_blueprint(db, fake_login_required, access, env_path=env_path))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": "super@x.com"}
        c = app.test_client()

        print("\n== Group Policy accepts the remote settings, and only policy-settable ones ==")
        error, clean = gpo.validate({"name": "x", "settings": {"agent_settings": {
            "remote.consent_mode": "attended", "remote.enabled": False}}})
        check("remote.consent_mode and remote.enabled validate", error is None)
        error, _ = gpo.validate({"name": "x", "settings": {"agent_settings": {
            "remote.consent_mode": "sometimes"}}})
        check("...an unknown mode is refused", error is not None)
        error, _ = gpo.validate({"name": "x", "settings": {"agent_settings": {
            "remote.session_ttl_seconds": 600}}})
        check("a hub setting that is neither agent nor policy is still refused",
              error is not None)

        print("\n== No policy: the fleet-wide mode ==")
        status, reply, queued = start(c, db, agents, "KIOSK-1")
        check("unattended by default, in the reply and in the command",
              status == 201 and reply == "unattended" and queued == "unattended")

        print("\n== A policy on a folder makes its PCs attended, and only them ==")
        folder = pc_tree.create_folder(db, "Kiosks")
        pc_tree.file_machines(db, ["KIOSK-1"], folder["id"])
        attended = policy(db, "Kiosks attended", {"remote.consent_mode": "attended"})
        attended_link = gpo.create_link(db, attended["id"], gpo.NODE_FOLDER, folder["id"])
        status, reply, queued = start(c, db, agents, "KIOSK-1")
        check("the PC in the folder gets an attended session",
              status == 201 and reply == "attended" and queued == "attended")
        status, reply, queued = start(c, db, agents, "DESK-1")
        check("a PC outside the folder is still unattended",
              status == 201 and queued == "unattended")

        print("\n== A policy on an AD OU works the same way ==")
        desk = policy(db, "Front desk attended", {"remote.consent_mode": "attended"})
        desk_link = gpo.create_link(db, desk["id"], gpo.NODE_OU, FRONT_DESK)
        status, reply, queued = start(c, db, agents, "DESK-1")
        check("the PC in the OU gets an attended session",
              status == 201 and reply == "attended" and queued == "attended")
        gpo.delete_link(db, desk_link["id"])

        print("\n== The hub-side keys never reach the agent ==")
        check("config_overrides leaves the remote keys out",
              gpo.config_overrides(db, "KIOSK-1") == {})
        check("...so the PC's config version does not move",
              gpo.config_version("base", gpo.config_overrides(db, "KIOSK-1")) == "base")
        check("...while the RSoP still shows who decided",
              gpo.rsop(db, "KIOSK-1")["agent_settings"]["remote.consent_mode"]["gpo"]
              == "Kiosks attended")

        print("\n== The fleet-wide mode is the fallback, the policy wins over it ==")
        settings.set_many(db, {"remote.consent_mode": "attended"}, updated_by="t")
        settings.invalidate()
        unattended = policy(db, "Kiosks unattended", {"remote.consent_mode": "unattended"})
        gpo.delete_link(db, attended_link["id"])
        unattended_link = gpo.create_link(db, unattended["id"], gpo.NODE_FOLDER, folder["id"])
        status, _, queued = start(c, db, agents, "KIOSK-1")
        check("an unattended policy beats a fleet-wide attended", queued == "unattended")
        status, _, queued = start(c, db, agents, "DESK-1")
        check("...and a PC with no policy follows the fleet", queued == "attended")
        gpo.delete_link(db, unattended_link["id"])
        settings.reset(db, ["remote.consent_mode"], updated_by="t")
        settings.invalidate()

        print("\n== A policy can turn remote off for its PCs ==")
        off = policy(db, "Kiosks off", {"remote.enabled": False})
        off_link = gpo.create_link(db, off["id"], gpo.NODE_FOLDER, folder["id"])
        before = len(remote.list_sessions(db))
        r = c.post("/api/remote/KIOSK-1/start", json={})
        check("start is refused with 403", r.status_code == 403)
        check("...naming Group Policy", "Group Policy" in (r.get_json() or {}).get("error", ""))
        check("...and no command was queued",
              not any(cmd["type"] == "start_remote_session"
                      for cmd in fleet.claim_commands(db, agents["KIOSK-1"], "KIOSK-1")))
        check("...nor a session row written", len(remote.list_sessions(db)) == before)
        status, _, _ = start(c, db, agents, "DESK-1")
        check("a PC outside the folder can still be viewed", status == 201)
        gpo.delete_link(db, off_link["id"])

        print("\n== The fleet-wide off is a kill switch no policy overrides ==")
        on = policy(db, "Kiosks on", {"remote.enabled": True})
        gpo.create_link(db, on["id"], gpo.NODE_FOLDER, folder["id"])
        settings.set_many(db, {"remote.enabled": False}, updated_by="t")
        settings.invalidate()
        status, _, _ = start(c, db, agents, "KIOSK-1")
        check("a policy saying on does not beat the fleet-wide off", status == 403)
        settings.reset(db, ["remote.enabled"], updated_by="t")
        settings.invalidate()
        status, _, _ = start(c, db, agents, "KIOSK-1")
        check("...and with the switch back on, the PC can be viewed again", status == 201)
    finally:
        try:
            os.unlink(db)
        except OSError:
            pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
