"""HTTP-layer test for onboarding_web.py (roadmap #29): the Onboarding admin page and the
public /onboarding wizard.

Wires the blueprint onto a minimal Flask app with a fake login_required, like
test_audit_web.py -- app.py cannot be imported here without an OAuth config.

**The silent failure this file exists to catch is a credential reaching somebody who should
not have it.** The public routes are unauthenticated by design, so what each may return is the
whole security of the feature: check and status never carry a token, the rendered install
script never carries a token, a bad code gets the same 404 whatever is wrong with it, and only
redeem -- called by the script on the PC -- hands out an agent credential, once. Any of these
going wrong still "works" from the person-at-the-PC's point of view, which is why it is
asserted rather than eyeballed.
"""
import functools
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import i18n
import onboarding
import onboarding_web
import permissions
from flask import Blueprint, Flask, session
from onboarding_web import create_onboarding_blueprint
from permissions_web import create_access

PASS = 0
FAIL = 0

SUPERUSERS = {"root@x.com"}
CURRENT_USER = "root@x.com"
HUB = "https://hub.example.com"


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


#: Every endpoint the shared sidebar url_for()s, as "blueprint.endpoint" or a bare endpoint.
#: The onboarding blueprint is the real one, so it is the one name missing here.
SIDEBAR_ENDPOINTS = """index inventory_page alerts_page tools_page remote_page settings_page
    permissions_page logout packages.packages_page backups.backups_page invites.invites_page
    audit.audit_page users.users_page bios.firmware_page rules.rules_page patches.patches_page
    events.events_page apitokens.download_page sharing.sharing_page
    provisioning.provisioning_page location.fleet_map_page reports.reports_page
    policy.policy_page device_groups.device_groups_page watchdogs.watchdogs_page""".split()


def build_app(db_path, unsigned=None):
    app = Flask(__name__, template_folder=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "templates"))
    app.secret_key = "test"
    access = create_access(db_path, SUPERUSERS)
    app.register_blueprint(create_onboarding_blueprint(
        db_path, fake_login_required, access, hub_url=HUB,
        valid_machine_name=lambda n: bool(n) and "<" not in n,
        unsigned_reporters=lambda: list(unsigned or [])))
    for name in SIDEBAR_ENDPOINTS:
        blueprint, _, endpoint = name.rpartition(".")
        if not blueprint:
            app.add_url_rule(f"/_stub/{endpoint}", endpoint, lambda: "")
            continue
        bp = Blueprint(blueprint, __name__)
        bp.add_url_rule(f"/_stub/{blueprint}", endpoint, lambda: "")
        app.register_blueprint(bp)

    @app.context_processor
    def _nav_context():
        email = (session.get("user") or {}).get("email")
        caps = permissions.effective_permissions(db_path, email, superusers=SUPERUSERS)
        return dict(i18n.template_context("en"), cap=permissions, hub_version="test",
                    user_capabilities=caps["capabilities"], open_alert_count=0,
                    is_superuser=email in SUPERUSERS, latest_agent_version=None)

    @app.before_request
    def _seed_session():
        session["user"] = {"email": CURRENT_USER}

    return app


def reset_limits():
    """The limiters are module-level, so one test's traffic would otherwise count against
    the next. Cleared rather than raised: the limits themselves are under test below."""
    onboarding_web._code_limiter._buckets.clear()
    onboarding_web._status_limiter._buckets.clear()


def main():
    global CURRENT_USER
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        permissions.init_permissions_db(db)
        fleet.init_fleet_db(db)
        onboarding.init_onboarding_db(db)
        permissions.invalidate()
        permissions.create_group(db, name="Viewers", capabilities=[permissions.VIEW],
                                 members=["viewer@x.com"], scope_mode=permissions.SCOPE_ALL)
        permissions.create_group(db, name="Helpdesk",
                                 capabilities=[permissions.VIEW, permissions.MANAGE_ONBOARDING],
                                 members=["tech@x.com"], scope_mode=permissions.SCOPE_ALL)
        app = build_app(db, unsigned=[{"machine": "OLD-PC", "last_report": 1}])
        c = app.test_client()

        print("\n== The admin routes need manage_onboarding ==")
        CURRENT_USER = "viewer@x.com"
        check("a viewer cannot open the page", c.get("/onboarding-codes").status_code == 403)
        check("...or list codes", c.get("/api/onboarding/codes").status_code == 403)
        check("...or create one", c.post("/api/onboarding/codes",
                                          json={"label": "x"}).status_code == 403)

        CURRENT_USER = "tech@x.com"
        page = c.get("/onboarding-codes")
        check("a technician with the capability opens it", page.status_code == 200)
        html = page.get_data(as_text=True)
        check("...and the sidebar links it", "/_stub" in html and 'href="/onboarding-codes"' in html)
        resp = c.post("/api/onboarding/codes", json={"label": "Reception PC"})
        check("creating a code answers 201", resp.status_code == 201)
        created = resp.get_json()
        code = created["code"]
        check("the code comes back once, with its link",
              created["link"] == f"{HUB}/onboarding?code={code}")
        listing = c.get("/api/onboarding/codes").get_json()
        check("the list never carries the code", code not in str(listing))
        check("the list carries the machines still reporting unsigned",
              listing["unsigned_reporters"][0]["machine"] == "OLD-PC")
        check("a bad lifetime is refused", c.post(
            "/api/onboarding/codes", json={"label": "x", "ttl_seconds": 5}).status_code == 400)

        print("\n== The public wizard ==")
        CURRENT_USER = None
        reset_limits()
        check("/onboarding renders without a session", c.get("/onboarding").status_code == 200)
        body = c.post("/api/onboarding/check", json={"code": code.lower()})
        check("check accepts the code in any case", body.status_code == 200)
        info = body.get_json()
        check("check returns the label and the script urls, and no token",
              info["label"] == "Reception PC" and info["script_url"].endswith(f"/{code}.ps1")
              and "token" not in info)
        bad = c.post("/api/onboarding/check", json={"code": "ZZZZZZ"})
        malformed = c.post("/api/onboarding/check", json={"code": "<script>"})
        check("an unknown and a malformed code get the same 404",
              bad.status_code == malformed.status_code == 404
              and bad.get_json() == malformed.get_json())

        print("\n== The install script ==")
        reset_limits()
        ps = c.get(f"/onboarding/run/{code}.ps1")
        script = ps.get_data(as_text=True)
        check("the .ps1 is served as plain text",
              ps.status_code == 200 and ps.mimetype == "text/plain")
        check("...naming this hub and this code", f"'{HUB}'" in script and f"'{code}'" in script)
        check("...installing with no enrollment secret", "-EnrollmentSecret" not in script)
        check("...and is not cached", ps.headers.get("Cache-Control") == "no-store")
        cmd = c.get(f"/onboarding/run/{code}.cmd")
        check("the .cmd downloads as an attachment",
              cmd.status_code == 200
              and f"FleetHub-Onboard-{code}.cmd" in cmd.headers.get("Content-Disposition", ""))
        check("...with CRLF line endings, which cmd.exe needs",
              b"\r\n" in cmd.data and b"\n" not in cmd.data.replace(b"\r\n", b""))
        check("...pointing at the .ps1 for the same code",
              f"{HUB}/onboarding/run/{code}.ps1".encode() in cmd.data)
        # Kaspersky blocked the first launcher, which piped the download into iex under
        # -ExecutionPolicy Bypass: the textbook dropper shape. Neither may come back.
        launcher = cmd.data.decode().lower()
        check("...saving the script and running it with -File, never piping into iex",
              "| iex" not in launcher and "invoke-expression" not in launcher
              and "-file" in launcher)
        check("...and never under -ExecutionPolicy Bypass",
              "bypass" not in launcher and "bypass" not in script.lower())
        # The wizard's copy-paste command is the one operators use most. It is the hub's
        # run_command, the same text the .cmd carries -- asserted, so the page cannot go back
        # to assembling its own (it did, and drifted).
        command = info["command"]
        check("the wizard is handed the very command the .cmd runs",
              command.encode() in cmd.data)
        wizard_js = os.path.join(os.path.dirname(onboarding_web.__file__),
                                 "static", "js", "onboarding.js")
        with open(wizard_js, encoding="utf-8") as fh:
            check("...and shows it as given", "psCommand.value = body.command;" in fh.read())
        # Saved into the user's TEMP and then run elevated, the script could be swapped by any
        # non-elevated process of that user in between. Both downloads that run elevated --
        # this script, and the agent installer it fetches -- go into an admin-only folder.
        check("the script is saved into a fresh admin-only folder, not the user's TEMP",
              "TEMP" not in command and "$env:ProgramData" in command
              and "CreateDirectory($d, $s)" in command
              and "SetAccessRuleProtection($true, $false)" in command)
        check("...run from there with -File under RemoteSigned, and the folder removed after",
              "-ExecutionPolicy RemoteSigned -File $f" in command
              and "Remove-Item -LiteralPath $d -Recurse" in command)
        check("...with no double quote or percent sign to break the .cmd's quoting",
              '"' not in command and "%" not in command)
        check("the agent installer goes into such a folder too",
              "GetTempPath" not in script and "$env:TEMP" not in script
              and "Join-Path $installerDir 'agent-install.ps1'" in script)
        check("a guessed code gets no script", c.get("/onboarding/run/ZZZZZZ.ps1").status_code == 404)
        check("...and no launcher", c.get("/onboarding/run/ZZZZZZ.cmd").status_code == 404)

        print("\n== Status, then redeem (what the script does) ==")
        reset_limits()
        check("status is 'waiting' before the script runs",
              c.get(f"/api/onboarding/status/{code}").get_json()["state"] == "waiting")
        fleet.issue_agent_identity(db, "DC-01", actor="test")
        taken = c.post("/api/onboarding/redeem", json={"code": code, "machine": "DC-01"})
        check("an enrolled machine's name gets 409, not a second credential",
              taken.status_code == 409 and "token" not in taken.get_json())
        check("...and the code is still waiting",
              c.get(f"/api/onboarding/status/{code}").get_json()["state"] == "waiting")
        check("a bad machine name is refused before the code is spent",
              c.post("/api/onboarding/redeem",
                     json={"code": code, "machine": "<x>"}).status_code == 400)
        red = c.post("/api/onboarding/redeem", json={"code": code, "machine": "PC-RECEPTION"})
        check("redeem returns a credential", red.status_code == 200)
        cred = red.get_json()
        check("...that authenticates as the machine",
              fleet.authenticate_agent(db, cred["agent_id"], cred["token"]) == "PC-RECEPTION")
        check("...and is not cached", red.headers.get("Cache-Control") == "no-store")
        again = c.post("/api/onboarding/redeem", json={"code": code, "machine": "PC-OTHER"})
        check("a second redeem gets the same 404 as a stranger",
              again.status_code == 404 and again.get_json() == bad.get_json())
        status = c.get(f"/api/onboarding/status/{code}").get_json()
        check("status now names the machine, and is online after its first auth",
              status == {"state": "online", "machine": "PC-RECEPTION"})
        check("status never carries the token", cred["token"] not in str(status))
        check("a spent code no longer gets a script",
              c.get(f"/onboarding/run/{code}.ps1").status_code == 404)

        print("\n== Revoking a spent code ==")
        CURRENT_USER = "tech@x.com"
        code_id = c.get("/api/onboarding/codes").get_json()["codes"][0]["code_id"]
        rv = c.post(f"/api/onboarding/codes/{code_id}/revoke", json={})
        check("revoke answers 200", rv.status_code == 200 and rv.get_json()["status"] == "revoked")
        check("...and the agent it minted is revoked too",
              fleet.authenticate_agent(db, cred["agent_id"], cred["token"]) is None)
        check("an unknown id is a 404",
              c.post("/api/onboarding/codes/nope/revoke", json={}).status_code == 404)

        print("\n== Rate limits ==")
        CURRENT_USER = None
        reset_limits()
        codes = [c.post("/api/onboarding/check", json={"code": "ZZZZZZ"}).status_code
                 for _ in range(onboarding_web.PER_CLIENT_PER_MINUTE + 1)]
        check("a guesser is cut off after the per-client limit", codes[-1] == 429)
        check("...and redeem shares that budget",
              c.post("/api/onboarding/redeem",
                     json={"code": "ZZZZZZ", "machine": "X"}).status_code == 429)
        reset_limits()
        spoofed = [c.post("/api/onboarding/check", json={"code": "ZZZZZZ"},
                          headers={"X-Forwarded-For": f"10.9.{i // 250}.{i % 250}"}).status_code
                   for i in range(onboarding_web.PER_CLIENT_PER_MINUTE + 1)]
        check("rotating X-Forwarded-For does not reset the per-client limit", spoofed[-1] == 429)
    finally:
        try:
            os.remove(db)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
