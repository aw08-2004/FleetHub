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


def create_gpo_blueprint(db_path, login_required, access):
    bp = Blueprint("gpo", __name__)

    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_GROUP_POLICY)

    def _actor():
        return access.email() or "unknown"

    def _author_scope():
        machines = access.current()["machines"]
        return None if machines is None else sorted(machines)

    def _out_of_scope(machines, what):
        outside = sorted(m for m in machines if not access.in_scope(m))
        if not outside:
            return None
        return (f"{what} would reach PCs outside your access: " + ", ".join(outside[:5])
                + (f" (and {len(outside) - 5} more)" if len(outside) > 5 else ""))

    def _node_scope_error(kind, key):
        try:
            machines = gpo.machines_under_node(db_path, kind, key)
        except ValueError as e:
            return refusals.message(e)
        return _out_of_scope(machines, "this node")

    def _audit(action, target, detail):
        # LEVEL_SECURITY: every row here records a change to what LocalSystem writes into HKLM,
        # or runs, on PCs nobody is watching.
        fleet.audit(db_path, _actor(), action, target, detail, level=fleet.LEVEL_SECURITY)

    def _public_link(link):
        return {"id": link["id"], "gpo_id": link["gpo_id"], "node_kind": link["node_kind"],
                "node_key": link["node_key"], "node_label": link["node_label"],
                "link_order": link["link_order"], "enforced": link["enforced"],
                "enabled": link["enabled"],
                # Whether the link is narrowed to its author's scope -- never the list itself,
                # which would name PCs outside the reader's scope.
                "scoped": link["author_scope"] is not None,
                "created_by": link["created_by"]}

    def _agent_setting_schema():
        lang = i18n.current()
        out = []
        for setting in settings.REGISTRY:
            if not setting.agent:
                continue
            choices = settings._resolve_choices(setting, db_path)
            out.append({"key": setting.key, "type": setting.type,
                        "label": settings.field_label(setting, lang),
                        "help": settings.field_help(setting, lang),
                        "unit": settings.unit_label(setting, lang),
                        "min": setting.minimum, "max": setting.maximum,
                        "choices": choices,
                        "choice_labels": {c: settings.choice_label(setting, c, lang)
                                          for c in (choices or [])},
                        "default": setting.default})
        return out

    # ---------------- Read (view) ----------------
    @bp.route("/api/gpo", methods=["GET"])
    @login_required
    @can_view
    def overview():
        lang = i18n.current()
        options = gpo.node_options(db_path)
        return jsonify({
            "gpos": gpo.list_gpos(db_path),
            "links": [_public_link(link) for link in gpo.list_links(db_path)],
            "blocked": [{"node_kind": kind, "node_key": key}
                        for (kind, key), opts in options.items()
                        if opts.get("block_inheritance")],
            "agent_settings": _agent_setting_schema(),
            "registry_types": [{"name": kind,
                                "label": i18n.translate(f"gpo.registry_type.{kind}", lang)}
                               for kind in gpo.REGISTRY_TYPES],
            # Metadata only -- bodies are issue_commands-readable (scripts.py).
            "scripts": scripts.list_scripts(db_path),
            "can_manage": access.can(permissions.MANAGE_GROUP_POLICY),
            "can_issue_commands": access.can(permissions.ISSUE_COMMANDS),
        }), 200

    @bp.route("/api/gpo/rsop/<machine>", methods=["GET"])
    @login_required
    @access.require_machine(permissions.VIEW)
    def resultant(machine):
        """A PC's Resultant Set of Policy, with what its agent reported for each registry value
        and how each script run went -- the "why is this PC like this?" page."""
        result = gpo.rsop(db_path, machine)
        states = gpo.registry_states(db_path, machine)
        for entry in result["registry"]:
            state = states.get(entry["id"])
            entry["state"] = state["status"] if state else None
            entry["state_detail"] = state["detail"] if state else ""
            entry["reported_at"] = state["reported_at"] if state else None
        result["agent_supports_registry"] = gpo.agent_supports(db_path, machine)
        result["script_runs"] = gpo.script_runs(db_path, machine)
        result["state_labels"] = {s: i18n.translate(f"{gpo.STATE_TEXT_KEY}.{s}", i18n.current())
                                  for s in gpo.STATES}
        return jsonify(result), 200

    # ---------------- Policy objects (manage_group_policy) ----------------
    def _validated(body):
        return gpo.validate(body, scripts.specs(db_path))

    def _scripts_refusal(*policies):
        if any(p and gpo.has_scripts(p) for p in policies) and \
                not access.can(permissions.ISSUE_COMMANDS):
            return "a policy that runs scripts needs the Issue commands capability as well"
        return None

    @bp.route("/api/gpo", methods=["POST"])
    @login_required
    @can_manage
    def create():
        error, clean = _validated(request.get_json(silent=True) or {})
        if error:
            return jsonify({"error": error}), 400
        refusal = _scripts_refusal(clean)
        if refusal:
            return jsonify({"error": refusal}), 403
        error, policy = gpo.save_gpo(db_path, clean, actor=_actor())
        if error:
            return jsonify({"error": error}), 400
        _audit("gpo.create", policy["name"], {"id": policy["id"], "settings": policy["settings"],
                                               "enabled": policy["enabled"]})
        return jsonify(policy), 201

    @bp.route("/api/gpo/<int:gpo_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update(gpo_id):
        existing = gpo.get_gpo(db_path, gpo_id)
        if existing is None:
            return jsonify({"error": "no such policy"}), 404
        error, clean = _validated(request.get_json(silent=True) or {})
        if error:
            return jsonify({"error": error}), 400
        # Both sides: taking the scripts OUT of a policy is as much a decision about what runs
        # as SYSTEM as putting them in.
        refusal = _scripts_refusal(existing, clean) or \
            _out_of_scope(gpo.machines_linked(db_path, gpo_id), "this policy")
        if refusal:
            return jsonify({"error": refusal}), 403
        error, policy = gpo.save_gpo(db_path, clean, gpo_id=gpo_id, actor=_actor())
        if error:
            return jsonify({"error": error}), 400
        _audit("gpo.update", policy["name"], {
            "id": gpo_id, "enabled": {"from": existing["enabled"], "to": policy["enabled"]},
            "settings": {"from": existing["settings"], "to": policy["settings"]}})
        return jsonify(policy), 200

    @bp.route("/api/gpo/<int:gpo_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def remove(gpo_id):
        existing = gpo.get_gpo(db_path, gpo_id)
        if existing is None:
            return jsonify({"error": "no such policy"}), 404
        refusal = _scripts_refusal(existing) or \
            _out_of_scope(gpo.machines_linked(db_path, gpo_id), "this policy")
        if refusal:
            return jsonify({"error": refusal}), 403
        links = [_public_link(link) for link in gpo.list_links(db_path, gpo_id)]
        gpo.delete_gpo(db_path, gpo_id)
        _audit("gpo.delete", existing["name"], {"id": gpo_id, "settings": existing["settings"],
                                                 "links": links})
        return jsonify({"deleted": gpo_id}), 200

    # ---------------- Links and nodes (manage_group_policy) ----------------
    @bp.route("/api/gpo/<int:gpo_id>/links", methods=["POST"])
    @login_required
    @can_manage
    def link(gpo_id):
        body = request.get_json(silent=True) or {}
        kind, key = body.get("node_kind"), body.get("node_key")
        policy = gpo.get_gpo(db_path, gpo_id)
        if policy is None:
            return jsonify({"error": "no such policy"}), 404
        refusal = _scripts_refusal(policy) or _node_scope_error(kind, key)
        if refusal:
            return jsonify({"error": refusal}), 403
        try:
            created = gpo.create_link(db_path, gpo_id, kind, key, label=body.get("node_label"),
                                      enforced=bool(body.get("enforced")),
                                      author_scope=_author_scope(), actor=_actor())
        except ValueError as e:
            return refusals.refuse(e)
        _audit("gpo.link", policy["name"], _public_link(created))
        return jsonify(_public_link(created)), 201

    @bp.route("/api/gpo/links/<int:link_id>", methods=["PUT"])
    @login_required
    @can_manage
    def edit_link(link_id):
        existing = gpo.get_link(db_path, link_id)
        if existing is None:
            return jsonify({"error": "no such link"}), 404
        policy = gpo.get_gpo(db_path, existing["gpo_id"])
        refusal = _scripts_refusal(policy) or \
            _node_scope_error(existing["node_kind"], existing["node_key"])
        if refusal:
            return jsonify({"error": refusal}), 403
        body = request.get_json(silent=True) or {}
        move = body.get("move")
        updated = gpo.update_link(
            db_path, link_id,
            enforced=body["enforced"] if "enforced" in body else None,
            enabled=body["enabled"] if "enabled" in body else None,
            move=move if move in (-1, 1) else None,
            author_scope=_author_scope())
        _audit("gpo.link_update", policy["name"] if policy else str(existing["gpo_id"]),
               {"before": _public_link(existing), "after": _public_link(updated)})
        return jsonify(_public_link(updated)), 200

    @bp.route("/api/gpo/links/<int:link_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def unlink(link_id):
        existing = gpo.get_link(db_path, link_id)
        if existing is None:
            return jsonify({"error": "no such link"}), 404
        policy = gpo.get_gpo(db_path, existing["gpo_id"])
        refusal = _scripts_refusal(policy) or \
            _node_scope_error(existing["node_kind"], existing["node_key"])
        if refusal:
            return jsonify({"error": refusal}), 403
        gpo.delete_link(db_path, link_id)
        _audit("gpo.unlink", policy["name"] if policy else str(existing["gpo_id"]),
               _public_link(existing))
        return jsonify({"deleted": link_id}), 200

    @bp.route("/api/gpo/nodes", methods=["PUT"])
    @login_required
    @can_manage
    def node():
        body = request.get_json(silent=True) or {}
        kind, key = body.get("node_kind"), body.get("node_key")
        refusal = _node_scope_error(kind, key)
        if refusal:
            return jsonify({"error": refusal}), 403
        try:
            result = gpo.set_block_inheritance(db_path, kind, key,
                                               bool(body.get("block_inheritance")),
                                               actor=_actor())
        except ValueError as e:
            return refusals.refuse(e)
        _audit("gpo.block_inheritance", f"{kind}:{result['node_key']}", result)
        return jsonify(result), 200

    # ---------------- Page ----------------
    @bp.route("/group-policy")
    @login_required
    @can_view
    def group_policy_page():
        return render_template("group_policy.html")

    return bp
