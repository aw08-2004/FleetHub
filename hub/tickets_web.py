"""Flask HTTP surface for tickets opened from a PC (roadmap #31) -- a thin layer over
tickets.py, registered as a Blueprint from app.py.

**Two capabilities, both scoped by machine.** `view_tickets` reads the queue and a ticket;
`manage_tickets` replies, moves status, assigns, labels and asks the AI. Neither is VIEW: a
ticket is somebody's own words about their work, often with a picture of their screen, which is
not inventory -- see permissions.VIEW_TICKETS.

**Routes keyed by ticket id check scope INLINE**, through `_gate`, because
`access.require_machine` reads the machine from the URL and a ticket URL names a ticket. The
check is the same two gates in the same order: the capability by decorator, then the ticket's
machine through `access.in_scope`. A ticket that does not exist and a ticket on a machine
outside the caller's scope answer the SAME 404, deliberately -- distinguishing them would let a
scoped operator count another site's tickets by walking the ids, which is require_machine's
enumeration argument applied to a different key. The one route that does name a machine
(`/api/tickets/machines/<machine>`, the machine page's list) uses `require_machine` itself.

**The agent half sits behind agent bearer auth** and files for the CALLING machine only. The
machine comes from the token, never from the body, so PC-3 cannot open a ticket as PC-4. The
reporter's name and SID do come from the body -- the service read them off the tray's pipe
(phase A) -- and are recorded as reported, not as proven.

**A screenshot is served only through the gated detail route**, with `no-store` and `nosniff`,
and with the content type the hub sniffed rather than the one the sender claimed.

**The AI never runs anything.** Triage is a button; its script reaches the library switched
off, and that draft additionally needs `issue_commands` -- the capability correlate_web.py's
draft route asks for, because what lands in the library is code that can run as SYSTEM once
somebody enables it.

**The routes are methods of one class, registered from a table**, rather than closures inside
the factory as most `*_web.py` files have them. Fifteen routes of closures make one function
whose every branch is scored against the factory (SonarCloud measured 99 against a limit of 15
on the first version of this file), and the table puts each route's path, methods and gates on
one line, where a reviewer can check the gates against each other. The gates are the same
decorators, applied at registration, so the assistant's capability labels
(permissions_web._label) still reach every view.

The CSRF note from fleet_web.py applies verbatim: bodies are read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
import traceback

from flask import Blueprint, jsonify, render_template, request, send_file

import auth_helpers
import fleet
import permissions
import permissions_web
import rules
import settings
import tickets

GENERIC_ERROR = "the hub could not complete that request; the details are in its log"
AGENT_AUTH_REQUIRED = "agent authentication required"
NO_SUCH_TICKET = "no such ticket"
NO_MORE_SCREENSHOTS = "this ticket takes no more screenshots"

#: tickets.accept_attachment outcomes -> (status, body). A table rather than a ladder of ifs
#: so the agent's answers can be read side by side. `no_ticket` and `not_yours` share one
#: answer on purpose: an agent must not be able to probe other machines' ticket ids.
_ATTACHMENT_REFUSALS = {
    "no_ticket": (404, {"error": NO_SUCH_TICKET}),
    "not_yours": (404, {"error": NO_SUCH_TICKET}),
    "no_length": (411, {"error": "a Content-Length is required"}),
    "too_large": (413, {"error": "that upload is larger than this hub accepts"}),
    "not_image": (415, {"error": "a screenshot must be a JPEG or a PNG"}),
    "too_many": (409, {"error": NO_MORE_SCREENSHOTS, "code": "too_many"}),
    "window_closed": (409, {"error": NO_MORE_SCREENSHOTS, "code": "window_closed"}),
}


def _log_generic(context):
    print(f"[tickets] {context}:\n{traceback.format_exc()}")
    return GENERIC_ERROR


def _require_json():
    if not request.is_json:
        return jsonify({"error": "expected application/json"}), 415
    return None


def _actor():
    return permissions_web.current_actor()


class _TicketRoutes:
    """Every route's body. Gates are NOT applied here -- see the table in
    create_tickets_blueprint, which is the one place to check them."""

    def __init__(self, db_path, attachment_root, access, facts_for, emit, ai_config, api_key):
        self.db_path = db_path
        self.attachment_root = attachment_root
        self.access = access
        self.facts_for = facts_for
        self.emit = emit
        self.ai_config = ai_config
        self.api_key = api_key

    # ---------------- shared ----------------
    def _gate(self, ticket_id):
        """The ticket's machine if the caller may see it, else None -- see the module
        docstring for why "missing" and "out of scope" are one answer."""
        machine = tickets.machine_of(self.db_path, ticket_id)
        if machine is None or not self.access.in_scope(machine):
            return None
        return machine

    def _managed(self, ticket_id):
        """(machine, None) when the caller may change this ticket, else (None, response)."""
        refusal = _require_json()
        if refusal:
            return None, refusal
        machine = self._gate(ticket_id)
        if machine is None:
            return None, (jsonify({"error": NO_SUCH_TICKET}), 404)
        return machine, None

    def _emit(self, event, ticket_id, machine, urgency=None):
        """Tell open consoles something changed. Ids only: the fleet room includes operators
        without `view_tickets`, and a socket payload must not carry what the route would
        have refused them. The console re-reads through the gated route."""
        if self.emit is None:
            return
        payload = {"id": int(ticket_id), "machine": machine}
        if urgency:
            payload["urgency"] = urgency
        try:
            self.emit(event, payload, machine)
        except Exception:
            _log_generic(f"emitting {event}")

    def _facts(self, machine):
        """The snapshot, or a marker saying it failed. A snapshot that failed to build must
        not lose the person's ticket: it is stored without facts and says so, which is still
        better than an email."""
        if self.facts_for is None:
            return {}
        try:
            return self.facts_for(machine)
        except Exception:
            _log_generic("building a ticket's facts snapshot")
            return {"error": "the hub could not record this machine's facts"}

    # ---------------- agent-facing ----------------
    def agent_submit(self):
        """One ticket from the machine holding this agent's token. Screenshots follow
        separately, one request each, to `/api/agent/tickets/<id>/attachments`."""
        agent_id, machine = auth_helpers.bearer_agent(self.db_path)
        if agent_id is None:
            return jsonify({"error": AGENT_AUTH_REQUIRED}), 401
        error, cleaned = tickets.validate_submission(request.get_json(silent=True))
        if error:
            return jsonify({"error": error}), 400
        policy = settings.get(self.db_path, "tickets.submitters")
        if not tickets.submitter_allowed(policy, cleaned["reporter_kind"]):
            # A code the tray can turn into a sentence for the person, rather than a ticket
            # it believes was sent.
            return jsonify({"error": "this hub only takes tickets from domain accounts",
                            "code": "submitter_not_allowed"}), 403
        if tickets.over_hourly_cap(self.db_path, machine):
            # A sentence the tray shows as it is: the person is told to call, not that the
            # ticket went.
            return jsonify({"error": "This PC has opened a lot of tickets in the last hour. "
                                     "Please call IT instead.",
                            "code": "rate_limited"}), 429
        try:
            ticket_id = tickets.create_ticket(self.db_path, machine, agent_id, cleaned,
                                              self._facts(machine))
        except Exception:
            return jsonify({"error": _log_generic("storing a ticket")}), 500
        self._emit("ticket_new", ticket_id, machine, cleaned["urgency"])
        return jsonify({"id": ticket_id,
                        "max_attachments": tickets.MAX_ATTACHMENTS,
                        "max_attachment_bytes": tickets.MAX_ATTACHMENT_BYTES}), 201

    def agent_attachment(self, ticket_id):
        """One screenshot, raw JPEG or PNG in the body, for a ticket THIS agent opened in the
        last few minutes. 409 means the ticket will not take more, not "try again"."""
        agent_id, _machine = auth_helpers.bearer_agent(self.db_path)
        if agent_id is None:
            return jsonify({"error": AGENT_AUTH_REQUIRED}), 401
        try:
            outcome, attachment_id = tickets.accept_attachment(
                self.db_path, self.attachment_root, ticket_id, agent_id, request.stream,
                request.content_length)
        except (OSError, ValueError):
            # A fixed sentence, never the exception's text (CodeQL, py/stack-trace-exposure).
            return jsonify({"error": "the hub could not store that upload"}), 400
        if outcome in _ATTACHMENT_REFUSALS:
            status, body = _ATTACHMENT_REFUSALS[outcome]
            return jsonify(body), status
        self._emit("ticket_updated", ticket_id, tickets.machine_of(self.db_path, ticket_id))
        return jsonify({"id": attachment_id}), 201

    def agent_mine(self):
        """The tray's "My tickets" (phase D): one person's tickets on this PC, without the
        internal notes. The SID is the service's to supply from the pipe; an empty one gets an
        empty list, never everybody's."""
        agent_id, machine = auth_helpers.bearer_agent(self.db_path)
        if agent_id is None:
            return jsonify({"error": AGENT_AUTH_REQUIRED}), 401
        return jsonify({"tickets": tickets.tickets_for_reporter(
            self.db_path, machine, request.args.get("sid", ""))}), 200

    def agent_reply(self, ticket_id):
        """The person answering their own ticket from the tray (phase D). The SID is the
        service's, read off the pipe like the one on `agent_mine`; a ticket that is not this
        person's on this PC is the same 404 as one that does not exist."""
        agent_id, machine = auth_helpers.bearer_agent(self.db_path)
        if agent_id is None:
            return jsonify({"error": AGENT_AUTH_REQUIRED}), 401
        body = request.get_json(silent=True) or {}
        try:
            error, _message_id = tickets.add_reporter_message(
                self.db_path, machine, ticket_id, body.get("sid", ""), body.get("body", ""))
        except Exception:
            return jsonify({"error": _log_generic("storing a reply from a PC")}), 500
        if error == tickets.NO_SUCH_TICKET:
            return jsonify({"error": NO_SUCH_TICKET}), 404
        if error == tickets.TOO_MANY_REPLIES:
            return jsonify({"error": error, "code": "rate_limited"}), 429
        if error:
            return jsonify({"error": error}), 409 if "closed" in error else 400
        self._emit("ticket_updated", ticket_id, machine)
        return jsonify({"ok": True}), 201

    # ---------------- console pages ----------------
    def tickets_page(self):
        return render_template("tickets.html")

    def ticket_page(self, ticket_id):
        if self._gate(ticket_id) is None:
            return render_template("denied.html",
                                   message="You do not have access to that ticket."), 404
        return render_template("ticket.html", ticket_id=ticket_id)

    # ---------------- console reads ----------------
    def list_all(self):
        """The queue, narrowed to the caller's machines. Filtering is here, not in the
        query, because tickets.py knows nothing about permission groups."""
        status = request.args.get("status") or None
        open_only = request.args.get("open") in ("1", "true", "yes")
        rows = tickets.list_tickets(self.db_path, status=status, open_only=open_only)
        return jsonify({
            "tickets": self.access.filter_rows(rows),
            "statuses": list(tickets.STATUSES),
            "urgencies": list(tickets.URGENCIES),
            "can_manage": self.access.can(permissions.MANAGE_TICKETS),
        }), 200

    def list_machine(self, machine):
        return jsonify({"tickets": tickets.list_tickets(
            self.db_path, machine=str(machine).strip())}), 200

    def get_one(self, ticket_id):
        if self._gate(ticket_id) is None:
            return jsonify({"error": NO_SUCH_TICKET}), 404
        ticket = tickets.get_ticket(self.db_path, ticket_id)
        ticket.pop("agent_id", None)
        can_manage = self.access.can(permissions.MANAGE_TICKETS)
        ticket["can_manage"] = can_manage
        ticket["can_draft"] = can_manage and self.access.can(permissions.ISSUE_COMMANDS)
        ticket["statuses"] = list(tickets.STATUSES)
        return jsonify(ticket), 200

    def get_attachment(self, ticket_id, attachment_id):
        path, content_type = None, None
        if self._gate(ticket_id) is not None:
            path, content_type = tickets.attachment_path(
                self.db_path, self.attachment_root, ticket_id, attachment_id)
        if path is None:
            # One answer for "no such ticket", "not yours to see" and "no such screenshot".
            return jsonify({"error": "no such screenshot"}), 404
        response = send_file(path, mimetype=content_type, max_age=0)
        # A picture of somebody's screen: never kept by a shared browser or a proxy.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # ---------------- console writes ----------------
    def reply(self, ticket_id):
        """A reply, or an internal note when `internal` is true. Only a reply will ever reach
        the person at the PC (phase D)."""
        machine, refusal = self._managed(ticket_id)
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        internal = bool(data.get("internal"))
        error, message_id = tickets.add_message(self.db_path, ticket_id, _actor(),
                                                data.get("body"), internal=internal)
        if error:
            return jsonify({"error": error}), 400
        fleet.audit(self.db_path, actor=_actor(), action="ticket_reply", target=machine,
                    detail={"ticket": ticket_id, "internal": internal})
        self._emit("ticket_updated", ticket_id, machine)
        return jsonify({"id": message_id}), 201

    def change_status(self, ticket_id):
        machine, refusal = self._managed(ticket_id)
        if refusal:
            return refusal
        status = str((request.get_json(silent=True) or {}).get("status") or "")
        error, _ = tickets.set_status(self.db_path, ticket_id, status)
        if error:
            return jsonify({"error": error}), 400
        fleet.audit(self.db_path, actor=_actor(), action="ticket_status", target=machine,
                    detail={"ticket": ticket_id, "status": status})
        self._emit("ticket_updated", ticket_id, machine)
        return jsonify({"status": status}), 200

    def change_assignee(self, ticket_id):
        machine, refusal = self._managed(ticket_id)
        if refusal:
            return refusal
        requested = (request.get_json(silent=True) or {}).get("assignee")
        # "me" is the console's shortcut; the stored value is always an address, so a list
        # filtered by assignee does not have to know who "me" was.
        assignee = _actor() if requested == "me" else (requested or "")
        error = tickets.assign(self.db_path, ticket_id, assignee)
        if error:
            return jsonify({"error": error}), 400
        fleet.audit(self.db_path, actor=_actor(), action="ticket_assign", target=machine,
                    detail={"ticket": ticket_id, "assignee": assignee})
        self._emit("ticket_updated", ticket_id, machine)
        return jsonify({"assignee": assignee}), 200

    def change_label(self, ticket_id):
        machine, refusal = self._managed(ticket_id)
        if refusal:
            return refusal
        label = (request.get_json(silent=True) or {}).get("label")
        error = None
        if request.method == "DELETE":
            tickets.remove_label(self.db_path, ticket_id, label)
        else:
            error = tickets.add_label(self.db_path, ticket_id, label)
        if error:
            return jsonify({"error": error}), 400
        self._emit("ticket_updated", ticket_id, machine)
        return jsonify({"status": "ok"}), 200

    def ask_triage(self, ticket_id):
        """Ask the configured provider to triage this ticket. A button, never a timer."""
        machine, refusal = self._managed(ticket_id)
        if refusal:
            return refusal
        try:
            error, saved = tickets.triage(
                self.db_path, self.ai_config() if self.ai_config else {},
                tickets.get_ticket(self.db_path, ticket_id),
                api_key=(self.api_key() if self.api_key else ""), actor=_actor())
        except Exception:
            return jsonify({"error": _log_generic("triaging a ticket")}), 500
        if error:
            # 502, not 400: the request was fine and a third party was not -- or AI is
            # off, which the console checks before offering the button.
            return jsonify({"error": error}), 502
        fleet.audit(self.db_path, actor=_actor(), action="ticket_triage", target=machine,
                    detail={"ticket": ticket_id, "script": bool(saved.get("script"))})
        self._emit("ticket_updated", ticket_id, machine)
        return jsonify(saved), 200

    def draft_script(self, ticket_id):
        """Put the triage's suggested script into the library, switched off.

        Drafted, never executed: the body goes through `scripts.validate_script` exactly as a
        hand-written one does and lands disabled for a human to read and enable.
        """
        machine, refusal = self._managed(ticket_id)
        if refusal:
            return refusal
        try:
            extra = rules.all_extra_variables(self.db_path)
            error, script = tickets.draft_script(
                self.db_path, ticket_id,
                known_variable=lambda ref: rules.lookup_variable(ref, extra) is not None,
                actor=_actor())
        except Exception:
            return jsonify({"error": _log_generic("drafting a suggested script")}), 500
        if error:
            return jsonify({"error": error}), 400
        # LEVEL_SECURITY, like every other write to the script library -- see
        # correlate_web.draft_bundle_script.
        fleet.audit(self.db_path, actor=_actor(), action="draft_suggested_script",
                    level=fleet.LEVEL_SECURITY, target=script["name"],
                    detail={"machine": machine, "ticket": ticket_id,
                            "source": "ticket_triage", "enabled": script["enabled"],
                            "chars": len(script["body"])})
        return jsonify(script), 200


def create_tickets_blueprint(db_path, attachment_root, login_required, access, *,
                             facts_for=None, emit=None, ai_config=None, api_key=None):
    """Build the tickets Blueprint.

    `attachment_root` is where screenshots live, passed in so tests can use a temp directory.
    `facts_for(machine)` is app.py's snapshot builder -- it lives in the composition root
    because it reads half a dozen other modules' tables, and this module should not import
    them all to freeze a dict. `emit(event, payload, machine)` sends a Socket.IO event to the
    fleet room and the machine's room. `ai_config` and `api_key` are read fresh per request,
    as in correlate_web.py, so the AI off switch takes effect without a restart.
    """
    bp = Blueprint("tickets", __name__)
    routes = _TicketRoutes(db_path, attachment_root, access, facts_for, emit, ai_config,
                           api_key)
    view = access.require(permissions.VIEW_TICKETS)
    manage = access.require(permissions.MANAGE_TICKETS)
    issue = access.require(permissions.ISSUE_COMMANDS)
    agent = ()  # bearer auth inside the method: no session, no capability
    ticket = "/api/tickets/<int:ticket_id>"

    # (rule, endpoint, methods, gates outermost first). The one table to read when checking who
    # may do what: every console row starts with login_required, every id-keyed row also goes
    # through _gate inside its method, and the one machine-named row uses require_machine.
    table = (
        ("/api/agent/tickets", "agent_submit", ["POST"], agent),
        ("/api/agent/tickets/<int:ticket_id>/attachments", "agent_attachment", ["POST"],
         agent),
        ("/api/agent/tickets", "agent_mine", ["GET"], agent),
        ("/api/agent/tickets/<int:ticket_id>/messages", "agent_reply", ["POST"], agent),
        ("/tickets", "tickets_page", ["GET"], (login_required, view)),
        ("/tickets/<int:ticket_id>", "ticket_page", ["GET"], (login_required, view)),
        ("/api/tickets", "list_all", ["GET"], (login_required, view)),
        ("/api/tickets/machines/<machine>", "list_machine", ["GET"],
         (login_required, access.require_machine(permissions.VIEW_TICKETS))),
        (ticket, "get_one", ["GET"], (login_required, view)),
        (ticket + "/attachments/<int:attachment_id>", "get_attachment", ["GET"],
         (login_required, view)),
        (ticket + "/messages", "reply", ["POST"], (login_required, manage)),
        (ticket + "/status", "change_status", ["POST"], (login_required, manage)),
        (ticket + "/assign", "change_assignee", ["POST"], (login_required, manage)),
        (ticket + "/labels", "change_label", ["POST", "DELETE"], (login_required, manage)),
        (ticket + "/triage", "ask_triage", ["POST"], (login_required, manage)),
        # Also issue_commands: what lands in the library is code that can run as SYSTEM once
        # somebody enables it -- correlate_web.py's draft route asks the same.
        (ticket + "/script", "draft_script", ["POST"], (login_required, manage, issue)),
    )
    for rule, endpoint, methods, gates in table:
        handler = getattr(routes, endpoint)
        for gate in reversed(gates):
            handler = gate(handler)
        bp.add_url_rule(rule, endpoint, handler, methods=methods)
    return bp
