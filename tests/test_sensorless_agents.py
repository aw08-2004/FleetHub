"""Machines with no temperature, and names with no machine, end to end through the real app.

**The silent failure this file exists to catch is an enrolled machine the console cannot
see.** A Windows Sandbox and a cloud-GPU VM enrolled, heartbeated and showed online in
/api/fleet/status, and were in neither the Inventory nor the Dashboard: the hub created a
machine's identity row only from /api/report, and the agent sent no report at all without a
CPU temperature, which no VM has. Each half is asserted here against what the hub serves:

- a report with `temp: null` is accepted and lists the machine, without inventing a reading;
- an enrollment that has never reported is listed anyway, online while it heartbeats;
- and the opposite mistake -- a name nobody enrolled -- gets no machine page and no
  command, instead of an empty page whose buttons queue work for nobody.

Imports app the way test_inventory.py does (env + cwd set before import).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMPDIR = tempfile.mkdtemp(prefix="hub-sensorless-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.environ["ALLOWED_EMAILS"] = "tester@example.com"
os.chdir(_TMPDIR)

import app  # noqa: E402
import fleet  # noqa: E402
from console_session import sign_in  # noqa: E402

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
sign_in(client, "tester@example.com")


def report(machine, agent, **fields):
    body = {"machine": machine}
    body.update(fields)
    return client.post("/api/report", json=body,
                       headers={"Authorization": f"Bearer {agent[0]}:{agent[1]}"})


def listed(machine):
    return next((row for row in client.get("/api/machines").get_json()
                 if row["machine"] == machine), None)


def test_null_temp_report():
    print("\n-- a VM reports with temp: null --")
    agent = fleet.issue_agent_identity(app.DB_PATH, "SANDBOX-VM", actor="test")
    # Intercepted rather than counted in SQLite: the writer is asynchronous, so "no row yet"
    # would pass whether or not one was on its way.
    enqueued = []
    real_enqueue = app.enqueue_reading
    app.enqueue_reading = lambda *a, **k: enqueued.append(a)
    try:
        r = report("SANDBOX-VM", agent, temp=None, model="Virtual Machine",
                   serial_number="VM-SERIAL-0001", os_caption="Microsoft Windows 11 Enterprise",
                   uptime_seconds=120)
    finally:
        app.enqueue_reading = real_enqueue
    check("the report is accepted", r.status_code == 200)
    check("...without queueing a reading -- readings.temp has no honest value for it",
          enqueued == [])
    row = listed("SANDBOX-VM")
    check("...and the machine is in Inventory", row is not None)
    check("...online", row is not None and row["status"] == "online")
    check("...with the identity it reported", row is not None and row["model"] == "Virtual Machine")
    check("...and no temperature, rather than a made-up one",
          row is not None and row["temp"] is None)
    check("its machine page opens", client.get("/machine/SANDBOX-VM").status_code == 200)

    print("\n-- ...while a malformed temperature is still refused --")
    check("a missing temp key is still a bad payload",
          client.post("/api/report", json={"machine": "SANDBOX-VM"}).status_code == 400)
    check("a non-numeric temp is still refused",
          report("SANDBOX-VM", agent, temp="hot").status_code == 400)


def test_enrolled_never_reported():
    print("\n-- an enrollment that never reported is still a machine --")
    fleet.issue_agent_identity(app.DB_PATH, "035A185C-668A-4", actor="test")
    row = listed("035A185C-668A-4")
    check("it is in Inventory", row is not None)
    check("...as enrolled", row is not None and row["enrolled"] is True)
    check("...and online, from its enrollment's last_seen",
          row is not None and row["status"] == "online")
    check("the Dashboard counts it too",
          "035A185C-668A-4" in app._all_known_machines())
    check("its machine page opens", client.get("/machine/035A185C-668A-4").status_code == 200)
    check("...and its detail payload answers",
          client.get("/api/machines/035A185C-668A-4").status_code == 200)

    print("\n-- ...but a rename that differs only in case is not listed twice --")
    agent = fleet.issue_agent_identity(app.DB_PATH, "CaseBox", actor="test")
    report("CaseBox", agent, temp=40.0, model="Box")
    with app.get_db_conn() as conn:
        conn.execute("UPDATE machine_info SET machine = 'CASEBOX' WHERE machine = 'CaseBox'")
    names = [row["machine"].lower() for row in client.get("/api/machines").get_json()]
    check("one row for CaseBox / CASEBOX", names.count("casebox") == 1)


def test_unknown_names():
    print("\n-- a name nobody enrolled --")
    ghost = "035a185c-668a-4791-b089-46d57c9fa405"
    r = client.get(f"/machine/{ghost}")
    check("its machine page is a 404", r.status_code == 404)
    check("...that says the machine was not found, naming it",
          b"Machine not found" in r.data and ghost.encode() in r.data)
    check("its detail payload is a 404", client.get(f"/api/machines/{ghost}").status_code == 404)

    r = client.post("/api/fleet/commands",
                    json={"machine": ghost, "type": "run_script",
                          "params": {"script": "hostname"}})
    check("a command for it is refused", r.status_code == 400)
    check("...saying no enrolled agent answers to it",
          "No enrolled agent" in (r.get_json() or {}).get("error", ""))
    check("...and nothing was queued", fleet.list_commands(app.DB_PATH, ghost) == [])

    r = client.post(f"/api/machines/{ghost}/files/list", json={"path": ""})
    check("the Files tool refuses it too, where the GUID listing was queued",
          r.status_code in (400, 404) and fleet.list_commands(app.DB_PATH, ghost) == [])

    fleet.issue_agent_identity(app.DB_PATH, "OFFLINE-PC", actor="test")
    r = client.post("/api/fleet/commands",
                    json={"machine": "OFFLINE-PC", "type": "run_script",
                          "params": {"script": "hostname"}})
    check("an enrolled machine still queues, online or not", r.status_code == 201)

    # The schedulers go straight to create_command and must NOT be refused: a raise there
    # escapes their loops (see fleet.require_enrolled). Their unenrolled targets end by TTL.
    try:
        fleet.create_command(app.DB_PATH, "AD-ONLY-PC", "run_script", {"script": "x"}, "test")
        queued = True
    except ValueError:
        queued = False
    check("create_command itself still queues for an unenrolled name, for the schedulers",
          queued)


def main():
    test_null_temp_report()
    test_enrolled_never_reported()
    test_unknown_names()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
