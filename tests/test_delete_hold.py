"""Deleting a machine holds its name against unsigned reports, so an onboarding code can take it.

**The silent failure this file exists to catch is a PC that can never be re-onboarded.** A
PC whose BIOS serial is a placeholder has nothing onboarding can match a record to, so the
only way in is a name with no record. The operator deletes the record; the onboarding script
installs and starts the agent before it spends the code; that agent's first unsigned report
rebuilds the record; and the redeem a few seconds later is refused "could not be matched".
Every delete looks like it worked, and every run of the installer fails the same way
(DESKTOP-DEQ7B5D, 2026-10-31). Also caught: a delete that matched the name case-sensitively
while onboarding's checks do not, so a record under another case survived and kept refusing.

Imports app the way test_decommission.py does (env + cwd set before import).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-delete-hold-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app  # noqa: E402
import fleet  # noqa: E402
import onboarding  # noqa: E402

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
PLACEHOLDER = "System Serial Number"


def unsigned_report(name):
    return client.post("/api/report", json={"machine": name, "temp": 40.0,
                                            "serial_number": PLACEHOLDER})


def has_record(name):
    return app._machine_record(name) is not None


def redeem(name):
    _, code = onboarding.create_code(app.DB_PATH, "test", actor="test")
    return client.post("/api/onboarding/redeem",
                       json={"code": code, "machine": name, "serial": PLACEHOLDER})


def test_placeholder_serial_can_be_reonboarded():
    print("\n-- a placeholder-serial PC: delete, its agent keeps reporting, the code still works --")
    name = "WHITEBOX-PC"
    check("an unsigned report creates the record", unsigned_report(name).status_code == 200
          and has_record(name))
    check("...and while it exists, a code is refused 409",
          redeem(name).status_code == 409)

    check("the delete succeeds",
          client.delete(f"/api/machines/{name}", headers=HEADERS).status_code == 200)
    r = unsigned_report(name)
    check("an unsigned report right after the delete is refused 401 'unsigned'",
          r.status_code == 401 and r.get_json().get("reason") == "unsigned")
    check("...and wrote no record back", not has_record(name))
    check("...under another case too", unsigned_report(name.lower()).status_code == 401
          and not has_record(name))

    r = redeem(name)
    check("the onboarding code now enrolls the PC", r.status_code == 200
          and r.get_json().get("token"))
    ident = r.get_json()
    signed = client.post("/api/report", json={"machine": name, "temp": 40.0},
                         headers={"Authorization": f"Bearer {ident['agent_id']}:{ident['token']}"})
    check("the new agent's signed report is accepted during the hold",
          signed.status_code == 200 and has_record(name))


def test_hold_expires():
    print("\n-- the hold is temporary: a still-running unsigned agent reappears afterwards --")
    name = "STALE-RECORD"
    unsigned_report(name)
    client.delete(f"/api/machines/{name}", headers=HEADERS)
    check("held right after the delete", unsigned_report(name).status_code == 401)
    with app._deleted_name_holds_lock:
        app._deleted_name_holds[name.lower()] -= app.DELETED_NAME_HOLD_SECONDS + 1
    check("accepted again once the hold has run out",
          unsigned_report(name).status_code == 200 and has_record(name))


def test_other_names_unaffected():
    print("\n-- the hold is per name --")
    unsigned_report("NEIGHBOUR-PC")
    client.delete("/api/machines/DELETED-PC", headers=HEADERS)
    check("an unsigned report under another name is still accepted",
          unsigned_report("NEIGHBOUR-PC").status_code == 200)


def test_delete_ignores_case():
    print("\n-- a delete removes the agent row and record whatever their case --")
    unsigned_report("mixed-case-pc")
    fleet.issue_agent_identity(app.DB_PATH, "mixed-case-pc", actor="test")
    check("the record was stored under the lower-case name", has_record("mixed-case-pc"))
    client.delete("/api/machines/MIXED-CASE-PC", headers=HEADERS)
    with fleet.get_conn(app.DB_PATH) as conn:
        check("the agent row under another case is gone",
              not fleet.live_agent_holds(conn, "MIXED-CASE-PC"))
    check("...and so is the record", not has_record("mixed-case-pc"))


def test_failed_delete_releases_hold():
    print("\n-- a delete that fails holds nothing: the record it left keeps reporting --")
    name = "FAILED-DELETE-PC"
    unsigned_report(name)
    real = fleet.delete_machine

    def boom(*a, **kw):
        raise RuntimeError("test")
    fleet.delete_machine = boom
    try:
        client.delete(f"/api/machines/{name}", headers=HEADERS)
    except RuntimeError:
        pass
    finally:
        fleet.delete_machine = real
    check("the name is not held", not app._deleted_name_held(name))
    check("...so its unsigned reports are still accepted",
          unsigned_report(name).status_code == 200)


def main():
    test_placeholder_serial_can_be_reonboarded()
    test_hold_expires()
    test_other_names_unaffected()
    test_delete_ignores_case()
    test_failed_delete_releases_hold()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
