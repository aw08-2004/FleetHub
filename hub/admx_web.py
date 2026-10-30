"""Administrative Templates -- the HTTP surface over admx.py (roadmap #32).

Two audiences:

  * **The agent** uploads its PC's template set to `/api/agent/gpo/templates`, and only when
    its heartbeat was answered `admx_wanted` (fleet_web.py). Bearer-authenticated like every
    agent route; the body is the zip, streamed to a spool with a Content-Length cap before a
    byte of it is parsed. Everything about what a valid upload is lives in admx.ingest.
  * **The console** reads the catalogue to build gpedit's tree: the sets the hub holds, one
    set's category tree, one setting's options and explanation. `view`, like the rest of the
    Group Policy page's reads. The templates are Microsoft's and vendors' public text, so they
    are not machine-scoped; what IS scoped is the set list's machine counts and OS labels,
    which say what the caller's PCs run and nothing about anybody else's.

Writing a template setting is not here: it is part of a policy object, saved through
gpo_web.py with that page's gates, and compiled by gpo.validate.
"""
from flask import Blueprint, jsonify, request

import admx
import auth_helpers
import i18n
import permissions
import refusals


def create_admx_blueprint(db_path, login_required, access):
    bp = Blueprint("admx", __name__)
    can_view = access.require(permissions.VIEW)

    # ---------------- Read (view) ----------------
    @bp.route("/api/gpo/admx/sets", methods=["GET"])
    @login_required
    @can_view
    def sets():
        unrestricted = access.current()["machines"] is None
        found = admx.list_sets(db_path, in_scope=None if unrestricted else access.in_scope)
        # A set none of the caller's PCs reports is left out for a scoped caller: that one
        # exists says some PC outside their scope runs that build.
        if not unrestricted:
            found = [s for s in found if s["machines"]]
        return jsonify({"sets": found}), 200

    @bp.route("/api/gpo/admx/<digest>/tree", methods=["GET"])
    @login_required
    @can_view
    def tree(digest):
        result = admx.tree(digest, i18n.current())
        if result is None:
            return jsonify({"error": "no such template set"}), 404
        return jsonify(result), 200

    @bp.route("/api/gpo/admx/<digest>/policy", methods=["GET"])
    @login_required
    @can_view
    def policy(digest):
        # The ref is a query parameter, not a path segment: it is `Namespace.Name:Policy`, and a
        # colon or a dot in a path is one more thing a proxy can decide to normalise.
        result = admx.policy_detail(digest, request.args.get("ref", ""), i18n.current())
        if result is None:
            return jsonify({"error": "no such template setting"}), 404
        return jsonify(result), 200

    # ---------------- Agent-facing ----------------
    @bp.route("/api/agent/gpo/templates", methods=["POST"])
    def agent_upload():
        """One PC's PolicyDefinitions, zipped. 200 for stored or already held (the agent then
        stops offering it), 4xx for a refusal the agent should not retry with the same bytes."""
        agent_id, machine = auth_helpers.bearer_agent(db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        length = request.content_length
        if length is None:
            return jsonify({"error": "a Content-Length is required"}), 411
        if length > admx.MAX_UPLOAD_BYTES:
            return jsonify({"error": "that upload is larger than this hub accepts"}), 413
        try:
            outcome = admx.ingest(db_path, machine, request.args.get("hash"), request.stream,
                                  length)
        except admx.AdmxRejected as e:
            print(f"[admx] Refused a template upload from {machine}: {e}")
            return refusals.refuse(e)
        except OSError:
            # A fixed sentence, never the exception's text: what went wrong writing the hub's
            # own disk is not the agent's business (CodeQL, py/stack-trace-exposure).
            return jsonify({"error": "the hub could not store that upload"}), 500
        return jsonify({"status": outcome}), 200

    return bp
