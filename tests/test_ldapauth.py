"""Tests ldapauth.py + ldapauth_web.py -- Active Directory sign-in (roadmap #4).

The silent failures this file exists to catch, each of which would look like a working
sign-in page:

  * **the password is tried for someone outside the login group.** The form would then be a
    password oracle and a lockout button for every account in the domain. Asserted by
    checking that no bind as the user ever happens when the service account's search came
    back empty.
  * **an empty password signs someone in.** An LDAP simple bind with a DN and no password is
    an unauthenticated bind, and many directories answer it with success.
  * **LDAP injection.** A user name of `*` or `x)(sAMAccountName=*` must not widen the filter.
  * **the hub locks out domain accounts** -- by counting in a sliding window AD does not use,
    by letting concurrent requests past a check-then-record, or by counting `jdoe`,
    `CORP\\jdoe` and the UPN as three accounts.
  * **the page or its timing says which part was wrong.** Unknown account, wrong password,
    not in the group and held back by the lockout guard must produce identical pages, and
    no refusal may come back faster than the floor.
  * **a hand-written ldap:// sends passwords in cleartext**, or a missing CA file is a 500.
  * **ldap3 rewrites the password** (SASLprep) before AD sees it.
  * **a forged sign-in post is accepted** (login CSRF).

No DC anywhere: the directory is a fake connector, and the ldap3 adapter checks below stop
before any socket. What this cannot cover is a real DC -- the TLS handshake and AD's real
bind replies are first-contact items on ROADMAP.MD's beta checklist.
"""
import json
import os
import sys
import threading
import time

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub")
sys.path.insert(0, HUB)
import ldapauth
from flask import Flask
from ldapauth_web import address_key, create_ldapauth_blueprint

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


GROUP = "CN=FleetHub Operators,OU=Groups,DC=corp,DC=local"
USER_DN = "CN=Jane Doe,OU=Staff,DC=corp,DC=local"
CONFIG = {
    "ldap_server": "ldaps://dc1.corp.local",
    "ldap_bind_dn": "CN=svc-fleethub,OU=Service,DC=corp,DC=local",
    "ldap_bind_password": "svc-pass",
    "ldap_user_base_dn": "OU=Staff,DC=corp,DC=local",
    "ldap_login_group_dn": GROUP,
    "ldap_display_name": "Corp AD",
    "ldap_ca_file": "",
    "ldap_tls_verify": True,
    "ldap_allow_insecure": False,
}
JANE = {"dn": USER_DN, "attributes": {
    "sAMAccountName": "jdoe", "userPrincipalName": "jdoe@corp.local",
    "mail": "jane.doe@example.com", "displayName": "Jane Doe"}}


class FakeDirectory:
    """A connector. `users` is what the service account's user search returns; `passwords`
    maps a DN to the password its bind accepts. Every bind and search is recorded, which is
    what lets the tests assert on what was NOT asked of the DC."""

    def __init__(self, users=(JANE,), passwords=None, groups=(GROUP,),
                 service_ok=True, bind_delay=0.0, policy=None, user_bind_error=None):
        self.users = list(users)
        self.passwords = {USER_DN: "right"} if passwords is None else passwords
        self.groups = list(groups)
        self.service_ok = service_ok
        self.bind_delay = bind_delay
        # The domain object's lockout policy, as a base read returns it. None: no policy.
        self.policy = policy
        self.user_bind_error = user_bind_error
        self.binds = []
        self.searches = []
        self._lock = threading.Lock()

    def __call__(self, dn, password, service=False):
        with self._lock:
            self.binds.append(dn)
        if service:
            if not self.service_ok:
                raise ldapauth.DirectoryUnavailable("service bind refused")
            return self._session()
        if self.bind_delay:
            time.sleep(self.bind_delay)
        if self.user_bind_error:
            raise self.user_bind_error
        if self.passwords.get(dn) != password:
            raise ldapauth.BindRejected("80090308: LdapErr: DSID-0C09044E, data 52e, v4563")
        return self._session()

    def _session(self):
        directory = self

        class Session:
            def search(self, base, search_filter, attributes, scope="subtree"):
                directory.searches.append((base, search_filter))
                if scope == "base":
                    return ([{"dn": base, "attributes": dict(directory.policy)}]
                            if directory.policy is not None else [])
                if "(objectClass=group)" in search_filter:
                    return [{"dn": g, "attributes": {}} for g in directory.groups]
                return list(directory.users)

            def close(self):
                pass

        return Session()

    def user_binds(self):
        with self._lock:
            return [dn for dn in self.binds if dn != CONFIG["ldap_bind_dn"]]


def fresh_guard(clock=time.monotonic):
    return ldapauth.LockoutGuard(ldapauth.USER_FAILURE_LIMIT, ldapauth.USER_RESET_AFTER,
                                 clock=clock)


def refused(fn):
    try:
        fn()
    except ldapauth.LoginRefused as e:
        return e
    return None


def test_pure_helpers():
    print("\n== Filter escaping (RFC 4515) ==")
    check("a star is escaped", ldapauth.escape_filter_value("*") == "\\2a")
    check("parentheses and backslash are escaped",
          ldapauth.escape_filter_value("a(b)c\\") == "a\\28b\\29c\\5c")
    check("NUL is escaped", ldapauth.escape_filter_value("a\x00") == "a\\00")
    injected = ldapauth.user_filter(("sam", "x)(sAMAccountName=*"), GROUP)
    check("an injected clause cannot close the filter",
          "(sAMAccountName=x\\29\\28sAMAccountName=\\2a)" in injected)

    print("\n== What the user search asks for ==")
    f = ldapauth.user_filter(("sam", "jdoe"), GROUP)
    check("the login group is in the filter, nested membership included",
          f"memberOf:{ldapauth.IN_CHAIN}:={GROUP}" in f)
    check("disabled accounts are filtered out", ldapauth.DISABLED_FILTER in f)
    check("people only, not computers", "(objectCategory=person)" in f)
    f = ldapauth.user_filter(("upn", "jane.doe@example.com"), GROUP)
    check("an address matches the UPN or the mail attribute",
          "(|(userPrincipalName=jane.doe@example.com)(mail=jane.doe@example.com))" in f)
    check("a group DN with a parenthesis is escaped inside the filter",
          "CN=Ops \\28EU\\29" in ldapauth.user_filter(("sam", "x"),
                                                     "CN=Ops (EU),DC=corp,DC=local"))

    print("\n== The three ways people type a user name ==")
    check("bare logon name", ldapauth.parse_username("jdoe") == ("sam", "jdoe"))
    check("down-level CORP\\jdoe drops the domain",
          ldapauth.parse_username("CORP\\jdoe") == ("sam", "jdoe"))
    check("UPN", ldapauth.parse_username(" jdoe@corp.local ") == ("upn", "jdoe@corp.local"))
    check("empty is refused", ldapauth.parse_username("  ") is None)
    check("a trailing backslash is refused", ldapauth.parse_username("CORP\\") is None)
    check("a control character is refused", ldapauth.parse_username("jd\noe") is None)
    check("an absurd length is refused", ldapauth.parse_username("a" * 300) is None)

    print("\n== Where groups are searched ==")
    # Groups live anywhere in the domain, not under the user search base.
    check("the domain root of the group DN",
          ldapauth.domain_root(GROUP) == "DC=corp,DC=local")
    check("AD's bind sub-code is put into words for the log",
          ldapauth.bind_reason("... data 775, v4563") == "account locked out")


def test_authenticate():
    print("\n== A member with the right password ==")
    directory = FakeDirectory()
    info = ldapauth.authenticate(CONFIG, "jdoe", "right", directory, fresh_guard())
    check("the mail attribute is the identity", info["email"] == "jane.doe@example.com")
    check("the display name is carried", info["name"] == "Jane Doe")
    # The groups claim is what lets a permission group map the same DN, exactly as it does
    # for an OIDC issuer's `groups` claim.
    check("the group DNs come back as the groups claim", info["groups"] == [GROUP])
    check("the group search starts at the domain root",
          any(base == "DC=corp,DC=local" for base, _ in directory.searches))
    check("the user's own password was tried exactly once",
          directory.user_binds() == [USER_DN])

    no_mail = {"dn": USER_DN, "attributes": {"userPrincipalName": "jdoe@corp.local",
                                              "sAMAccountName": "jdoe"}}
    info = ldapauth.authenticate(CONFIG, "jdoe", "right", FakeDirectory(users=[no_mail]),
                                 fresh_guard())
    check("with no mail attribute the UPN is the identity",
          info["email"] == "jdoe@corp.local" and info["name"] == "jdoe")

    print("\n== The group is checked BEFORE the password ==")
    # The design point of the module: the search is filtered by the group, and an empty
    # result means the DC is never asked about this person's password at all.
    directory = FakeDirectory(users=[])
    e = refused(lambda: ldapauth.authenticate(CONFIG, "outsider", "anything", directory,
                                              fresh_guard()))
    check("someone not in the login group is refused", e is not None)
    check("...and their password was never tried", directory.user_binds() == [])

    print("\n== An empty password never reaches the DC ==")
    directory = FakeDirectory(passwords={USER_DN: ""})
    e = refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "", directory, fresh_guard()))
    check("an empty password is refused even where the directory would accept it",
          e is not None)
    check("...without contacting the directory at all", directory.binds == [])
    check("a non-string password is refused",
          refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", None, FakeDirectory(),
                                                fresh_guard())) is not None)
    check("an over-long password is refused before the bind",
          refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "x" * 300, FakeDirectory(),
                                                fresh_guard())) is not None)

    print("\n== Ambiguity is refused, not guessed ==")
    twin = {"dn": "CN=Jane Doe 2,OU=Staff,DC=corp,DC=local", "attributes": {}}
    directory = FakeDirectory(users=[JANE, twin])
    check("two accounts answering to one name are refused",
          refused(lambda: ldapauth.authenticate(CONFIG, "jane.doe@example.com", "right",
                                                directory, fresh_guard())) is not None)
    check("...and neither password is tried", directory.user_binds() == [])

    print("\n== A wrong service account is a configuration problem ==")
    try:
        ldapauth.authenticate(CONFIG, "jdoe", "right", FakeDirectory(service_ok=False),
                              fresh_guard())
        check("a refused service bind raises DirectoryUnavailable", False)
    except ldapauth.DirectoryUnavailable:
        check("a refused service bind raises DirectoryUnavailable", True)


def test_lockout_protection():
    print("\n== The hub stops asking before AD locks the account ==")
    guard = fresh_guard()
    directory = FakeDirectory()
    # Every spelling of one account, in turn. Keyed on what was TYPED, this would be four
    # budgets and four times the failures reaching AD's lockout count.
    spellings = ("jdoe", "CORP\\jdoe", "jdoe@corp.local", "jane.doe@example.com")
    for i in range(ldapauth.USER_FAILURE_LIMIT):
        spelling = spellings[i % len(spellings)]
        refused(lambda s=spelling: ldapauth.authenticate(CONFIG, s, "wrong", directory, guard))
    check(f"{ldapauth.USER_FAILURE_LIMIT} wrong passwords reached the DC",
          len(directory.user_binds()) == ldapauth.USER_FAILURE_LIMIT)
    before = len(directory.user_binds())
    e = refused(lambda: ldapauth.authenticate(CONFIG, "CORP\\jdoe", "right", directory, guard))
    check("the next attempt, under any spelling, is refused even with the RIGHT password",
          e is not None and e.throttled_for > 0)
    check("...without a bind, so it cannot add to AD's lockout count",
          len(directory.user_binds()) == before)

    print("\n== Counted the way AD counts: reset only after a quiet hour ==")
    # The sliding window this replaced let a patient attacker refill one failure at a time:
    # 4 at t=0, then one more each time the OLDEST aged out -- while AD, which resets only
    # after a full window with NO failure, saw 5 and locked the account. Found in review.
    now = [0.0]
    guard = fresh_guard(clock=lambda: now[0])
    directory = FakeDirectory()
    for _ in range(ldapauth.USER_FAILURE_LIMIT):
        refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "wrong", directory, guard))
        now[0] += 60
    now[0] = ldapauth.USER_RESET_AFTER - 30   # past the FIRST failure's hour, not the last's
    refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "wrong", directory, guard))
    check("a fifth failure is NOT sent once only the oldest has aged out",
          len(directory.user_binds()) == ldapauth.USER_FAILURE_LIMIT)
    now[0] = 3 * 60 + ldapauth.USER_RESET_AFTER + 1   # an hour after the LAST failure
    refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "wrong", directory, guard))
    check("an hour after the last failure the count starts again",
          len(directory.user_binds()) == ldapauth.USER_FAILURE_LIMIT + 1)

    print("\n== Concurrent attempts cannot overshoot ==")
    # Check-then-record let thirty simultaneous wrong passwords all pass the check before the
    # first was recorded. Reserve-before-bind under one lock is what this pins.
    guard = fresh_guard()
    directory = FakeDirectory(bind_delay=0.05)
    threads = [threading.Thread(target=lambda: refused(lambda: ldapauth.authenticate(
        CONFIG, "jdoe", "wrong", directory, guard))) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(f"30 concurrent wrong passwords put at most {ldapauth.USER_FAILURE_LIMIT} binds "
          f"on the DC (got {len(directory.user_binds())})",
          len(directory.user_binds()) <= ldapauth.USER_FAILURE_LIMIT)

    print("\n== A success resets the account's count ==")
    guard = fresh_guard()
    directory = FakeDirectory()
    for _ in range(ldapauth.USER_FAILURE_LIMIT - 1):
        refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "wrong", directory, guard))
    ldapauth.authenticate(CONFIG, "jdoe", "right", directory, guard)
    refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "wrong", directory, guard))
    check("one typo after a good sign-in is not a lockout",
          guard.blocked_for(USER_DN.casefold()) == 0)

    print("\n== The address throttle ==")
    now = [0.0]
    throttle = ldapauth.AddressThrottle(2, 60, clock=lambda: now[0])
    _, t1 = throttle.reserve("a")
    throttle.reserve("a")
    check("blocked at the limit", throttle.reserve("a")[0] > 0)
    throttle.release("a", t1)
    check("a released attempt (a success) gives its slot back", throttle.reserve("a")[0] == 0)
    now[0] += 61
    check("free again once the window has passed", throttle.reserve("a")[0] == 0)
    check("an IPv6 client is keyed on its /64",
          address_key("2001:db8:1:2:aaaa::1") == address_key("2001:db8:1:2:ffff::9")
          == "2001:db8:1:2::/64")
    check("another /64 is another client",
          address_key("2001:db8:1:3::1") != address_key("2001:db8:1:2::1"))
    check("an IPv4 address is itself", address_key("203.0.113.9") == "203.0.113.9")
    check("an IPv4-mapped IPv6 address counts as its IPv4 address",
          address_key("::ffff:203.0.113.9") == "203.0.113.9")


def _filetime(epoch):
    return int((epoch + ldapauth.FILETIME_EPOCH_OFFSET) * 10_000_000)


def with_count(count, last_bad_epoch):
    attrs = dict(JANE["attributes"], badPwdCount=str(count),
                 badPasswordTime=str(_filetime(last_bad_epoch)))
    return {"dn": USER_DN, "attributes": attrs}


def _spend(directory, guard, now, times):
    """Send `times` wrong passwords, raising the fake's badPwdCount as AD would for each
    one that actually reached it."""
    for _ in range(times):
        before = len(directory.user_binds())
        refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "wrong", directory, guard,
                                              clock=lambda: now))
        if len(directory.user_binds()) > before:
            n = int(directory.users[0]["attributes"]["badPwdCount"]) + 1
            directory.users = [with_count(n, now)]


def test_ad_own_count():
    print("\n== AD's own lockout count is read before every password ==")
    now = 1_800_000_000.0
    thirty_min = str(-30 * 60 * 10_000_000)
    policy = {"lockoutThreshold": "5", "lockOutObservationWindow": thirty_min}
    check("headroom is threshold minus AD's count",
          ldapauth.ad_lockout_headroom(policy, with_count(1, now - 60)["attributes"], now) == 4)
    check("a count whose last failure is past AD's window (plus skew margin) is zero",
          ldapauth.ad_lockout_headroom(
              policy, with_count(4, now - 30 * 60 - ldapauth.CLOCK_SKEW_MARGIN - 1)
              ["attributes"], now) == 5)
    check("...but not at the boundary, where the DC's clock could disagree",
          ldapauth.ad_lockout_headroom(policy, with_count(4, now - 30 * 60 - 1)["attributes"],
                                       now) == 1)
    check("no lockout policy (threshold 0) leaves it to the hub's own guard",
          ldapauth.ad_lockout_headroom({"lockoutThreshold": "0"},
                                       with_count(3, now)["attributes"], now) is None)
    check("unreadable attributes leave it to the hub's own guard",
          ldapauth.ad_lockout_headroom({}, {}, now) is None)
    import datetime
    check("ldap3's decoded forms are understood too (int, list, bytes, timedelta)",
          ldapauth._int([7]) == 7 and ldapauth._int(b"12") == 12
          and ldapauth._int(datetime.timedelta(minutes=30)) == int(thirty_min))

    # The user typed a wrong password at a workstation once; an attacker then spends the
    # hub's attempts. Counting only its own, the hub would let all 4 through and AD would
    # lock at 5. Found in the second review.
    directory = FakeDirectory(users=[with_count(1, now - 60)], policy=policy)
    _spend(directory, fresh_guard(), now, ldapauth.USER_FAILURE_LIMIT)
    check(f"with one outside failure standing, AD ends at 4 of 5, not 5 "
          f"(hub sent {len(directory.user_binds())})",
          int(directory.users[0]["attributes"]["badPwdCount"]) == 4)

    # A DISA STIG domain: threshold 3. The hub's own limit of 4 would lock it on its own.
    stig = {"lockoutThreshold": "3", "lockOutObservationWindow": thirty_min}
    directory = FakeDirectory(users=[with_count(0, now - 3600)], policy=stig)
    _spend(directory, fresh_guard(), now, ldapauth.USER_FAILURE_LIMIT)
    check("under a threshold of 3, the hub sends at most 2 wrong passwords",
          len(directory.user_binds()) == 2)

    # It reads AD's count, so a hub restart (a fresh guard) does not hand out 4 more.
    directory = FakeDirectory(users=[with_count(4, now - 60)], policy=policy)
    e = refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "right", directory,
                                              fresh_guard(), clock=lambda: now))
    check("a fresh guard (a restarted hub) still refuses an account AD has at 4 of 5",
          e is not None and directory.user_binds() == [])

    print("\n== A success does not wipe failures still in flight ==")
    # B, C, D are binding when A (the real user) succeeds. AD resets on A, then counts B, C,
    # D. The second version popped the whole entry on A and then let 4 more through.
    guard = fresh_guard()
    key = "k"
    for _ in range(4):
        guard.reserve(key)       # B, C, D, then A
    guard.succeeded(key)         # A finishes first
    for _ in range(3):
        guard.failed(key)        # B, C, D land after it
    allowed = 0
    while guard.reserve(key) == 0:
        allowed += 1
        guard.failed(key)
    check(f"after that, the hub allows only {ldapauth.USER_FAILURE_LIMIT - 3} more "
          f"(allowed {allowed})", allowed == ldapauth.USER_FAILURE_LIMIT - 3)

    print("\n== A member's bind that does not complete is a refusal, and counted ==")
    # Only members reach this bind, so a distinct 503 here would name them.
    guard = fresh_guard()
    directory = FakeDirectory(user_bind_error=ldapauth.DirectoryUnavailable("reset by peer"))
    e = refused(lambda: ldapauth.authenticate(CONFIG, "jdoe", "right", directory, guard))
    check("it is LoginRefused, not DirectoryUnavailable", e is not None)
    check("...and it counts against the account",
          guard._state.get(USER_DN.casefold(), {}).get("settled") == 1)


def test_ldap3_adapter():
    """The real adapter, stopped before any socket. ldap3 is a hub requirement and present
    wherever this suite runs."""
    import ldap3

    print("\n== The adapter refuses cleartext on its own authority ==")
    for url in ("ldap://dc1.corp.local", "dc1.corp.local"):
        try:
            ldapauth.ldap3_connector({**CONFIG, "ldap_server": url})
            check(f"{url!r} without LDAP_ALLOW_INSECURE is refused", False)
        except ldapauth.DirectoryUnavailable as e:
            check(f"{url!r} without LDAP_ALLOW_INSECURE is refused", "cleartext" in str(e))

    print("\n== A missing CA file is 'unavailable', not a 500 ==")
    try:
        ldapauth.ldap3_connector({**CONFIG, "ldap_ca_file": "/nowhere/ad-ca.pem"})
        check("a missing CA file raises DirectoryUnavailable", False)
    except ldapauth.DirectoryUnavailable:
        check("a missing CA file raises DirectoryUnavailable", True)
    except Exception as e:  # the bug: ldap3's own exception escaping to the route
        check(f"a missing CA file raises DirectoryUnavailable (got {type(e).__name__})", False)

    print("\n== The password reaches ldap3 as bytes, so SASLprep never rewrites it ==")
    seen = []

    class FakeConnection:
        def __init__(self, server, user=None, password=None, **kwargs):
            seen.append(password)
            self.result = {"result": 49, "description": "invalidCredentials",
                           "message": "data 52e"}

        def bind(self):
            return False

        def unbind(self):
            pass

    real = ldap3.Connection
    ldap3.Connection = FakeConnection
    try:
        connector = ldapauth.ldap3_connector(CONFIG)
        tricky = "Passµwort²"     # micro sign and superscript two: NFKC folds both
        try:
            connector(USER_DN, tricky, service=False)
        except ldapauth.BindRejected:
            pass
        check("the password was handed over as bytes", seen and isinstance(seen[-1], bytes))
        check("...byte for byte as typed, not NFKC-folded",
              seen and seen[-1] == tricky.encode("utf-8"))
        try:
            connector(USER_DN, "bell\x07", service=False)
            check("a control character is a wrong password, not 'unavailable'", False)
        except ldapauth.BindRejected:
            check("a control character is a wrong password, not 'unavailable'", True)
        except ldapauth.DirectoryUnavailable:
            check("a control character is a wrong password, not 'unavailable'", False)
    finally:
        ldap3.Connection = real


def test_web():
    print("\n== The form ==")
    logged = []
    completed = []
    slept = []
    now = [1000.0]
    state = {"config": dict(CONFIG), "directory": FakeDirectory(), "token_ok": True}

    def render_login(error_key, status, **args):
        return json.dumps({"error": error_key, "args": args}), status

    def complete_login(user_info, provider):
        completed.append((user_info, provider))
        return "signed in", 302

    app = Flask(__name__)
    app.secret_key = "test"
    bp = create_ldapauth_blueprint(
        load_config=lambda: state["config"],
        complete_login=complete_login,
        render_login=render_login,
        form_token_valid=lambda token: state["token_ok"] and token == "tok",
        connector_factory=lambda config: state["directory"],
        log=logged.append,
        clock=lambda: now[0],
        sleep=slept.append,
    )
    app.register_blueprint(bp)
    client = app.test_client()

    def post(username="jdoe", password="right", token="tok", address="10.0.0.1"):
        return client.post("/login/ldap", data={"username": username, "password": password,
                                                "csrf_token": token},
                           environ_base={"REMOTE_ADDR": address})

    resp = post()
    check("a member with the right password is handed to complete_login",
          resp.status_code == 302 and completed
          and completed[-1][0]["email"] == "jane.doe@example.com")
    check("...under the configured display name", completed[-1][1] == "Corp AD")
    check("...and a success is not slowed down", slept == [])

    print("\n== One answer for every refusal ==")
    wrong = post(password="wrong")
    check("a refusal is held to the timing floor",
          slept and abs(slept[-1] - ldapauth.REFUSAL_FLOOR_SECONDS) < 1e-9)
    state["directory"] = FakeDirectory(users=[])
    unknown = post(username="nobody")
    check("a wrong password is a 401", wrong.status_code == 401)
    # Byte-identical, or the page tells an attacker which accounts exist and are in the group.
    check("unknown account and wrong password render the same page",
          wrong.get_data() == unknown.get_data() and wrong.status_code == unknown.status_code)
    check("the precise reason went to the log instead",
          any("wrong password" in line for line in logged)
          and any("no enabled account" in line for line in logged))
    check("the password never reaches the log", not any("right" in line or "'wrong'" in line
                                                       for line in logged))

    # The per-account block must look like any other refusal: a distinct 429 there would
    # tell an anonymous client the name is an enabled member, since only members are counted.
    state["directory"] = FakeDirectory()
    for _ in range(ldapauth.USER_FAILURE_LIMIT):
        post(password="wrong", address="10.0.0.2")
    blocked = post(password="right", address="10.0.0.2")
    check("an account the guard holds back gets the SAME page as a wrong password",
          blocked.status_code == wrong.status_code and blocked.get_data() == wrong.get_data())
    check("...while the log says why", any("not asking the DC" in line for line in logged))

    print("\n== Login CSRF ==")
    state["directory"] = FakeDirectory()
    count = len(completed)
    resp = post(token="forged", address="10.0.0.3")
    check("a post without the page's token is refused",
          resp.status_code == 400 and len(completed) == count)
    check("...with the expired-page message, which re-renders a fresh token",
          json.loads(resp.get_data())["error"] == "login.ldap_expired")

    print("\n== Turned off, or switched off by its own settings ==")
    state["config"] = {**CONFIG, "ldap_login_group_dn": ""}
    check("the route is a 404 when AD sign-in is not fully configured",
          post().status_code == 404)
    # A hand-written .env the console would have refused. Before this, it was used as-is.
    state["config"] = {**CONFIG, "ldap_server": "ldap://dc1.corp.local"}
    state["directory"] = FakeDirectory()
    resp = post(address="10.0.0.4")
    check("a hand-written ldap:// is refused as unavailable, before any bind",
          resp.status_code == 503 and state["directory"].binds == [])
    check("...and the log names the reason", any("switched off" in line for line in logged))
    state["config"] = dict(CONFIG)

    print("\n== The directory being down is not the user's fault ==")
    state["directory"] = FakeDirectory(service_ok=False)
    resp = post(address="10.0.0.5")
    check("an unreachable DC is a 503 with its own message",
          resp.status_code == 503
          and json.loads(resp.get_data())["error"] == "login.ldap_unavailable")
    check("...and does not spend the client's budget",
          bp.addresses.reserve(address_key("10.0.0.5")) == (0, now[0]))

    print("\n== One client walking through user names ==")
    state["directory"] = FakeDirectory(users=[])
    for i in range(ldapauth.ADDRESS_FAILURE_LIMIT):
        post(username=f"guess{i}", address=f"2001:db8:9:9::{i + 1:x}")
    resp = post(username="another", address="2001:db8:9:9::ffff")
    check("an IPv6 client rotating addresses inside its /64 is still throttled",
          resp.status_code == 429
          and json.loads(resp.get_data())["error"] == "login.ldap_throttled")
    state["directory"] = FakeDirectory()
    # jdoe is still held back by the guard from the per-account check above; let that hour
    # pass, or this would be testing the account guard instead of the address throttle.
    now[0] += ldapauth.USER_RESET_AFTER + 1
    check("another client is unaffected", post(address="198.51.100.4").status_code == 302)

    print("\n== Every message the form can show exists in the catalog ==")
    with open(os.path.join(HUB, "locales", "en.json"), encoding="utf-8") as handle:
        login = json.load(handle)["login"]
    for key in ("ldap_failed", "ldap_throttled", "ldap_unavailable", "ldap_expired"):
        check(f"login.{key} is in en.json", key in login)


def main():
    test_pure_helpers()
    test_authenticate()
    test_lockout_protection()
    test_ad_own_count()
    test_ldap3_adapter()
    test_web()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
