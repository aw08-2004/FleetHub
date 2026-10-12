"""Onboarding codes -- a PC enrols itself with a six-character code (roadmap #29).

Until now a Windows PC joined the fleet one way: a technician ran the installer with the
fleet-wide AGENT_ENROLLMENT_SECRET, and the secret then stayed on that PC, in plaintext in
HKLM, for as long as the agent was installed. Every enrolled machine was therefore holding a
credential that could enrol any other machine -- and the one PC a helpdesk cannot vouch
for, the one an end user is setting up alone, was the one that needed it most.

An onboarding code replaces the secret for that path. An operator mints a code under
Administration -> Onboarding, the person at the PC opens `/onboarding`, and the script it
hands out installs the agent and spends the code. **Spending a code mints THIS machine its
own agent credential** (`fleet.issue_agent_identity`, the same call the shared secret ends
in) and the shared secret never leaves the hub.

**The silent failure this module exists to prevent is a code that admits more than one
machine.** A six-character code is short enough to read over a phone, which is the point and
also the risk, so every rule below bounds what one leaked code can do:

  * **Single use, claimed atomically.** `redeem_code` claims with one conditional UPDATE
    (`redeemed_at IS NULL AND revoked_at IS NULL AND expires_at > now`). Two PCs racing one
    code cannot both win; the loser is told the code is invalid, exactly like a stranger is.
  * **Short-lived.** Every code expires; there is no "never". TTL_CHOICES is the whole menu.
  * **Hash only.** The table stores sha256(code), the same rule as invites and agent tokens.
    A six-character code is brute-forceable from its hash offline, which is acceptable only
    because of the two rules above: a stolen table holds codes that are spent or about to
    expire, and a spent code is worth nothing.
  * **One answer for every refusal.** `check_code` and `redeem_code` say "invalid or expired"
    for an unknown, spent, expired and revoked code alike, so the public routes are not an
    oracle for which codes exist. The operator sees the real state in the list.
  * **Who spent it is recorded.** The machine name and the redeeming IP are on the row and in
    the audit trail, so a code that leaked shows up as a machine nobody expected, and is one
    click from revoked (`revoke_code` also revokes the agent it minted).

**The token never reaches a browser.** This module returns it once, to the caller of
`redeem_code` -- onboarding_web's redeem route, which the install script running on the PC
calls after the agent is installed. The `/onboarding` page only ever calls `check_code`.

Rejected: handing the shared AGENT_ENROLLMENT_SECRET to whoever presents a valid code. It
needed no agent change and no new identity path, and it would have put the fleet credential
into the registry of exactly the machines this feature exists to onboard without one.
Also rejected: a new agent-side `enrollment_code` that the agent spends itself. That moves
the same exchange into the agent, and costs an agent release for what the install script can
already do -- an agent that finds `agent.json` on disk is enrolled (FleetClient.
EnsureEnrolledAsync returns before it ever looks for a secret).

Flask-free on purpose, like invites.py; the HTTP surface is onboarding_web.py.
"""
import hashlib
import re
import secrets
import time

import fleet
import permissions

#: The code alphabet. Upper-case letters and digits **without 0, O, 1, I and L**: a code is
#: read aloud and typed by somebody who is not a technician, and those five are the ones that
#: get mistaken for each other. 31 symbols, six of them: about 887 million codes.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6
CODE_PATTERN = re.compile(r"^[" + ALPHABET + r"]{" + str(CODE_LENGTH) + r"}$")

#: The lifetimes the admin form offers, in seconds. **No "never"** -- see the module docstring.
#: A day is the default: long enough to send a code in the morning and have it used after
#: lunch, short enough that one written on a sticky note is dead by the next morning.
TTL_CHOICES = (3600, 86400, 7 * 86400)
DEFAULT_TTL_SECONDS = 86400

MAX_LABEL_CHARS = 80
#: A Windows NetBIOS name is 15 characters and a DNS label 63; app.is_valid_machine_name
#: allows 128, and the redeem route checks that too. This is the narrower bound the
#: onboarding path accepts, because the name arrives from a script on an unenrolled PC.
MAX_MACHINE_CHARS = 64

STATUS_ACTIVE = "active"
STATUS_REDEEMED = "redeemed"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"

#: What a public caller is told for every refusal. One sentence, on purpose -- see the
#: module docstring.
INVALID_MESSAGE = "That onboarding code is invalid or has expired."


class OnboardingError(Exception):
    """A code that cannot be created or used, with a message meant for a human."""


# ================================
# SCHEMA
# ================================
def init_onboarding_db(db_path):
    with permissions.get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # One row per code. The redemption columns live on the same row rather than in a
        # side table, unlike invites: a code admits exactly one machine, so there is
        # exactly one redemption to record and a second table would only be a join.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS onboarding_codes (
                code_id          TEXT PRIMARY KEY,
                code_hash        TEXT NOT NULL UNIQUE,
                label            TEXT NOT NULL DEFAULT '',
                created_by       TEXT NOT NULL DEFAULT 'unknown',
                created_at       INTEGER NOT NULL,
                expires_at       INTEGER NOT NULL,
                revoked_at       INTEGER,
                redeemed_at      INTEGER,
                redeemed_machine TEXT,
                redeemed_ip      TEXT,
                agent_id         TEXT
            )
        """)


# ================================
# HELPERS
# ================================
def _hash_code(code):
    """Same rule as invites._hash_code and fleet._hash_token, for the reason given there."""
    return hashlib.sha256(str(code).encode("utf-8")).hexdigest()


def normalize_code(code):
    """The code as stored, or None if it cannot be one.

    Upper-cased and stripped of spaces and dashes, so `abc-234` typed from a phone call
    matches. Anything outside ALPHABET after that is refused rather than coerced: the
    excluded look-alikes are excluded precisely so that no guess about them is needed.
    """
    text = re.sub(r"[\s-]", "", str(code or "")).upper()
    return text if CODE_PATTERN.match(text) else None


def _generate_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def _status(row, now):
    """Order matters: a redeemed code reads as redeemed after it also expires, and a revoked
    one reads as revoked whatever else is true, because that is the fact the admin acted on."""
    if row["revoked_at"] is not None:
        return STATUS_REVOKED
    if row["redeemed_at"] is not None:
        return STATUS_REDEEMED
    if row["expires_at"] <= now:
        return STATUS_EXPIRED
    return STATUS_ACTIVE


def _decode(row, now, online=None):
    """One code as the admin API returns it. **Never carries the code** -- it is returned
    once, by create_code, and only its hash exists afterwards."""
    return {
        "code_id": row["code_id"],
        "label": row["label"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "redeemed_at": row["redeemed_at"],
        "redeemed_machine": row["redeemed_machine"],
        "redeemed_ip": row["redeemed_ip"],
        "agent_id": row["agent_id"],
        "status": _status(row, now),
        "online": online,
    }


def _row_by_id(conn, code_id):
    return conn.execute("SELECT * FROM onboarding_codes WHERE code_id = ?",
                        (str(code_id or "").strip(),)).fetchone()


def _live_row(conn, code, now):
    norm = normalize_code(code)
    if norm is None:
        return None
    row = conn.execute("SELECT * FROM onboarding_codes WHERE code_hash = ?",
                       (_hash_code(norm),)).fetchone()
    if row is None or _status(row, now) != STATUS_ACTIVE:
        return None
    return row


def _agent_state(conn, agent_id):
    """(revoked, last_auth) for the agent a code minted, or (None, None) if it is gone."""
    if not agent_id:
        return None, None
    row = conn.execute("SELECT revoked, last_auth FROM agents WHERE agent_id = ?",
                       (agent_id,)).fetchone()
    if row is None:
        return None, None
    return bool(row["revoked"]), row["last_auth"]


# ================================
# WRITES
# ================================
def create_code(db_path, label, ttl_seconds=DEFAULT_TTL_SECONDS, actor="unknown", now=None):
    """Create a code and return (code_id, code). The code is returned exactly once."""
    label = str(label or "").strip()[:MAX_LABEL_CHARS]
    if not label:
        raise OnboardingError("A code needs a label, so it can be told apart in the list.")
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise OnboardingError("Pick how long the code stays valid.")
    if ttl not in TTL_CHOICES:
        raise OnboardingError("Pick how long the code stays valid.")

    now = int(now if now is not None else time.time())
    code_id = secrets.token_hex(16)
    with permissions.get_conn(db_path) as conn:
        # A collision with ANY existing row is retried, not just with a live one: code_hash
        # is UNIQUE, and a spent code's row is the record of which machine it admitted.
        for _ in range(20):
            code = _generate_code()
            exists = conn.execute("SELECT 1 FROM onboarding_codes WHERE code_hash = ?",
                                  (_hash_code(code),)).fetchone()
            if exists is None:
                break
        else:
            raise OnboardingError("Could not find an unused code. Try again.")
        conn.execute(
            "INSERT INTO onboarding_codes(code_id, code_hash, label, created_by, created_at, "
            "                             expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code_id, _hash_code(code), label, actor, now, now + ttl),
        )
    fleet.audit(db_path, actor, "onboarding.create", label,
                {"code_id": code_id, "expires_at": now + ttl}, level=fleet.LEVEL_SECURITY)
    return code_id, code


def redeem_code(db_path, code, machine, ip=None, now=None):
    """Spend `code` for `machine` and return (agent_id, token). Raises OnboardingError.

    The claim comes first and is the only thing that decides who wins -- the agent row is
    minted only by the request whose UPDATE changed a row. If minting then fails, the code
    stays spent: a code that could be claimed twice because the first claim errored halfway
    is exactly the failure this module is written against, and an operator can mint another.
    """
    machine = str(machine or "").strip()
    if not machine or len(machine) > MAX_MACHINE_CHARS:
        raise OnboardingError("The machine name is missing or too long.")
    norm = normalize_code(code)
    if norm is None:
        raise OnboardingError(INVALID_MESSAGE)

    now = int(now if now is not None else time.time())
    with permissions.get_conn(db_path) as conn:
        claimed = conn.execute(
            "UPDATE onboarding_codes SET redeemed_at = ?, redeemed_machine = ?, redeemed_ip = ? "
            "WHERE code_hash = ? AND redeemed_at IS NULL AND revoked_at IS NULL "
            "  AND expires_at > ?",
            (now, machine, str(ip or "")[:64] or None, _hash_code(norm), now),
        ).rowcount
        row = conn.execute("SELECT code_id, label FROM onboarding_codes WHERE code_hash = ?",
                           (_hash_code(norm),)).fetchone()
    if claimed != 1 or row is None:
        raise OnboardingError(INVALID_MESSAGE)

    agent_id, token = fleet.issue_agent_identity(
        db_path, machine, actor=f"onboarding:{row['label']}",
        detail={"code_id": row["code_id"]})
    with permissions.get_conn(db_path) as conn:
        conn.execute("UPDATE onboarding_codes SET agent_id = ? WHERE code_id = ?",
                     (agent_id, row["code_id"]))
    fleet.audit(db_path, f"onboarding:{row['label']}", "onboarding.redeem", machine,
                {"code_id": row["code_id"], "agent_id": agent_id, "ip": ip},
                level=fleet.LEVEL_SECURITY)
    return agent_id, token


def revoke_code(db_path, code_id, actor="unknown", now=None):
    """Kill a code. Raises KeyError if unknown.

    **On a redeemed code this also revokes the agent it minted.** That is what "revoke" has
    to mean on this page: the operator looking at a code redeemed by a machine they did not
    expect wants that machine out, and a button that only marked an already-spent code as
    revoked would look like it worked and change nothing.
    """
    now = int(now if now is not None else time.time())
    with permissions.get_conn(db_path) as conn:
        row = _row_by_id(conn, code_id)
        if row is None:
            raise KeyError(code_id)
        conn.execute("UPDATE onboarding_codes SET revoked_at = ? WHERE code_id = ? "
                     "AND revoked_at IS NULL", (now, row["code_id"]))
    if row["agent_id"]:
        fleet.revoke_agent(db_path, row["agent_id"], actor=actor)
    fleet.audit(db_path, actor, "onboarding.revoke", row["label"],
                {"code_id": row["code_id"], "agent_id": row["agent_id"],
                 "machine": row["redeemed_machine"]}, level=fleet.LEVEL_SECURITY)
    return True


# ================================
# READS
# ================================
def check_code(db_path, code, now=None):
    """{label, expires_at} for a live code, else None. **Consumes nothing** -- a browser
    prefetch or a double-click on the wizard must not spend the code."""
    now = int(now if now is not None else time.time())
    with permissions.get_conn(db_path) as conn:
        row = _live_row(conn, code, now)
    if row is None:
        return None
    return {"label": row["label"], "expires_at": row["expires_at"]}


def status_for_code(db_path, code, now=None):
    """What the wizard's last step shows: {state, machine}, or None for an unknown code.

    `state` is "waiting" (not yet redeemed), "enrolled" (redeemed, the agent has not
    authenticated yet), "online" (its token has authenticated at least once), "expired" or
    "revoked". The machine name is only ever told to somebody holding the code -- the same
    person whose script reported it.
    """
    now = int(now if now is not None else time.time())
    norm = normalize_code(code)
    if norm is None:
        return None
    with permissions.get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM onboarding_codes WHERE code_hash = ?",
                           (_hash_code(norm),)).fetchone()
        if row is None:
            return None
        revoked, last_auth = _agent_state(conn, row["agent_id"])
    status = _status(row, now)
    if status == STATUS_REVOKED or revoked:
        return {"state": "revoked", "machine": None}
    if status == STATUS_EXPIRED:
        return {"state": "expired", "machine": None}
    if status == STATUS_ACTIVE:
        return {"state": "waiting", "machine": None}
    online = last_auth is not None and last_auth >= row["redeemed_at"]
    return {"state": "online" if online else "enrolled", "machine": row["redeemed_machine"]}


def list_codes(db_path, now=None):
    """Every code, newest first, each with its status and whether its agent has checked in."""
    now = int(now if now is not None else time.time())
    out = []
    with permissions.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM onboarding_codes ORDER BY created_at DESC").fetchall()
        for row in rows:
            online = None
            if row["agent_id"]:
                revoked, last_auth = _agent_state(conn, row["agent_id"])
                online = bool(not revoked and last_auth is not None
                              and last_auth >= row["redeemed_at"])
            out.append(_decode(row, now, online))
    return out


def get_code(db_path, code_id, now=None):
    now = int(now if now is not None else time.time())
    with permissions.get_conn(db_path) as conn:
        row = _row_by_id(conn, code_id)
    return _decode(row, now) if row is not None else None
