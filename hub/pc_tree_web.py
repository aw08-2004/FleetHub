"""The PC tree -- the HTTP surface over pc_tree.py.

Two gates, the second of which is the one that matters:

  * `view` reads the tree. `manage_pc_tree` creates, renames, moves and deletes folders, and
    files PCs into them.
  * **Filing a PC needs that PC in the filer's own scope, and moving a folder needs every PC
    beneath it in scope.** A folder is a permission scope (permissions.SCOPE_FOLDER), so
    filing is a grant: dropping a PC into "Cajas" hands it to every group scoped to "Cajas".
    An operator may do that only with PCs they already hold -- the same rule device groups
    apply to what a group resolves to (device_groups_web._scope_error), for the same reason.

**What a reader sees is narrowed to their scope.** The AD half is built only from PCs in
scope, so an OU holding none of yours never appears and its name is not disclosed; the filed
map and every count are narrowed the same way. Folder NAMES are shown to every reader: they
are labels an administrator chose, and a scoped operator has to be able to see the empty
folder they are about to file into.

**Every write re-points permission scope.** pc_tree.py invalidates the permissions cache;
this layer additionally calls `on_scope_change`, which app.py wires to the same hook an AD
sync uses, so live sockets whose rooms were resolved against the old tree are re-resolved.

Request bodies are read with get_json(silent=True): the hub only reads JSON bodies, which is
what makes a cross-site form POST unable to write here.
"""
from flask import Blueprint, jsonify, render_template, request

import device_groups
import fleet
import gpo
import pc_tree
import permissions
import refusals
import rules
import watchdogs

# Generous for "file this whole OU's worth of PCs", small enough that one request cannot be a
# fleet-sized write by accident. The same bound the rules target grammar uses.
MAX_FILE_BATCH = rules.MAX_TARGET_MACHINES


def create_pc_tree_blueprint(db_path, login_required, access, on_scope_change=None):
    bp = Blueprint("pc_tree", __name__)

    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_PC_TREE)

    def _actor():
        return access.email() or "unknown"

    def _scope_changed():
        if on_scope_change is not None:
            on_scope_change()

    def _audit(action, target, detail):
        # LEVEL_SECURITY: a folder is a permission scope, so every one of these rows records a
        # change to who can reach which PCs.
        fleet.audit(db_path, _actor(), action, target, detail, level=fleet.LEVEL_SECURITY)

    def _visible_rows():
        with pc_tree.get_conn(db_path) as conn:
            rows = conn.execute("SELECT machine, ad_ou FROM machine_info").fetchall()
        return [dict(r) for r in rows if access.in_scope(r["machine"])]

    def _out_of_scope(machines):
        outside = sorted(m for m in machines if not access.in_scope(m))
        if not outside:
            return None
        return ("these PCs are outside your access: " + ", ".join(outside[:5])
                + (f" (and {len(outside) - 5} more)" if len(outside) > 5 else ""))

    def _in_use(folder_ids):
        """Everything that still names one of `folder_ids`, for the delete refusal."""
        used = [{"name": f"rule {r['name']}"} for r in rules.rules_using_folder(db_path,
                                                                                folder_ids)]
        used += [{"name": f"device group {g['name']}"}
                 for g in device_groups.list_groups(db_path)
                 if rules.targets_name_folder(g["target"], folder_ids)]
        used += [{"name": f"watchdog {w['name']}"}
                 for w in watchdogs.list_watchdogs(db_path)
                 if rules.targets_name_folder(w.get("target"), folder_ids)]
        used += [{"name": f"permission group {g['name']}"}
                 for g in permissions.groups_using_folder(db_path, folder_ids)]
        # A policy link on a vanished folder would stop applying silently -- the PCs filed
        # there would lose their policy with nothing on screen to say why.
        used += gpo.folders_linked(db_path, folder_ids)
        return used

    # ---------------- Read (view) ----------------
    @bp.route("/api/pc-tree", methods=["GET"])
    @login_required
    @can_view
    def tree():
        rows = _visible_rows()
        visible = {r["machine"] for r in rows}
        filed = {m: f for m, f in pc_tree.assignments(db_path).items() if m in visible}
        direct = {}
        for folder_id in filed.values():
            direct[folder_id] = direct.get(folder_id, 0) + 1
        folders = [{"id": f["id"], "parent_id": f["parent_id"], "name": f["name"],
                    "description": f["description"], "count": direct.get(f["id"], 0),
                    "updated_at": f["updated_at"], "updated_by": f["updated_by"]}
                   for f in pc_tree.list_folders(db_path)]
        return jsonify({
            "folders": folders,
            "filed": filed,
            "ous": pc_tree.ou_tree(rows),
            "machines": sorted(visible, key=str.lower),
            "can_manage": access.can(permissions.MANAGE_PC_TREE),
        }), 200

    # ---------------- Write (manage_pc_tree) ----------------
    @bp.route("/api/pc-tree/folders", methods=["POST"])
    @login_required
    @can_manage
    def create_folder():
        body = request.get_json(silent=True) or {}
        try:
            folder = pc_tree.create_folder(db_path, body.get("name"),
                                           parent_id=body.get("parent_id"),
                                           description=body.get("description"),
                                           actor=_actor())
        except ValueError as e:
            return refusals.refuse(e)
        _audit("pc_folder.create", folder["name"],
               {"id": folder["id"], "parent_id": folder["parent_id"]})
        return jsonify(folder), 201

    @bp.route("/api/pc-tree/folders/<int:folder_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update_folder(folder_id):
        body = request.get_json(silent=True) or {}
        before = pc_tree.get_folder(db_path, folder_id)
        if before is None:
            return jsonify({"error": "no such folder"}), 404
        # The KEY decides whether this is a move, because null is a real destination (the top
        # level) -- the same reading permissions_web gives a group's parent_id.
        move = "parent_id" in body
        if move and (body.get("parent_id") or None) != before["parent_id"]:
            error = _out_of_scope(pc_tree.machines_in_folders(db_path, [folder_id]))
            if error:
                return jsonify({"error": error}), 403
        try:
            folder = pc_tree.update_folder(db_path, folder_id, name=body.get("name"),
                                           description=body.get("description"),
                                           parent_id=body.get("parent_id"), move=move,
                                           actor=_actor())
        except ValueError as e:
            return refusals.refuse(e)
        changes = {k: {"from": before[k], "to": folder[k]}
                   for k in ("name", "description", "parent_id") if before[k] != folder[k]}
        if changes:
            _audit("pc_folder.update", folder["name"], {"id": folder_id, "changes": changes})
        if "parent_id" in changes:
            _scope_changed()
        return jsonify(folder), 200

    @bp.route("/api/pc-tree/folders/<int:folder_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_folder(folder_id):
        try:
            folder = pc_tree.delete_folder(db_path, folder_id, in_use=_in_use([folder_id]))
        except KeyError:
            return jsonify({"error": "no such folder"}), 404
        except ValueError as e:
            # 409: permitted, but the folder still holds or is named by something -- the fix
            # is to move or edit those, the same call deleting a used device group makes.
            return refusals.refuse(e, 409)
        gpo.forget_folder(db_path, folder["id"])
        _audit("pc_folder.delete", folder["name"],
               {"id": folder["id"], "parent_id": folder["parent_id"]})
        return jsonify({"deleted": folder["id"]}), 200

    @bp.route("/api/pc-tree/file", methods=["POST"])
    @login_required
    @can_manage
    def file_machines():
        """File PCs into a folder, or (folder_id null) take them out of every folder."""
        body = request.get_json(silent=True) or {}
        machines = body.get("machines")
        if not isinstance(machines, list) or not machines:
            return jsonify({"error": "name at least one PC to move"}), 400
        if len(machines) > MAX_FILE_BATCH:
            return jsonify({"error": f"too many PCs at once (limit {MAX_FILE_BATCH})"}), 400
        names = [str(m or "").strip() for m in machines if str(m or "").strip()]
        error = _out_of_scope(names)
        if error:
            return jsonify({"error": error}), 403
        folder_id = body.get("folder_id")
        try:
            moved = pc_tree.file_machines(db_path, names, folder_id, actor=_actor())
        except ValueError as e:
            return refusals.refuse(e)
        if moved:
            folder = pc_tree.get_folder(db_path, folder_id) if folder_id is not None else None
            _audit("pc_folder.file", folder["name"] if folder else "(no folder)",
                   {"folder_id": folder_id, "moved": moved})
            _scope_changed()
        return jsonify({"moved": sorted(moved)}), 200

    # ---------------- Page ----------------
    @bp.route("/pc-tree")
    @login_required
    @can_view
    def pc_tree_page():
        return render_template("pc_tree.html")

    return bp
