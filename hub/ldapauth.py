"""Active Directory sign-in over LDAP -- a user name and password checked against a domain
controller, for a domain with no ADFS and no Entra ID in front of it (roadmap #4).

Every other way into this hub is OAuth/OIDC: the identity provider holds the password and
the hub only ever sees a signed assertion. This one is different, and the difference is the
whole risk: **the hub handles the operator's domain password itself.** It never stores it,
never logs it, and forgets it when the request ends, but for the length of one request it
has it. Everything below is shaped by that.

**The order of operations is the security design, not an implementation detail.**

  1. The hub binds as its own read-only **service account** and searches for the user,
     with the login group already in the filter (`memberOf` with the in-chain matching
     rule, so nested groups count) and disabled accounts filtered out.
  2. Only an account that search returned has its password tried, by binding as that
     account's own DN.
  3. Its group DNs are then read back, as the service account, and handed on as the
     `groups` claim, so the permission-group mappings that already work for OIDC
     (`permissions.directory_groups_from_claims`) work here unchanged.

Doing step 1 before step 2 means the login form can never be used to test a password for
an account outside the login group. The alternative -- bind as whoever typed their name, then
check the group -- turns a public sign-in page into a password oracle and an account-lockout
button for every account in the domain, the domain admins included. That was rejected, and
is why a service account and a login group are both required rather than optional.

**The hub must not lock out domain accounts.** AD counts a failed bind towards its lockout
threshold whoever sent it. `LockoutGuard` refuses a sign-in locally, without contacting the
DC, once one account has `USER_FAILURE_LIMIT` failures standing, until `USER_RESET_AFTER`
has passed since the last of them -- AD's own counting rule, so the hub stays under AD's
count rather than racing it. It keys on the account's DN rather than on what was typed --
`jdoe`, `CORP\\jdoe` and `jdoe@corp.local` are one account and get one budget -- and reserves
each attempt before the bind, so concurrent requests cannot all slip through. A separate
per-address limit stops one client walking through user names.

**The cost, accepted on purpose:** anyone who knows a member's user name can keep that member
from signing in THROUGH THIS FORM, by spending the account's four failures once an hour. The
alternative -- a budget per (account, address) -- would let a distributed attacker drive the
account into a domain-wide AD lockout instead, which is the same denial of service made
worse. Break-glass admins keep their other provider, if one is configured.

**Every refusal looks the same from outside**, the per-account block included: the same
page, the same status, and no faster than `REFUSAL_FLOOR_SECONDS`. A distinct "too many
attempts" message for a blocked account would tell an anonymous client that the name is an
enabled member of the login group, since only those ever reach the counter.

**Rejected alternatives, for the record:**

  * *Reusing the `directory.*` sync settings.* Those are `manage_settings`; see the
    authconfig docstring for why the DC that decides who you are cannot be.
  * *Kerberos / Windows integrated sign-in.* Needs a keytab and an SPN for the hub, and does
    not survive the Cloudflare tunnel or nginx in front of most deployments.
  * *NTLM.* Deprecated by Microsoft, and the same proxy problem.
  * *Plain ldap:// by default.* A simple bind sends the password in cleartext; it is refused
    unless explicitly allowed for an isolated lab (`authconfig._validate_ldap`).

Where ADFS or Entra ID exists, OIDC is still the better door -- it brings the directory's
own MFA and never shows the hub a password. docs/access.md says so.

Flask-free, like directory.py. `ldapauth_web.py` is the HTTP surface. **ldap3 is imported
lazily**, inside `ldap3_connector`, for the same reason directory.py does it: the tests below
it run against a fake directory, and a hub with this provider off never imports it.
"""
import re
import threading
import time

# The in-chain matching rule (LDAP_MATCHING_RULE_IN_CHAIN). `memberOf=X` alone matches
# DIRECT membership only, so an operator in a group nested inside the login group would be
# refused with nothing to say why -- and nesting is how most AD shops grant access.
IN_CHAIN = "1.2.840.113556.1.4.1941"
# userAccountControl bit 2, ACCOUNTDISABLE, tested with the bitwise-AND rule.
DISABLED_FILTER = "(!(userAccountControl:1.2.840.113556.1.4.803:=2))"

USER_ATTRIBUTES = ("distinguishedName", "sAMAccountName", "userPrincipalName", "mail",
                   "displayName", "badPwdCount", "badPasswordTime")
# Read from the domain object before every password is tried -- see ad_lockout_headroom.
POLICY_ATTRIBUTES = ("lockoutThreshold", "lockOutObservationWindow")

# Per account, keyed on its DN: at most 4 attempts standing, then none until 70 minutes have
# passed since the last one. This is the BACKSTOP. The primary guard reads AD's own
# lockoutThreshold and the account's badPwdCount before every bind (ad_lockout_headroom), so
# it also covers a threshold below 5 and failures the hub never saw. The backstop alone
# keeps the hub's OWN failures under any policy with a threshold of 5 or more and a reset of
# 60 minutes or less; the extra 10 minutes is margin for the bind's latency and clock drift
# between hub and DC, because a guard set exactly at the boundary it promises is beaten by
# whichever handshake was slower. Found in the second review.
USER_FAILURE_LIMIT = 4
USER_RESET_AFTER = 70 * 60
# AD's badPasswordTime is the DC's clock and this is the hub's. A stored failure is treated
# as expired only once it is this much older than AD's window, never at the boundary.
CLOCK_SKEW_MARGIN = 10 * 60
FILETIME_EPOCH_OFFSET = 11644473600   # seconds from 1601-01-01 to 1970-01-01
# Per client address. Higher, because an office behind one NAT address is many people.
ADDRESS_FAILURE_LIMIT = 20
ADDRESS_FAILURE_WINDOW = 15 * 60
# Every refusal takes at least this long. An account in the login group costs a second
# connection and bind that an unknown name does not, and those tens of milliseconds would
# otherwise tell an anonymous client which names are members. A floor, not a fixed time:
# a DC slower than this still shows through, and the log says so.
REFUSAL_FLOOR_SECONDS = 0.75

MAX_USERNAME_LENGTH = 256
# AD's own limit on a password is 256 characters. Anything longer is not a password, and
# refusing it before the bind keeps a megabyte form field from reaching the DC.
MAX_PASSWORD_LENGTH = 256

# The sub-codes AD puts in the text of a failed bind ("... data 52e, v4563"). Logged so an
# admin reading the hub log can tell a typo from a locked account; NEVER shown to the person
# signing in, because each one confirms the password was right or the account exists.
AD_BIND_REASONS = {
    "525": "user not found",
    "52e": "wrong password",
    "530": "not permitted to sign in at this time",
    "531": "not permitted to sign in from this workstation",
    "532": "password expired",
    "533": "account disabled",
    "701": "account expired",
    "773": "password must be changed at next sign-in",
    "775": "account locked out",
}


class LoginRefused(Exception):
    """This person may not sign in. `reason` is for the hub log only; the person signing in
    is told one generic sentence whatever it says, so a refusal can never be used to learn
    whether an account exists, is in the group, or had the right password."""

    def __init__(self, reason, *, throttled_for=0):
        super().__init__(reason)
        self.reason = reason
        self.throttled_for = throttled_for


class DirectoryUnavailable(Exception):
    """The DC could not be asked: unreachable, TLS refused, or the service account rejected.
    A configuration or network problem, never the user's fault -- reported as such."""


class BindRejected(Exception):
    """The DC answered a bind with invalidCredentials (result 49). Raised by a connector;
    `detail` is AD's sub-code text when there is one."""

    def __init__(self, detail=""):
        super().__init__(detail)
        self.detail = detail


# ================================
# PURE HELPERS
# ================================
def escape_filter_value(value):
    """RFC 4515 escaping for a value placed inside a search filter.

    Without it a user name of `*` matches every account in the login group, and
    `x)(sAMAccountName=*` rewrites the filter -- LDAP injection, here on a page anyone on the
    internet can reach. Done by hand rather than with ldap3's helper so the pure half of this
    module, and its tests, do not need ldap3 at all.
    """
    out = []
    for char in str(value):
        if char in "\\*()\x00":
            out.append("\\%02x" % ord(char))
        else:
            out.append(char)
    return "".join(out)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def parse_username(raw):
    """What was typed, as ("upn", "jdoe@corp.local") or ("sam", "jdoe"), or None.

    Three spellings are accepted because people type all three: the bare logon name, the
    down-level `CORP\\jdoe` (the domain part is dropped -- the user search base already says
    which domain), and the UPN or email address.
    """
    text = str(raw or "").strip()
    if not text or len(text) > MAX_USERNAME_LENGTH or _CONTROL.search(text):
        return None
    if "\\" in text:
        text = text.rsplit("\\", 1)[1].strip()
        if not text:
            return None
    if "@" in text:
        return ("upn", text)
    return ("sam", text)


def user_filter(login, group_dn):
    """The service account's search: the account that was typed, enabled, and in the login
    group (directly or through nesting). One filter, so "not found", "disabled" and "not in
    the group" are the same empty result and cannot be told apart from outside."""
    kind, value = login
    value = escape_filter_value(value)
    if kind == "upn":
        # Email as well as UPN: people know their address, and in many domains the UPN
        # suffix is the internal .local one nobody remembers. Two accounts answering to one
        # address is refused as ambiguous rather than guessed.
        who = f"(|(userPrincipalName={value})(mail={value}))"
    else:
        who = f"(sAMAccountName={value})"
    return ("(&(objectCategory=person)(objectClass=user)" + DISABLED_FILTER + who
            + f"(memberOf:{IN_CHAIN}:={escape_filter_value(group_dn)}))")


def groups_filter(user_dn):
    """Every group the user is in, nested membership included."""
    return f"(&(objectClass=group)(member:{IN_CHAIN}:={escape_filter_value(user_dn)}))"


def domain_root(dn):
    """`DC=corp,DC=local` from any DN under it -- where the group search starts. Groups live
    anywhere in the domain, not under the user search base, so searching only there would
    silently miss the very groups an admin maps."""
    parts = [part.strip() for part in str(dn or "").split(",")]
    return ",".join(part for part in parts if part.lower().startswith("dc="))


def _first(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value or ""


def _text(value):
    value = _first(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value or "").strip()


def bind_reason(detail):
    """Translate AD's sub-code into words for the log, or return the raw text."""
    match = re.search(r"data ([0-9a-f]{3})", str(detail or ""), re.IGNORECASE)
    if match:
        return AD_BIND_REASONS.get(match.group(1).lower(), f"AD code {match.group(1)}")
    return str(detail or "invalid credentials")


def _int(value):
    """An integer out of whatever ldap3 hands back for one: an int, a numeric string, a
    one-element list, bytes, a datetime (a FILETIME it decoded) or a timedelta (an interval
    it decoded). None when it is none of those -- and the callers treat None as "cannot
    tell", never as zero."""
    import datetime
    value = _first(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        if value.year <= 1601:
            return 0
        return int((value.timestamp() + FILETIME_EPOCH_OFFSET) * 10_000_000)
    if isinstance(value, datetime.timedelta):
        return -int(value.total_seconds() * 10_000_000)
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def ad_lockout_headroom(policy, user, now):
    """How many more wrong passwords AD will accept for this account before locking it, by
    AD's own count -- or None when that cannot be told (no lockout policy, or the attributes
    were not readable), in which case LockoutGuard alone decides.

    **Why read AD at all, when LockoutGuard already counts.** The guard counts only the
    hub's own failures. AD's `badPwdCount` also holds the user's typo at a workstation, a
    phone still trying an old password, a VPN client -- so an attacker spending the hub's 4
    attempts on an account already at 1 locks it under a threshold of 5. And a domain on the
    DISA STIG (threshold 3) is locked by the guard's 4th attempt on its own. Both found in
    the second review. Reading the domain's own threshold and the account's own count closes
    both, and survives a hub restart, which the in-memory guard does not.

    `policy` holds the domain object's lockoutThreshold and lockOutObservationWindow (a
    negative interval in 100 ns ticks); `user` the account's badPwdCount and badPasswordTime
    (a FILETIME). `now` is epoch seconds. AD resets the count lazily, so a count whose last
    failure is older than the window is 0 -- but only past CLOCK_SKEW_MARGIN, since the
    stamp is the DC's clock.

    Caveat, stated rather than hidden: badPwdCount is per DC. The read and the bind go to
    the same configured server, which is the same DC unless that name round-robins.
    """
    threshold = _int((policy or {}).get("lockoutThreshold"))
    if threshold is None or threshold <= 0:
        return None
    count = _int((user or {}).get("badPwdCount"))
    if count is None:
        return None
    window = _int((policy or {}).get("lockOutObservationWindow"))
    bad_time = _int((user or {}).get("badPasswordTime"))
    if count and window and bad_time:
        last_bad = bad_time / 10_000_000 - FILETIME_EPOCH_OFFSET
        if now - last_bad > abs(window) / 10_000_000 + CLOCK_SKEW_MARGIN:
            count = 0
    return threshold - count


# ================================
# THROTTLES
# ================================
# Both are in memory. One hub process is the whole deployment (a second replica is refused
# for other reasons), so memory is enough; a restart forgets the counts, which costs an
# attacker a restart they cannot cause.
#
# **Both RESERVE before the bind, in one locked step.** The first version checked, bound,
# and only then recorded the failure. The hub serves requests on a thread pool, so thirty
# concurrent wrong passwords for one account all passed the check before the first one was
# recorded -- thirty binds at the DC, and the account locked domain-wide. Found in review.
_TABLE_CAP = 10000


def _bound(table):
    # Keys are DNs the directory returned and client addresses, so the table is not grown by
    # typing names -- but a cap costs nothing.
    if len(table) > _TABLE_CAP:
        for stale in list(table)[:_TABLE_CAP // 10]:
            table.pop(stale, None)


class LockoutGuard:
    """Per-account failures, counted the way AD counts them, so the hub's own failures stay
    under AD's lockout threshold rather than merely near it. (AD's OWN count -- failures
    from workstations, phones, other services -- is read separately before every bind; see
    `ad_lockout_headroom`. This guard is the backstop when that read is unavailable, and the
    only thing that sees attempts still in flight.)

    AD does not count in a sliding window: it keeps `badPwdCount` and resets it only once a
    full observation window has passed since the LAST bad password. The first version used a
    30-minute sliding window, which let a patient attacker send 4 failures, wait for the
    OLDEST to age out, and send one more each time -- 5 in AD's eyes, a locked account.
    Found in review. So: refuse locally while `limit` attempts stand, until `reset_after`
    has passed since the most recent one, and only then start counting from zero again.

    **Attempts in flight are counted separately from settled failures**, and a success
    clears only the settled ones. The second version dropped the whole entry on a success,
    so a real sign-in that finished while three guesses were still binding reset the hub to
    0 while AD went on to count those three -- and the hub then let four more through.
    Found in the second review. Each attempt is stamped again when its bind returns, too:
    AD stamps `badPasswordTime` when the DC processes the bind, after the TLS handshake, so a
    stamp taken only before the bind runs early by however long that took.
    """

    def __init__(self, limit, reset_after, clock=time.monotonic):
        self.limit = limit
        self.reset_after = reset_after
        self.clock = clock
        self._state = {}          # key -> {"settled", "inflight", "last"}
        self._lock = threading.Lock()

    def _current(self, key, now):
        entry = self._state.get(key)
        if (entry is not None and not entry["inflight"]
                and now - entry["last"] >= self.reset_after):
            del self._state[key]
            return None
        return entry

    def reserve(self, key, ad_headroom=None):
        """Count an attempt BEFORE its bind. Returns 0 if it may go ahead, otherwise the
        seconds until it may. A refused attempt is not counted -- it never reached the DC,
        so it cannot have moved AD's counter.

        `ad_headroom` is how many more failures AD itself will take (ad_lockout_headroom).
        Settled failures are already in AD's count, so only the attempts in flight are set
        against it, and the attempt is refused if, together with them, it could be the one
        that reaches AD's threshold."""
        with self._lock:
            now = self.clock()
            entry = self._current(key, now)
            settled = 0 if entry is None else entry["settled"]
            inflight = 0 if entry is None else entry["inflight"]
            last = now if entry is None else entry["last"]
            if settled + inflight >= self.limit:
                return max(1, int(last + self.reset_after - now))
            if ad_headroom is not None and inflight + 1 >= ad_headroom:
                return max(1, int(self.reset_after))
            if entry is None:
                entry = self._state[key] = {"settled": 0, "inflight": 0, "last": now}
            entry["inflight"] += 1
            entry["last"] = now
            _bound(self._state)
            return 0

    def failed(self, key):
        """The bind for one reserved attempt came back refused -- or did not come back, which
        is counted the same way: whether it reached AD's counter cannot be known."""
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return
            entry["inflight"] = max(0, entry["inflight"] - 1)
            entry["settled"] += 1
            entry["last"] = self.clock()

    def succeeded(self, key):
        """A good password resets the settled failures, as a successful logon resets AD's
        badPwdCount. Attempts still in flight stay counted: they may yet land after it."""
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return
            entry["inflight"] = max(0, entry["inflight"] - 1)
            entry["settled"] = 0
            if not entry["inflight"]:
                del self._state[key]

    def blocked_for(self, key):
        with self._lock:
            now = self.clock()
            entry = self._current(key, now)
            if entry is None or entry["settled"] + entry["inflight"] < self.limit:
                return 0
            return max(1, int(entry["last"] + self.reset_after - now))


class AddressThrottle:
    """Failed sign-ins per client, in a sliding window. Reserved up front like the account
    guard, and RELEASED again for an attempt that was not a refusal -- so an office behind
    one NAT address does not use up its budget by signing in successfully."""

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._stamps = {}
        self._lock = threading.Lock()

    def reserve(self, key):
        """Returns (seconds to wait, None) when refused, else (0, ticket for release)."""
        with self._lock:
            now = self.clock()
            stamps = [t for t in self._stamps.get(key, ()) if t > now - self.window]
            if len(stamps) >= self.limit:
                self._stamps[key] = stamps
                return max(1, int(stamps[0] + self.window - now)), None
            stamps.append(now)
            self._stamps[key] = stamps
            _bound(self._stamps)
            return 0, now

    def release(self, key, ticket):
        with self._lock:
            stamps = self._stamps.get(key)
            if stamps and ticket in stamps:
                stamps.remove(ticket)
                if not stamps:
                    del self._stamps[key]


# ================================
# THE SIGN-IN
# ================================
def authenticate(config, username, password, connector, lockout, clock=time.time):
    """Check a user name and password against AD, and return the claims `_complete_login`
    expects: `email`, `name`, and `groups` (the user's group DNs).

    `connector(dn, password)` binds and returns a session with `search(base, filter,
    attributes)` and `close()`; it raises BindRejected for wrong credentials and
    DirectoryUnavailable for anything else. Injected, so everything here is tested against
    literals with no DC and no ldap3.
    """
    login = parse_username(username)
    if login is None:
        raise LoginRefused("no usable user name was entered")
    # An EMPTY password is not a typo to wave through: an LDAP simple bind with a DN and no
    # password is an "unauthenticated bind", which many directories answer with SUCCESS.
    # Letting one through here would sign in anyone who knows a member's user name.
    if not isinstance(password, str) or password == "":
        raise LoginRefused("no password was entered")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise LoginRefused("the password is longer than Active Directory allows")

    group_dn = config["ldap_login_group_dn"]
    service = connector(config["ldap_bind_dn"], config["ldap_bind_password"],
                        service=True)
    try:
        found = service.search(config["ldap_user_base_dn"], user_filter(login, group_dn),
                               USER_ATTRIBUTES)
        if not found:
            raise LoginRefused(
                f"no enabled account named {login[1]!r} is in the login group under "
                f"{config['ldap_user_base_dn']}")
        if len(found) > 1:
            raise LoginRefused(f"{len(found)} accounts answer to {login[1]!r}; refusing "
                               f"to guess which one was meant")
        entry = found[0]
        attrs = entry.get("attributes") or {}
        user_dn = entry.get("dn") or _text(attrs.get("distinguishedName"))
        if not user_dn:
            raise DirectoryUnavailable("the directory returned an account with no DN")

        # AD's own count for this account, read before its password is tried. None (no
        # lockout policy, or unreadable) leaves the decision to LockoutGuard alone; a read
        # that FAILS outright is treated the same, rather than blocking every sign-in on a
        # policy lookup.
        headroom = None
        try:
            root = domain_root(user_dn)
            policy = service.search(root, "(objectClass=*)", POLICY_ATTRIBUTES, scope="base")
            policy_attrs = (policy[0].get("attributes") or {}) if policy else {}
            headroom = ad_lockout_headroom(policy_attrs, attrs, clock())
        except DirectoryUnavailable as e:
            print(f"[auth] could not read the domain lockout policy under {user_dn}: {e}")

        # Reserved BEFORE the bind, atomically -- see THROTTLES.
        account = user_dn.casefold()
        wait = lockout.reserve(account, ad_headroom=headroom)
        if wait:
            raise LoginRefused(
                f"{user_dn}: not asking the DC, so the hub cannot lock the account out -- "
                + (f"AD's own count leaves {headroom} attempt(s) before lockout"
                   if headroom is not None and headroom <= lockout.limit
                   else f"the hub's own limit is reached for {wait}s"),
                throttled_for=wait)
        try:
            connector(user_dn, password, service=False).close()
        except BindRejected as e:
            lockout.failed(account)
            raise LoginRefused(f"the DC refused the password for {user_dn}: "
                               f"{bind_reason(e.detail)}")
        except DirectoryUnavailable as e:
            # Counted as a failure: whether the bind reached AD's counter cannot be known.
            # And answered as a REFUSAL, not "unavailable": only an enabled member of the
            # login group ever reaches this bind, so a distinct 503 here would tell an
            # anonymous client which names are members. Found in the second review.
            lockout.failed(account)
            raise LoginRefused(f"the bind as {user_dn} did not complete: {e}")
        except BaseException:
            lockout.failed(account)
            raise
        lockout.succeeded(account)

        groups = []
        root = domain_root(group_dn) or domain_root(user_dn)
        for group in service.search(root, groups_filter(user_dn), ("distinguishedName",)):
            dn = group.get("dn") or _text((group.get("attributes") or {})
                                          .get("distinguishedName"))
            if dn and dn not in groups:
                groups.append(dn)
    finally:
        try:
            service.close()
        except Exception:
            pass

    email = _text(attrs.get("mail")) or _text(attrs.get("userPrincipalName"))
    return {
        "email": email,
        "name": _text(attrs.get("displayName")) or _text(attrs.get("sAMAccountName"))
                or email,
        "groups": groups,
    }


# ================================
# THE ONE FUNCTION THAT TALKS TO A NETWORK
# ================================
LDAP_IMPORT_HINT = (
    "Active Directory sign-in needs the 'ldap3' package, which is not installed on this "
    "hub. Install it with:  pip install -r hub/requirements.txt  and restart the hub."
)


def ldap3_connector(config, timeout=10):
    """Build `connector(dn, password, service)` over ldap3 for `config`.

    Thin on purpose, like directory.fetch_computers: everything with a decision in it is
    above and tested. What this cannot be tested against is a real DC -- the TLS handshake,
    AD's bind replies and the paged search are exercised only on first contact.
    """
    try:
        import ldap3
        from ldap3.core.exceptions import LDAPException
    except ImportError:
        raise DirectoryUnavailable(LDAP_IMPORT_HINT)
    import ssl

    url = config["ldap_server"]
    # Checked again HERE, at the last point before a password leaves the hub, and not only in
    # authconfig: this is the function that would send it in cleartext, so it refuses to on
    # its own authority whatever path the config took to get here. ldap3 treats a URL with no
    # scheme as plain LDAP on 389, so anything that is not ldaps:// counts as plain.
    if not url.lower().startswith("ldaps://") and not config.get("ldap_allow_insecure"):
        raise DirectoryUnavailable(
            f"refusing to bind to {url!r}: not ldaps://, and LDAP_ALLOW_INSECURE is not set, "
            f"so every password would cross the network in cleartext")
    try:
        tls = None
        if url.lower().startswith("ldaps://"):
            tls = ldap3.Tls(
                validate=ssl.CERT_REQUIRED if config.get("ldap_tls_verify", True)
                else ssl.CERT_NONE,
                ca_certs_file=config.get("ldap_ca_file") or None,
            )
        server = ldap3.Server(url, get_info=ldap3.NONE, tls=tls, connect_timeout=timeout)
    except LDAPException as e:
        # A CA file deleted since it was saved, or a port ldap3 will not accept. Without this
        # the route had no handler for it and every sign-in was a bare 500.
        raise DirectoryUnavailable(f"cannot set up the connection to {url} "
                                   f"(CA file {config.get('ldap_ca_file') or 'none'}): {e}")

    class _Session:
        def __init__(self, conn):
            self.conn = conn

        def search(self, base, search_filter, attributes, scope="subtree"):
            entries = []
            if scope == "base":
                # One object -- the domain's lockout policy. Not paged, and never SUBTREE:
                # a subtree search from the domain root would walk the whole directory.
                try:
                    self.conn.search(base, search_filter, search_scope=ldap3.BASE,
                                     attributes=list(attributes))
                except LDAPException as e:
                    raise DirectoryUnavailable(f"LDAP read of {base} failed: {e}")
                for entry in self.conn.response or []:
                    if entry.get("type") == "searchResEntry":
                        entries.append({"dn": entry.get("dn"),
                                        "attributes": dict(entry.get("attributes") or {})})
                return entries
            try:
                for entry in self.conn.extend.standard.paged_search(
                        search_base=base, search_filter=search_filter,
                        search_scope=ldap3.SUBTREE, attributes=list(attributes),
                        paged_size=500, generator=True):
                    if entry.get("type") != "searchResEntry":
                        continue
                    entries.append({"dn": entry.get("dn"),
                                    "attributes": dict(entry.get("attributes") or {})})
            except LDAPException as e:
                raise DirectoryUnavailable(f"LDAP search under {base} failed: {e}")
            # With raise_exceptions off a failed search just ends early, and an empty result
            # would then be logged as "no such account" -- sending an admin to look for a
            # missing user when the search base is what is wrong.
            result = self.conn.result or {}
            if result.get("result") not in (None, 0):
                raise DirectoryUnavailable(
                    f"LDAP search under {base} failed: {result.get('description')} "
                    f"{result.get('message') or ''}".strip())
            return entries

        def close(self):
            try:
                self.conn.unbind()
            except Exception:
                pass

    def connector(dn, password, service=False):
        # BYTES, not str. Given a str, ldap3 runs SASLprep on a simple-bind password: NFKC
        # folds the micro sign into Greek mu and a superscript 2 into 2, and a control
        # character raises. AD applies none of that -- it compares the UTF-8 it receives -- so
        # correct passwords were refused (and counted towards lockout), and a control
        # character produced a "directory unavailable" page the per-account guard never saw.
        # ldap3 passes bytes through untouched. Found in review.
        if isinstance(password, str):
            password = password.encode("utf-8")
        conn = ldap3.Connection(server, user=dn, password=password,
                                authentication=ldap3.SIMPLE, read_only=True,
                                auto_referrals=False, raise_exceptions=False,
                                receive_timeout=timeout)
        try:
            ok = conn.bind()
        except LDAPException as e:
            raise DirectoryUnavailable(f"could not reach {url}: {e}")
        if ok:
            return _Session(conn)
        result = conn.result or {}
        try:
            conn.unbind()
        except Exception:
            pass
        if result.get("result") == 49 and not service:
            raise BindRejected(result.get("message") or "")
        # The SERVICE account being refused is a configuration problem, not a wrong
        # password from the person signing in, and must not be reported as one.
        raise DirectoryUnavailable(
            f"bind as {dn} to {url} failed: {result.get('description')} "
            f"({bind_reason(result.get('message'))})")

    return connector
