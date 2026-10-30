"""Administrative Templates -- the HTTP surface over admx.py (roadmap #32).

Two audiences:

  * **The agent** uploads its PC's template set to `/api/agent/gpo/templates/<hash>`, and only
    when its heartbeat was answered `admx_wanted` (fleet_web.py) -- admx.ingest refuses an
    upload from any PC not holding the claim on that set. Bearer-authenticated like every
    agent route; the body is the zip, streamed to a spool with a Content-Length cap before a
    byte of it is parsed. Everything about what a valid upload is lives in admx.ingest.
  * **The console** reads the catalogue to build gpedit's tree: the sets the hub holds, one
    set's category tree, one setting's options and explanation. `view`, like the rest of the
    Group Policy page's reads. The templates are Microsoft's and vendors' public text, so they
    are not machine-scoped; what IS scoped is the set list's machine counts and OS labels,
    which say what the caller's PCs run and nothing about anybody else's.

Writing a template setting is not here: it is part of a policy object, saved through
gpo_web.py with that page's gates, and compiled by gpo.validate.

The route bodies live on AdmxApi, as on gpo_web.GpoApi: the factory only binds URLs and gates.
"""
from flask import Blueprint, jsonify, request

import admx
import auth_helpers
import i18n
import permissions
import refusals


class AdmxApi:
    """The handlers behind every Administrative Templates route."""

    def __init__(self, db_path, access):
        self.db_path = db_path
        self.access = access

    # ---------------- reads (view) ----------------
    def sets(self):
        unrestricted = self.access.current()["machines"] is None
        found = admx.list_sets(self.db_path,
                               in_scope=None if unrestricted else self.access.in_scope)
        return jsonify({"sets": found}), 200

    def tree(self, digest):
        result = admx.tree(digest, i18n.current())
        if result is None:
            return jsonify({"error": "no such template set"}), 404
        return jsonify(result), 200

    def policy(self, digest):
        # The ref is a query parameter, not a path segment: it is `Namespace.Name:Policy`, and a
        # colon or a dot in a path is one more thing a proxy can decide to normalise.
        result = admx.policy_detail(digest, request.args.get("ref", ""), i18n.current())
        if result is None:
            return jsonify({"error": "no such template setting"}), 404
        return jsonify(result), 200

    # ---------------- agent-facing ----------------
    def upload(self, digest):
        """One PC's PolicyDefinitions, zipped. 200 for stored or already held (the agent then
        stops offering it), 4xx for a refusal the agent should not retry with the same bytes."""
        agent_id, machine = auth_helpers.bearer_agent(self.db_path)
        if agent_id is None:
            return jsonify({"error": "agent authentication required"}), 401
        length = request.content_length
        if length is None:
            return jsonify({"error": "a Content-Length is required"}), 411
        if length > admx.MAX_UPLOAD_BYTES:
            return jsonify({"error": "that upload is larger than this hub accepts"}), 413
        try:
            outcome = admx.ingest(self.db_path, machine, digest, request.stream, length)
        except admx.AdmxRejected as e:
            print(f"[admx] Refused a template upload from {machine}: {e}")
            return refusals.refuse(e)
        except OSError:
            # A fixed sentence, never the exception's text: what went wrong writing the hub's
            # own disk is not the agent's business (CodeQL, py/stack-trace-exposure).
            return jsonify({"error": "the hub could not store that upload"}), 500
        return jsonify({"status": outcome}), 200


def create_admx_blueprint(db_path, login_required, access):
    bp = Blueprint("admx", __name__)
    api = AdmxApi(db_path, access)
    can_view = access.require(permissions.VIEW)

    @bp.route("/api/gpo/admx/sets", methods=["GET"])
    @login_required
    @can_view
    def sets():
        return api.sets()

    @bp.route("/api/gpo/admx/<digest>/tree", methods=["GET"])
    @login_required
    @can_view
    def tree(digest):
        return api.tree(digest)

    @bp.route("/api/gpo/admx/<digest>/policy", methods=["GET"])
    @login_required
    @can_view
    def policy(digest):
        return api.policy(digest)

    # The hash in the path rather than a query string: it names the resource being created.
    @bp.route("/api/agent/gpo/templates/<digest>", methods=["POST"])
    def agent_upload(digest):
        return api.upload(digest)

    return bp
