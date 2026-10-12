"""Signed telemetry on /api/report, end to end through the real app (roadmap #29).

**The silent failure this file exists to catch is a report accepted under a machine name it
is not entitled to.** /api/report used to be open: anything that could reach the hub could
post as any machine, and the console rendered it as that machine. Agents now sign their
reports with their own token, and `security.require_enrolled_telemetry`
refuses unsigned ones. Every way that can go quietly wrong is a report that is still
ACCEPTED -- a refused one that still left a reading behind, a forged token downgraded to
"unsigned", a latched machine accepting anonymous reports again once the switch is off --
so each is asserted against what the hub stored, not just the status code.

Imports app the way test_inventory.py does (env + cwd set before import).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-report-auth-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)

import app  # noqa: E402
import fleet  # noqa: E402
import settings  # noqa: E402

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


def report(machine, agent=None, temp=41.0):
    headers = {"Authorization": f"Bearer {agent[0]}:{agent[1]}"} if agent else {}
    return client.post("/api/report", json={"machine": machine, "temp": temp}, headers=headers)


def stored(machine):
    """Did a report for `machine` leave anything behind?"""
    with app.get_db_conn() as conn:
        row = conn.execute("SELECT 1 FROM machine_info WHERE machine = ?", (machine,)).fetchone()
    with app.latest_temp_lock:
        live = machine in app.latest_temp
    return row is not None or live


def set_switch(on):
    settings.set_many(app.DB_PATH, {"security.require_enrolled_telemetry": on})


def test_default_is_off():
    # Off because hubs update from main long before every agent signs -- see the comment on
    # the setting. If this flips, the roll-out note in ROADMAP #29 has to change with it.
    print("\n-- the switch defaults to off, for the roll-out --")
    check("security.require_enrolled_telemetry is off out of the box",
          settings.get_bool(app.DB_PATH, "security.require_enrolled_telemetry") is False)
    check("...so an agent that does not sign yet keeps reporting",
          report("PRE-29-AGENT").status_code == 200)


def test_switch_on():
    print("\n-- switch on: only signed reports --")
    set_switch(True)
    r = report("ANON-1")
    check("an unsigned report is refused with 401", r.status_code == 401)
    check("...saying why", r.get_json().get("reason") == "unsigned")
    check("...and left nothing behind", not stored("ANON-1"))
    check("...but the Onboarding page hears about it",
          any(row["machine"] == "ANON-1" for row in app.unsigned_reporters()))

    agent = fleet.issue_agent_identity(app.DB_PATH, "SIGNED-1", actor="test")
    r = report("SIGNED-1", agent)
    check("a signed report is accepted", r.status_code == 200)
    check("...and stored", stored("SIGNED-1"))

    r = report("SIGNED-1", (agent[0], "forged"))
    check("a forged token is refused, not treated as unsigned",
          r.status_code == 401 and r.get_json().get("reason") == fleet.AUTH_UNKNOWN)

    other = fleet.issue_agent_identity(app.DB_PATH, "SIGNED-2", actor="test")
    r = report("SIGNED-2", agent)
    check("an enrolled agent cannot report as another enrolled machine", r.status_code == 403)
    check("...and SIGNED-2 is still SIGNED-2's",
          fleet.authenticate_agent(app.DB_PATH, other[0], other[1], touch=False) == "SIGNED-2")

    fleet.revoke_agent(app.DB_PATH, other[0])
    r = report("SIGNED-2", other)
    check("a revoked agent is refused and told so",
          r.status_code == 401 and r.get_json().get("reason") == fleet.AUTH_REVOKED)


def test_rename():
    print("\n-- a renamed PC keeps reporting --")
    set_switch(True)
    agent = fleet.issue_agent_identity(app.DB_PATH, "OLD-NAME", actor="test")
    report("OLD-NAME", agent)
    r = report("NEW-NAME", agent)
    check("a signed report under a new, free name is accepted", r.status_code == 200)
    check("...and the agent's enrollment moved with it",
          fleet.authenticate_agent(app.DB_PATH, agent[0], agent[1], touch=False) == "NEW-NAME")


def test_switch_off():
    print("\n-- switch off: anonymous accepted, latched names still protected --")
    set_switch(False)
    r = report("ANON-2")
    check("an unsigned report for an unknown machine is accepted", r.status_code == 200)
    check("...and stored", stored("ANON-2"))

    agent = fleet.issue_agent_identity(app.DB_PATH, "LATCH-1", actor="test")
    check("a signed report latches the machine", report("LATCH-1", agent).status_code == 200)
    r = report("LATCH-1")
    check("an unsigned report under a latched name is refused even with the switch off",
          r.status_code == 401)
    check("...in any case", report("latch-1").status_code == 401)

    enrolled_not_signed = fleet.issue_agent_identity(app.DB_PATH, "LEGACY-1", actor="test")
    check("an enrolled agent that has never signed is not latched",
          report("LEGACY-1").status_code == 200 and bool(enrolled_not_signed))
    set_switch(True)


def main():
    test_default_is_off()
    test_switch_on()
    test_rename()
    test_switch_off()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
