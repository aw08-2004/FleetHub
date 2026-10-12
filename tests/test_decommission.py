"""Deleting a machine with "uninstall the agent" ticked (roadmap #30), end to end through app.py.

**The silent failure this file exists to catch is an uninstall that goes to the wrong place,
or nowhere.** The wrong place: the duplicate-serial merge also deletes a machine record, but it
is folding one PC's old name into its new one, and an uninstall there would take the agent off
a machine that is still in use. Nowhere: a delete with the box ticked that forgets the agent's
credential before recording it, so the agent is never recognised again and simply re-enrolls --
the bug this feature exists to end, back again with nothing failing.

Imports app the way test_inventory.py does (env + cwd set before import).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-decommission-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app  # noqa: E402
import fleet  # noqa: E402

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


client = app.app.test_client()
with client.session_transaction() as sess:
    sess["user"] = {"email": "tester@example.com"}
    sess["csrf_token"] = "t"
HEADERS = {"X-CSRF-Token": "t"}


def heartbeat(agent):
    return client.post("/api/agent/heartbeat", json={},
                       headers={"Authorization": f"Bearer {agent[0]}:{agent[1]}"})


def enrolled(name):
    agent = fleet.issue_agent_identity(app.DB_PATH, name, actor="test")
    client.post("/api/report", json={"machine": name, "temp": 40.0},
                headers={"Authorization": f"Bearer {agent[0]}:{agent[1]}"})
    return agent


def test_delete_with_uninstall():
    print("\n-- delete with uninstall: the agent is told, whenever it next calls --")
    agent = enrolled("GONE-PC")
    r = client.delete("/api/machines/GONE-PC?uninstall=1", headers=HEADERS)
    check("the delete succeeds", r.status_code == 200)
    check("...and says one agent will be told", r.get_json().get("agents_to_uninstall") == 1)
    hb = heartbeat(agent)
    check("its next heartbeat is refused as decommissioned",
          hb.status_code == 401 and hb.get_json().get("reason") == fleet.AUTH_DECOMMISSIONED)
    check("...and so is the one after (the agent waits for three)",
          heartbeat(agent).get_json().get("reason") == fleet.AUTH_DECOMMISSIONED)
    rep = client.post("/api/report", json={"machine": "GONE-PC", "temp": 40.0},
                      headers={"Authorization": f"Bearer {agent[0]}:{agent[1]}"})
    check("a signed report hears the same reason",
          rep.status_code == 401 and rep.get_json().get("reason") == fleet.AUTH_DECOMMISSIONED)
    forged = heartbeat((agent[0], "not-the-token"))
    check("only the real token is told; a forged one is just unknown",
          forged.get_json().get("reason") == fleet.AUTH_UNKNOWN)


def test_delete_without_uninstall():
    print("\n-- delete without the box: the old behaviour --")
    agent = enrolled("STALE-RECORD")
    r = client.delete("/api/machines/STALE-RECORD", headers=HEADERS)
    check("the delete succeeds and tells nobody",
          r.status_code == 200 and r.get_json().get("agents_to_uninstall") == 0)
    check("the agent hears 'unknown' and will re-enroll, as before",
          heartbeat(agent).get_json().get("reason") == fleet.AUTH_UNKNOWN)


def test_merge_never_uninstalls():
    print("\n-- the duplicate-serial merge never uninstalls anything --")
    old = enrolled("OLD-NAME")
    enrolled("NEW-NAME")
    app.merge_machines("NEW-NAME", "OLD-NAME")
    check("the dropped name's agent is unknown, not decommissioned",
          heartbeat(old).get_json().get("reason") == fleet.AUTH_UNKNOWN)


def test_uninstall_needs_issue_commands():
    print("\n-- uninstalling runs code on the PC, so it needs issue_commands --")
    import permissions
    agent = enrolled("SETTINGS-ONLY-PC")
    permissions.create_group(app.DB_PATH, "Settings only",
                             capabilities=[permissions.VIEW, permissions.MANAGE_SETTINGS],
                             machines=["SETTINGS-ONLY-PC"], members=["settings@example.com"])
    with client.session_transaction() as sess:
        sess["user"] = {"email": "settings@example.com"}
    try:
        r = client.delete("/api/machines/SETTINGS-ONLY-PC?uninstall=1", headers=HEADERS)
        check("manage_settings alone may not uninstall: 403", r.status_code == 403)
        check("...and nothing was deleted or recorded",
              heartbeat(agent).status_code == 200)
        r = client.delete("/api/machines/SETTINGS-ONLY-PC", headers=HEADERS)
        check("...but may still delete the record without the flag", r.status_code == 200)
        check("...which tells the agent nothing",
              heartbeat(agent).get_json().get("reason") == fleet.AUTH_UNKNOWN)
    finally:
        with client.session_transaction() as sess:
            sess["user"] = {"email": "tester@example.com"}


def test_failed_delete_leaves_no_record():
    print("\n-- a delete that fails leaves no record to uninstall on a later, unticked one --")
    import sqlite3
    agent = enrolled("HALF-DELETED")
    # Make deleting this machine's agents row fail, AFTER the record would have been written.
    with sqlite3.connect(app.DB_PATH) as conn:
        conn.execute("CREATE TRIGGER block_half_deleted BEFORE DELETE ON agents "
                     "WHEN OLD.machine = 'HALF-DELETED' BEGIN SELECT RAISE(ABORT, 'test'); END")
    try:
        fleet.delete_machine(app.DB_PATH, "HALF-DELETED", uninstall_actor="test")
        raised = False
    except sqlite3.DatabaseError:
        raised = True
    finally:
        with sqlite3.connect(app.DB_PATH) as conn:
            conn.execute("DROP TRIGGER block_half_deleted")
    check("the forced failure happened", raised)
    check("...and the agent still authenticates", heartbeat(agent).status_code == 200)
    client.delete("/api/machines/HALF-DELETED", headers=HEADERS)
    check("a retry with the box unticked does NOT uninstall it",
          heartbeat(agent).get_json().get("reason") == fleet.AUTH_UNKNOWN)


def test_prune_keeps_a_row_while_it_has_a_job():
    print("\n-- the record is pruned once its question is settled, never before --")
    import time
    told = enrolled("TOLD-PC")
    client.delete("/api/machines/TOLD-PC?uninstall=1", headers=HEADERS)
    heartbeat(told)
    untold = enrolled("CUPBOARD-PC")
    client.delete("/api/machines/CUPBOARD-PC?uninstall=1", headers=HEADERS)
    now = time.time()
    check("nothing is pruned straight away", fleet.prune_decommissioned(app.DB_PATH, now) == 0)
    month = now + fleet.DECOMMISSION_TOLD_KEEP_SECONDS + 60
    fleet.prune_decommissioned(app.DB_PATH, month)
    check("a month after the PC was told and fell silent, its record goes",
          heartbeat(told).get_json().get("reason") == fleet.AUTH_UNKNOWN)
    check("...but a PC that has never called since is still waited for",
          heartbeat(untold).get_json().get("reason") == fleet.AUTH_DECOMMISSIONED)
    # That heartbeat just told it, so it is now a told row: silent a month after THIS call.
    fleet.prune_decommissioned(app.DB_PATH, time.time() + fleet.DECOMMISSION_TOLD_KEEP_SECONDS - 60)
    check("...and once told, it is kept while it might still be calling",
          heartbeat(untold).get_json().get("reason") == fleet.AUTH_DECOMMISSIONED)
    gone = enrolled("SCRAPPED-PC")
    client.delete("/api/machines/SCRAPPED-PC?uninstall=1", headers=HEADERS)
    fleet.prune_decommissioned(app.DB_PATH, now + fleet.DECOMMISSION_UNTOLD_KEEP_SECONDS + 60)
    check("a PC never heard from in a year is let go",
          heartbeat(gone).get_json().get("reason") == fleet.AUTH_UNKNOWN)


def main():
    test_delete_with_uninstall()
    test_delete_without_uninstall()
    test_merge_never_uninstalls()
    test_uninstall_needs_issue_commands()
    test_failed_delete_leaves_no_record()
    test_prune_keeps_a_row_while_it_has_a_job()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
