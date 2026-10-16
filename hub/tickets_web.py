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


def _log_generic(context):
    print(f"[tickets] {context}:\n{traceback.format_exc()}")
    return GENERIC_ERROR


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
    can_view = access.require(permissions.VIEW_TICKETS)
    can_manage = access.require(permissions.MANAGE_TICKETS)
    can_issue = access.require(permissions.ISSUE_COMMANDS)

    def _actor():
        return permissions_web.current_actor()

    def _require_json():
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _gate(ticket_id):
        """The ticket's machine if the caller may see it, else None -- see the module
        docstring for why "missing" and "out of scope" are one answer."""
        machine = tickets.machine_of(db_path, ticket_id)
        if machine is None or not access.in_scope(machine):
            return None
        return machine

    def _not_found():
        return jsonify({"error": "no such ticket"}), 404

    def _emit(event, ticket_id, machine, urgency=None):
        """Tell open consoles something changed. Ids only: the fleet room includes operators
        without `view_tickets`, and a socket payload must not carry what the route would
        have refused them. The console re-reads through the gated route."""
        if emit is None:
            return
        try:
            payload = {"id": int(ticket_id), "machine": machine}
            if urgency:
                payload["urgency"] = urgency
            emit(event, payload, machine)
        except Exception:
            _log_generic(f"emitting {event}")

    # ================================================================
    # Agent-facing
    # ================================================================
    @bp.route("/api/agent/tickets", methods=["POST"])
    def agent_submit():
        """One ticket from the machine holding this agent's token. Screenshots follow
        separately, one request each, to `/api/agent/tickets/<id>/attachments`."""
        agent_id, machine = auth_helpers.bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        error, cleaned = tickets.validate_submission(request.get_json(silent=True))
        if error:
            return jsonify({"error": error}), 400
        policy = settings.get(db_path, "tickets.submitters")
        if not tickets.submitter_allowed(policy, cleaned["reporter_kind"]):
            # A code the tray can turn into a sentence for the person, rather than a ticket
            # it believes was sent.
            return jsonify({"error": "this hub only takes tickets from domain accounts",
                            "code": "submitter_not_allowed"}), 403
        try:
            facts = facts_for(machine) if facts_for else {}
        except Exception:
            # A snapshot that failed to build must not lose the person's ticket. It is
            # stored without facts and says so, which is still better than an email.
            _log_generic("building a ticket's facts snapshot")
            facts = {"error": "the hub could not record this machine's facts"}
        try:
            ticket_id = tickets.create_ticket(db_path, machine, agent_id, cleaned, facts)
        except Exception:
            return jsonify({"error": _log_generic("storing a ticket")}), 500
        _emit("ticket_new", ticket_id, machine, cleaned["urgency"])
        return jsonify({"id": ticket_id,
                        "max_attachments": tickets.MAX_ATTACHMENTS,
                        "max_attachment_bytes": tickets.MAX_ATTACHMENT_BYTES}), 201

    @bp.route("/api/agent/tickets/<int:ticket_id>/attachments", methods=["POST"])
    def agent_attachment(ticket_id):
        """One screenshot, raw JPEG or PNG in the body, for a ticket THIS agent opened in the
        last few minutes. 409 means the ticket will not take more, not "try again"."""
        agent_id, _machine = auth_helpers.bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        try:
            outcome, attachment_id = tickets.accept_attachment(
                db_path, attachment_root, ticket_id, agent_id, request.stream,
                request.content_length)
        except (OSError, ValueError):
            # A fixed sentence, never the exception's text (CodeQL, py/stack-trace-exposure).
            return jsonify({"error": "the hub could not store that upload"}), 400
        if outcome in ("no_ticket", "not_yours"):
            # One answer for both, so an agent cannot probe other machines' ticket ids.
            return jsonify({"error": "no such ticket"}), 404
        if outcome == "no_length":
            return jsonify({"error": "a Content-Length is required"}), 411
        if outcome == "too_large":
            return jsonify({"error": "that upload is larger than this hub accepts"}), 413
        if outcome == "not_image":
            return jsonify({"error": "a screenshot must be a JPEG or a PNG"}), 415
        if outcome in ("too_many", "window_closed"):
            return jsonify({"error": "this ticket takes no more screenshots",
                            "code": outcome}), 409
        machine = tickets.machine_of(db_path, ticket_id)
        _emit("ticket_updated", ticket_id, machine)
        return jsonify({"id": attachment_id}), 201

    @bp.route("/api/agent/tickets", methods=["GET"])
    def agent_mine():
        """The tray's "My tickets" (phase D): one person's tickets on this PC, without the
        internal notes. The SID is the service's to supply from the pipe; an empty one gets an
        empty list, never everybody's."""
        agent_id, machine = auth_helpers.bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        return jsonify({"tickets": tickets.tickets_for_reporter(
            db_path, machine, request.args.get("sid", ""))}), 200

    # ================================================================
    # Console pages
    # ================================================================
    @bp.route("/tickets")
    @login_required
    @can_view
    def tickets_page():
        return render_template("tickets.html")

    @bp.route("/tickets/<int:ticket_id>")
    @login_required
    @can_view
    def ticket_page(ticket_id):
        if _gate(ticket_id) is None:
            return render_template("denied.html",
                                   message="You do not have access to that ticket."), 404
        return render_template("ticket.html", ticket_id=ticket_id)

    # ================================================================
    # Console API -- reading
    # ================================================================
    @bp.route("/api/tickets", methods=["GET"])
    @login_required
    @can_view
    def list_all():
        """The queue, narrowed to the caller's machines. Filtering is here, not in the
        query, because tickets.py knows nothing about permission groups."""
        status = request.args.get("status") or None
        open_only = request.args.get("open") in ("1", "true", "yes")
        rows = tickets.list_tickets(db_path, status=status, open_only=open_only)
        return jsonify({
            "tickets": access.filter_rows(rows),
            "statuses": list(tickets.STATUSES),
            "urgencies": list(tickets.URGENCIES),
            "can_manage": access.can(permissions.MANAGE_TICKETS),
        }), 200

    @bp.route("/api/tickets/machines/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW_TICKETS)
    def list_machine(machine):
        return jsonify({"tickets": tickets.list_tickets(db_path,
                                                        machine=str(machine).strip())}), 200

    @bp.route("/api/tickets/<int:ticket_id>", methods=["GET"])
    @login_required
    @can_view
    def get_one(ticket_id):
        if _gate(ticket_id) is None:
            return _not_found()
        ticket = tickets.get_ticket(db_path, ticket_id)
        ticket.pop("agent_id", None)
        ticket["can_manage"] = access.can(permissions.MANAGE_TICKETS)
        ticket["can_draft"] = ticket["can_manage"] and access.can(permissions.ISSUE_COMMANDS)
        ticket["statuses"] = list(tickets.STATUSES)
        return jsonify(ticket), 200

    @bp.route("/api/tickets/<int:ticket_id>/attachments/<int:attachment_id>", methods=["GET"])
    @login_required
    @can_view
    def get_attachment(ticket_id, attachment_id):
        if _gate(ticket_id) is None:
            return _not_found()
        path, content_type = tickets.attachment_path(db_path, attachment_root, ticket_id,
                                                     attachment_id)
        if path is None:
            return jsonify({"error": "no such screenshot"}), 404
        response = send_file(path, mimetype=content_type, max_age=0)
        # A picture of somebody's screen: never kept by a shared browser or a proxy.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # ================================================================
    # Console API -- managing
    # ================================================================
    def _managed(ticket_id):
        """(machine, None) when the caller may change this ticket, else (None, response)."""
        refusal = _require_json()
        if refusal:
            return None, refusal
        machine = _gate(ticket_id)
        if machine is None:
            return None, _not_found()
        return machine, None

    @bp.route("/api/tickets/<int:ticket_id>/messages", methods=["POST"])
    @login_required
    @can_manage
    def reply(ticket_id):
        """A reply, or an internal note when `internal` is true. Only a reply will ever reach
        the person at the PC (phase D)."""
        machine, refusal = _managed(ticket_id)
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        internal = bool(data.get("internal"))
        error, message_id = tickets.add_message(db_path, ticket_id, _actor(), data.get("body"),
                                                internal=internal)
        if error:
            return jsonify({"error": error}), 400
        fleet.audit(db_path, actor=_actor(), action="ticket_reply", target=machine,
                    detail={"ticket": ticket_id, "internal": internal})
        _emit("ticket_updated", ticket_id, machine)
        return jsonify({"id": message_id}), 201

    @bp.route("/api/tickets/<int:ticket_id>/status", methods=["POST"])
    @login_required
    @can_manage
    def change_status(ticket_id):
        machine, refusal = _managed(ticket_id)
        if refusal:
            return refusal
        status = str((request.get_json(silent=True) or {}).get("status") or "")
        error, _ = tickets.set_status(db_path, ticket_id, status)
        if error:
            return jsonify({"error": error}), 400
        fleet.audit(db_path, actor=_actor(), action="ticket_status", target=machine,
                    detail={"ticket": ticket_id, "status": status})
        _emit("ticket_updated", ticket_id, machine)
        return jsonify({"status": status}), 200

    @bp.route("/api/tickets/<int:ticket_id>/assign", methods=["POST"])
    @login_required
    @can_manage
    def change_assignee(ticket_id):
        machine, refusal = _managed(ticket_id)
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        # "me" is the console's shortcut; the stored value is always an address, so a list
        # filtered by assignee does not have to know who "me" was.
        assignee = _actor() if data.get("assignee") == "me" else data.get("assignee")
        error = tickets.assign(db_path, ticket_id, assignee or "")
        if error:
            return jsonify({"error": error}), 400
        fleet.audit(db_path, actor=_actor(), action="ticket_assign", target=machine,
                    detail={"ticket": ticket_id, "assignee": assignee or ""})
        _emit("ticket_updated", ticket_id, machine)
        return jsonify({"assignee": assignee or ""}), 200

    @bp.route("/api/tickets/<int:ticket_id>/labels", methods=["POST", "DELETE"])
    @login_required
    @can_manage
    def change_label(ticket_id):
        machine, refusal = _managed(ticket_id)
        if refusal:
            return refusal
        label = (request.get_json(silent=True) or {}).get("label")
        if request.method == "DELETE":
            tickets.remove_label(db_path, ticket_id, label)
        else:
            error = tickets.add_label(db_path, ticket_id, label)
            if error:
                return jsonify({"error": error}), 400
        _emit("ticket_updated", ticket_id, machine)
        return jsonify({"status": "ok"}), 200

    @bp.route("/api/tickets/<int:ticket_id>/triage", methods=["POST"])
    @login_required
    @can_manage
    def ask_triage(ticket_id):
        """Ask the configured provider to triage this ticket. A button, never a timer."""
        machine, refusal = _managed(ticket_id)
        if refusal:
            return refusal
        try:
            ticket = tickets.get_ticket(db_path, ticket_id)
            error, saved = tickets.triage(
                db_path, ai_config() if ai_config else {}, ticket,
                api_key=(api_key() if api_key else ""), actor=_actor())
            if error:
                # 502, not 400: the request was fine and a third party was not -- or AI is
                # off, which the console checks before offering the button.
                return jsonify({"error": error}), 502
            fleet.audit(db_path, actor=_actor(), action="ticket_triage", target=machine,
                        detail={"ticket": ticket_id, "script": bool(saved.get("script"))})
            _emit("ticket_updated", ticket_id, machine)
            return jsonify(saved), 200
        except Exception:
            return jsonify({"error": _log_generic("triaging a ticket")}), 500

    @bp.route("/api/tickets/<int:ticket_id>/script", methods=["POST"])
    @login_required
    @can_manage
    @can_issue
    def draft_script(ticket_id):
        """Put the triage's suggested script into the library, switched off.

        Drafted, never executed: the body goes through `scripts.validate_script` exactly as a
        hand-written one does and lands disabled for a human to read and enable.
        """
        machine, refusal = _managed(ticket_id)
        if refusal:
            return refusal
        try:
            extra = rules.all_extra_variables(db_path)
            error, script = tickets.draft_script(
                db_path, ticket_id,
                known_variable=lambda ref: rules.lookup_variable(ref, extra) is not None,
                actor=_actor())
            if error:
                return jsonify({"error": error}), 400
            # LEVEL_SECURITY, like every other write to the script library -- see
            # correlate_web.draft_bundle_script.
            fleet.audit(db_path, actor=_actor(), action="draft_suggested_script",
                        level=fleet.LEVEL_SECURITY, target=script["name"],
                        detail={"machine": machine, "ticket": ticket_id,
                                "source": "ticket_triage", "enabled": script["enabled"],
                                "chars": len(script["body"])})
            return jsonify(script), 200
        except Exception:
            return jsonify({"error": _log_generic("drafting a suggested script")}), 500

    return bp
