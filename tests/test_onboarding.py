"""Model test for onboarding.py (roadmap #29), and the telemetry identity check beside it.

**The silent failure this file exists to catch is one onboarding code admitting two
machines.** A six-character code is short enough to read over a phone, so everything that
bounds it -- single use claimed atomically, expiry, revocation, hash-only storage, one
refusal sentence for every bad code -- has to hold, and a regression in any of them would
look like success: the second PC enrols, appears in the console, and nothing says it should
not have.

Also pinned here, because it shares the agent row with onboarding: fleet.authorize_report,
which decides who a /api/report is from. The failure there is quieter still -- a report
accepted under a name it is not entitled to renders as that machine.
"""
import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import onboarding
import permissions

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


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    permissions.init_permissions_db(path)
    fleet.init_fleet_db(path)
    onboarding.init_onboarding_db(path)
    return path


def raises(fn, exc=onboarding.OnboardingError):
    try:
        fn()
    except exc as e:
        return e
    return None


def test_codes(db):
    print("\n-- a code is short, readable, and stored only as a hash --")
    code_id, code = onboarding.create_code(db, "Reception PC", actor="ann@x.com")
    check("six characters", len(code) == 6)
    check("only from the look-alike-free alphabet", all(ch in onboarding.ALPHABET for ch in code))
    check("no 0, O, 1, I or L in the alphabet", not set("0O1IL") & set(onboarding.ALPHABET))
    with sqlite3.connect(db) as conn:
        dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM onboarding_codes"))
    check("the plaintext code is nowhere in the table", code not in dump)
    listed = onboarding.list_codes(db)
    check("the list never carries the code", all("code" not in row for row in listed))
    check("a new code is active", listed[0]["status"] == onboarding.STATUS_ACTIVE)

    print("\n-- normalisation is for typing, not for guessing --")
    check("lower case and a dash still match",
          onboarding.check_code(db, code[:3].lower() + "-" + code[3:].lower()) is not None)
    check("a look-alike is refused, not coerced", onboarding.normalize_code("ABC10O") is None)
    check("check consumes nothing", onboarding.check_code(db, code) is not None)

    print("\n-- refusals at creation --")
    check("a label is required", raises(lambda: onboarding.create_code(db, "  ")) is not None)
    check("only the offered lifetimes are accepted",
          raises(lambda: onboarding.create_code(db, "x", ttl_seconds=10 ** 9)) is not None)
    return code_id, code


def test_redeem(db, code_id, code):
    print("\n-- redeeming mints this machine its own credential --")
    agent_id, token = onboarding.redeem_code(db, code, "PC-RECEPTION", ip="10.0.0.5")
    check("an agent id and a token come back", bool(agent_id) and bool(token))
    # touch=False: this is the test looking, not the agent checking in -- status below
    # has to see a PC that has not used its token yet.
    check("the credential authenticates as that machine",
          fleet.authenticate_agent(db, agent_id, token, touch=False) == "PC-RECEPTION")
    row = onboarding.get_code(db, code_id)
    check("the row records who spent it, and from where",
          row["redeemed_machine"] == "PC-RECEPTION" and row["redeemed_ip"] == "10.0.0.5"
          and row["agent_id"] == agent_id)
    check("and reads as redeemed", row["status"] == onboarding.STATUS_REDEEMED)
    with sqlite3.connect(db) as conn:
        enrolls = conn.execute("SELECT actor FROM audit_log WHERE action = 'enroll'").fetchall()
    check("the enrollment is audited as coming from the code",
          any(a[0].startswith("onboarding:") for a in enrolls))

    print("\n-- and only once --")
    e = raises(lambda: onboarding.redeem_code(db, code, "PC-OTHER"))
    check("a second redeem is refused", e is not None)
    check("...with the same sentence a stranger gets", str(e) == onboarding.INVALID_MESSAGE)
    check("check no longer finds it", onboarding.check_code(db, code) is None)
    check("status says the PC has not checked in yet",
          onboarding.status_for_code(db, code) == {"state": "enrolled", "machine": "PC-RECEPTION"})
    fleet.authenticate_agent(db, agent_id, token)          # the agent's first heartbeat
    check("...and online once its token has been used",
          onboarding.status_for_code(db, code)["state"] == "online")
    return agent_id, token


def test_race(db):
    print("\n-- two PCs racing one code: exactly one wins --")
    _, code = onboarding.create_code(db, "Race")
    wins, losses = [], []
    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait()
        try:
            wins.append(onboarding.redeem_code(db, code, f"RACE-{i}"))
        except onboarding.OnboardingError:
            losses.append(i)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(f"one winner ({len(wins)})", len(wins) == 1)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM agents WHERE machine LIKE 'RACE-%'").fetchone()[0]
    check(f"one agent row minted ({rows})", rows == 1)


def test_expiry_and_revoke(db):
    print("\n-- expired and revoked codes are refused --")
    now = 1_000_000
    _, old = onboarding.create_code(db, "Old", ttl_seconds=3600, now=now)
    check("live just before it expires", onboarding.check_code(db, old, now=now + 3599) is not None)
    check("dead at expiry", onboarding.check_code(db, old, now=now + 3600) is None)
    check("redeem after expiry is refused",
          raises(lambda: onboarding.redeem_code(db, old, "PC-LATE", now=now + 3601)) is not None)

    rid, revoked = onboarding.create_code(db, "Revoked")
    onboarding.revoke_code(db, rid, actor="ann@x.com")
    check("a revoked code is refused",
          raises(lambda: onboarding.redeem_code(db, revoked, "PC-X")) is not None)
    check("...and reads as revoked", onboarding.get_code(db, rid)["status"] == onboarding.STATUS_REVOKED)
    check("revoking an unknown code is a KeyError",
          raises(lambda: onboarding.revoke_code(db, "nope"), KeyError) is not None)

    print("\n-- revoking a SPENT code takes the machine off the fleet channel --")
    sid, spent = onboarding.create_code(db, "Leaked")
    agent_id, token = onboarding.redeem_code(db, spent, "PC-UNEXPECTED")
    onboarding.revoke_code(db, sid, actor="ann@x.com")
    check("the agent it minted no longer authenticates",
          fleet.authenticate_agent(db, agent_id, token) is None)
    check("status reports it revoked", onboarding.status_for_code(db, spent)["state"] == "revoked")


def test_secret_path_unchanged(db):
    print("\n-- the shared-secret enrollment still behaves as it did --")
    agent_id, token = fleet.enroll_agent(db, "PC-SECRET", "s3cret", "s3cret")
    check("a right secret enrols", fleet.authenticate_agent(db, agent_id, token) == "PC-SECRET")
    check("a wrong secret is refused",
          raises(lambda: fleet.enroll_agent(db, "PC-X", "wrong", "s3cret"), PermissionError))
    check("an unset hub secret fails closed",
          raises(lambda: fleet.enroll_agent(db, "PC-X", "", ""), PermissionError))


def test_authorize_report(db):
    print("\n-- who a /api/report is from --")
    a_id, a_tok = fleet.issue_agent_identity(db, "PC-A", actor="test")
    b_id, b_tok = fleet.issue_agent_identity(db, "PC-B", actor="test")

    check("no token, never signed: unsigned",
          fleet.authorize_report(db, None, None, "PC-A")[0] == fleet.REPORT_UNSIGNED)
    check("a valid token under its own name: signed",
          fleet.authorize_report(db, a_id, a_tok, "PC-A")[0] == fleet.REPORT_SIGNED)
    check("...after which an unsigned report under that name is latched",
          fleet.authorize_report(db, None, None, "PC-A")[0] == fleet.REPORT_LATCHED)
    check("...case-insensitively",
          fleet.authorize_report(db, None, None, "pc-a")[0] == fleet.REPORT_LATCHED)
    check("an unrelated name is not latched",
          fleet.authorize_report(db, None, None, "PC-NEW")[0] == fleet.REPORT_UNSIGNED)

    outcome, reason = fleet.authorize_report(db, a_id, "forged", "PC-A")
    check("a wrong token is refused, not downgraded to unsigned",
          outcome == fleet.REPORT_BAD_TOKEN and reason == fleet.AUTH_UNKNOWN)

    check("an enrolled agent may not take another enrolled machine's name",
          fleet.authorize_report(db, a_id, a_tok, "PC-B")[0] == fleet.REPORT_NAME_TAKEN)
    check("...and PC-B is still PC-B's",
          fleet.authenticate_agent(db, b_id, b_tok) == "PC-B")

    print("\n-- a renamed PC keeps its credential --")
    check("a valid token under a new, free name is accepted",
          fleet.authorize_report(db, a_id, a_tok, "PC-A-RENAMED")[0] == fleet.REPORT_SIGNED)
    check("...and the enrollment follows it",
          fleet.authenticate_agent(db, a_id, a_tok) == "PC-A-RENAMED")
    with sqlite3.connect(db) as conn:
        renamed = conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'agent.rename'").fetchone()
    check("...which is audited", renamed is not None)
    check("a re-cased hostname is a rename too, not a refusal",
          fleet.authorize_report(db, a_id, a_tok, "pc-a-renamed")[0] == fleet.REPORT_SIGNED)

    fleet.revoke_agent(db, b_id)
    outcome, reason = fleet.authorize_report(db, b_id, b_tok, "PC-B")
    check("a revoked agent is refused and told so",
          outcome == fleet.REPORT_BAD_TOKEN and reason == fleet.AUTH_REVOKED)


def main():
    db = fresh_db()
    try:
        code_id, code = test_codes(db)
        test_redeem(db, code_id, code)
        test_race(db)
        test_expiry_and_revoke(db)
        test_secret_path_unchanged(db)
        test_authorize_report(db)
    finally:
        try:
            os.remove(db)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
