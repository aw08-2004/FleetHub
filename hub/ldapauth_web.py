"""Flask surface for the Active Directory sign-in form -- POST /login/ldap (roadmap #4).

The model half is ldapauth.py, and its docstring carries the design. This file adds the
things only the HTTP layer can:

  * **a form token.** The login page is the one form in the hub that posts without a
    session, so login_required's CSRF gate does not cover it. Without a token a hostile page
    could sign a visitor's browser in as the ATTACKER's account (login CSRF), and whatever
    the victim then typed into the console would be done -- and audited -- as somebody else.
    The token rides in the same session cookie `_complete_login` rotates.
  * **the per-address limit**, keyed on `request.remote_addr` -- which ProxyFix has already
    set from the one proxy hop it trusts -- and NOT on the raw X-Forwarded-For header
    rate_limit.py reads, whose first entry any client can write. An IPv6 client is keyed on
    its /64, because one ordinary IPv6 assignment is 2^64 addresses and a key per address
    would give every request a fresh budget (found in review).
  * **one answer for every refusal.** Wrong password, unknown account, disabled account, not
    in the login group, and an account the lockout guard is holding back: the same page, the
    same status, and never faster than ldapauth.REFUSAL_FLOOR_SECONDS. The reason goes to the
    hub log, where the admin who can act on it reads it.

The success path is `complete_login(user_info, provider)`, the same function every OAuth
callback ends in, so the authorization decision cannot differ by which door was used. The
login group decides who may TRY; ALLOWED_EMAILS and the permission groups still decide who
gets in and what they hold.
"""
import ipaddress
import time

from flask import Blueprint, request

import authconfig
import ldapauth


def address_key(address):
    """The per-address throttle's key: an IPv4 address as it is, an IPv6 address as its /64."""
    try:
        ip = ipaddress.ip_address(str(address or "").strip())
    except ValueError:
        return str(address or "unknown")
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def create_ldapauth_blueprint(*, load_config, complete_login, render_login,
                              form_token_valid, connector_factory=None, log=print,
                              clock=time.monotonic, sleep=time.sleep):
    """`render_login(error_key, status, **error_args)` re-renders the sign-in page with a
    message. `connector_factory`, `clock` and `sleep` exist for the tests."""
    bp = Blueprint("ldapauth", __name__)
    make_connector = connector_factory or ldapauth.ldap3_connector
    lockout = ldapauth.LockoutGuard(ldapauth.USER_FAILURE_LIMIT, ldapauth.USER_RESET_AFTER,
                                    clock=clock)
    addresses = ldapauth.AddressThrottle(ldapauth.ADDRESS_FAILURE_LIMIT,
                                         ldapauth.ADDRESS_FAILURE_WINDOW, clock=clock)
    # Exposed for the tests, which need to see what was counted.
    bp.lockout = lockout
    bp.addresses = addresses

    def _minutes(seconds):
        return max(1, (int(seconds) + 59) // 60)

    def _refuse(started):
        # The floor: see ldapauth.REFUSAL_FLOOR_SECONDS.
        elapsed = clock() - started
        if elapsed < ldapauth.REFUSAL_FLOOR_SECONDS:
            sleep(ldapauth.REFUSAL_FLOOR_SECONDS - elapsed)
        else:
            # The floor hides the member path's extra bind only while the whole refusal fits
            # under it. Said in the log so a slow DC is visible as the reason it stopped
            # fitting, rather than the floor quietly doing nothing.
            log(f"[auth] AD sign-in refusal took {elapsed:.2f}s, over the "
                f"{ldapauth.REFUSAL_FLOOR_SECONDS}s floor -- response time may show which "
                f"names are login-group members; check the DC's latency")
        return render_login("login.ldap_failed", 401)

    @bp.route("/login/ldap", methods=["POST"])
    def login_ldap():
        started = clock()
        config = load_config()
        if not authconfig.ldap_configured(config):
            return "Active Directory sign-in is not configured on this hub.", 404
        problem = authconfig.ldap_problem(config)
        if problem:
            # A hand-edited .env that the console would have refused. app.py logs it at boot
            # as well; this line is for the admin reading the log when a user reports it.
            log(f"[auth] AD sign-in is switched off until its settings are fixed: {problem}")
            return render_login("login.ldap_unavailable", 503)
        if not form_token_valid(request.form.get("csrf_token")):
            # Almost always a sign-in page left open across a hub restart or for longer
            # than the cookie lived. Re-rendering issues a fresh token, so the retry works.
            return render_login("login.ldap_expired", 400)

        address = request.remote_addr or "unknown"
        key = address_key(address)
        wait, ticket = addresses.reserve(key)
        if wait:
            # Said plainly, unlike the per-account block: it describes the CLIENT, not
            # whether some account exists, so it gives nothing away.
            log(f"[auth] AD sign-in from {address} refused: too many failures from {key}, "
                f"{wait}s left")
            return render_login("login.ldap_throttled", 429, minutes=_minutes(wait))

        username = request.form.get("username") or ""
        password = request.form.get("password") or ""
        try:
            user_info = ldapauth.authenticate(config, username, password,
                                              make_connector(config), lockout)
        except ldapauth.LoginRefused as e:
            # !r, so a user name carrying a line break cannot forge a second log line.
            log(f"[auth] AD sign-in refused for {username.strip()[:100]!r} from {address}: "
                f"{e.reason}")
            return _refuse(started)
        except ldapauth.DirectoryUnavailable as e:
            # Not the client's fault, so it does not spend the client's budget.
            addresses.release(key, ticket)
            log(f"[auth] AD sign-in could not reach the directory: {e}")
            return render_login("login.ldap_unavailable", 503)

        addresses.release(key, ticket)
        if not user_info.get("email"):
            log(f"[auth] AD account {username.strip()[:100]!r} has neither mail nor "
                f"userPrincipalName set, so it cannot be matched to a permission group")
        return complete_login(user_info, config.get("ldap_display_name")
                              or authconfig.DEFAULT_LDAP_DISPLAY_NAME)

    return bp
