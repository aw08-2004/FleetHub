"""The PC tree -- AD OUs and the hub's own folders, browsable like ADUC.

Until now a PC had no place to live. The Devices list is flat, a device group is a saved
filter rather than a container, and the only hierarchy anywhere was the AD OU string on
machine_info -- which exists only when AD sync is on, and only for domain-joined boxes. An IT
group whose fleet is organised in AD as ASUNCION / FILADELFIA / Cajas / Oficinas had to
re-type those OUs into every rule, and had nowhere at all to file the workgroup PCs AD never
sees.

This module is that place. The tree has two halves, shown side by side:

  * **The AD half is derived, read-only.** Built on every read from `machine_info.ad_ou`, so
    it is exactly the OU layout the last directory sync saw. It is never stored and cannot be
    edited here: AD is the source of truth for it, and a hub-side copy that drifted from AD
    would be two trees that disagree about where a PC is.
  * **The folder half is the hub's own.** Nested folders, created and filed by hand, for
    whatever AD does not express -- the workgroup boxes, a hub with no directory at all, or a
    grouping that cuts across OUs. A PC is in at most ONE folder. Allowing several would make
    "which folder is this PC in?" a list, and a folder-scoped permission grant then depends on
    every folder a PC was ever dropped into.

**Both halves resolve with containment, never string matching.** A folder covers the PCs
filed in it and in every folder beneath; an OU covers the PCs in it and in every OU beneath,
compared component-wise by directory.ou_contains. A string `endswith` would let
`OU=Cajas` capture `OU=NoCajas` -- ROADMAP calls that an access-control bug, and a folder
scope IS access control (permissions.SCOPE_FOLDER).

**Every write invalidates the permissions cache**, here in the model rather than in the HTTP
layer, for the reason permissions.py audits in its model: a folder-scoped group's machine list
is derived at cache-build time, and a move made by any caller -- the API, a test, a future
import -- must re-point it. The web layer additionally tells live sockets.

**Deleting a folder that is not empty, or that anything still names, is refused.** Moving its
PCs to the parent would be a scope change nobody made; deleting a folder a permission group or
a rule names would leave that group granting nothing and that rule matching nothing, silently.

Authorization lives upstream in pc_tree_web.py. Kept free of Flask so it can be unit-tested in
isolation, like every other model module here.
"""
import sqlite3
import time

import directory
import permissions

NAME_MAX = 80
DESCRIPTION_MAX = 500
# The same readability cap as permission groups (permissions.MAX_GROUP_DEPTH), and for the
# same reason: past it, nobody can say what a folder covers without drawing it.
MAX_FOLDER_DEPTH = 8

# Taking a PC out of every folder. Shared by filing, forget and rename, so all three agree on
# what "unfiled" means.
_UNFILE_SQL = "DELETE FROM pc_folder_machines WHERE machine = ?"


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_pc_tree_db(db_path):
    """Create the tables if absent. Idempotent, like every other init_*_db."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pc_folders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id   INTEGER,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL,
                updated_by  TEXT
            )
            """
        )
        # Unique among SIBLINGS, case-insensitively: two "Cajas" folders side by side is a
        # filing accident every time, while "Cajas" under both ASUNCION and FILADELFIA is the
        # whole point of a tree. COALESCE because SQLite treats every NULL as distinct, which
        # would let any number of identically named top-level folders through.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pc_folders_sibling_name "
            "ON pc_folders(COALESCE(parent_id, 0), name COLLATE NOCASE)"
        )
        # One row per filed PC -- the PRIMARY KEY on machine is what makes "a PC is in at
        # most one folder" a property of the table rather than of the code that writes it.
        # Not foreign-keyed to machine_info, like permission_group_machines: a PC can be
        # filed before it first reports, and deleting one is handled by forget_machine.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pc_folder_machines (
                machine   TEXT PRIMARY KEY,
                folder_id INTEGER NOT NULL,
                filed_at  INTEGER NOT NULL,
                filed_by  TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pc_folder_machines_folder "
            "ON pc_folder_machines(folder_id)"
        )


# ================================
# READS
# ================================
def list_folders(db_path):
    """Every folder, with `count` = PCs filed DIRECTLY in it. Ordered by name, so a caller
    building the tree gets siblings in display order for free."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM pc_folder_machines m WHERE m.folder_id = f.id)"
            " AS count FROM pc_folders f ORDER BY f.name COLLATE NOCASE, f.id").fetchall()
    return [dict(r) for r in rows]


def get_folder(db_path, folder_id):
    try:
        folder_id = int(folder_id)
    except (TypeError, ValueError):
        return None
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM pc_folders WHERE id = ?", (folder_id,)).fetchone()
    return dict(row) if row else None


def _parents(db_path):
    with get_conn(db_path) as conn:
        return {r["id"]: r["parent_id"]
                for r in conn.execute("SELECT id, parent_id FROM pc_folders")}


def ancestors(db_path, folder_id, parents=None):
    """The folders above `folder_id`, nearest first. Cut at a cycle -- the writes refuse to
    make one, so it can only come from a hand-edited row, and resolution must not loop."""
    parents = parents if parents is not None else _parents(db_path)
    chain, seen = [], {folder_id}
    current = parents.get(folder_id)
    while current is not None and current in parents and current not in seen:
        chain.append(current)
        seen.add(current)
        current = parents.get(current)
    return chain


def descendants(db_path, folder_ids, parents=None):
    """`folder_ids` plus every folder beneath any of them, as a set."""
    parents = parents if parents is not None else _parents(db_path)
    children = {}
    for fid, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(fid)
    found = {int(f) for f in folder_ids if int(f) in parents}
    frontier = list(found)
    while frontier:
        nxt = [c for f in frontier for c in children.get(f, ()) if c not in found]
        found.update(nxt)
        frontier = nxt
    return found


def assignments(db_path):
    """{machine: folder_id} for every filed PC."""
    with get_conn(db_path) as conn:
        return {r["machine"]: r["folder_id"]
                for r in conn.execute("SELECT machine, folder_id FROM pc_folder_machines")}


def folder_of(db_path, machine):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT folder_id FROM pc_folder_machines WHERE machine = ?",
                           (str(machine or "").strip(),)).fetchone()
    return row["folder_id"] if row else None


def machines_in_folders(db_path, folder_ids, include_children=True):
    """Every PC filed in any of `folder_ids` (and, by default, in any folder beneath),
    sorted. This is what permissions.SCOPE_FOLDER and the rules `folder` selector resolve
    through, so both agree on what a folder covers."""
    wanted = [int(f) for f in (folder_ids or [])]
    if not wanted:
        return []
    ids = descendants(db_path, wanted) if include_children else set(wanted)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT machine FROM pc_folder_machines WHERE folder_id IN ({marks})",
            tuple(ids)).fetchall()
    return sorted(r["machine"] for r in rows)


def member_sets(db_path, selectors):
    """{(folder_id, include_children): {lowercased machine names}} for rules.resolve_targets.
    A folder that no longer exists maps to an empty set, so a stale selector matches nothing
    rather than raising inside a rule tick -- the same contract device_groups.member_sets
    keeps."""
    out = {}
    for folder_id, include_children in selectors:
        key = (int(folder_id), bool(include_children))
        if key not in out:
            out[key] = {m.lower() for m in machines_in_folders(
                db_path, [key[0]], include_children=key[1])}
    return out


def _ou_name(components):
    """How a node is labelled: a domain in DNS form (DC=ccmf,DC=com,DC=py -> ccmf.com.py),
    the way ADUC shows it, and an OU by its own name, unescaped enough to read."""
    if all(c.lower().startswith("dc=") for c in components):
        return ".".join(c.split("=", 1)[-1] for c in components)
    return components[0].split("=", 1)[-1].replace("\\,", ",")


class _OuTreeBuilder:
    """Nodes keyed on the case-folded DN, so two spellings of one OU land in one node."""

    def __init__(self):
        self.roots = {}
        self.nodes = {}

    def node_for(self, components):
        key = ",".join(c.casefold() for c in components)
        if key in self.nodes:
            return self.nodes[key]
        node = {"dn": ",".join(components), "name": _ou_name(components),
                "machines": [], "children": []}
        self.nodes[key] = node
        # A domain is a root; so is a DN whose domain part is not purely trailing DC=
        # components, which is simply rooted at its last component.
        if len(components) == 1 or all(c.lower().startswith("dc=") for c in components):
            self.roots[key] = node
        else:
            self.node_for(components[1:])["children"].append(node)
        return node


def _finish(node):
    node["machines"].sort(key=str.lower)
    node["children"].sort(key=lambda n: n["name"].lower())
    for child in node["children"]:
        _finish(child)
    node["count"] = len(node["machines"]) + sum(c["count"] for c in node["children"])
    return node


def ou_tree(rows):
    """The AD half of the tree, built from `rows` of {machine, ad_ou}.

    Pure, so the web layer can hand it rows already narrowed to the caller's scope -- an OU
    that holds none of your PCs then never appears, and its name is not disclosed.

    Returns a list of root nodes, one per domain, each
    {"dn", "name", "machines": [direct PCs], "children": [...], "count"}. Display case is the
    first spelling seen.
    """
    builder = _OuTreeBuilder()
    for row in rows:
        components = directory.split_dn(str(row.get("ad_ou") or "").strip())
        if components:
            builder.node_for(components)["machines"].append(row["machine"])
    return sorted((_finish(n) for n in builder.roots.values()), key=lambda n: n["name"].lower())


# ================================
# VALIDATION
# ================================
def _validate_name(name):
    text = str(name or "").strip()
    if not text:
        raise ValueError("A folder needs a name.")
    if len(text) > NAME_MAX:
        raise ValueError(f"A folder name is limited to {NAME_MAX} characters.")
    # A slash is how the console writes a folder's path ("FILADELFIA / Cajas"); a name
    # containing one would make two different folders print as the same path.
    if "/" in text or "\\" in text:
        raise ValueError("A folder name cannot contain / or \\.")
    return text


def _validate_description(description):
    text = str(description or "").strip()
    if len(text) > DESCRIPTION_MAX:
        raise ValueError(f"A description is limited to {DESCRIPTION_MAX} characters.")
    return text


def _validate_parent(db_path, folder_id, parent_id):
    """None/"" is top level. Refuses a missing parent, the folder itself, anything beneath
    it, and a placement that pushes the moved subtree past MAX_FOLDER_DEPTH."""
    if parent_id in (None, ""):
        return None
    try:
        parent_id = int(parent_id)
    except (TypeError, ValueError):
        raise ValueError("That parent folder does not exist.")
    parents = _parents(db_path)
    if parent_id not in parents:
        raise ValueError("That parent folder does not exist.")
    if folder_id is not None:
        if parent_id == folder_id:
            raise ValueError("A folder cannot be inside itself.")
        if parent_id in descendants(db_path, [folder_id], parents):
            raise ValueError("A folder cannot be moved into one of its own subfolders.")
    parent_depth = len(ancestors(db_path, parent_id, parents)) + 1
    height = 1
    if folder_id is not None:
        # Depth of the deepest folder in the subtree being moved, relative to its root.
        sub = descendants(db_path, [folder_id], parents)
        height = 1 + max((len(ancestors(db_path, f, parents))
                          - len(ancestors(db_path, folder_id, parents)) for f in sub),
                         default=0)
    if parent_depth + height > MAX_FOLDER_DEPTH:
        raise ValueError(f"Folders can nest at most {MAX_FOLDER_DEPTH} levels deep.")
    return parent_id


# ================================
# WRITES
# ================================
def create_folder(db_path, name, parent_id=None, description="", actor=None):
    """Create a folder; returns it. Raises ValueError on bad input or a sibling name clash."""
    name = _validate_name(name)
    description = _validate_description(description)
    parent_id = _validate_parent(db_path, None, parent_id)
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO pc_folders (parent_id, name, description, created_at, updated_at,"
                " updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                (parent_id, name, description, now, now, actor))
            folder_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"A folder named {name!r} already exists here.")
    # A new folder covers no PCs, but a group may already name its id if one was deleted and
    # its row re-used -- cheap enough to just be right.
    permissions.invalidate()
    return get_folder(db_path, folder_id)


def update_folder(db_path, folder_id, name=None, parent_id=None, description=None,
                  move=False, actor=None):
    """Rename, re-describe and/or move a folder. `parent_id` is only read when `move` is
    True, because "top level" (None) is a real destination and cannot double as "unchanged".
    Raises KeyError for an unknown folder, ValueError on bad input."""
    before = get_folder(db_path, folder_id)
    if before is None:
        raise KeyError(folder_id)
    new_name = _validate_name(name) if name is not None else before["name"]
    new_description = (_validate_description(description) if description is not None
                       else before["description"])
    new_parent = (_validate_parent(db_path, before["id"], parent_id) if move
                  else before["parent_id"])
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE pc_folders SET name = ?, description = ?, parent_id = ?,"
                " updated_at = ?, updated_by = ? WHERE id = ?",
                (new_name, new_description, new_parent, now, actor, before["id"]))
    except sqlite3.IntegrityError:
        raise ValueError(f"A folder named {new_name!r} already exists there.")
    if new_parent != before["parent_id"]:
        # Moving a folder moves every PC under it in and out of folder-scoped groups.
        permissions.invalidate()
    return get_folder(db_path, before["id"])


def delete_folder(db_path, folder_id, in_use=None):
    """Delete an EMPTY folder that nothing names. Returns the deleted folder.

    `in_use` is the list of {name} things still naming it (rules, device groups, permission
    groups), supplied by the caller so the refusal can say which. Raises KeyError for an
    unknown folder and ValueError for a refusal.
    """
    folder = get_folder(db_path, folder_id)
    if folder is None:
        raise KeyError(folder_id)
    with get_conn(db_path) as conn:
        subfolders = conn.execute("SELECT COUNT(*) FROM pc_folders WHERE parent_id = ?",
                                  (folder["id"],)).fetchone()[0]
        filed = conn.execute("SELECT COUNT(*) FROM pc_folder_machines WHERE folder_id = ?",
                             (folder["id"],)).fetchone()[0]
    if subfolders:
        raise ValueError(f"{folder['name']} still has subfolders. Move or delete them first.")
    if filed:
        raise ValueError(f"{folder['name']} still holds {filed} PC(s). Move them first.")
    if in_use:
        names = ", ".join(u["name"] for u in in_use[:5])
        more = f" (and {len(in_use) - 5} more)" if len(in_use) > 5 else ""
        raise ValueError(f"{folder['name']} is still used by: {names}{more}")
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM pc_folders WHERE id = ?", (folder["id"],))
    permissions.invalidate()
    return folder


def _unique_names(machines):
    names = []
    for machine in machines or []:
        name = str(machine or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _file_one(conn, name, folder_id, now, actor):
    if folder_id is None:
        conn.execute(_UNFILE_SQL, (name,))
    else:
        conn.execute(
            "INSERT INTO pc_folder_machines (machine, folder_id, filed_at, filed_by)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(machine) DO UPDATE SET"
            " folder_id = excluded.folder_id, filed_at = excluded.filed_at,"
            " filed_by = excluded.filed_by",
            (name, folder_id, now, actor))


def file_machines(db_path, machines, folder_id, actor=None):
    """File `machines` into `folder_id`, or take them out of any folder when it is None.
    Returns {machine: previous folder id or None} for the PCs that actually moved, so the
    caller can audit where each came from. Raises ValueError for an unknown folder."""
    if folder_id is not None and get_folder(db_path, folder_id) is None:
        raise ValueError("That folder does not exist.")
    folder_id = int(folder_id) if folder_id is not None else None
    before = assignments(db_path)
    moved = {name: before.get(name) for name in _unique_names(machines)
             if before.get(name) != folder_id}
    now = int(time.time())
    with get_conn(db_path) as conn:
        for name in moved:
            _file_one(conn, name, folder_id, now, actor)
    if moved:
        permissions.invalidate()
    return moved


def forget_machine(db_path, machine):
    """Unfile a hard-deleted PC, so a hostname later reused by a different box does not
    silently land in the old box's folder -- and in every folder-scoped group's reach."""
    with get_conn(db_path) as conn:
        removed = conn.execute(_UNFILE_SQL,
                               (str(machine or "").strip(),)).rowcount or 0
    if removed:
        permissions.invalidate()
    return removed


def rename_machine(db_path, old_machine, new_machine):
    """Keep a merged PC in its folder: the survivor is the same box (see
    permissions.rename_machine, whose reasoning this follows). If the survivor is already
    filed, its own filing wins -- it is the record still reporting."""
    old_machine = str(old_machine or "").strip()
    new_machine = str(new_machine or "").strip()
    if not old_machine or not new_machine or old_machine == new_machine:
        return 0
    with get_conn(db_path) as conn:
        moved = conn.execute(
            "UPDATE OR IGNORE pc_folder_machines SET machine = ? WHERE machine = ?",
            (new_machine, old_machine)).rowcount or 0
        conn.execute(_UNFILE_SQL, (old_machine,))
    permissions.invalidate()
    return moved
