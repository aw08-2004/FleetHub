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


def _minutes(seconds):
    return max(1, (int(seconds) + 59) // 60)


class _SignIn:
    """One POST /login/ldap, step by step. A class rather than a closure in the factory so
    each step -- the gates, the address budget, the attempt -- reads on its own."""

    def __init__(self, deps, started):
        self.d = deps
        self.started = started

    def run(self):
        config = self.d.load_config()
        refusal = self._gate(config)
        if refusal is not None:
            return refusal
        address = request.remote_addr or "unknown"
        key = address_key(address)
        wait, ticket = self.d.addresses.reserve(key)
        if wait:
            # Said plainly, unlike the per-account block: it describes the CLIENT, not
            # whether some account exists, so it gives nothing away.
            self.d.log(f"[auth] AD sign-in from {address} refused: too many failures from "
                       f"{key}, {wait}s left")
            return self.d.render_login("login.ldap_throttled", 429, minutes=_minutes(wait))
        return self._attempt(config, address, key, ticket)

    def _gate(self, config):
        """The refusals that come before any directory traffic, or None."""
        if not authconfig.ldap_configured(config):
            return "Active Directory sign-in is not configured on this hub.", 404
        problem = authconfig.ldap_problem(config)
        if problem:
            # A hand-edited .env that the console would have refused. app.py logs it at boot
            # as well; this line is for the admin reading the log when a user reports it.
            self.d.log(f"[auth] AD sign-in is switched off until its settings are fixed: "
                       f"{problem}")
            return self.d.render_login("login.ldap_unavailable", 503)
        if not self.d.form_token_valid(request.form.get("csrf_token")):
            # Almost always a sign-in page left open across a hub restart or for longer
            # than the cookie lived. Re-rendering issues a fresh token, so the retry works.
            return self.d.render_login("login.ldap_expired", 400)
        return None

    def _attempt(self, config, address, key, ticket):
        username = request.form.get("username") or ""
        password = request.form.get("password") or ""
        shown = username.strip()[:100]
        try:
            user_info = ldapauth.authenticate(config, username, password,
                                              self.d.make_connector(config), self.d.lockout)
        except ldapauth.LoginRefused as e:
            # !r, so a user name carrying a line break cannot forge a second log line.
            self.d.log(f"[auth] AD sign-in refused for {shown!r} from {address}: {e.reason}")
            return self._refuse()
        except ldapauth.DirectoryUnavailable as e:
            # Not the client's fault, so it does not spend the client's budget.
            self.d.addresses.release(key, ticket)
            self.d.log(f"[auth] AD sign-in could not reach the directory: {e}")
            return self.d.render_login("login.ldap_unavailable", 503)
        self.d.addresses.release(key, ticket)
        if not user_info.get("email"):
            self.d.log(f"[auth] AD account {shown!r} has neither mail nor userPrincipalName "
                       f"set, so it cannot be matched to a permission group")
        return self.d.complete_login(user_info, config.get("ldap_display_name")
                                     or authconfig.DEFAULT_LDAP_DISPLAY_NAME)

    def _refuse(self):
        # The floor: see ldapauth.REFUSAL_FLOOR_SECONDS.
        elapsed = self.d.clock() - self.started
        if elapsed < ldapauth.REFUSAL_FLOOR_SECONDS:
            self.d.sleep(ldapauth.REFUSAL_FLOOR_SECONDS - elapsed)
        else:
            # The floor hides the member path's extra bind only while the whole refusal fits
            # under it. Said in the log so a slow DC is visible as the reason it stopped
            # fitting, rather than the floor quietly doing nothing.
            self.d.log(f"[auth] AD sign-in refusal took {elapsed:.2f}s, over the "
                       f"{ldapauth.REFUSAL_FLOOR_SECONDS}s floor -- response time may show "
                       f"which names are login-group members; check the DC's latency")
        return self.d.render_login("login.ldap_failed", 401)


class _Deps:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def create_ldapauth_blueprint(*, load_config, complete_login, render_login,
                              form_token_valid, connector_factory=None, log=print,
                              clock=time.monotonic, sleep=time.sleep):
    """`render_login(error_key, status, **error_args)` re-renders the sign-in page with a
    message. `connector_factory`, `clock` and `sleep` exist for the tests."""
    bp = Blueprint("ldapauth", __name__)
    deps = _Deps(
        load_config=load_config, complete_login=complete_login, render_login=render_login,
        form_token_valid=form_token_valid, log=log, clock=clock, sleep=sleep,
        make_connector=connector_factory or ldapauth.ldap3_connector,
        lockout=ldapauth.LockoutGuard(ldapauth.USER_FAILURE_LIMIT,
                                      ldapauth.USER_RESET_AFTER, clock=clock),
        addresses=ldapauth.AddressThrottle(ldapauth.ADDRESS_FAILURE_LIMIT,
                                           ldapauth.ADDRESS_FAILURE_WINDOW, clock=clock),
    )
    # Exposed for the tests, which need to see what was counted.
    bp.lockout = deps.lockout
    bp.addresses = deps.addresses

    @bp.route("/login/ldap", methods=["POST"])
    def login_ldap():
        return _SignIn(deps, clock()).run()

    return bp
