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

The route bodies live on PcTreeApi rather than inside the factory, which only binds URLs and
gates to them -- the shape gpo_web.py uses.

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


NO_SUCH_FOLDER = "no such folder"


def _error(message, status):
    return jsonify({"error": message}), status


def _public_folder(folder, direct):
    return {"id": folder["id"], "parent_id": folder["parent_id"], "name": folder["name"],
            "description": folder["description"], "count": direct.get(folder["id"], 0),
            "updated_at": folder["updated_at"], "updated_by": folder["updated_by"]}


def _in_use(db_path, folder_ids):
    """Everything that still names one of `folder_ids`, for the delete refusal."""
    used = [{"name": f"rule {r['name']}"} for r in rules.rules_using_folder(db_path, folder_ids)]
    used += [{"name": f"device group {g['name']}"} for g in device_groups.list_groups(db_path)
             if rules.targets_name_folder(g["target"], folder_ids)]
    used += [{"name": f"watchdog {w['name']}"} for w in watchdogs.list_watchdogs(db_path)
             if rules.targets_name_folder(w.get("target"), folder_ids)]
    used += [{"name": f"permission group {g['name']}"}
             for g in permissions.groups_using_folder(db_path, folder_ids)]
    # A policy link on a vanished folder would stop applying silently -- the PCs filed there
    # would lose their policy with nothing on screen to say why.
    return used + gpo.folders_linked(db_path, folder_ids)


def _machine_names(machines):
    """The PC names a filing request carries, or (None, refusal)."""
    if not isinstance(machines, list) or not machines:
        return None, "name at least one PC to move"
    if len(machines) > MAX_FILE_BATCH:
        return None, f"too many PCs at once (limit {MAX_FILE_BATCH})"
    return [str(m or "").strip() for m in machines if str(m or "").strip()], None


class PcTreeApi:
    """The handlers behind every PC tree route. See the module docstring for the gates."""

    def __init__(self, db_path, access, on_scope_change=None):
        self.db_path = db_path
        self.access = access
        self.on_scope_change = on_scope_change

    def actor(self):
        return self.access.email() or "unknown"

    def scope_changed(self):
        if self.on_scope_change is not None:
            self.on_scope_change()

    def audit(self, action, target, detail):
        # LEVEL_SECURITY: a folder is a permission scope, so every one of these rows records a
        # change to who can reach which PCs.
        fleet.audit(self.db_path, self.actor(), action, target, detail, level=fleet.LEVEL_SECURITY)

    def visible_rows(self):
        with pc_tree.get_conn(self.db_path) as conn:
            rows = conn.execute("SELECT machine, ad_ou FROM machine_info").fetchall()
        return [dict(r) for r in rows if self.access.in_scope(r["machine"])]

    def out_of_scope(self, machines):
        outside = sorted(m for m in machines if not self.access.in_scope(m))
        if not outside:
            return None
        more = f" (and {len(outside) - 5} more)" if len(outside) > 5 else ""
        return "these PCs are outside your access: " + ", ".join(outside[:5]) + more

    # ---------------- read (view) ----------------
    def tree(self):
        rows = self.visible_rows()
        visible = {r["machine"] for r in rows}
        filed = {m: f for m, f in pc_tree.assignments(self.db_path).items() if m in visible}
        direct = {}
        for folder_id in filed.values():
            direct[folder_id] = direct.get(folder_id, 0) + 1
        return jsonify({
            "folders": [_public_folder(f, direct) for f in pc_tree.list_folders(self.db_path)],
            "filed": filed,
            "ous": pc_tree.ou_tree(rows),
            "machines": sorted(visible, key=str.lower),
            "can_manage": self.access.can(permissions.MANAGE_PC_TREE),
        }), 200

    # ---------------- write (manage_pc_tree) ----------------
    def create_folder(self):
        body = request.get_json(silent=True) or {}
        try:
            folder = pc_tree.create_folder(self.db_path, body.get("name"),
                                           parent_id=body.get("parent_id"),
                                           description=body.get("description"),
                                           actor=self.actor())
        except ValueError as e:
            return refusals.refuse(e)
        self.audit("pc_folder.create", folder["name"],
                   {"id": folder["id"], "parent_id": folder["parent_id"]})
        return jsonify(folder), 201

    def move_refusal(self, folder_id, before, body):
        """Why this edit may not move the folder, or None. The KEY decides whether it is a move,
        because null is a real destination (the top level) -- the reading permissions_web gives
        a group's parent_id."""
        if (body.get("parent_id") or None) == before["parent_id"]:
            return None
        return self.out_of_scope(pc_tree.machines_in_folders(self.db_path, [folder_id]))

    def update_folder(self, folder_id):
        body = request.get_json(silent=True) or {}
        before = pc_tree.get_folder(self.db_path, folder_id)
        if before is None:
            return _error(NO_SUCH_FOLDER, 404)
        move = "parent_id" in body
        refusal = self.move_refusal(folder_id, before, body) if move else None
        if refusal:
            return _error(refusal, 403)
        try:
            folder = pc_tree.update_folder(self.db_path, folder_id, name=body.get("name"),
                                           description=body.get("description"),
                                           parent_id=body.get("parent_id"), move=move,
                                           actor=self.actor())
        except ValueError as e:
            return refusals.refuse(e)
        changes = {k: {"from": before[k], "to": folder[k]}
                   for k in ("name", "description", "parent_id") if before[k] != folder[k]}
        if changes:
            self.audit("pc_folder.update", folder["name"], {"id": folder_id, "changes": changes})
        if "parent_id" in changes:
            self.scope_changed()
        return jsonify(folder), 200

    def delete_folder(self, folder_id):
        try:
            folder = pc_tree.delete_folder(self.db_path, folder_id,
                                           in_use=_in_use(self.db_path, [folder_id]))
        except KeyError:
            return _error(NO_SUCH_FOLDER, 404)
        except ValueError as e:
            # 409: permitted, but the folder still holds or is named by something -- the fix is
            # to move or edit those, the same call deleting a used device group makes.
            return refusals.refuse(e, 409)
        gpo.forget_folder(self.db_path, folder["id"])
        self.audit("pc_folder.delete", folder["name"],
                   {"id": folder["id"], "parent_id": folder["parent_id"]})
        return jsonify({"deleted": folder["id"]}), 200

    def file_machines(self):
        """File PCs into a folder, or (folder_id null) take them out of every folder."""
        body = request.get_json(silent=True) or {}
        names, refusal = _machine_names(body.get("machines"))
        if refusal:
            return _error(refusal, 400)
        refusal = self.out_of_scope(names)
        if refusal:
            return _error(refusal, 403)
        folder_id = body.get("folder_id")
        try:
            moved = pc_tree.file_machines(self.db_path, names, folder_id, actor=self.actor())
        except ValueError as e:
            return refusals.refuse(e)
        if moved:
            folder = pc_tree.get_folder(self.db_path, folder_id) if folder_id is not None else None
            self.audit("pc_folder.file", folder["name"] if folder else "(no folder)",
                       {"folder_id": folder_id, "moved": moved})
            self.scope_changed()
        return jsonify({"moved": sorted(moved)}), 200


def create_pc_tree_blueprint(db_path, login_required, access, on_scope_change=None):
    bp = Blueprint("pc_tree", __name__)
    api = PcTreeApi(db_path, access, on_scope_change)
    can_view = access.require(permissions.VIEW)
    can_manage = access.require(permissions.MANAGE_PC_TREE)

    # ---------------- Read (view) ----------------
    @bp.route("/api/pc-tree", methods=["GET"])
    @login_required
    @can_view
    def tree():
        return api.tree()

    # ---------------- Write (manage_pc_tree) ----------------
    @bp.route("/api/pc-tree/folders", methods=["POST"])
    @login_required
    @can_manage
    def create_folder():
        return api.create_folder()

    @bp.route("/api/pc-tree/folders/<int:folder_id>", methods=["PUT"])
    @login_required
    @can_manage
    def update_folder(folder_id):
        return api.update_folder(folder_id)

    @bp.route("/api/pc-tree/folders/<int:folder_id>", methods=["DELETE"])
    @login_required
    @can_manage
    def delete_folder(folder_id):
        return api.delete_folder(folder_id)

    @bp.route("/api/pc-tree/file", methods=["POST"])
    @login_required
    @can_manage
    def file_machines():
        return api.file_machines()

    # ---------------- Page ----------------
    @bp.route("/pc-tree", methods=["GET"])
    @login_required
    @can_view
    def pc_tree_page():
        return render_template("pc_tree.html")

    return bp
