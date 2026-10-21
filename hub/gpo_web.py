"""Group Policy -- the HTTP surface over gpo.py (roadmap #32).

Three gates, the last of which is the one that matters:

  * `view` reads policies, links and a PC's resultant set (that last one per machine, through
    `require_machine`).
  * `manage_group_policy` writes; a policy that runs a script, or an edit to one that already
    does, additionally needs `issue_commands` -- a script is code as SYSTEM, and the rules
    engine draws the same line.
  * **The writer's scope must cover every PC the write can reach.** Linking, unlinking,
    re-ordering or enforcing a link, and blocking inheritance on a node, each reach every PC
    beneath that node; editing or deleting a policy reaches every PC beneath every node it is
    linked to (gpo.machines_linked). Checked on the RESOLVED machines, never on the node's
    name, for the reason device_groups_web gives: an OU or "every PC" reaches machines its
    text never names.

**Links persist their author's scope** (gpo.py's module docstring): the check above holds at
write time, and the stored scope keeps it holding after somebody files another PC under the
node. Re-stamped on every link edit, as rules.save_rule does.

The route bodies live on GpoApi rather than inside the factory: the factory only binds URLs
and gates to methods, so each handler reads (and is measured) on its own.

Request bodies are read with get_json(silent=True): the hub only reads JSON bodies, which is
what makes a cross-site form POST unable to write here.
"""
from flask import Blueprint, jsonify, render_template, request

import fleet
import gpo
import i18n
import permissions
import refusals
import scripts
import settings


NO_SUCH_POLICY = "no such policy"
NO_SUCH_LINK = "no such link"
SCRIPTS_NEED_COMMANDS = "a policy that runs scripts needs the Issue commands capability as well"


def _public_link(link):
    return {"id": link["id"], "gpo_id": link["gpo_id"], "node_kind": link["node_kind"],
            "node_key": link["node_key"], "node_label": link["node_label"],
            "link_order": link["link_order"], "enforced": link["enforced"],
            "enabled": link["enabled"],
            # Whether the link is narrowed to its author's scope -- never the list itself,
            # which would name PCs outside the reader's scope.
            "scoped": link["author_scope"] is not None,
            "created_by": link["created_by"]}


def _setting_schema(setting, db_path, lang):
    choices = settings._resolve_choices(setting, db_path)
    return {"key": setting.key, "type": setting.type,
            "label": settings.field_label(setting, lang),
            "help": settings.field_help(setting, lang),
            "unit": settings.unit_label(setting, lang),
            "min": setting.minimum, "max": setting.maximum,
            "choices": choices,
            "choice_labels": {c: settings.choice_label(setting, c, lang) for c in (choices or [])},
            "default": setting.default}


def _with_state(entry, states):
    state = states.get(entry["id"]) or {}
    return {**entry, "state": state.get("status"), "state_detail": state.get("detail", ""),
            "reported_at": state.get("reported_at")}


def _error(message, status):
    return jsonify({"error": message}), status


class GpoApi:
    """The handlers behind every Group Policy route. See the module docstring for the gates."""

    def __init__(self, db_path, access):
        self.db_path = db_path
        self.access = access

    # ---------------- helpers ----------------
    def actor(self):
        return self.access.email() or "unknown"

    def author_scope(self):
        machines = self.access.current()["machines"]
        return None if machines is None else sorted(machines)

    def out_of_scope(self, machines, what):
        outside = sorted(m for m in machines if not self.access.in_scope(m))
        if not outside:
            return None
        more = f" (and {len(outside) - 5} more)" if len(outside) > 5 else ""
        return f"{what} would reach PCs outside your access: " + ", ".join(outside[:5]) + more

    def node_scope_error(self, kind, key):
        try:
            machines = gpo.machines_under_node(self.db_path, kind, key)
        except ValueError as e:
            return refusals.message(e)
        return self.out_of_scope(machines, "this node")

    def scripts_refusal(self, *policies):
        touches_scripts = any(p and gpo.has_scripts(p) for p in policies)
        if touches_scripts and not self.access.can(permissions.ISSUE_COMMANDS):
            return SCRIPTS_NEED_COMMANDS
        return None

    def link_refusal(self, link):
        """Why the caller may not change this link: scripts, or a node outside their scope."""
        policy = gpo.get_gpo(self.db_path, link["gpo_id"])
        return policy, (self.scripts_refusal(policy)
                        or self.node_scope_error(link["node_kind"], link["node_key"]))

    def audit(self, action, target, detail):
        # LEVEL_SECURITY: every row here records a change to what LocalSystem writes into HKLM,
        # or runs, on PCs nobody is watching.
        fleet.audit(self.db_path, self.actor(), action, target, detail, level=fleet.LEVEL_SECURITY)

    def validated(self):
        return gpo.validate(request.get_json(silent=True) or {}, scripts.specs(self.db_path))

    # ---------------- reads (view) ----------------
    def overview(self):
        lang = i18n.current()
        options = gpo.node_options(self.db_path)
        return jsonify({
            "gpos": gpo.list_gpos(self.db_path),
            "links": [_public_link(link) for link in gpo.list_links(self.db_path)],
            "blocked": [{"node_kind": kind, "node_key": key}
                        for (kind, key), opts in options.items() if opts.get("block_inheritance")],
            "agent_settings": [_setting_schema(s, self.db_path, lang)
                               for s in settings.REGISTRY if s.agent],
            "registry_types": [{"name": kind,
                                "label": i18n.translate(f"gpo.registry_type.{kind}", lang)}
                               for kind in gpo.REGISTRY_TYPES],
            # Metadata only -- bodies are issue_commands-readable (scripts.py).
            "scripts": scripts.list_scripts(self.db_path),
            "can_manage": self.access.can(permissions.MANAGE_GROUP_POLICY),
            "can_issue_commands": self.access.can(permissions.ISSUE_COMMANDS),
        }), 200

    def resultant(self, machine):
        """A PC's Resultant Set of Policy, with what its agent reported for each registry value
        and how each script run went -- the "why is this PC like this?" page."""
        result = gpo.rsop(self.db_path, machine)
        states = gpo.registry_states(self.db_path, machine)
        result["registry"] = [_with_state(entry, states) for entry in result["registry"]]
        result["agent_supports_registry"] = gpo.agent_supports(self.db_path, machine)
        result["script_runs"] = gpo.script_runs(self.db_path, machine)
        lang = i18n.current()
        result["state_labels"] = {s: i18n.translate(f"{gpo.STATE_TEXT_KEY}.{s}", lang)
                                  for s in gpo.STATES}
        return jsonify(result), 200

    # ---------------- policy objects (manage_group_policy) ----------------
    def create(self):
        error, clean = self.validated()
        if error:
            return _error(error, 400)
        refusal = self.scripts_refusal(clean)
        if refusal:
            return _error(refusal, 403)
        error, policy = gpo.save_gpo(self.db_path, clean, actor=self.actor())
        if error:
            return _error(error, 400)
        self.audit("gpo.create", policy["name"], {"id": policy["id"], "settings": policy["settings"],
                                                   "enabled": policy["enabled"]})
        return jsonify(policy), 201

    def update(self, gpo_id):
        existing = gpo.get_gpo(self.db_path, gpo_id)
        if existing is None:
            return _error(NO_SUCH_POLICY, 404)
        error, clean = self.validated()
        if error:
            return _error(error, 400)
        # Both sides: taking the scripts OUT of a policy is as much a decision about what runs
        # as SYSTEM as putting them in.
        refusal = (self.scripts_refusal(existing, clean)
                   or self.out_of_scope(gpo.machines_linked(self.db_path, gpo_id), "this policy"))
        if refusal:
            return _error(refusal, 403)
        error, policy = gpo.save_gpo(self.db_path, clean, gpo_id=gpo_id, actor=self.actor())
        if error:
            return _error(error, 400)
        self.audit("gpo.update", policy["name"], {
            "id": gpo_id, "enabled": {"from": existing["enabled"], "to": policy["enabled"]},
            "settings": {"from": existing["settings"], "to": policy["settings"]}})
        return jsonify(policy), 200

    def remove(self, gpo_id):
        existing = gpo.get_gpo(self.db_path, gpo_id)
        if existing is None:
            return _error(NO_SUCH_POLICY, 404)
        refusal = (self.scripts_refusal(existing)
                   or self.out_of_scope(gpo.machines_linked(self.db_path, gpo_id), "this policy"))
        if refusal:
            return _error(refusal, 403)
        links = [_public_link(link) for link in gpo.list_links(self.db_path, gpo_id)]
        gpo.delete_gpo(self.db_path, gpo_id)
        self.audit("gpo.delete", existing["name"], {"id": gpo_id, "settings": existing["settings"],
                                                     "links": links})
        return jsonify({"deleted": gpo_id}), 200

    # ---------------- links and nodes (manage_group_policy) ----------------
    def link(self, gpo_id):
        body = request.get_json(silent=True) or {}
        kind, key = body.get("node_kind"), body.get("node_key")
        policy = gpo.get_gpo(self.db_path, gpo_id)
        if policy is None:
            return _error(NO_SUCH_POLICY, 404)
        refusal = self.scripts_refusal(policy) or self.node_scope_error(kind, key)
        if refusal:
            return _error(refusal, 403)
        try:
            created = gpo.create_link(self.db_path, gpo_id, kind, key,
                                      label=body.get("node_label"),
                                      enforced=bool(body.get("enforced")),
                                      author_scope=self.author_scope(), actor=self.actor())
        except ValueError as e:
            return refusals.refuse(e)
        self.audit("gpo.link", policy["name"], _public_link(created))
        return jsonify(_public_link(created)), 201

    def edit_link(self, link_id):
        existing = gpo.get_link(self.db_path, link_id)
        if existing is None:
            return _error(NO_SUCH_LINK, 404)
        policy, refusal = self.link_refusal(existing)
        if refusal:
            return _error(refusal, 403)
        body = request.get_json(silent=True) or {}
        move = body.get("move")
        updated = gpo.update_link(
            self.db_path, link_id,
            enforced=body.get("enforced"), enabled=body.get("enabled"),
            move=move if move in (-1, 1) else None,
            author_scope=self.author_scope())
        self.audit("gpo.link_update", policy["name"] if policy else str(existing["gpo_id"]),
                   {"before": _public_link(existing), "after": _public_link(updated)})
        return jsonify(_public_link(updated)), 200

    def unlink(self, link_id):
        existing = gpo.get_link(self.db_path, link_id)
        if existing is None:
            return _error(NO_SUCH_LINK, 404)
        policy, refusal = self.link_refusal(existing)
        if refusal:
            return _error(refusal, 403)
        gpo.delete_link(self.db_path, link_id)
        self.audit("gpo.unlink", policy["name"] if policy else str(existing["gpo_id"]),
                   _public_link(existing))
        return jsonify({"deleted": link_id}), 200

    def node(self):
        body = request.get_json(silent=True) or {}
        kind, key = body.get("node_kind"), body.get("node_key")
        refusal = self.node_scope_error(kind, key)
        if refusal:
            return _error(refusal, 403)
        try:
            result = gpo.set_block_inheritance(self.db_path, kind, key,
                                               bool(body.get("block_inheritance")),
                                               actor=self.actor())
        except ValueError as e:
            return refusals.refuse(e)
        self.audit("gpo.block_inheritance", f"{kind}:{result['node_key']}", result)
        return jsonify(result), 200


def create_gpo_blueprint(db_path, login_required, access):
    bp = Blueprint("gpo", __name__)
    api = GpoApi(db_path, access)
    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_GROUP_POLICY)

    # ---------------- Read (view) ----------------
    @bp.route("/api/gpo", methods=["GET"])
    @login_required
    @can_view
    def overview():
        return api.overview()

    @bp.route("/api/gpo/rsop/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def resultant(machine):
        return api.resultant(machine)

    # ---------------- Write (manage_group_policy) ----------------
    @bp.route("/api/gpo", methods=["POST"])
    @login_required
    @can_manage
    def create():
        return api.create()

    @bp.route("/api/gpo/<int:gpo_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update(gpo_id):
        return api.update(gpo_id)

    @bp.route("/api/gpo/<int:gpo_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def remove(gpo_id):
        return api.remove(gpo_id)

    @bp.route("/api/gpo/<int:gpo_id>/links", methods=["POST"])
    @login_required
    @can_manage
    def link(gpo_id):
        return api.link(gpo_id)

    @bp.route("/api/gpo/links/<int:link_id>", methods=["PUT"])
    @login_required
    @can_manage
    def edit_link(link_id):
        return api.edit_link(link_id)

    @bp.route("/api/gpo/links/<int:link_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def unlink(link_id):
        return api.unlink(link_id)

    @bp.route("/api/gpo/nodes", methods=["PUT"])
    @login_required
    @can_manage
    def node():
        return api.node()

    # ---------------- Page ----------------
    @bp.route("/group-policy", methods=["GET"])
    @login_required
    @can_view
    def group_policy_page():
        return render_template("group_policy.html")

    return bp
