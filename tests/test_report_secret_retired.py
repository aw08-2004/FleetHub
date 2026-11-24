"""AGENT_REPORT_SECRET set on the hub must not refuse a single report (issue #215).

**The silent failure this file exists to catch is a fleet that enrolls and shows no
telemetry.** The Docker template listed AGENT_REPORT_SECRET as required, and the hub refused
every /api/report without a matching `report_secret` field -- which no agent (Windows, Linux,
Android) or installer has ever sent. The fleet channel kept working, so every machine
enrolled, took config and showed online, while its serial, uptime and temperatures never
arrived; the agent logged the 403 at Information as "Sent". Nothing looked broken.

The variable is retired: still read, only to warn at startup that it is ignored. Asserted
here with it set in the environment before app is imported, exactly as a .env would:

- the startup warning names it and says it is ignored;
- a signed report without `report_secret` is accepted and the machine is stored;
- an unsigned one is too, while signed telemetry is not required -- the secret is not a
  second gate hiding behind the first.

Imports app the way test_sensorless_agents.py does (env + cwd set before import).
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMPDIR = tempfile.mkdtemp(prefix="hub-report-secret-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.environ["ALLOWED_EMAILS"] = "tester@example.com"
os.environ["AGENT_REPORT_SECRET"] = "change-me"
os.chdir(_TMPDIR)

_BOOT = io.StringIO()
with contextlib.redirect_stdout(_BOOT):
    import app  # noqa: E402
import fleet  # noqa: E402
import settings  # noqa: E402
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


def listed(machine):
    return next((row for row in client.get("/api/machines").get_json()
                 if row["machine"] == machine), None)


def test_startup_warning():
    print("\n-- the hub says the variable is ignored --")
    boot = _BOOT.getvalue()
    check("a startup line names AGENT_REPORT_SECRET", "AGENT_REPORT_SECRET" in boot)
    check("...and says it is ignored", "IGNORED" in boot)


def test_signed_report_accepted():
    print("\n-- a signed report with no report_secret --")
    agent = fleet.issue_agent_identity(app.DB_PATH, "SECRET-PC", actor="test")
    r = client.post("/api/report",
                    json={"machine": "SECRET-PC", "temp": 41.5, "model": "Box",
                          "serial_number": "SN-215"},
                    headers={"Authorization": f"Bearer {agent[0]}:{agent[1]}"})
    check("is accepted (200), not 403", r.status_code == 200)
    check("...without naming a report secret",
          "report secret" not in (r.get_data(as_text=True) or "").lower())
    row = listed("SECRET-PC")
    check("...and the machine is stored", row is not None)
    check("...with the identity it reported",
          row is not None and row.get("model") == "Box")


def test_unsigned_report_accepted():
    print("\n-- an unsigned report, while signed telemetry is not required --")
    settings.set_many(app.DB_PATH, {"security.require_enrolled_telemetry": False})
    r = client.post("/api/report", json={"machine": "UNSIGNED-PC", "temp": 38.0})
    check("is accepted too -- the secret is no second gate", r.status_code == 200)


def main():
    test_startup_warning()
    test_signed_report_accepted()
    test_unsigned_report_accepted()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
