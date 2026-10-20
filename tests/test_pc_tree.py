"""pc_tree.py -- hub folders and the derived AD OU tree.

The silent failures this file exists to catch:

  * **A folder scope that does not follow the tree.** A permission group scoped to a folder
    must reach the PCs filed beneath it the moment they are filed, and lose them the moment
    they leave -- a stale permissions cache here is access granted or withheld with nothing on
    screen to say so.
  * **Sibling-name collisions and lookalike containment.** "Cajas" under two parents is the
    point of a tree; two "Cajas" side by side is an accident. And `OU=Cajas` must never
    capture `OU=NoCajas`, in targeting as in scoping (rules' OU selector was a string
    endswith until hub 1.159.0).
  * **A folder deleted out from under something that names it.** A permission group or a rule
    aimed at a vanished folder grants or matches nothing, silently.
  * **A reused hostname landing in the old box's folder**, and through it in every
    folder-scoped group.
  * **An OU tree that discloses OUs.** ou_tree is pure, and is only ever handed rows already in
    the caller's scope; an OU without such a row must not appear.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import pc_tree
import permissions
import rules

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def refused(fn, needle=""):
    try:
        fn()
    except ValueError as e:
        return needle.lower() in str(e).lower()
    return False


ROWS = [
    {"machine": "CAJA-1", "ad_ou": "OU=Cajas,OU=Usuarios,OU=FILADELFIA,DC=ccmf,DC=com,DC=py"},
    {"machine": "CAJA-2", "ad_ou": "OU=RDP,OU=Cajas,OU=Usuarios,OU=FILADELFIA,DC=ccmf,DC=com,DC=py"},
    {"machine": "NOCAJA", "ad_ou": "OU=NoCajas,OU=Usuarios,OU=FILADELFIA,DC=ccmf,DC=com,DC=py"},
    {"machine": "ASU-1", "ad_ou": "OU=ASUNCION,DC=ccmf,DC=com,DC=py"},
    {"machine": "WG-1", "ad_ou": None},
]


def main():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, ad_ou TEXT, ad_dn TEXT)")
            conn.executemany("INSERT INTO machine_info (machine, ad_ou) VALUES (?, ?)",
                             [(r["machine"], r["ad_ou"]) for r in ROWS])
        fleet.init_fleet_db(db)
        permissions.init_permissions_db(db)
        pc_tree.init_pc_tree_db(db)
        pc_tree.init_pc_tree_db(db)          # idempotent
        rules.init_rules_db(db)
        permissions.invalidate()

        print("\n== Folders nest, and sibling names are unique ==")
        fil = pc_tree.create_folder(db, "FILADELFIA", actor="t")
        asu = pc_tree.create_folder(db, "ASUNCION", actor="t")
        cajas = pc_tree.create_folder(db, "Cajas", parent_id=fil["id"], actor="t")
        rdp = pc_tree.create_folder(db, "RDP", parent_id=cajas["id"], actor="t")
        check("the same name under two parents is fine",
              pc_tree.create_folder(db, "Cajas", parent_id=asu["id"], actor="t")["parent_id"] == asu["id"])
        check("a duplicate sibling is refused, ignoring case",
              refused(lambda: pc_tree.create_folder(db, "cajas", parent_id=fil["id"]), "already exists"))
        check("a duplicate TOP-LEVEL name is refused too (NULL parents compare equal here)",
              refused(lambda: pc_tree.create_folder(db, "filadelfia"), "already exists"))
        check("a slash in a name is refused -- it is the path separator",
              refused(lambda: pc_tree.create_folder(db, "A/B"), "cannot contain"))
        check("an empty name is refused", refused(lambda: pc_tree.create_folder(db, "  "), "name"))

        print("\n== Moves cannot make a cycle or exceed the depth cap ==")
        check("a folder cannot move into itself",
              refused(lambda: pc_tree.update_folder(db, cajas["id"], parent_id=cajas["id"], move=True),
                      "inside itself"))
        check("...nor into its own subfolder",
              refused(lambda: pc_tree.update_folder(db, cajas["id"], parent_id=rdp["id"], move=True),
                      "own subfolders"))
        parent = rdp["id"]
        depth = 3
        while depth < pc_tree.MAX_FOLDER_DEPTH:
            parent = pc_tree.create_folder(db, f"L{depth + 1}", parent_id=parent)["id"]
            depth += 1
        check("one level past the cap is refused",
              refused(lambda: pc_tree.create_folder(db, "Too deep", parent_id=parent), "levels deep"))
        check("moving a subtree past the cap is refused",
              refused(lambda: pc_tree.update_folder(db, fil["id"], parent_id=asu["id"], move=True),
                      "levels deep"))
        renamed = pc_tree.update_folder(db, asu["id"], name="Asunción")
        check("a rename without move=True leaves the parent alone",
              renamed["name"] == "Asunción" and renamed["parent_id"] is None)

        print("\n== Filing: one folder per PC, subtree resolution ==")
        moved = pc_tree.file_machines(db, ["CAJA-1", "CAJA-2"], cajas["id"], actor="t")
        check("filing reports where each PC came from", moved == {"CAJA-1": None, "CAJA-2": None})
        pc_tree.file_machines(db, ["CAJA-2"], rdp["id"], actor="t")
        check("re-filing moves the PC rather than adding a second folder",
              pc_tree.folder_of(db, "CAJA-2") == rdp["id"]
              and len([m for m, f in pc_tree.assignments(db).items() if m == "CAJA-2"]) == 1)
        check("a folder covers PCs filed beneath it",
              pc_tree.machines_in_folders(db, [fil["id"]]) == ["CAJA-1", "CAJA-2"])
        check("...unless asked for that folder alone",
              pc_tree.machines_in_folders(db, [cajas["id"]], include_children=False) == ["CAJA-1"])
        check("refiling into the same folder moves nothing",
              pc_tree.file_machines(db, ["CAJA-1"], cajas["id"]) == {})
        check("filing into a missing folder is refused",
              refused(lambda: pc_tree.file_machines(db, ["WG-1"], 99999), "does not exist"))

        print("\n== A folder-scoped permission group follows the tree ==")
        group = permissions.create_group(db, name="Cajas team", capabilities=[permissions.VIEW],
                                         scope_mode=permissions.SCOPE_FOLDER,
                                         folders=[cajas["id"]], members=["cajero@x.com"])
        check("the group resolves to the folder's subtree",
              permissions.effective_permissions(db, "cajero@x.com")["machines"] == {"CAJA-1", "CAJA-2"})
        pc_tree.file_machines(db, ["WG-1"], rdp["id"], actor="t")
        check("filing a PC beneath the folder grants it at once (cache invalidated)",
              "WG-1" in permissions.effective_permissions(db, "cajero@x.com")["machines"])
        pc_tree.file_machines(db, ["WG-1"], None, actor="t")
        check("taking it out of every folder revokes it at once",
              "WG-1" not in permissions.effective_permissions(db, "cajero@x.com")["machines"])
        pc_tree.update_folder(db, rdp["id"], parent_id=None, move=True)
        check("moving a subfolder out of the scoped folder takes its PCs with it",
              permissions.effective_permissions(db, "cajero@x.com")["machines"] == {"CAJA-1"})
        pc_tree.update_folder(db, rdp["id"], parent_id=cajas["id"], move=True)
        check("an unknown folder id grants nothing, never everything",
              permissions.get_group(db, permissions.create_group(
                  db, name="Ghost folder", scope_mode=permissions.SCOPE_FOLDER,
                  folders=[424242]))["machines"] == [])

        print("\n== The rules `folder` selector resolves the same way ==")
        err, target = rules.validate_target({"include": [{"kind": "folder", "folder_id": cajas["id"]}]})
        check("a folder selector validates, defaulting include_children on",
              err is None and target["include"][0]["include_children"] is True)
        err, _ = rules.validate_target({"include": [{"kind": "folder", "folder_id": "x"}]})
        check("a folder selector without a folder is refused", err is not None)
        check("it reaches the subtree",
              rules.resolve_targets(db, target) == ["CAJA-1", "CAJA-2"])
        check("an exclusion by folder wins over an include",
              rules.resolve_targets(db, {"include": [{"kind": "all"}],
                                         "exclude": [{"kind": "folder", "folder_id": fil["id"]}]})
              == ["ASU-1", "NOCAJA", "WG-1"])
        check("targets_name_folder finds it",
              rules.targets_name_folder(target, [cajas["id"]])
              and not rules.targets_name_folder(target, [fil["id"]]))

        print("\n== The rules OU selector is component-wise now ==")
        ou = "OU=Cajas,OU=Usuarios,OU=FILADELFIA,DC=ccmf,DC=com,DC=py"
        reached = rules.resolve_targets(db, {"include": [{"kind": "ad_ou", "ou": ou}]})
        check("OU=Cajas does not capture OU=NoCajas", "NOCAJA" not in reached)
        check("...and does include its sub-OU", reached == ["CAJA-1", "CAJA-2"])
        exact = rules.resolve_targets(db, {"include": [{"kind": "ad_ou", "ou": ou.lower(),
                                                         "include_children": False}]})
        check("an exact OU match ignores case and excludes sub-OUs", exact == ["CAJA-1"])

        print("\n== Deleting a folder is refused while anything still needs it ==")
        check("a folder with subfolders is refused",
              refused(lambda: pc_tree.delete_folder(db, cajas["id"]), "subfolders"))
        leaf = pc_tree.create_folder(db, "Leaf", actor="t")
        pc_tree.file_machines(db, ["WG-1"], leaf["id"])
        check("a folder holding PCs is refused",
              refused(lambda: pc_tree.delete_folder(db, leaf["id"]), "holds"))
        pc_tree.file_machines(db, ["WG-1"], None)
        spare = pc_tree.create_folder(db, "Spare", actor="t")
        check("a folder something names is refused, naming it",
              refused(lambda: pc_tree.delete_folder(db, spare["id"], in_use=[{"name": "rule X"}]),
                      "rule X"))
        check("groups_using_folder finds a scoped permission group",
              [g["name"] for g in permissions.groups_using_folder(db, [cajas["id"]])] == ["Cajas team"])
        pc_tree.delete_folder(db, spare["id"])
        check("an empty, unused folder deletes", pc_tree.get_folder(db, spare["id"]) is None)

        print("\n== Machine lifecycle ==")
        pc_tree.rename_machine(db, "CAJA-1", "CAJA-1-NEW")
        check("a merge keeps the survivor in the folder", pc_tree.folder_of(db, "CAJA-1-NEW") == cajas["id"])
        check("...and the group's scope follows",
              "CAJA-1-NEW" in permissions.effective_permissions(db, "cajero@x.com")["machines"])
        pc_tree.forget_machine(db, "CAJA-1-NEW")
        check("a deleted PC is unfiled", pc_tree.folder_of(db, "CAJA-1-NEW") is None)
        check("...and leaves the group's scope",
              "CAJA-1-NEW" not in permissions.effective_permissions(db, "cajero@x.com")["machines"])

        print("\n== The AD tree ==")
        tree = pc_tree.ou_tree(ROWS)
        check("one root per domain, named the way ADUC shows it",
              [n["name"] for n in tree] == ["ccmf.com.py"])
        root = tree[0]
        check("OUs nest under their parents",
              sorted(c["name"] for c in root["children"]) == ["ASUNCION", "FILADELFIA"])
        fil_node = next(c for c in root["children"] if c["name"] == "FILADELFIA")
        check("the count includes every PC beneath", fil_node["count"] == 3 and root["count"] == 4)
        check("a PC with no OU is not in the AD tree", "WG-1" not in str(tree))
        narrowed = pc_tree.ou_tree([r for r in ROWS if r["machine"] == "ASU-1"])
        check("an OU with none of the caller's PCs does not appear",
              "FILADELFIA" not in str(narrowed))
        escaped = pc_tree.ou_tree([{"machine": "X", "ad_ou": "OU=Sales\\, EMEA,DC=corp"}])
        check("an escaped comma is one OU, shown unescaped",
              escaped[0]["children"][0]["name"] == "Sales, EMEA")
    finally:
        try:
            os.remove(db)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
