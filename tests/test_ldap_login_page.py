"""Tests the Active Directory sign-in as app.py wires it -- the boot check and the real page.

test_ldapauth.py covers the module against a fake directory in a bare Flask app. What it
cannot see is the composition root, and two silent failures live there:

  * **a hub whose only provider is Active Directory refuses to boot.** app.py's "no sign-in
    provider" check predates this provider; left alone, it crash-loops exactly the Docker
    hub the provider was added for, with an error telling the operator to configure Google.
  * **the page renders no form, or a form with no token.** A login page without the form
    looks like a hub with no AD configured; one without the token refuses every sign-in as
    "expired", which looks like a broken DC.

The DC here is a name that cannot resolve, so the one real network attempt fails fast and
must come back as the "cannot reach Active Directory" page -- which is also the first time
the ldap3 adapter itself is exercised, if only up to the socket.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-ldap-login-test-")
LDAP_ENV = {
    "LDAP_SERVER": "ldaps://dc1.corp.invalid",
    "LDAP_BIND_DN": "CN=svc-fleethub,OU=Service,DC=corp,DC=invalid",
    "LDAP_BIND_PASSWORD": "svc-pass",
    "LDAP_USER_BASE_DN": "OU=Staff,DC=corp,DC=invalid",
    "LDAP_LOGIN_GROUP_DN": "CN=FleetHub Operators,OU=Groups,DC=corp,DC=invalid",
}
# Under run_all.py this module has its own interpreter, so app has not been imported yet and
# the boot check below is real. Under a shared pytest process it may have been, with whatever
# provider an earlier module configured; then only the page half can be tested.
_FRESH = "app" not in sys.modules
if _FRESH:
    for _key in list(os.environ):
        if _key.startswith(("GOOGLE_", "OIDC_", "LDAP_")):
            del os.environ[_key]
    os.environ.update(LDAP_ENV)
    # The state dir too, so a real .env beside the checkout cannot add a Google key back.
    os.environ["HUB_STATE_DIR"] = _TMPDIR
    os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")
    os.environ.setdefault("ALLOWED_EMAILS", "root@example.com")
    os.chdir(_TMPDIR)

import app  # noqa: E402
import authconfig  # noqa: E402

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


def test_boot_with_active_directory_only():
    print("\n== A hub with only Active Directory boots ==")
    if not _FRESH:
        print("  (app was already imported in this process; boot check skipped)")
        return
    check("app.py imported without the no-provider RuntimeError", True)
    check("Active Directory is the enabled provider",
          app.LDAP_ENABLED and not app.GOOGLE_ENABLED and not app.OIDC_ENABLED)


def test_page_and_post():
    previous = dict(app.AUTH_CONFIG)
    app.configure_oauth(authconfig.load({**LDAP_ENV, "LDAP_DISPLAY_NAME": "Corp AD"}),
                        announce=False)
    try:
        client = app.app.test_client()
        print("\n== The sign-in page ==")
        html = client.get("/login").get_data(as_text=True)
        check("it has the Active Directory form", 'action="/login/ldap"' in html)
        check("...labelled with the display name", "Corp AD" in html)
        token = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
        check("...carrying a form token", token is not None)
        check("no 'or' divider when AD is the only provider",
              "login-ldap__divider" not in html)

        print("\n== Posting it ==")
        forged = client.post("/login/ldap", data={"username": "jdoe", "password": "pw",
                                                  "csrf_token": "forged"})
        check("a forged token is refused as an expired page", forged.status_code == 400)
        odd = client.post("/login/ldap", data={"username": "jdoe", "password": "pw",
                                               "csrf_token": "ñ" * 64})
        check("a non-ASCII token is a refusal too, not a 500", odd.status_code == 400)
        resp = client.post("/login/ldap", data={
            "username": "jdoe", "password": "pw",
            "csrf_token": token.group(1) if token else ""})
        body = resp.get_data(as_text=True)
        check("an unreachable DC is a 503, not a sign-in failure", resp.status_code == 503)
        check("...with the unavailable message",
              app.i18n.translate("login.ldap_unavailable") in body)
        check("the user name is kept for the retry", 'value="jdoe"' in body)
        check("the password is not echoed back", 'value="pw"' not in body)
        # /login redirects a signed-in session to the dashboard, so a 200 means none exists.
        check("nobody was signed in", client.get("/login").status_code == 200)
    finally:
        app.configure_oauth(previous, announce=False)


def main():
    test_boot_with_active_directory_only()
    test_page_and_post()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
