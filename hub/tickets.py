"""Ticketing from the PC -- roadmap #31, the hub half (phases B, C and C2).

Today a person with a broken PC phones IT, and the first ten minutes of the call are spent
finding out which machine it is, what it runs and what changed. The hub already knows all of
that. This module stores a ticket the person opens FROM the PC (through the agent's tray, phase
A, which ships after this) next to the machine's own facts, labelled before an operator reads it.

**The facts are a SNAPSHOT, frozen at submit time, and that is the point of the table.** A
ticket read the next morning must show the machine as it was when the person gave up -- 2 GB
free, a patch run from an hour before -- not as it is after a reboot fixed it. So `facts_json`
is written once by `create_ticket` and nothing here ever updates it. The live machine page is
one click away for the other question.

**Labels come from three places and say which.** `auto` labels are computed here from the
snapshot by a pure function (`auto_labels`), `ai` labels from the optional triage, `operator`
labels by hand. The source is stored with the label because "the hub decided this ticket is
about KB5034441" and "a model guessed it" deserve different amounts of trust.

**AI triage SUGGESTS and never runs anything** (owner, 2026-10-16). `triage` copies
`correlate.recommend`: one provider call, made only when an operator presses the button, every
outcome in the shared `ai_requests` audit trail. Its script reaches the library through
`draft_script`, switched off, and only a human enabling it there makes it runnable -- the same
path, and the same `scripts.validate_script` check, as a hand-written one. Rejected: letting
the AI run its own fix from an allow-list, which would be a new unattended path to run commands
outside the permission groups that are the whole perimeter.

**Alerts are one per machine per kind, not one per ticket.** The open-alert unique index is
(kind, machine, rule_id, watchdog_id); a ticket per row would need a `ticket_id` column added
to that index, a unique-index migration on the busiest table in the hub, for an operator gain
of none -- "PC-7 has 2 urgent tickets" is the card anybody wants. See `_sync_alert`.

**Screenshots are pictures of somebody's screen.** They are stored outside the database, checked
by their first bytes rather than by the Content-Type the sender claims, capped in size and in
count, accepted only for ten minutes after the ticket was opened and only from the agent that
opened it, and kept for a retention of their own that is shorter than the ticket's.

Kept free of Flask so it can be unit-tested in isolation; tickets_web.py is the HTTP surface.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time

import ai
import alerts
import scripts

URGENCIES = ("low", "normal", "high", "critical")
#: The urgencies that raise a `ticket_new` alert, and so reach a phone through push.scan.
#: Low and normal do not: an alert per "my mouse is a bit slow" is how an alert list stops
#: being read, and the Tickets page already shows them.
ALERTING_URGENCIES = ("high", "critical")

STATUS_NEW = "new"
STATUS_OPEN = "open"
STATUS_WAITING = "waiting"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"
STATUSES = (STATUS_NEW, STATUS_OPEN, STATUS_WAITING, STATUS_RESOLVED, STATUS_CLOSED)
#: A ticket in one of these is finished: it no longer counts as overdue, and the retention
#: window runs from when it got here.
DONE_STATUSES = (STATUS_RESOLVED, STATUS_CLOSED)

LABEL_AUTO = "auto"
LABEL_AI = "ai"
LABEL_OPERATOR = "operator"
LABEL_SOURCES = (LABEL_AUTO, LABEL_AI, LABEL_OPERATOR)

AUTHOR_OPERATOR = "operator"
AUTHOR_REPORTER = "reporter"

REPORTER_DOMAIN = "domain"
REPORTER_LOCAL = "local"
REPORTER_KINDS = (REPORTER_DOMAIN, REPORTER_LOCAL)

#: `tickets.submitters` choices. `domain` refuses a ticket from a local account -- on a shared
#: PC that is the kiosk login nobody can be phoned back on.
SUBMITTERS_ANY = "any"
SUBMITTERS_DOMAIN = "domain"
SUBMITTERS = (SUBMITTERS_ANY, SUBMITTERS_DOMAIN)

MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 8000
MAX_MESSAGE_CHARS = 8000
MAX_LABEL_CHARS = 64
MAX_LABELS = 24
MAX_NAME_CHARS = 256

#: One screenshot, after the tray has downscaled it. 5 MB is a 4K screen as a decent JPEG
#: with room to spare; anything bigger was not downscaled, and the hub sets no
#: MAX_CONTENT_LENGTH, so a route that forgot its own cap would accept anything.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
#: The person picks which screens to send (owner, 2026-10-16), so this is a screen count, not
#: an attachment budget. Six covers every desk in this fleet.
MAX_ATTACHMENTS = 6
#: How long after the ticket was opened its screenshots may still arrive. The tray uploads
#: them straight after the ticket, so ten minutes is a slow network, and closing the window
#: is what stops a stolen agent token being used to keep adding pictures to an old ticket.
ATTACHMENT_WINDOW_SECONDS = 10 * 60

#: Sniffed, not trusted from Content-Type. The served type comes from here too, so a file
#: that starts like a JPEG is served as one whatever the sender said it was.
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
)

#: The window `auto_labels` treats as "just happened". 48 hours covers "it broke after the
#: update last night" told on a Monday about a Saturday patch run, which is the case the
#: label exists for.
RECENT_CHANGE_SECONDS = 48 * 3600

KIND_TICKET_TRIAGE = "ticket_triage"

#: Upper bounds on what a model answer may put into a stored triage.
MAX_SUMMARY_CHARS = 1200
MAX_CAUSE_CHARS = 600
MAX_STEP_CHARS = 300
MAX_STEPS = 6
MAX_SCRIPT_CHARS = 4000
MAX_AI_LABELS = 6


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_tickets_db(db_path):
    """Create the ticket tables. Idempotent -- safe to call on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                machine        TEXT NOT NULL,
                agent_id       TEXT NOT NULL DEFAULT '',  -- who may add screenshots to it
                reporter_name  TEXT NOT NULL DEFAULT '',
                reporter_sid   TEXT NOT NULL DEFAULT '',
                reporter_kind  TEXT NOT NULL DEFAULT 'domain',
                subject        TEXT NOT NULL,
                body           TEXT NOT NULL DEFAULT '',
                urgency        TEXT NOT NULL DEFAULT 'normal',
                due_at         INTEGER,
                status         TEXT NOT NULL DEFAULT 'new',
                assignee       TEXT NOT NULL DEFAULT '',
                facts_json     TEXT NOT NULL DEFAULT '{}', -- frozen at submit, never updated
                overdue_alerted_at INTEGER,                -- see scan_overdue
                created_at     INTEGER NOT NULL,
                updated_at     INTEGER NOT NULL,
                closed_at      INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_machine ON tickets(machine)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   INTEGER NOT NULL,
                author      TEXT NOT NULL DEFAULT '',
                author_kind TEXT NOT NULL DEFAULT 'operator',
                body        TEXT NOT NULL,
                internal    INTEGER NOT NULL DEFAULT 0,  -- never shown to the reporter
                created_at  INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket "
                     "ON ticket_messages(ticket_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_attachments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id    INTEGER NOT NULL,
                filename     TEXT NOT NULL,     -- under <root>/<ticket_id>/, ours, never theirs
                content_type TEXT NOT NULL,
                size         INTEGER NOT NULL,
                sha256       TEXT NOT NULL,
                created_at   INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticket_attachments_ticket "
                     "ON ticket_attachments(ticket_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_labels (
                ticket_id  INTEGER NOT NULL,
                label      TEXT NOT NULL,
                source     TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (ticket_id, label)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_triage (
                ticket_id    INTEGER PRIMARY KEY,
                summary      TEXT NOT NULL,
                likely_cause TEXT NOT NULL DEFAULT '',
                steps_json   TEXT NOT NULL DEFAULT '[]',
                script_json  TEXT,               -- {label, shell, body} or NULL
                script_name  TEXT,               -- set once drafted into the library
                provider     TEXT NOT NULL DEFAULT '',
                model        TEXT NOT NULL DEFAULT '',
                actor        TEXT NOT NULL DEFAULT '',
                created_at   INTEGER NOT NULL
            )
            """
        )


# ================================
# CLEANING
# ================================
def _clean(value, limit):
    return str(value or "").strip()[:limit]


def clean_label(value):
    """One label as stored: trimmed, one line, capped. None for an empty one."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_LABEL_CHARS]
    return text or None


def parse_due(value, now=None):
    """A `due_at` from the tray -- epoch seconds -- or None. A time in the past is kept: the
    person said "by 9" at 9:05, and the ticket is overdue rather than undated."""
    if value in (None, ""):
        return None
    try:
        due = int(float(value))
    except (TypeError, ValueError):
        return None
    return due if due > 0 else None


def validate_submission(payload):
    """Agent JSON -> (error, cleaned). The error is a fixed sentence for the 400."""
    if not isinstance(payload, dict):
        return "expected a ticket", None
    subject = _clean(payload.get("subject"), MAX_SUBJECT_CHARS)
    if not subject:
        return "a ticket needs a subject", None
    urgency = str(payload.get("urgency") or "normal").strip().lower()
    if urgency not in URGENCIES:
        return "urgency must be one of " + ", ".join(URGENCIES), None
    kind = str(payload.get("reporter_kind") or REPORTER_DOMAIN).strip().lower()
    if kind not in REPORTER_KINDS:
        return "reporter_kind must be domain or local", None
    return None, {
        "subject": subject,
        "body": _clean(payload.get("body"), MAX_BODY_CHARS),
        "urgency": urgency,
        "due_at": parse_due(payload.get("due_at")),
        "reporter_name": _clean(payload.get("reporter_name"), MAX_NAME_CHARS),
        "reporter_sid": _clean(payload.get("reporter_sid"), MAX_NAME_CHARS),
        "reporter_kind": kind,
    }


def submitter_allowed(policy, reporter_kind):
    """Whether `tickets.submitters` lets this account open a ticket.

    Enforced HERE, on the hub, and not only in the tray: the tray hides its icon for a local
    account when the policy says domain, but a check that lives only on the PC is a check the
    PC can skip.
    """
    if policy == SUBMITTERS_DOMAIN:
        return reporter_kind == REPORTER_DOMAIN
    return True


# ================================
# PHASE C -- labels the hub can put on by itself
# ================================
#: Display names for normalize_os buckets. The bucket itself is a slug, and a label is read by
#: a person scanning a list, so `os:windows_11` would be the hub talking to itself.
_OS_LABELS = {
    "windows_11": "Windows 11", "windows_10": "Windows 10",
    "windows_legacy": "Windows (legacy)", "windows_server": "Windows Server",
    "linux": "Linux", "android": "Android",
}


def parse_install_date(value):
    """machine_software.install_date -> epoch seconds at local midnight, or None.

    The column is stored raw as the registry had it, which is usually yyyymmdd and sometimes
    anything at all ("", "0", a localised date). Only the first form is believed; guessing at
    the others would label a ticket with a program installed in a year nobody can check.
    """
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        day = datetime.datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return int(day.timestamp())


def auto_labels(facts, now=None):
    """The labels the snapshot alone justifies. Pure: same facts and clock, same labels.

    Only facts the hub OBSERVED are used. **Known gap:** a Windows Update the PC installed by
    itself is recorded nowhere -- the hub knows its own patch runs, not Windows' history -- so
    `update:` labels cover hub-run patches only. Closing that needs the WindowsUpdateClient
    install events, which is a subscription for #16, not collection here.
    """
    now = int(time.time() if now is None else now)
    since = now - RECENT_CHANGE_SECONDS
    facts = facts or {}
    labels = []

    os_info = facts.get("os") or {}
    bucket = os_info.get("bucket")
    if bucket in _OS_LABELS:
        labels.append(_OS_LABELS[bucket])

    for item in facts.get("patches") or []:
        resolved = item.get("resolved_at") or item.get("attempted_at") or 0
        kb = str(item.get("kb") or "").strip()
        if kb and item.get("status") == "applied" and int(resolved) >= since:
            labels.append(f"update:{kb if kb.upper().startswith('KB') else 'KB' + kb}")

    for program in facts.get("installed") or []:
        installed_at = parse_install_date(program.get("install_date"))
        # A day, not an instant: yyyymmdd says "some time on the 14th", so the 14th counts
        # for the whole 48 hours from its midnight -- erring towards labelling, which an
        # operator can remove, over missing the install that broke it.
        if installed_at is not None and installed_at + 86400 >= since and program.get("name"):
            labels.append(f"installed:{program['name']}")

    if facts.get("low_disk"):
        labels.append("low-disk")
    if facts.get("manufacturer"):
        labels.append(f"vendor:{facts['manufacturer']}")
    if facts.get("model"):
        labels.append(f"model:{facts['model']}")

    seen, out = set(), []
    for label in labels:
        cleaned = clean_label(label)
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            out.append(cleaned)
    return out[:MAX_LABELS]


# ================================
# TICKETS
# ================================
def _decode_ticket(row):
    ticket = dict(row)
    try:
        ticket["facts"] = json.loads(ticket.pop("facts_json") or "{}")
    except (TypeError, ValueError):
        ticket["facts"] = {}
    return ticket


def create_ticket(db_path, machine, agent_id, cleaned, facts, now=None):
    """Store one ticket with its frozen snapshot and its auto labels. Returns the ticket id.

    Raises the machine's `ticket_new` alert when the urgency calls for one -- the only place a
    NEW alert row for tickets is ever inserted, so dismissing that card stays dismissed until
    another urgent ticket actually arrives.
    """
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO tickets (machine, agent_id, reporter_name, reporter_sid,
                                    reporter_kind, subject, body, urgency, due_at, status,
                                    facts_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (machine, str(agent_id or ""), cleaned["reporter_name"], cleaned["reporter_sid"],
             cleaned["reporter_kind"], cleaned["subject"], cleaned["body"],
             cleaned["urgency"], cleaned["due_at"], STATUS_NEW,
             json.dumps(facts or {}, sort_keys=True, default=str), now, now))
        ticket_id = cur.lastrowid
        for label in auto_labels(facts, now=now):
            conn.execute("INSERT OR IGNORE INTO ticket_labels (ticket_id, label, source, "
                         "created_at) VALUES (?,?,?,?)", (ticket_id, label, LABEL_AUTO, now))
    if cleaned["urgency"] in ALERTING_URGENCIES:
        sync_alerts(db_path, machine, raise_new=True, now=now)
    return ticket_id


def get_ticket(db_path, ticket_id, *, include_internal=True):
    """One ticket with its labels, messages, attachments and triage, or None."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (int(ticket_id),)).fetchone()
        if row is None:
            return None
        ticket = _decode_ticket(row)
        ticket["labels"] = [dict(r) for r in conn.execute(
            "SELECT label, source, created_at FROM ticket_labels WHERE ticket_id = ? "
            "ORDER BY source, label COLLATE NOCASE", (ticket["id"],))]
        query = "SELECT * FROM ticket_messages WHERE ticket_id = ?"
        if not include_internal:
            query += " AND internal = 0"
        ticket["messages"] = [dict(r) for r in conn.execute(query + " ORDER BY id",
                                                            (ticket["id"],))]
        ticket["attachments"] = [dict(r) for r in conn.execute(
            "SELECT id, content_type, size, created_at FROM ticket_attachments "
            "WHERE ticket_id = ? ORDER BY id", (ticket["id"],))]
        triage = conn.execute("SELECT * FROM ticket_triage WHERE ticket_id = ?",
                              (ticket["id"],)).fetchone()
    ticket["triage"] = _decode_triage(triage) if triage else None
    return ticket


def list_tickets(db_path, *, status=None, machine=None, open_only=False, limit=500):
    """Tickets newest first, each with its labels and counts, without bodies of messages.

    Scope filtering is the caller's (tickets_web.py, through access.filter_rows) -- this
    module knows nothing about permission groups, the division fleet.py and wake.py keep.
    """
    clauses, params = [], []
    if status in STATUSES:
        clauses.append("t.status = ?")
        params.append(status)
    if open_only:
        clauses.append("t.status NOT IN (?, ?)")
        params.extend(DONE_STATUSES)
    if machine:
        clauses.append("t.machine = ?")
        params.append(str(machine))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"""SELECT t.id, t.machine, t.reporter_name, t.reporter_kind, t.subject,
                       t.urgency, t.due_at, t.status, t.assignee, t.created_at,
                       t.updated_at, t.closed_at,
                       (SELECT COUNT(*) FROM ticket_messages m
                         WHERE m.ticket_id = t.id) AS message_count,
                       (SELECT COUNT(*) FROM ticket_attachments a
                         WHERE a.ticket_id = t.id) AS attachment_count
                FROM tickets t {where} ORDER BY t.id DESC LIMIT ?""",
            (*params, max(1, min(2000, int(limit))))).fetchall()
        tickets = [dict(r) for r in rows]
        if tickets:
            ids = [t["id"] for t in tickets]
            marks = ",".join("?" * len(ids))
            by_id = {}
            for r in conn.execute(f"SELECT ticket_id, label, source FROM ticket_labels "
                                  f"WHERE ticket_id IN ({marks}) ORDER BY source, label",
                                  ids):
                by_id.setdefault(r["ticket_id"], []).append(
                    {"label": r["label"], "source": r["source"]})
            for t in tickets:
                t["labels"] = by_id.get(t["id"], [])
    return tickets


def tickets_for_reporter(db_path, machine, reporter_sid, limit=50):
    """What the tray's "My tickets" shows (phase D): this person's tickets on this PC, with
    the operator's replies and NONE of the internal notes.

    Filtered by SID and machine together, so one user on a shared PC never sees another's
    tickets, and an empty SID matches nothing rather than every ticket filed without one.
    """
    sid = str(reporter_sid or "").strip()
    if not sid:
        return []
    with get_conn(db_path) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM tickets WHERE machine = ? AND reporter_sid = ? "
            "ORDER BY id DESC LIMIT ?", (machine, sid, max(1, int(limit))))]
    out = []
    for ticket_id in ids:
        ticket = get_ticket(db_path, ticket_id, include_internal=False)
        if ticket is None:
            continue
        out.append({
            "id": ticket["id"], "subject": ticket["subject"], "urgency": ticket["urgency"],
            "status": ticket["status"], "created_at": ticket["created_at"],
            "updated_at": ticket["updated_at"],
            # Operator names are left out: the person at the PC is told THAT IT answered,
            # and the helpdesk decides elsewhere whether its staff are named to users.
            "messages": [{"body": m["body"], "author_kind": m["author_kind"],
                          "created_at": m["created_at"]} for m in ticket["messages"]],
        })
    return out


def machine_of(db_path, ticket_id):
    """The machine a ticket belongs to, or None -- what the console's scope check needs
    before it may read anything else about the ticket."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT machine FROM tickets WHERE id = ?",
                           (int(ticket_id),)).fetchone()
    return row["machine"] if row else None


def _touch(conn, ticket_id, now, *, from_new=True):
    """Bump updated_at, and move a NEW ticket to OPEN: an operator having done anything at
    all is what "new" stops meaning."""
    conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
    if from_new:
        conn.execute("UPDATE tickets SET status = ? WHERE id = ? AND status = ?",
                     (STATUS_OPEN, ticket_id, STATUS_NEW))


def add_message(db_path, ticket_id, author, body, *, internal=False,
                author_kind=AUTHOR_OPERATOR, now=None):
    """Append one message. Returns (error, message_id)."""
    text = _clean(body, MAX_MESSAGE_CHARS)
    if not text:
        return "a reply needs some text", None
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        machine = conn.execute("SELECT machine FROM tickets WHERE id = ?",
                               (int(ticket_id),)).fetchone()
        if machine is None:
            return "no such ticket", None
        cur = conn.execute(
            "INSERT INTO ticket_messages (ticket_id, author, author_kind, body, internal, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (int(ticket_id), str(author or ""), author_kind, text, 1 if internal else 0, now))
        _touch(conn, int(ticket_id), now, from_new=author_kind == AUTHOR_OPERATOR)
    sync_alerts(db_path, machine["machine"], now=now)
    return None, cur.lastrowid


def set_status(db_path, ticket_id, status, now=None):
    """Move a ticket to `status`. Returns (error, machine)."""
    if status not in STATUSES:
        return "status must be one of " + ", ".join(STATUSES), None
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT machine FROM tickets WHERE id = ?",
                           (int(ticket_id),)).fetchone()
        if row is None:
            return "no such ticket", None
        conn.execute(
            "UPDATE tickets SET status = ?, updated_at = ?, "
            "closed_at = CASE WHEN ? IN (?, ?) THEN COALESCE(closed_at, ?) ELSE NULL END "
            "WHERE id = ?",
            (status, now, status, *DONE_STATUSES, now, int(ticket_id)))
    sync_alerts(db_path, row["machine"], now=now)
    return None, row["machine"]


def assign(db_path, ticket_id, assignee, now=None):
    """Set (or clear, with "") who is handling a ticket. Returns an error or None."""
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        cur = conn.execute("UPDATE tickets SET assignee = ? WHERE id = ?",
                           (_clean(assignee, MAX_NAME_CHARS), int(ticket_id)))
        if cur.rowcount == 0:
            return "no such ticket"
        _touch(conn, int(ticket_id), now)
        machine = conn.execute("SELECT machine FROM tickets WHERE id = ?",
                               (int(ticket_id),)).fetchone()["machine"]
    sync_alerts(db_path, machine, now=now)
    return None


def add_label(db_path, ticket_id, label, source=LABEL_OPERATOR, now=None):
    """Add one label. An existing label keeps its ORIGINAL source: an operator re-typing a
    label the hub already put on does not make it any less the hub's finding."""
    cleaned = clean_label(label)
    if cleaned is None:
        return "a label needs some text"
    if source not in LABEL_SOURCES:
        return "unknown label source"
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        if conn.execute("SELECT 1 FROM tickets WHERE id = ?", (int(ticket_id),)).fetchone() \
                is None:
            return "no such ticket"
        count = conn.execute("SELECT COUNT(*) AS c FROM ticket_labels WHERE ticket_id = ?",
                             (int(ticket_id),)).fetchone()["c"]
        if count >= MAX_LABELS:
            return "this ticket already has as many labels as it can hold"
        conn.execute("INSERT OR IGNORE INTO ticket_labels (ticket_id, label, source, "
                     "created_at) VALUES (?,?,?,?)", (int(ticket_id), cleaned, source, now))
    return None


def remove_label(db_path, ticket_id, label):
    """Remove one label, whatever put it there -- an operator who disagrees with the hub or
    the model has the last word on their own ticket."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM ticket_labels WHERE ticket_id = ? AND label = ?",
                     (int(ticket_id), str(label or "").strip()))


# ================================
# SCREENSHOTS
# ================================
def sniff_image(head):
    """(content_type, extension) for a JPEG or PNG by its first bytes, else (None, None)."""
    for signature, content_type, ext in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return content_type, ext
    return None, None


def _ticket_dir(root, ticket_id):
    return os.path.join(root, str(int(ticket_id)))


def accept_attachment(db_path, root, ticket_id, agent_id, stream, length, now=None):
    """Store one screenshot for a ticket. Returns (outcome, attachment_id).

    Outcomes, each mapped to a status by tickets_web.py: `ok`, `no_ticket`, `not_yours`,
    `window_closed`, `too_many`, `no_length`, `too_large`, `not_image`.

    **The length is checked before a byte is read, and again while reading.** The declared
    Content-Length is what refuses a 2 GB upload without taking it; the running count is what
    stops a sender that declared 1 KB and kept going.
    """
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT agent_id, created_at FROM tickets WHERE id = ?",
                           (int(ticket_id),)).fetchone()
        if row is None:
            return "no_ticket", None
        if row["agent_id"] != str(agent_id or ""):
            return "not_yours", None
        if now - int(row["created_at"]) > ATTACHMENT_WINDOW_SECONDS:
            return "window_closed", None
        count = conn.execute("SELECT COUNT(*) AS c FROM ticket_attachments "
                             "WHERE ticket_id = ?", (int(ticket_id),)).fetchone()["c"]
        if count >= MAX_ATTACHMENTS:
            return "too_many", None
    if length is None:
        return "no_length", None
    if int(length) > MAX_ATTACHMENT_BYTES or int(length) <= 0:
        return "too_large", None

    folder = _ticket_dir(root, ticket_id)
    os.makedirs(folder, exist_ok=True)
    temp_path = os.path.join(folder, f".upload-{os.getpid()}-{time.monotonic_ns()}")
    digest, size, head = hashlib.sha256(), 0, b""
    try:
        with open(temp_path, "wb") as out:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    return "too_large", None
                if len(head) < 16:
                    head += chunk[:16 - len(head)]
                digest.update(chunk)
                out.write(chunk)
        content_type, ext = sniff_image(head)
        if content_type is None:
            return "not_image", None
        with get_conn(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO ticket_attachments (ticket_id, filename, content_type, size, "
                "sha256, created_at) VALUES (?,?,?,?,?,?)",
                (int(ticket_id), "", content_type, size, digest.hexdigest(), now))
            attachment_id = cur.lastrowid
            filename = f"{attachment_id}.{ext}"
            conn.execute("UPDATE ticket_attachments SET filename = ? WHERE id = ?",
                         (filename, attachment_id))
        os.replace(temp_path, os.path.join(folder, filename))
        return "ok", attachment_id
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def attachment_path(db_path, root, ticket_id, attachment_id):
    """(path, content_type) of one stored screenshot, or (None, None)."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT filename, content_type FROM ticket_attachments "
                           "WHERE id = ? AND ticket_id = ?",
                           (int(attachment_id), int(ticket_id))).fetchone()
    if row is None or not row["filename"]:
        return None, None
    path = os.path.join(_ticket_dir(root, ticket_id), row["filename"])
    return (path, row["content_type"]) if os.path.isfile(path) else (None, None)


def _delete_attachment_files(root, ticket_ids):
    for ticket_id in ticket_ids:
        shutil.rmtree(_ticket_dir(root, ticket_id), ignore_errors=True)


# ================================
# ALERTS
# ================================
def sync_alerts(db_path, machine, *, raise_new=False, now=None):
    """Bring this machine's `ticket_new` alert in line with its tickets.

    Urgent tickets still in NEW keep the alert open with the count; none left resolves it.
    A NEW row is inserted only when `raise_new` -- i.e. when an urgent ticket has just
    arrived -- so an operator who dismissed the card is not shown it again by somebody
    else's reply to a different ticket.
    """
    marks = ",".join("?" * len(ALERTING_URGENCIES))
    with get_conn(db_path) as conn:
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM tickets WHERE machine = ? AND status = ? "
            f"AND urgency IN ({marks}) ORDER BY id", (machine, STATUS_NEW, *ALERTING_URGENCIES))]
    alerts.sync_ticket_alert(db_path, alerts.KIND_TICKET_NEW, machine, ids,
                             raise_new=raise_new, now=now)
    _sync_overdue_alert(db_path, machine, raise_new=False, now=now)


def _overdue_ids(conn, machine, now):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM tickets WHERE machine = ? AND due_at IS NOT NULL AND due_at <= ? "
        "AND status NOT IN (?, ?) ORDER BY id", (machine, now, *DONE_STATUSES))]


def _sync_overdue_alert(db_path, machine, *, raise_new, now=None):
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        ids = _overdue_ids(conn, machine, now)
    alerts.sync_ticket_alert(db_path, alerts.KIND_TICKET_OVERDUE, machine, ids,
                             raise_new=raise_new, now=now)


def scan_overdue(db_path, now=None):
    """Raise `ticket_overdue` for tickets whose "needed by" has just passed. Returns how many
    tickets newly went overdue.

    Runs on the hub's 30-second sweep. `overdue_alerted_at` is what makes it raise once per
    ticket rather than once per tick: a dismissed overdue card would otherwise be back within
    thirty seconds, which is the fastest way to teach somebody to ignore the alert list.
    """
    now = int(time.time() if now is None else now)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, machine FROM tickets WHERE due_at IS NOT NULL AND due_at <= ? "
            "AND overdue_alerted_at IS NULL AND status NOT IN (?, ?)",
            (now, *DONE_STATUSES)).fetchall()
        if rows:
            conn.executemany("UPDATE tickets SET overdue_alerted_at = ? WHERE id = ?",
                             [(now, r["id"]) for r in rows])
    for machine in sorted({r["machine"] for r in rows}):
        _sync_overdue_alert(db_path, machine, raise_new=True, now=now)
    return len(rows)


# ================================
# LIFECYCLE
# ================================
def _ticket_ids(conn, machine):
    return [r["id"] for r in conn.execute("SELECT id FROM tickets WHERE machine = ?",
                                          (machine,))]


def _delete_tickets(conn, ids):
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    for table in ("ticket_messages", "ticket_attachments", "ticket_labels", "ticket_triage"):
        conn.execute(f"DELETE FROM {table} WHERE ticket_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM tickets WHERE id IN ({marks})", ids)


def forget_machine(db_path, root, machine):
    """Drop a deleted machine's tickets, and the screenshots on disk with them.

    The files matter more than the rows: a screenshot left in a folder no row points at is a
    picture of somebody's screen that no retention will ever delete.
    """
    with get_conn(db_path) as conn:
        ids = _ticket_ids(conn, machine)
        _delete_tickets(conn, ids)
    _delete_attachment_files(root, ids)


def rename_machine(db_path, old_machine, new_machine):
    """Move tickets during a duplicate-serial merge. History always moves: "did anybody at
    this PC complain on Tuesday" is the same question after a merge as before it. The
    snapshots keep the old name inside them, because that is what the PC was called then."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE tickets SET machine = ? WHERE machine = ?",
                     (new_machine, old_machine))


def prune(db_path, root, cutoff):
    """Delete tickets finished before `cutoff`. Open tickets are never pruned, however old --
    a retention setting must not be able to close somebody's problem for them."""
    with get_conn(db_path) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM tickets WHERE status IN (?, ?) AND closed_at IS NOT NULL "
            "AND closed_at < ?", (*DONE_STATUSES, int(cutoff)))]
        _delete_tickets(conn, ids)
    _delete_attachment_files(root, ids)
    return len(ids)


def prune_attachments(db_path, root, cutoff):
    """Delete screenshots stored before `cutoff`, whatever state their ticket is in.

    The shorter of the two windows on purpose: the text of a ticket is the helpdesk's
    record, the picture of somebody's desktop is not, and an open ticket a month old has
    long since stopped needing what the screen looked like that morning.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT id, ticket_id, filename FROM ticket_attachments "
                            "WHERE created_at < ?", (int(cutoff),)).fetchall()
        if rows:
            conn.executemany("DELETE FROM ticket_attachments WHERE id = ?",
                             [(r["id"],) for r in rows])
    for r in rows:
        if r["filename"]:
            try:
                os.remove(os.path.join(_ticket_dir(root, r["ticket_id"]), r["filename"]))
            except OSError:
                pass
    return len(rows)


# ================================
# PHASE C2 -- AI triage, suggest-only
# ================================
_SYSTEM_PROMPT = """You are helping a Windows helpdesk technician read one ticket that a \
person opened from their own PC.

You are given the person's own words and FACTS the console recorded about the PC at the \
moment the ticket was opened. The facts are correct; the person's words are their description \
and may be wrong about causes.

Rules you must follow:
- Never state a number, a drive letter, an update name or a program name that is not in the \
ticket or the facts.
- If the facts point at a likely cause (a low disk, an update or program installed just \
before), say so and say which fact. If nothing does, say so and do not guess.
- Labels are short tags a technician would filter by, such as "printing", "outlook", \
"network", "performance". At most six, lower case.
- The steps are for a technician who can reach the machine remotely and run PowerShell as \
SYSTEM. Prefer checking before changing.
- The optional script must be safe to READ and to review. It may inspect and report. It must \
not delete user data, disable security software, or reboot the machine.
- Write British English, plain and short. No marketing, no apologies, no headings.

Answer with one JSON object and nothing else:
{"summary": "two or three sentences", "likely_cause": "one sentence or empty", \
"labels": ["..."], "steps": ["...", "..."], \
"script": {"label": "short title", "shell": "powershell", "body": "..."}}

`script` is optional -- use null when no script would help. Never include `{{` in the body."""

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
UNREADABLE = "the AI provider did not answer with a triage this hub could read"


def triage_prompt(ticket):
    """The user half of the prompt: the ticket's text and its snapshot, as JSON.

    **The screenshot is not sent.** It is a picture of somebody's screen -- their mail,
    their chat -- and sending it to a provider is a decision this feature does not get to
    make on their behalf. The reporter's name and SID are not sent either; the model has no
    use for who it was.
    """
    return json.dumps({
        "subject": ticket.get("subject") or "",
        "description": ticket.get("body") or "",
        "urgency": ticket.get("urgency") or "",
        "labels": [l["label"] for l in ticket.get("labels") or [] if l["source"] == LABEL_AUTO],
        "facts": ticket.get("facts") or {},
    }, sort_keys=True, default=str)


def parse_triage(text):
    """Model text -> (error, triage). Nothing here trusts the payload's shape -- the same
    discipline, and the same reasons, as correlate.parse_recommendation."""
    body = str(text or "").strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        payload = json.loads(body)
    except ValueError:
        return UNREADABLE, None
    if not isinstance(payload, dict):
        return UNREADABLE, None
    summary = _clean(payload.get("summary"), MAX_SUMMARY_CHARS)
    if not summary:
        return UNREADABLE, None
    steps = [s for s in (_clean(x, MAX_STEP_CHARS) for x in payload.get("steps") or []) if s]
    labels = []
    for raw in payload.get("labels") or []:
        cleaned = clean_label(raw)
        if cleaned and cleaned.lower() not in {l.lower() for l in labels}:
            labels.append(cleaned.lower())
    script = None
    raw_script = payload.get("script")
    if isinstance(raw_script, dict):
        script_body = str(raw_script.get("body") or "").strip()
        shell = str(raw_script.get("shell") or scripts.DEFAULT_SHELL).strip().lower()
        # Refused outright rather than escaped -- see correlate.parse_recommendation: a
        # template binds a value at run time that nobody reviewed at read time.
        if script_body and "{{" not in script_body and shell in scripts.SHELLS:
            script = {"label": _clean(raw_script.get("label"), scripts.MAX_LABEL_CHARS),
                      "shell": shell, "body": script_body[:MAX_SCRIPT_CHARS]}
    return None, {"summary": summary,
                  "likely_cause": _clean(payload.get("likely_cause"), MAX_CAUSE_CHARS),
                  "steps": steps[:MAX_STEPS], "labels": labels[:MAX_AI_LABELS],
                  "script": script}


def _decode_triage(row):
    out = dict(row)
    try:
        out["steps"] = json.loads(out.pop("steps_json") or "[]")
    except (TypeError, ValueError):
        out["steps"] = []
    raw = out.pop("script_json", None)
    try:
        out["script"] = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        out["script"] = None
    return out


def triage(db_path, config, ticket, *, api_key="", actor="", now=None):
    """Ask the configured provider to triage one ticket. Returns (error, saved).

    Only ever reached from an operator pressing a button -- nothing calls this when a ticket
    arrives. A feature that quietly bills a provider per ticket is one somebody finds out
    about from an invoice; correlate.recommend draws the same line for the same reason.
    Every outcome goes to the shared `ai_requests` trail.
    """
    now = int(time.time() if now is None else now)
    stamp = {"provider": str((config or {}).get("provider") or ""),
             "model": str((config or {}).get("model") or "")}
    prompt = triage_prompt(ticket)

    def _record(outcome, error=""):
        ai.record_request(db_path, actor=actor, kind=KIND_TICKET_TRIAGE,
                          provider=stamp["provider"], model=stamp["model"],
                          machine=str(ticket.get("machine") or ""),
                          prompt_chars=len(prompt), outcome=outcome, error=error, now=now)

    if not ai.is_enabled(config):
        message = "AI assistance is switched off"
        _record(ai.OUTCOME_DISABLED, message)
        return message, None
    error, text = ai.complete(config, [{"role": "system", "content": _SYSTEM_PROMPT},
                                       {"role": "user", "content": prompt}], api_key=api_key)
    if error:
        _record(ai.OUTCOME_PROVIDER_ERROR, error)
        return error, None
    error, result = parse_triage(text)
    if error:
        _record(ai.OUTCOME_REFUSED, error)
        return error, None
    _record(ai.OUTCOME_OK)

    ticket_id = int(ticket["id"])
    with get_conn(db_path) as conn:
        # A new triage replaces the old one, its labels with it. Operator and auto labels
        # stay: asking again must not undo what a person decided.
        conn.execute("DELETE FROM ticket_labels WHERE ticket_id = ? AND source = ?",
                     (ticket_id, LABEL_AI))
        conn.execute(
            """INSERT OR REPLACE INTO ticket_triage (ticket_id, summary, likely_cause,
                   steps_json, script_json, script_name, provider, model, actor, created_at)
               VALUES (?,?,?,?,?,NULL,?,?,?,?)""",
            (ticket_id, result["summary"], result["likely_cause"],
             json.dumps(result["steps"]),
             json.dumps(result["script"]) if result["script"] else None,
             stamp["provider"], stamp["model"], str(actor or ""), now))
        for label in result["labels"]:
            conn.execute("INSERT OR IGNORE INTO ticket_labels (ticket_id, label, source, "
                         "created_at) VALUES (?,?,?,?)", (ticket_id, label, LABEL_AI, now))
        row = conn.execute("SELECT * FROM ticket_triage WHERE ticket_id = ?",
                           (ticket_id,)).fetchone()
    return None, _decode_triage(row)


def suggested_script_name(ticket_id):
    """A library name inside scripts' name grammar, derived from the ticket so asking twice
    replaces the draft instead of adding `ticket_fix_2`."""
    return f"ticket_{int(ticket_id)}_suggested"


def draft_script(db_path, ticket_id, *, known_variable=None, actor="", now=None):
    """Put a triage's suggested script into the library, SWITCHED OFF. Returns (error, script).

    Drafted, never executed -- exactly correlate.draft_script: `enabled=False`, through the
    same `scripts.save_script` validation a hand-written script gets. Running it is an
    operator enabling it and running it on the Scripts path, behind that path's own gate.
    """
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM ticket_triage WHERE ticket_id = ?",
                           (int(ticket_id),)).fetchone()
        subject = conn.execute("SELECT subject, machine FROM tickets WHERE id = ?",
                               (int(ticket_id),)).fetchone()
    if row is None or subject is None:
        return "there is no triage for this ticket", None
    saved = _decode_triage(row)
    script = saved.get("script")
    if not script:
        return "that triage did not include a script", None
    label = script.get("label") or f"Suggested fix for ticket {int(ticket_id)}"
    description = f"Ticket #{int(ticket_id)} on {subject['machine']}: {subject['subject']}"
    error, stored = scripts.save_script(
        db_path, suggested_script_name(ticket_id), label,
        description[:scripts.MAX_DESCRIPTION_CHARS], script.get("shell"), script.get("body"),
        [], scripts.DEFAULT_TIMEOUT_SECONDS,
        enabled=False,  # a human enables it after reading it -- that is the whole mitigation
        known_variable=known_variable, actor=actor, now=now)
    if error:
        return error, None
    with get_conn(db_path) as conn:
        conn.execute("UPDATE ticket_triage SET script_name = ? WHERE ticket_id = ?",
                     (stored["name"], int(ticket_id)))
    return None, stored
