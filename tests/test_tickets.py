"""Tickets opened from a PC (roadmap #31), the model half: tickets.py on its own.

The silent failures this file exists to catch:

  * **A snapshot that is not frozen.** The facts on a ticket are the PC as it was when the
    person gave up. Anything that re-reads them later -- or a create path that stores a
    reference instead of a copy -- shows the machine after the reboot that fixed it, and the
    ticket reads as "no problem found" while looking perfectly healthy.
  * **Auto labels claiming more than the hub saw.** An `installed:` label from an unparseable
    install date, or an `update:` label for a patch that FAILED, is a confident wrong answer an
    operator filters on.
  * **An alert that comes back after it was dismissed.** One alert per machine is refreshed by
    every reply and every sweep; if those could insert, a dismissed card would reappear within
    thirty seconds and the alert list would stop being read.
  * **The overdue sweep raising every tick** instead of once per ticket.
  * **A deleted machine leaving screenshots on disk** -- a picture of somebody's screen that no
    retention will ever find, because no row points at it.
  * **Retention closing somebody's problem for them**: an open ticket must never be pruned.
  * **The AI reaching further than a suggestion**: a drafted script must land disabled, and the
    screenshot and the reporter's identity must never be in the prompt.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

import ai
import alerts
import scripts
import tickets

PASS = 0
FAIL = 0

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


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
    alerts.init_alerts_db(path)
    ai.init_ai_db(path)
    scripts.init_scripts_db(path)
    tickets.init_tickets_db(path)
    tickets.init_tickets_db(path)  # idempotent
    return path, tempfile.mkdtemp(prefix="tickets-attach-")


def submission(**over):
    data = {"subject": "Outlook will not start", "body": "Since this morning",
            "urgency": "normal", "reporter_name": "CORP\\ana", "reporter_sid": "S-1-5-21-1",
            "reporter_kind": "domain"}
    data.update(over)
    error, cleaned = tickets.validate_submission(data)
    assert error is None, error
    return cleaned


def open_alerts(db, kind, machine):
    return [a for a in alerts.list_open(db) if a["kind"] == kind and a["machine"] == machine]


def test_validation():
    print("\n-- submission validation and the submitter policy --")
    check("a ticket without a subject is refused",
          tickets.validate_submission({"subject": "  "})[0] is not None)
    check("an unknown urgency is refused",
          tickets.validate_submission({"subject": "x", "urgency": "panic"})[0] is not None)
    check("an unknown reporter kind is refused",
          tickets.validate_submission({"subject": "x", "reporter_kind": "guest"})[0] is not None)
    _, cleaned = tickets.validate_submission({"subject": "x" * 500, "due_at": "nonsense"})
    check("the subject is capped", len(cleaned["subject"]) == tickets.MAX_SUBJECT_CHARS)
    check("an unreadable due time is dropped, not guessed", cleaned["due_at"] is None)
    check("'any' takes a local account",
          tickets.submitter_allowed(tickets.SUBMITTERS_ANY, tickets.REPORTER_LOCAL))
    check("'domain' refuses a local account",
          not tickets.submitter_allowed(tickets.SUBMITTERS_DOMAIN, tickets.REPORTER_LOCAL))
    check("'domain' takes a domain account",
          tickets.submitter_allowed(tickets.SUBMITTERS_DOMAIN, tickets.REPORTER_DOMAIN))


def test_auto_labels():
    print("\n-- auto labels claim only what the hub observed --")
    now = int(time.time())
    today = time.strftime("%Y%m%d", time.localtime(now))
    facts = {
        "os": {"bucket": "windows_11"},
        "patches": [
            {"kb": "KB5034441", "status": "applied", "resolved_at": now - 3600},
            {"kb": "5030000", "status": "applied", "resolved_at": now - 7200},
            {"kb": "KB111", "status": "failed", "resolved_at": now - 3600},
            {"kb": "KB222", "status": "applied", "resolved_at": now - 10 * 86400},
        ],
        "installed": [
            {"name": "Zoom", "install_date": today},
            {"name": "Garbage", "install_date": "14/10/2026"},
            {"name": "Ancient", "install_date": "20200101"},
        ],
        "low_disk": True, "manufacturer": "Dell Inc.", "model": "Latitude 5440",
    }
    labels = tickets.auto_labels(facts, now=now)
    check("the OS bucket becomes a readable label", "Windows 11" in labels)
    check("a recent applied hub patch is labelled", "update:KB5034441" in labels)
    check("a bare KB number gets its prefix", "update:KB5030000" in labels)
    check("a FAILED patch is not labelled as an update", "update:KB111" not in labels)
    check("an old patch is not labelled", "update:KB222" not in labels)
    check("today's install is labelled", "installed:Zoom" in labels)
    check("an unparseable install date is ignored", "installed:Garbage" not in labels)
    check("an old install is not labelled", "installed:Ancient" not in labels)
    check("low disk, vendor and model", {"low-disk", "vendor:Dell Inc.",
                                         "model:Latitude 5440"} <= set(labels))
    check("unknown OS adds nothing", tickets.auto_labels({"os": {"bucket": "unknown"}}) == [])
    check("labels are de-duplicated",
          tickets.auto_labels({"model": "X", "patches": [], "installed": [],
                               "manufacturer": None}) == ["model:X"])


def test_snapshot_frozen():
    print("\n-- the facts snapshot is frozen at submit --")
    db, root = fresh_db()
    facts = {"os": {"bucket": "windows_10"}, "disks": [{"name": "C:", "free_gb": 2.0}]}
    ticket_id = tickets.create_ticket(db, "PC-1", "agent-1", submission(), facts)
    facts["disks"][0]["free_gb"] = 200.0  # the PC was cleaned up afterwards
    ticket = tickets.get_ticket(db, ticket_id)
    check("the stored snapshot is a copy, not a reference",
          ticket["facts"]["disks"][0]["free_gb"] == 2.0)
    tickets.add_message(db, ticket_id, "op@example.com", "Looking")
    tickets.set_status(db, ticket_id, tickets.STATUS_WAITING)
    check("nothing later rewrites it",
          tickets.get_ticket(db, ticket_id)["facts"]["disks"][0]["free_gb"] == 2.0)
    check("auto labels were stored with source auto",
          {"label": "Windows 10", "source": "auto"} in
          [{"label": l["label"], "source": l["source"]} for l in ticket["labels"]])


def test_status_and_thread():
    print("\n-- status, assignment, internal notes --")
    db, root = fresh_db()
    ticket_id = tickets.create_ticket(db, "PC-1", "agent-1", submission(), {})
    check("a new ticket is NEW", tickets.get_ticket(db, ticket_id)["status"] == "new")
    tickets.add_message(db, ticket_id, "op@example.com", "Note to self", internal=True)
    check("an operator touching it moves it to OPEN",
          tickets.get_ticket(db, ticket_id)["status"] == "open")
    tickets.add_message(db, ticket_id, "op@example.com", "Please reboot")
    reporter_view = tickets.tickets_for_reporter(db, "PC-1", "S-1-5-21-1")
    bodies = [m["body"] for m in reporter_view[0]["messages"]]
    check("the reporter sees the reply", "Please reboot" in bodies)
    check("the reporter never sees the internal note", "Note to self" not in bodies)
    check("the reporter is not told the operator's address",
          all("author" not in m for m in reporter_view[0]["messages"]))
    check("another SID on the same PC sees nothing",
          tickets.tickets_for_reporter(db, "PC-1", "S-1-5-21-2") == [])
    check("an empty SID sees nothing, not everything",
          tickets.tickets_for_reporter(db, "PC-1", "") == [])
    check("the same SID on another PC sees nothing",
          tickets.tickets_for_reporter(db, "PC-2", "S-1-5-21-1") == [])
    error, _ = tickets.set_status(db, ticket_id, "bogus")
    check("an unknown status is refused", error is not None)
    tickets.set_status(db, ticket_id, tickets.STATUS_RESOLVED, now=1000)
    check("finishing stamps closed_at", tickets.get_ticket(db, ticket_id)["closed_at"] == 1000)
    tickets.set_status(db, ticket_id, tickets.STATUS_OPEN)
    check("re-opening clears it", tickets.get_ticket(db, ticket_id)["closed_at"] is None)
    check("an empty reply is refused",
          tickets.add_message(db, ticket_id, "op", "   ")[0] is not None)


def test_labels():
    print("\n-- operator labels --")
    db, root = fresh_db()
    ticket_id = tickets.create_ticket(db, "PC-1", "a", submission(),
                                      {"os": {"bucket": "linux"}})
    check("adding a label works", tickets.add_label(db, ticket_id, "printing") is None)
    tickets.add_label(db, ticket_id, "Linux")  # already there as auto
    sources = {l["label"]: l["source"] for l in tickets.get_ticket(db, ticket_id)["labels"]}
    check("re-adding a hub label keeps it the hub's", sources.get("Linux") == "auto")
    tickets.remove_label(db, ticket_id, "Linux")
    check("an operator can remove a hub label",
          "Linux" not in {l["label"] for l in tickets.get_ticket(db, ticket_id)["labels"]})
    check("an empty label is refused", tickets.add_label(db, ticket_id, "  ") is not None)


def test_alerts():
    print("\n-- one ticket alert per machine, and dismissal sticks --")
    db, root = fresh_db()
    tickets.create_ticket(db, "PC-1", "a", submission(urgency="normal"), {})
    check("a normal ticket raises nothing", not open_alerts(db, alerts.KIND_TICKET_NEW, "PC-1"))
    first = tickets.create_ticket(db, "PC-1", "a", submission(urgency="high"), {})
    second = tickets.create_ticket(db, "PC-1", "a", submission(urgency="critical"), {})
    rows = open_alerts(db, alerts.KIND_TICKET_NEW, "PC-1")
    check("two urgent tickets are ONE alert", len(rows) == 1)
    check("...carrying both ids", rows and rows[0]["detail"]["ticket_ids"] == [first, second])
    check("the kind is per-machine", alerts.KIND_TICKET_NEW in alerts.PER_MACHINE_KINDS)

    alerts.dismiss(db, rows[0]["id"])
    tickets.add_message(db, first, "op", "on it")  # touches `first`, `second` still new
    check("a reply does not resurrect a dismissed alert",
          not open_alerts(db, alerts.KIND_TICKET_NEW, "PC-1"))
    third = tickets.create_ticket(db, "PC-1", "a", submission(urgency="high"), {})
    rows = open_alerts(db, alerts.KIND_TICKET_NEW, "PC-1")
    check("a NEW urgent ticket does raise again", len(rows) == 1
          and rows[0]["detail"]["ticket_ids"] == [second, third])
    tickets.assign(db, second, "op@example.com")
    tickets.set_status(db, third, tickets.STATUS_OPEN)
    check("once none is new, the alert resolves",
          not open_alerts(db, alerts.KIND_TICKET_NEW, "PC-1"))


def test_overdue():
    print("\n-- overdue raises once per ticket --")
    db, root = fresh_db()
    now = int(time.time())
    ticket_id = tickets.create_ticket(db, "PC-1", "a", submission(due_at=now + 60), {}, now=now)
    check("not yet due: nothing", tickets.scan_overdue(db, now=now) == 0)
    check("past due: one ticket goes overdue", tickets.scan_overdue(db, now=now + 120) == 1)
    rows = open_alerts(db, alerts.KIND_TICKET_OVERDUE, "PC-1")
    check("...and raises one alert", len(rows) == 1)
    alerts.dismiss(db, rows[0]["id"])
    check("the next sweep does not count it again", tickets.scan_overdue(db, now=now + 150) == 0)
    check("...nor bring the dismissed card back",
          not open_alerts(db, alerts.KIND_TICKET_OVERDUE, "PC-1"))
    tickets.set_status(db, ticket_id, tickets.STATUS_RESOLVED)
    tickets.create_ticket(db, "PC-1", "a", submission(due_at=now - 5), {}, now=now)
    tickets.scan_overdue(db, now=now + 200)
    check("a second overdue ticket raises again",
          len(open_alerts(db, alerts.KIND_TICKET_OVERDUE, "PC-1")) == 1)


def test_attachments():
    print("\n-- screenshots: sniffed, capped, windowed, owned --")
    db, root = fresh_db()
    now = int(time.time())
    ticket_id = tickets.create_ticket(db, "PC-1", "agent-1", submission(), {}, now=now)

    def put(data, agent="agent-1", length="auto", at=None):
        size = len(data) if length == "auto" else length
        return tickets.accept_attachment(db, root, ticket_id, agent, io.BytesIO(data), size,
                                         now=at or now)

    outcome, jpeg_id = put(JPEG)
    check("a JPEG is stored", outcome == "ok")
    path, content_type = tickets.attachment_path(db, root, ticket_id, jpeg_id)
    check("...served as JPEG from the hub's own name", content_type == "image/jpeg"
          and path and os.path.basename(path) == f"{jpeg_id}.jpg")
    check("a PNG is stored", put(PNG)[0] == "ok")
    check("an HTML file claiming to be an image is refused",
          put(b"<html><script>alert(1)</script>")[0] == "not_image")
    check("no Content-Length is refused before reading", put(JPEG, length=None)[0] == "no_length")
    check("a declared size over the cap is refused",
          put(JPEG, length=tickets.MAX_ATTACHMENT_BYTES + 1)[0] == "too_large")
    check("another agent may not add to it", put(JPEG, agent="agent-2")[0] == "not_yours")
    check("after the window it takes nothing",
          put(JPEG, at=now + tickets.ATTACHMENT_WINDOW_SECONDS + 1)[0] == "window_closed")
    while put(JPEG)[0] == "ok":
        pass
    count = len(tickets.get_ticket(db, ticket_id)["attachments"])
    check("the count stops at the cap", count == tickets.MAX_ATTACHMENTS)
    check("a refused upload leaves no temp file behind",
          not [f for f in os.listdir(os.path.join(root, str(ticket_id))) if f.startswith(".")])


def test_lifecycle():
    print("\n-- rename, forget, retention --")
    db, root = fresh_db()
    now = int(time.time())
    open_id = tickets.create_ticket(db, "PC-1", "a", submission(), {}, now=now - 400 * 86400)
    done_id = tickets.create_ticket(db, "PC-1", "a", submission(), {}, now=now - 400 * 86400)
    tickets.accept_attachment(db, root, done_id, "a", io.BytesIO(JPEG), len(JPEG),
                              now=now - 400 * 86400)
    tickets.set_status(db, done_id, tickets.STATUS_CLOSED, now=now - 380 * 86400)
    dropped = tickets.prune(db, root, now - 365 * 86400)
    check("a long-finished ticket is pruned", dropped == 1
          and tickets.get_ticket(db, done_id) is None)
    check("...with its screenshot folder", not os.path.exists(os.path.join(root, str(done_id))))
    check("an open ticket is never pruned, however old",
          tickets.get_ticket(db, open_id) is not None)

    fresh = tickets.create_ticket(db, "PC-1", "a", submission(), {}, now=now)
    tickets.accept_attachment(db, root, fresh, "a", io.BytesIO(JPEG), len(JPEG),
                              now=now - 40 * 86400 + 5)
    shot = tickets.accept_attachment(db, root, fresh, "a", io.BytesIO(JPEG), len(JPEG), now=now)
    check("old screenshots expire on their own window",
          tickets.prune_attachments(db, root, now - 30 * 86400) == 1)
    remaining = tickets.get_ticket(db, fresh)["attachments"]
    check("...newer ones and the ticket stay", [a["id"] for a in remaining] == [shot[1]])

    tickets.rename_machine(db, "PC-1", "PC-1-NEW")
    check("a merge moves tickets", tickets.machine_of(db, fresh) == "PC-1-NEW")
    tickets.forget_machine(db, root, "PC-1-NEW")
    check("forgetting a machine drops its tickets", tickets.machine_of(db, fresh) is None)
    check("...and its screenshots on disk", not os.path.exists(os.path.join(root, str(fresh))))


def test_triage():
    print("\n-- AI triage suggests, never runs --")
    db, root = fresh_db()
    ticket_id = tickets.create_ticket(
        db, "PC-1", "a", submission(reporter_name="CORP\\secret-person"),
        {"os": {"bucket": "windows_11"}})
    tickets.add_label(db, ticket_id, "kept-by-operator")
    ticket = tickets.get_ticket(db, ticket_id)

    error, _ = tickets.triage(db, {"enabled": False}, ticket, actor="op")
    check("AI off: refused", error is not None)
    check("...and audited as disabled",
          ai.list_requests(db)[0]["outcome"] == ai.OUTCOME_DISABLED)

    prompt = tickets.triage_prompt(ticket)
    check("the prompt carries no reporter name or SID",
          "secret-person" not in prompt and "S-1-5-21-1" not in prompt)
    check("...and no screenshot", "attachments" not in json.loads(prompt))

    answer = json.dumps({"summary": "Outlook profile is broken.", "likely_cause": "",
                         "labels": ["Outlook", "outlook", "mail"],
                         "steps": ["Check the OST size"],
                         "script": {"label": "Check OST", "shell": "powershell",
                                    "body": "Get-ChildItem $env:LOCALAPPDATA"}})
    def provider(reply):
        """ai switched on, answering `reply`; undone when the block ends."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(ai, "is_enabled", return_value=True))
        stack.enter_context(mock.patch.object(ai, "complete", return_value=(None, reply)))
        return stack

    with provider("```json\n" + answer + "\n```"):
        error, saved = tickets.triage(db, {"provider": "test", "model": "m"}, ticket, actor="op")
    check("a fenced answer is read", error is None and saved["summary"].startswith("Outlook"))
    labels = {l["label"]: l["source"] for l in tickets.get_ticket(db, ticket_id)["labels"]}
    check("AI labels are lower-cased, de-duplicated and marked ai",
          labels.get("outlook") == "ai" and labels.get("mail") == "ai")
    check("operator labels survive a triage", labels.get("kept-by-operator") == "operator")
    with provider("```json\n" + answer):
        error, _ = tickets.triage(db, {"provider": "test"}, ticket, actor="op")
    check("a fence that never closes is still read", error is None)
    with provider("not json"):
        error, _ = tickets.triage(db, {"provider": "test"}, ticket, actor="op")
    check("an unreadable answer is refused, not stored", error == tickets.UNREADABLE)
    check("...and the earlier triage is kept",
          tickets.get_ticket(db, ticket_id)["triage"]["summary"].startswith("Outlook"))
    with provider(json.dumps({"summary": "x",
                              "script": {"shell": "powershell", "body": "Write {{secret}}"}})):
        _, saved = tickets.triage(db, {"provider": "test"}, ticket, actor="op")
    check("a templated script body is dropped", saved["script"] is None)

    error, _ = tickets.draft_script(db, ticket_id, actor="op")
    check("no script, no draft", error is not None)
    with provider(answer):
        tickets.triage(db, {"provider": "test"}, ticket, actor="op")
    error, script = tickets.draft_script(db, ticket_id, actor="op")
    check("a drafted script lands in the library", error is None
          and script["name"] == tickets.suggested_script_name(ticket_id))
    check("...switched OFF", script and script["enabled"] is False)
    check("...and the triage remembers where it went",
          tickets.get_ticket(db, ticket_id)["triage"]["script_name"] == script["name"])


def test_reporter_marker():
    print("\n-- the phase D marker: moves on what the person can see, and only that --")
    db, root = fresh_db()
    empty = tickets.reporter_marker(db, "PC-1")
    check("a PC with no tickets still has a marker", isinstance(empty, str) and len(empty) == 16)
    ticket_id = tickets.create_ticket(db, "PC-1", "agent-1", submission(), {})
    opened = tickets.reporter_marker(db, "PC-1")
    check("a new ticket moves it", opened != empty)
    tickets.add_message(db, ticket_id, "op", "Note to self", internal=True)
    after_note = tickets.reporter_marker(db, "PC-1")
    # The note moved the ticket NEW -> OPEN, which the person does see; a second note must not.
    tickets.add_message(db, ticket_id, "op", "Another note", internal=True)
    check("an internal note does not move it", tickets.reporter_marker(db, "PC-1") == after_note)
    tickets.add_label(db, ticket_id, "printer")
    tickets.assign(db, ticket_id, "op@example.com")
    check("a label or an assignment does not move it",
          tickets.reporter_marker(db, "PC-1") == after_note)
    tickets.add_message(db, ticket_id, "op", "Please reboot")
    replied = tickets.reporter_marker(db, "PC-1")
    check("a visible reply moves it", replied != after_note)
    tickets.set_status(db, ticket_id, tickets.STATUS_RESOLVED)
    check("a status change moves it", tickets.reporter_marker(db, "PC-1") != replied)
    check("another PC's marker is untouched by all of this",
          tickets.reporter_marker(db, "PC-2") == empty)


def test_reporter_reply():
    print("\n-- the person answering from the tray --")
    db, root = fresh_db()
    ticket_id = tickets.create_ticket(db, "PC-1", "agent-1", submission(), {})
    error, _ = tickets.add_reporter_message(db, "PC-1", ticket_id, "S-1-5-21-1", "Still broken")
    check("the reporter can answer their own ticket", error is None)
    ticket = tickets.get_ticket(db, ticket_id)
    check("it is stored as the reporter's",
          ticket["messages"][-1]["author_kind"] == tickets.AUTHOR_REPORTER)
    check("a reply on a NEW ticket leaves it NEW for the queue", ticket["status"] == "new")
    for name, args in (("another person's SID", ("PC-1", "S-1-5-21-2")),
                       ("an empty SID", ("PC-1", "")),
                       ("the right SID from another PC", ("PC-2", "S-1-5-21-1"))):
        error, _ = tickets.add_reporter_message(db, args[0], ticket_id, args[1], "hi")
        check(f"{name} gets the same answer as a missing ticket",
              error == tickets.NO_SUCH_TICKET)
    check("a missing ticket is NO_SUCH_TICKET",
          tickets.add_reporter_message(db, "PC-1", 9999, "S-1-5-21-1", "hi")[0]
          == tickets.NO_SUCH_TICKET)
    tickets.set_status(db, ticket_id, tickets.STATUS_WAITING)
    tickets.add_reporter_message(db, "PC-1", ticket_id, "S-1-5-21-1", "Tried it, no luck")
    check("answering a WAITING ticket re-opens it",
          tickets.get_ticket(db, ticket_id)["status"] == "open")
    tickets.set_status(db, ticket_id, tickets.STATUS_RESOLVED)
    tickets.add_reporter_message(db, "PC-1", ticket_id, "S-1-5-21-1", "It is back")
    check("answering a RESOLVED ticket re-opens it",
          tickets.get_ticket(db, ticket_id)["status"] == "open")
    tickets.set_status(db, ticket_id, tickets.STATUS_CLOSED)
    error, _ = tickets.add_reporter_message(db, "PC-1", ticket_id, "S-1-5-21-1", "hello?")
    check("a CLOSED ticket refuses, and says so", error == "this ticket is closed")
    check("...and stays closed", tickets.get_ticket(db, ticket_id)["status"] == "closed")


def main():
    test_validation()
    test_auto_labels()
    test_snapshot_frozen()
    test_status_and_thread()
    test_labels()
    test_alerts()
    test_overdue()
    test_attachments()
    test_lifecycle()
    test_triage()
    test_reporter_marker()
    test_reporter_reply()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
