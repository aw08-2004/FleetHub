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

**The routes are methods of one class, registered from a table**, as in tickets_web.py:
eight routes of closures inside the factory made one function no reviewer could hold in
their head, and the table puts each route's path, methods and gates on one line.
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

AGENT_AUTH_REQUIRED = "agent authentication required"
UNKNOWN_TRANSFER = "unknown transfer"

#: How a relay answer reaches an agent. The agents branch on the status code alone.
#: FORBIDDEN is a 404, not a 403: a machine that is not an end of the transfer is not told
#: the id exists.
_AGENT_STATUS = {
    folder_relay.STORED: 200,
    folder_relay.DUPLICATE: 200,
    folder_relay.FULL: 503,
    folder_relay.OUT_OF_ORDER: 409,
    folder_relay.GONE: 410,
    folder_relay.FORBIDDEN: 404,
}


class _RelayRefusal(ValueError):
    """A send the route refuses before anything is queued: the message, and its status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _sentence(value, limit=500):
    """An agent's own words, capped. Shown to operators as text (textContent, never markup),
    so a cap is the only shaping it needs."""
    if not value:
        return None
    return str(value)[:limit]


def _answer(state):
    return jsonify({"status": state}), _AGENT_STATUS.get(state, 400)


def _agent_refused():
    return jsonify({"error": AGENT_AUTH_REQUIRED}), 401


def _clean_paths(raw_paths):
    """The files and folders to send, validated, or _RelayRefusal. Each must be below a drive
    root, and no two may share a name -- they would land in the same place on the target and
    the second would be merged into the first without anybody asking. Whether each is a file
    or a folder is the source agent's to find out: the hub has only the path."""
    if not isinstance(raw_paths, list) or not raw_paths:
        raise _RelayRefusal("Pick at least one file or folder to send.")
    if len(raw_paths) > files.MAX_OPERATION_PATHS:
        raise _RelayRefusal(f"Send at most {files.MAX_OPERATION_PATHS} items at a time.")
    paths, names = [], set()
    for raw in raw_paths:
        path = files.validate_path(raw)
        if files.parent_path(path) is None:
            # A drive root has no name to land under, and "send C:\" is a request for a
            # whole disk, which is not what the menu item means.
            raise _RelayRefusal("Pick files or folders, not a whole drive.")
        name = path.rsplit("\\", 1)[-1].lower()
        if name in names:
            raise _RelayRefusal("Two of those items have the same name.")
        names.add(name)
        paths.append(path)
    return paths


class _RelayRoutes:
    """Every route's body. Gates are NOT applied here -- see the table in
    create_folder_relay_blueprint, which is the one place to read who may call what."""

    def __init__(self, db_path, access, relay):
        self.db_path = db_path
        self.access = access
        self.relay = relay

    # ---------------- helpers ----------------
    def _queue(self, machine, command_type, params):
        fleet.require_enrolled(self.db_path, machine)
        return fleet.create_command(
            self.db_path, machine=machine, command_type=command_type, params=params,
            issued_by=permissions_web.current_actor(),
            ttl_seconds=settings.get_int(self.db_path, "fleet.command_ttl_seconds"))

    def _agent_version(self, machine):
        with files.get_conn(self.db_path) as conn:
            row = conn.execute("SELECT companion_version FROM machine_info WHERE machine = ?",
                               (machine,)).fetchone()
        return row["companion_version"] if row else None

    def _check_agents(self, *machines):
        for end in machines:
            if not folder_relay.agent_supports_stream(self._agent_version(end)):
                need = ".".join(str(p) for p in folder_relay.MIN_AGENT_VERSION)
                raise _RelayRefusal(f"{end} needs agent {need} or newer for a live copy.", 409)

    def _agent_machine(self):
        _agent_id, machine = auth_helpers.bearer_agent(self.db_path)
        return machine

    # ---------------- console ----------------
    def send_folders(self, machine):
        """Copy files and folders from this machine into a folder on another.

        Body: `{"paths": [...], "target": "<machine>", "destination": "D:\\\\Incoming"}`.
        Each item lands as `<destination>\\<its own name>`; nothing on the source changes.
        """
        # Restated at the route for the reason files_web._require_json gives: this is the
        # control that stops a cross-site form from starting a copy.
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        data = request.get_json(silent=True) or {}
        source = str(machine).strip()
        target = str(data.get("target") or "").strip()
        if not target or not self.access.in_scope(target):
            # Same words for "no such PC" and "not yours" -- see the module docstring.
            return jsonify({"error": "Pick a computer you manage to send to."}), 403
        try:
            paths = _clean_paths(data.get("paths"))
            destination = files.validate_path(data.get("destination"))
            self._check_agents(source, target)
            transfer_id = self.relay.open(source, target, paths, destination,
                                          permissions_web.current_actor())
        except folder_relay.RelayRefused as e:
            return refusals.refuse(e, status=409)
        except _RelayRefusal as e:
            return refusals.refuse(e, status=e.status)
        except ValueError as e:
            return refusals.refuse(e)

        # The receiver first: if the target cannot even be queued for, there is no point
        # starting the source reading a folder nobody will collect.
        try:
            receive_id = self._queue(target, "stream_receive", {
                "transfer_id": transfer_id, "destination": destination, "overwrite": False,
            })
            send_id = self._queue(source, "stream_send", {
                "transfer_id": transfer_id, "paths": paths,
            })
        except ValueError as e:
            self.relay.cancel(transfer_id, str(e))
            return refusals.refuse(e)
        self.relay.attach_commands(transfer_id, send_id, receive_id)
        return jsonify({"transfer_id": transfer_id, "send_command": send_id,
                        "receive_command": receive_id,
                        "poll_interval": files.POLL_INTERVAL_SECONDS}), 201

    def send_status(self, machine, transfer_id):
        """Where one copy has got to. Readable from either end's page, and from no other."""
        here = str(machine).strip()
        status = self.relay.status(transfer_id, machine=here)
        if status is None:
            return jsonify({"error": UNKNOWN_TRANSFER}), 404
        # The other end must be in scope too: an operator who manages the source but not the
        # target may see that a copy left, but not the target's name or path.
        other_key = "target" if status["source"] == here else "source"
        if not self.access.in_scope(status[other_key]):
            status[other_key] = None
            status["destination"] = None
        status["poll_interval"] = files.POLL_INTERVAL_SECONDS
        return jsonify(status), 200

    def send_cancel(self, machine, transfer_id):
        """Stop a copy. Files already written on the target stay; a half-written one does not
        (the receiver writes each file beside its name and renames it only when complete)."""
        if not request.is_json:
            return jsonify({"error": "expected application/json"}), 415
        if self.relay.status(transfer_id, machine=str(machine).strip()) is None:
            return jsonify({"error": UNKNOWN_TRANSFER}), 404
        self.relay.cancel(transfer_id, f"Cancelled by {permissions_web.current_actor()}.")
        return jsonify({"status": "cancelled"}), 200

    # ---------------- agent-facing ----------------
    def agent_meta(self, transfer_id):
        machine = self._agent_machine()
        if machine is None:
            return _agent_refused()
        data = request.get_json(silent=True) or {}
        try:
            total_bytes = int(data.get("total_bytes") or 0)
            total_files = int(data.get("total_files") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "totals must be numbers"}), 400
        return _answer(self.relay.describe(transfer_id, machine, total_bytes, total_files))

    def agent_put(self, transfer_id, seq):
        """One chunk from the source. 503 means "the window is full, send it again"."""
        machine = self._agent_machine()
        if machine is None:
            return _agent_refused()
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
        return _answer(self.relay.put_chunk(transfer_id, machine, seq, data))

    def agent_end(self, transfer_id):
        """The source is finished -- or failed, with `error`."""
        machine = self._agent_machine()
        if machine is None:
            return _agent_refused()
        data = request.get_json(silent=True) or {}
        return _answer(self.relay.finish_send(transfer_id, machine,
                                              error=_sentence(data.get("error"))))

    def agent_take(self, transfer_id, seq):
        """Chunk `seq` for the target: 200 with bytes, 204 not yet, 200 + X-Stream-End when
        everything has been taken, 410 when the copy is over for any other reason."""
        machine = self._agent_machine()
        if machine is None:
            return _agent_refused()
        state, data = self.relay.take_chunk(transfer_id, machine, seq)
        if state == folder_relay.CHUNK:
            return Response(data, status=200, mimetype="application/octet-stream")
        if state == folder_relay.PENDING:
            return Response(status=204)
        if state == folder_relay.END:
            return Response(b"", status=200, mimetype="application/octet-stream",
                            headers={"X-Stream-End": "1"})
        return _answer(state)

    def agent_result(self, transfer_id):
        """The target's verdict, with a one-paragraph summary the console shows."""
        machine = self._agent_machine()
        if machine is None:
            return _agent_refused()
        data = request.get_json(silent=True) or {}
        return _answer(self.relay.finish_receive(
            transfer_id, machine, error=_sentence(data.get("error")),
            summary=_sentence(data.get("summary"), limit=2000)))


def create_folder_relay_blueprint(db_path, login_required, access, relay=None):
    """Build the live-copy Blueprint. `relay` is injectable so tests can drive one directly
    (and use a fake clock); the hub passes nothing and gets the process's one instance."""
    bp = Blueprint("folder_relay", __name__)
    routes = _RelayRoutes(db_path, access, relay or folder_relay.FolderRelay())
    machine_gate = access.require_machine(permissions.ISSUE_COMMANDS)
    console = (login_required, machine_gate)
    agent = ()  # bearer auth inside the method: no session, no capability
    send = "/api/machines/<machine>/files/send"
    stream = "/api/agent/files/stream/<transfer_id>"

    # (rule, endpoint, methods, gates outermost first). Every console row is login_required
    # plus require_machine on the SOURCE; the target's scope is checked inside send_folders,
    # because it arrives in the body where no decorator can see it.
    table = (
        (send, "send_folders", ["POST"], console),
        (send + "/<transfer_id>", "send_status", ["GET"], console),
        (send + "/<transfer_id>/cancel", "send_cancel", ["POST"], console),
        (stream + "/meta", "agent_meta", ["POST"], agent),
        (stream + "/chunk/<int:seq>", "agent_put", ["PUT"], agent),
        (stream + "/end", "agent_end", ["POST"], agent),
        (stream + "/chunk/<int:seq>", "agent_take", ["GET"], agent),
        (stream + "/result", "agent_result", ["POST"], agent),
    )
    for rule, endpoint, methods, gates in table:
        handler = getattr(routes, endpoint)
        for gate in reversed(gates):
            handler = gate(handler)
        bp.add_url_rule(rule, endpoint, handler, methods=methods)
    return bp
