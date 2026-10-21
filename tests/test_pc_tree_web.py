"""The PC tree and nested groups over HTTP: the gates, scope on every filing, narrowed reads.

The silent failures this file exists to catch:

  * **Filing as a grant past the filer's scope.** A folder is a permission scope, so dropping a
    PC into one hands it to every group scoped there. An operator may only file PCs they hold,
    and may only move a folder whose every PC they hold.
  * **A viewer reading OUs and filings outside their scope.** The tree is narrowed server-side;
    an OU holding none of their PCs must not appear in the response at all.
  * **Write routes that only check `view`.** The page hides its controls, and the API must
    refuse anyway.
  * **A folder deleted while a rule or a permission group names it.** 409, naming them --
    including a rule that only FILES into it (move_to_folder, hub 1.162.0).
  * **A rule that files PCs, saved by someone who may not file them.** Filing is a grant, so
    the rules API needs manage_pc_tree for it exactly as /api/pc-tree/file does.
  * **The group editor unable to say "move this group to the top level".** JSON null and ""
    for parent_id both mean top level; an absent key means unchanged.

Run from the repo root so `import app` resolves.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-pc-tree-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@example.com"

import app
import pc_tree
import permissions

PASS = 0
FAIL = 0
IFRAME = {"Sec-Fetch-Dest": "iframe"}
CSRF_TOKEN = "pc-tree-test-token"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def client_for(email):
    """A signed-in client sending the CSRF header, as common.js does in a browser -- without it
    every write is a 403 for the wrong reason (see test_device_groups_web.client_for)."""
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = {"email": email}
        sess["csrf_token"] = CSRF_TOKEN
    c.environ_base["HTTP_X_CSRF_TOKEN"] = CSRF_TOKEN
    return c


def setup():
    with sqlite3.connect(app.DB_PATH) as conn:
        for name, ou in (("PC-1", "OU=Cajas,DC=corp"), ("PC-2", "OU=Secret,DC=corp"),
                         ("PC-3", None)):
            conn.execute("INSERT OR IGNORE INTO machine_info (machine) VALUES (?)", (name,))
            conn.execute("UPDATE machine_info SET ad_ou = ? WHERE machine = ?", (ou, name))
    permissions.invalidate()
    permissions.create_group(
        app.DB_PATH, name="Viewers", capabilities=[permissions.VIEW],
        machines=["PC-1"], members=["viewer@example.com"], actor="root@example.com")
    permissions.create_group(
        app.DB_PATH, name="Filers", capabilities=[permissions.VIEW, permissions.MANAGE_PC_TREE],
        machines=["PC-1", "PC-3"], members=["filer@example.com"], actor="root@example.com")
    permissions.create_group(
        app.DB_PATH, name="Rule writers", capabilities=[permissions.VIEW, permissions.MANAGE_RULES],
        machines=["PC-1"], members=["ruler@example.com"], actor="root@example.com")


def test_gates_and_reads():
    print("\n-- view reads (narrowed), manage_pc_tree writes --")
    viewer = client_for("viewer@example.com")
    r = viewer.get("/api/pc-tree")
    check("a viewer reads the tree", r.status_code == 200)
    body = r.get_json()
    check("...seeing only their PCs", body["machines"] == ["PC-1"])
    check("...and only the OUs those PCs are in", "Secret" not in str(body["ous"])
          and "Cajas" in str(body["ous"]))
    check("...and is told they cannot manage", body["can_manage"] is False)
    r = viewer.post("/api/pc-tree/folders", json={"name": "Nope"})
    check("a viewer cannot create a folder", r.status_code == 403)
    r = viewer.post("/api/pc-tree/file", json={"machines": ["PC-1"], "folder_id": None})
    check("a viewer cannot file a PC", r.status_code == 403)
    page = viewer.get("/pc-tree", headers=IFRAME).get_data(as_text=True)
    check("a viewer gets the page without its write controls",
          'id="pc-tree-root"' in page and 'data-can-manage="0"' in page
          and 'id="pc-folder-dialog"' not in page)
    page = client_for("filer@example.com").get("/pc-tree", headers=IFRAME).get_data(as_text=True)
    check("a filer gets the folder editor", 'id="pc-folder-dialog"' in page)
    shell = viewer.get("/", headers={"Sec-Fetch-Dest": "document"}).get_data(as_text=True)
    check("the sidebar links the page", 'data-nav-prefix="/pc-tree"' in shell)


def test_filing_scope():
    print("\n-- filing needs the PC in the filer's scope --")
    filer = client_for("filer@example.com")
    r = filer.post("/api/pc-tree/folders", json={"name": "Cajas"})
    check("a filer creates a folder", r.status_code == 201)
    cajas = r.get_json()
    r = filer.post("/api/pc-tree/folders", json={"name": "Sub", "parent_id": cajas["id"]})
    check("...and a subfolder", r.status_code == 201 and r.get_json()["parent_id"] == cajas["id"])
    sub = r.get_json()
    r = filer.post("/api/pc-tree/file", json={"machines": ["PC-1"], "folder_id": sub["id"]})
    check("a filer files a PC they hold", r.status_code == 200 and r.get_json()["moved"] == ["PC-1"])
    r = filer.post("/api/pc-tree/file", json={"machines": ["PC-2"], "folder_id": cajas["id"]})
    check("...but not one outside their scope", r.status_code == 403
          and "outside your access" in r.get_json()["error"])
    check("...and nothing was filed", pc_tree.folder_of(app.DB_PATH, "PC-2") is None)
    r = filer.post("/api/pc-tree/file", json={"machines": [], "folder_id": cajas["id"]})
    check("an empty batch is refused", r.status_code == 400)

    root = client_for("root@example.com")
    r = root.post("/api/pc-tree/file", json={"machines": ["PC-2"], "folder_id": sub["id"]})
    check("a superuser files any PC", r.status_code == 200)
    r = filer.put(f"/api/pc-tree/folders/{sub['id']}", json={"name": "Sub", "parent_id": None})
    check("a filer cannot MOVE a folder holding a PC outside their scope", r.status_code == 403)
    r = filer.put(f"/api/pc-tree/folders/{sub['id']}", json={"name": "Renamed", "parent_id": cajas["id"]})
    check("...but can rename it in place", r.status_code == 200 and r.get_json()["name"] == "Renamed")
    return cajas, sub


def test_folder_scope_and_delete(cajas, sub):
    print("\n-- a folder-scoped group follows the tree; deletes are refused while named --")
    root = client_for("root@example.com")
    r = root.post("/api/permissions/groups", json={
        "name": "Cajas operators", "capabilities": ["view"], "scope_mode": "folder",
        "folders": [cajas["id"]], "members": ["cajero@example.com"]})
    check("a folder-scoped permission group is created over the API", r.status_code == 201)
    group = r.get_json()
    check("...resolving to the PCs beneath the folder",
          sorted(group["machines"]) == ["PC-1", "PC-2"])
    cajero = client_for("cajero@example.com")
    check("its member sees those PCs",
          sorted(cajero.get("/api/pc-tree").get_json()["machines"]) == ["PC-1", "PC-2"])

    r = root.post("/api/rules", json={
        "name": "Folder rule", "condition_text": "sys.uptime_days > 7",
        "target": {"include": [{"kind": "folder", "folder_id": sub["id"]}]},
        "actions": [{"type": "alert", "params": {"text": "{{sys.machine}} needs a restart"}}],
        "for_seconds": 0, "cooldown_seconds": 0})
    rule_ok = r.status_code == 201
    root.post("/api/pc-tree/file", json={"machines": ["PC-1", "PC-2"], "folder_id": None})
    r = root.delete(f"/api/pc-tree/folders/{cajas['id']}")
    check("deleting a folder with subfolders is a 409", r.status_code == 409)
    if rule_ok:
        r = root.delete(f"/api/pc-tree/folders/{sub['id']}")
        check("deleting a folder a rule names is a 409 naming it",
              r.status_code == 409 and "Folder rule" in r.get_json()["error"])
    else:
        check("a rule aimed at a folder can be saved", False)
    r = root.delete(f"/api/pc-tree/folders/{cajas['id']}")
    with sqlite3.connect(app.DB_PATH) as conn:
        conn.execute("DELETE FROM rules")
    r = root.delete(f"/api/pc-tree/folders/{sub['id']}")
    check("once nothing names it, an empty folder deletes", r.status_code == 200)
    r = root.delete(f"/api/pc-tree/folders/{cajas['id']}")
    check("a folder a permission group scopes to is a 409 naming the group",
          r.status_code == 409 and "Cajas operators" in r.get_json()["error"])
    root.put(f"/api/permissions/groups/{group['id']}", json={"scope_mode": "list"})
    r = root.delete(f"/api/pc-tree/folders/{cajas['id']}")
    check("...and deletes once that group no longer scopes by folder", r.status_code == 200)


def test_move_action_over_http():
    print("\n-- a rule that files PCs needs manage_pc_tree, and pins its folder --")
    root = client_for("root@example.com")
    folder = root.post("/api/pc-tree/folders", json={"name": "Win11"}).get_json()
    rule = {"name": "File Win11", "condition_text": "sys.uptime_days > 7",
            "target": {"include": [{"kind": "machines", "machines": ["PC-1"]}]},
            "actions": [{"type": "move_to_folder", "params": {"folder_id": folder["id"]}}],
            "for_seconds": 0, "cooldown_seconds": 0}
    r = client_for("ruler@example.com").post("/api/rules", json=rule)
    check("manage_rules alone cannot save a rule that files PCs",
          r.status_code == 400 and "permission" in r.get_json()["error"])
    r = root.post("/api/rules", json=rule)
    check("an operator who may file PCs can", r.status_code == 201)
    r = root.delete(f"/api/pc-tree/folders/{folder['id']}")
    check("deleting the folder that rule files into is a 409 naming it",
          r.status_code == 409 and "File Win11" in r.get_json()["error"])
    with sqlite3.connect(app.DB_PATH) as conn:
        conn.execute("DELETE FROM rules")
    r = root.delete(f"/api/pc-tree/folders/{folder['id']}")
    check("...and deletes once no rule files into it", r.status_code == 200)


def test_group_parent_over_http():
    print("\n-- the group API reads parent_id by key --")
    root = client_for("root@example.com")
    parent = root.post("/api/permissions/groups", json={"name": "Parent", "capabilities": ["view"]}).get_json()
    child = root.post("/api/permissions/groups",
                      json={"name": "Child", "parent_id": parent["id"]}).get_json()
    check("a child is created beneath its parent", child["parent_id"] == parent["id"])
    check("...and reports what it inherits", child["inherited_capabilities"] == ["view"])
    r = root.put(f"/api/permissions/groups/{child['id']}", json={"description": "x"})
    check("an absent parent_id leaves the parent alone", r.get_json()["parent_id"] == parent["id"])
    r = root.delete(f"/api/permissions/groups/{parent['id']}")
    check("deleting a group with subgroups is a 409", r.status_code == 409
          and "Child" in r.get_json()["error"])
    r = root.put(f"/api/permissions/groups/{child['id']}", json={"parent_id": None})
    check("JSON null moves it to the top level", r.get_json()["parent_id"] is None)
    r = root.put(f"/api/permissions/groups/{child['id']}", json={"parent_id": child["id"]})
    check("a group cannot be its own parent over the API either", r.status_code == 400)


def main():
    setup()
    test_gates_and_reads()
    cajas, sub = test_filing_scope()
    test_folder_scope_and_delete(cajas, sub)
    test_move_action_over_http()
    test_group_parent_over_http()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
