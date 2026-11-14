"""Flask HTTP surface for the live folder copy between two PCs -- a thin layer over
folder_relay.py, registered as a Blueprint from app.py.

**Two machines, two gates, and both are checked.** The route is named for the SOURCE, so
`access.require_machine(issue_commands)` covers it the usual way. The TARGET arrives in the
body, where no decorator can see it, and is checked inline with `access.in_scope()`. Missing
that second check is the silent failure this file exists to prevent: an operator scoped to one
site could otherwise copy a folder from a PC they manage onto any PC in the fleet -- writing
onto a machine they were never given -- and nothing would look wrong from their side. The
refusal for an out-of-scope target is the same sentence as for an unknown one, for the
enumeration reason permissions_web.require_machine gives.

**Why `issue_commands` and not a new capability.** Writing files onto a machine is what this
capability already grants (the Files tab's upload, a SYSTEM shell), and reading them off one is
too (its download). A copy from A to B is a download from A and an upload to B, each of which
the operator could already do by hand -- only without the 2 GiB cap and the hub's disk.

**The agent half (`/api/agent/files/stream/*`) is bearer-authenticated**, and folder_relay.py
checks on every call that the calling machine is the end of the transfer it claims to be: the
source alone may put chunks, the target alone may take them. Same rule as files_web.py: an
enrolled agent acts for itself and nothing else.

**Nothing on the console side waits for a machine.** The send route queues two commands and
answers; the console polls the status route. The agent routes DO wait -- briefly, bounded by
folder_relay.WAIT_SECONDS -- because a long-poll is what keeps a chunk moving the moment the
other side is ready instead of on the next fixed tick.
"""
from flask import Blueprint, Response, jsonify, request

import auth_helpers
import files
import fleet
import folder_relay
import permissions
import permissions_web
import refusals
import settings


def create_folder_relay_blueprint(db_path, login_required, access, relay=None):
    """Build the live-copy Blueprint. `relay` is injectable so tests can drive one directly
    (and use a fake clock); the hub passes nothing and gets the process's one instance."""
    bp = Blueprint("folder_relay", __name__)
    relay = relay or folder_relay.FolderRelay()

    def _require_json():
        # Restated at the route for the reason files_web._require_json gives: this is the
        # control that stops a cross-site form from starting a copy.
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        return None

    def _queue(machine, command_type, params):
        fleet.require_enrolled(db_path, machine)
        return fleet.create_command(
            db_path, machine=machine, command_type=command_type, params=params,
            issued_by=permissions_web.current_actor(),
            ttl_seconds=settings.get_int(db_path, "fleet.command_ttl_seconds"))

    def _agent_version(machine):
        with files.get_conn(db_path) as conn:
            row = conn.execute("SELECT companion_version FROM machine_info WHERE machine = ?",
                               (machine,)).fetchone()
        return row["companion_version"] if row else None

    # ================================================================
    # Console
    # ================================================================
    @bp.route("/api/machines/<machine>/files/send", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def send_folders(machine):
        """Copy one or more folders from this machine into a folder on another.

        Body: `{"paths": [...], "target": "<machine>", "destination": "D:\\\\Incoming"}`.
        Each folder lands as `<destination>\\<its own name>`; nothing on the source changes.
        """
        refusal = _require_json()
        if refusal:
            return refusal
        data = request.get_json(silent=True) or {}
        source = str(machine).strip()
        target = str(data.get("target") or "").strip()
        if not target or not access.in_scope(target):
            # Same words for "no such PC" and "not yours" -- see the module docstring.
            return jsonify({"error": "Pick a computer you manage to send to."}), 403

        raw_paths = data.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            return jsonify({"error": "Pick at least one folder to send."}), 400
        if len(raw_paths) > files.MAX_OPERATION_PATHS:
            return jsonify({"error": f"Send at most {files.MAX_OPERATION_PATHS} folders "
                                     "at a time."}), 400
        try:
            paths, names = [], set()
            for raw in raw_paths:
                path = files.validate_path(raw)
                if files.parent_path(path) is None:
                    # A drive root has no name to land under, and "send C:\" is a request
                    # for a whole disk, which is not what the menu item means.
                    raise ValueError("Pick folders, not a whole drive.")
                name = path.rsplit("\\", 1)[-1].lower()
                if name in names:
                    # Two folders with one name would land in the same place on the target
                    # and the second would be merged into the first without anybody asking.
                    raise ValueError("Two of those folders have the same name.")
                names.add(name)
                paths.append(path)
            destination = files.validate_path(data.get("destination"))
        except ValueError as e:
            return refusals.refuse(e)

        for end in (source, target):
            if not folder_relay.agent_supports_stream(_agent_version(end)):
                need = ".".join(str(p) for p in folder_relay.MIN_AGENT_VERSION)
                return jsonify({"error": f"{end} needs agent {need} or newer for a "
                                         "live copy."}), 409

        try:
            transfer_id = relay.open(source, target, paths, destination,
                                     permissions_web.current_actor())
        except folder_relay.RelayRefused as e:
            return refusals.refuse(e, status=409)

        # The receiver first: if the target cannot even be queued for, there is no point
        # starting the source reading a folder nobody will collect.
        try:
            receive_id = _queue(target, "stream_receive", {
                "transfer_id": transfer_id,
                "destination": destination,
                "overwrite": False,
            })
            send_id = _queue(source, "stream_send", {
                "transfer_id": transfer_id,
                "paths": paths,
            })
        except ValueError as e:
            relay.cancel(transfer_id, str(e))
            return refusals.refuse(e)
        relay.attach_commands(transfer_id, send_id, receive_id)
        return jsonify({"transfer_id": transfer_id, "send_command": send_id,
                        "receive_command": receive_id,
                        "poll_interval": files.POLL_INTERVAL_SECONDS}), 201

    @bp.route("/api/machines/<machine>/files/send/<transfer_id>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def send_status(machine, transfer_id):
        """Where one copy has got to. Readable from either end's page, and from no other."""
        status = relay.status(transfer_id, machine=str(machine).strip())
        if status is None:
            return jsonify({"error": "unknown transfer"}), 404
        # The other end must be in scope too: an operator who manages the source but not
        # the target may see that a copy left, but not the target's name or path.
        other = status["target"] if status["source"] == str(machine).strip() else status["source"]
        if not access.in_scope(other):
            status["target" if other == status["target"] else "source"] = None
            status["destination"] = None
        status["poll_interval"] = files.POLL_INTERVAL_SECONDS
        return jsonify(status), 200

    @bp.route("/api/machines/<machine>/files/send/<transfer_id>/cancel", methods=["POST"])
    @login_required
    @access.require_machine(permissions.ISSUE_COMMANDS)
    def send_cancel(machine, transfer_id):
        """Stop a copy. Files already written on the target stay; a half-written one does not
        (the receiver writes each file beside its name and renames it only when complete)."""
        refusal = _require_json()
        if refusal:
            return refusal
        if relay.status(transfer_id, machine=str(machine).strip()) is None:
            return jsonify({"error": "unknown transfer"}), 404
        relay.cancel(transfer_id, f"Cancelled by {permissions_web.current_actor()}.")
        return jsonify({"status": "cancelled"}), 200

    # ================================================================
    # Agent-facing
    # ================================================================
    def _agent():
        _agent_id, machine = auth_helpers.bearer_agent(db_path)
        return machine

    def _sentence(value, limit=500):
        """An agent's own words, capped. Shown to operators as text (textContent, never
        markup), so a cap is the only shaping it needs."""
        if not value:
            return None
        return str(value)[:limit]

    def _answer(state):
        """Map a relay answer onto a status code the agents branch on."""
        codes = {
            folder_relay.STORED: 200,
            folder_relay.DUPLICATE: 200,
            folder_relay.FULL: 503,
            folder_relay.OUT_OF_ORDER: 409,
            folder_relay.GONE: 410,
            folder_relay.FORBIDDEN: 404,   # not "403": do not confirm the id exists
        }
        return jsonify({"status": state}), codes.get(state, 400)

    @bp.route("/api/agent/files/stream/<transfer_id>/meta", methods=["POST"])
    def agent_stream_meta(transfer_id):
        machine = _agent()
        if machine is None:
            return jsonify({"error": "agent authentication required"}), 401
        data = request.get_json(silent=True) or {}
        try:
            total_bytes = int(data.get("total_bytes") or 0)
            total_files = int(data.get("total_files") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "totals must be numbers"}), 400
        return _answer(relay.describe(transfer_id, machine, total_bytes, total_files))

    @bp.route("/api/agent/files/stream/<transfer_id>/chunk/<int:seq>", methods=["PUT"])
    def agent_stream_put(transfer_id, seq):
        """One chunk from the source. 503 means "the window is full, send it again"."""
        machine = _agent()
        if machine is None:
            return jsonify({"error": "agent authentication required"}), 401
        length = request.content_length
        if length is None:
            return jsonify({"error": "a Content-Length is required"}), 411
        if length > folder_relay.MAX_CHUNK_BYTES:
            return jsonify({"error": "chunk is larger than the relay accepts"}), 413
        # Read at most what was declared and one byte more, so a body that runs past its own
        # Content-Length is caught rather than trusted.
        data = request.stream.read(length + 1)
        if len(data) != length:
            return jsonify({"error": "body does not match its Content-Length"}), 400
        return _answer(relay.put_chunk(transfer_id, machine, seq, data))

    @bp.route("/api/agent/files/stream/<transfer_id>/end", methods=["POST"])
    def agent_stream_end(transfer_id):
        """The source is finished -- or failed, with `error`."""
        machine = _agent()
        if machine is None:
            return jsonify({"error": "agent authentication required"}), 401
        data = request.get_json(silent=True) or {}
        error = _sentence(data.get("error"))
        return _answer(relay.finish_send(transfer_id, machine, error=error))

    @bp.route("/api/agent/files/stream/<transfer_id>/chunk/<int:seq>", methods=["GET"])
    def agent_stream_take(transfer_id, seq):
        """Chunk `seq` for the target: 200 with bytes, 204 not yet, 200 + X-Stream-End when
        everything has been taken, 410 when the copy is over for any other reason."""
        machine = _agent()
        if machine is None:
            return jsonify({"error": "agent authentication required"}), 401
        state, data = relay.take_chunk(transfer_id, machine, seq)
        if state == folder_relay.CHUNK:
            return Response(data, status=200, mimetype="application/octet-stream")
        if state == folder_relay.PENDING:
            return Response(status=204)
        if state == folder_relay.END:
            return Response(b"", status=200, mimetype="application/octet-stream",
                            headers={"X-Stream-End": "1"})
        return _answer(state)

    @bp.route("/api/agent/files/stream/<transfer_id>/result", methods=["POST"])
    def agent_stream_result(transfer_id):
        """The target's verdict, with a one-paragraph summary the console shows."""
        machine = _agent()
        if machine is None:
            return jsonify({"error": "agent authentication required"}), 401
        data = request.get_json(silent=True) or {}
        error = _sentence(data.get("error"))
        summary = _sentence(data.get("summary"), limit=2000)
        return _answer(relay.finish_receive(transfer_id, machine, error=error,
                                            summary=summary))

    return bp
