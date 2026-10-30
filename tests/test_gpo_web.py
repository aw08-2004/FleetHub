"""Group Policy over HTTP and on the heartbeat (roadmap #32).

The silent failures this file exists to catch:

  * **A write that reaches past the writer's scope.** Linking at a node, blocking inheritance
    and editing a linked policy each reach every PC beneath; a site-scoped operator must not be
    able to aim a policy at the whole fleet.
  * **A policy that runs code written without `issue_commands`.**
  * **A library script edit that re-runs as SYSTEM wherever a policy reaches**, made by
    someone whose scope covers none of those PCs, and audited as if no policy used it.
  * **A `view`-only route that writes**, and a resultant set readable for a PC outside scope.
  * **The heartbeat not carrying the result**: config overrides that never reach the agent, a
    registry document sent to an agent that never asked for one, or none sent to one that did.
  * **A template upload taken from anyone**, or a set list that tells a scoped operator which
    Windows builds run on PCs outside their scope.

Run from the repo root so `import app` resolves.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-gpo-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "root@example.com"

import admx
import app
import fleet
import gpo
import permissions
import scripts

PASS = 0
FAIL = 0
IFRAME = {"Sec-Fetch-Dest": "iframe"}
CSRF_TOKEN = "gpo-test-token"
FIL = "OU=FILADELFIA,DC=corp"
ASU = "OU=ASUNCION,DC=corp"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def client_for(email):
    """A signed-in client sending the CSRF header (see test_device_groups_web.client_for)."""
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = {"email": email}
        sess["csrf_token"] = CSRF_TOKEN
    c.environ_base["HTTP_X_CSRF_TOKEN"] = CSRF_TOKEN
    return c


def setup():
    with sqlite3.connect(app.DB_PATH) as conn:
        for name, ou in (("FIL-1", FIL), ("ASU-1", ASU)):
            conn.execute("INSERT OR IGNORE INTO machine_info (machine) VALUES (?)", (name,))
            conn.execute("UPDATE machine_info SET ad_ou = ? WHERE machine = ?", (ou, name))
        conn.execute(
            "INSERT OR IGNORE INTO scripts (name, label, description, shell, body, inputs_json,"
            " timeout_seconds, enabled, created_at, created_by, updated_at, updated_by)"
            " VALUES ('setup', 'Setup', '', 'powershell', 'Write-Output hi', '[]', 60, 1, 0,"
            " 't', 0, 't')")
    permissions.invalidate()
    gpo.invalidate()
    permissions.create_group(
        app.DB_PATH, name="Viewers", capabilities=[permissions.VIEW],
        machines=["FIL-1"], members=["viewer@example.com"], actor="root@example.com")
    permissions.create_group(
        app.DB_PATH, name="Filadelfia coders",
        capabilities=[permissions.VIEW, permissions.ISSUE_COMMANDS],
        scope_mode=permissions.SCOPE_AD_OU, ous=[FIL], members=["coder@example.com"],
        actor="root@example.com")
    permissions.create_group(
        app.DB_PATH, name="Filadelfia policy",
        capabilities=[permissions.VIEW, permissions.MANAGE_GROUP_POLICY],
        scope_mode=permissions.SCOPE_AD_OU, ous=[FIL], members=["fil@example.com"],
        actor="root@example.com")


def body(name, **settings_):
    return {"name": name, "settings": {"agent_settings": settings_.get("agent", {}),
                                       "registry": settings_.get("registry", []),
                                       "scripts": settings_.get("scripts", [])}}


EDGE = {"path": "SOFTWARE\\Policies\\Microsoft\\Edge", "name": "HideFirstRunExperience",
        "type": "dword", "value": 1}


def test_gates():
    print("\n-- view reads, manage_group_policy writes --")
    viewer = client_for("viewer@example.com")
    r = viewer.get("/api/gpo")
    check("a viewer reads the overview", r.status_code == 200 and r.get_json()["can_manage"] is False)
    check("...which carries the agent-setting schema with labels",
          any(s["key"] == "metrics.report_interval_seconds" and s["label"]
              for s in r.get_json()["agent_settings"]))
    r = viewer.post("/api/gpo", json=body("Nope"))
    check("a viewer cannot create a policy", r.status_code == 403)
    check("a viewer can read the resultant set of a PC in scope",
          viewer.get("/api/gpo/rsop/FIL-1").status_code == 200)
    check("...but not of a PC outside it", viewer.get("/api/gpo/rsop/ASU-1").status_code == 403)
    page = viewer.get("/group-policy", headers=IFRAME).get_data(as_text=True)
    check("a viewer gets the page without its editor",
          'id="gpo-root"' in page and 'id="gpo-dialog"' not in page)
    shell = viewer.get("/", headers={"Sec-Fetch-Dest": "document"}).get_data(as_text=True)
    check("the sidebar links the page", 'data-nav-prefix="/group-policy"' in shell)


def test_scope_and_scripts():
    print("\n-- the writer's scope must cover what the write reaches --")
    fil = client_for("fil@example.com")
    r = fil.post("/api/gpo", json=body("Edge for Filadelfia", registry=[EDGE]))
    check("a scoped operator creates a policy", r.status_code == 201)
    policy = r.get_json()
    r = fil.post(f"/api/gpo/{policy['id']}/links", json={"node_kind": "ou", "node_key": FIL,
                                                         "node_label": FIL})
    check("...and links it at an OU inside their scope", r.status_code == 201)
    link = r.get_json()
    check("the link records that it is scoped", link["scoped"] is True)
    r = fil.post(f"/api/gpo/{policy['id']}/links", json={"node_kind": "root", "node_key": ""})
    check("...but not at every PC", r.status_code == 403
          and "outside your access" in r.get_json()["error"])
    r = fil.post(f"/api/gpo/{policy['id']}/links", json={"node_kind": "ou", "node_key": ASU})
    check("...nor at an OU outside their scope", r.status_code == 403)
    r = fil.put("/api/gpo/nodes", json={"node_kind": "ou", "node_key": ASU,
                                        "block_inheritance": True})
    check("blocking inheritance outside their scope is refused", r.status_code == 403)
    r = fil.put("/api/gpo/nodes", json={"node_kind": "ou", "node_key": FIL,
                                        "block_inheritance": True})
    check("...and allowed inside it", r.status_code == 200)

    root = client_for("root@example.com")
    r = root.post("/api/gpo", json=body("Fleet", registry=[dict(EDGE, value=2)]))
    fleet_policy = r.get_json()
    root.post(f"/api/gpo/{fleet_policy['id']}/links", json={"node_kind": "root", "node_key": ""})
    r = fil.put(f"/api/gpo/{fleet_policy['id']}", json=body("Fleet", registry=[dict(EDGE, value=3)]))
    check("a scoped operator cannot edit a policy linked fleet-wide", r.status_code == 403)
    r = fil.delete(f"/api/gpo/{fleet_policy['id']}")
    check("...nor delete it", r.status_code == 403)

    print("\n-- a script needs issue_commands --")
    r = fil.post("/api/gpo", json=body("Runs code", scripts=[{"name": "setup"}]))
    check("a policy with a script is refused without issue_commands", r.status_code == 403)
    r = root.post("/api/gpo", json=body("Runs code", scripts=[{"name": "setup"}]))
    check("...and allowed with it", r.status_code == 201)
    r = root.delete("/api/rules/scripts/setup")
    check("the script a policy runs cannot be deleted", r.status_code == 409
          and "Runs code" in r.get_json()["error"])
    r = root.post("/api/gpo", json=body("Bad path", registry=[dict(EDGE, path="SOFTWARE\\Run")]))
    check("a path outside the policy tree is refused over HTTP", r.status_code == 400)
    return policy, link


def test_script_edit_reach():
    print("\n-- editing a script a policy runs is a write to that policy's reach --")
    root = client_for("root@example.com")
    runs = [p for p in root.get("/api/gpo").get_json()["gpos"] if p["name"] == "Runs code"][0]
    r = root.post(f"/api/gpo/{runs['id']}/links", json={"node_kind": "root", "node_key": ""})
    check("the script-running policy is linked to every PC", r.status_code == 201)
    script = {"name": "setup", "label": "Setup", "description": "", "shell": "powershell",
              "body": "Write-Output changed", "inputs": [], "timeout_seconds": 60}
    r = client_for("coder@example.com").post("/api/rules/scripts", json=script)
    check("a scoped operator with Issue commands cannot change it -- the new body would run "
          "on every PC", r.status_code == 403 and "outside your access" in r.get_json()["error"])
    check("...and the stored body is unchanged",
          scripts.get_script(app.DB_PATH, "setup")["body"] == "Write-Output hi")
    r = root.post("/api/rules/scripts", json=script)
    check("an operator who holds every PC it reaches can", r.status_code == 200)
    with fleet.get_conn(app.DB_PATH) as conn:
        row = conn.execute("SELECT detail_json FROM audit_log WHERE action = 'save_script'"
                           " ORDER BY id DESC LIMIT 1").fetchone()
    check("the audit row names the policy among what the edit changed",
          row is not None and "Runs code" in (row["detail_json"] or ""))


def test_heartbeat(policy):
    print("\n-- the heartbeat carries the result --")
    agent_id, token = fleet.enroll_agent(app.DB_PATH, "FIL-1", "secret", "secret")
    agent = app.app.test_client()
    # `<agent_id>:<token>`, not the token alone -- see auth_helpers.bearer_parts.
    headers = {"Authorization": f"Bearer {agent_id}:{token}"}
    r = agent.post("/api/agent/heartbeat", json={}, headers=headers)
    check("an agent heartbeat is answered", r.status_code == 200)
    first = r.get_json()
    check("an agent that never sends gpo_version gets no registry document",
          "gpo_document" not in first)
    r = agent.post("/api/agent/heartbeat", json={"gpo_version": ""}, headers=headers)
    reply = r.get_json()
    check("one that does gets the values the policy sets",
          [v["name"] for v in reply.get("gpo_document", {}).get("values", [])]
          == ["HideFirstRunExperience"])
    check("...and the hub remembers that it can", gpo.agent_supports(app.DB_PATH, "FIL-1"))
    check("...writing the registry directly, as it sent no features",
          gpo.agent_features(app.DB_PATH, "FIL-1") == [])
    r = agent.post("/api/agent/heartbeat", json={"gpo_version": reply["gpo_version"]},
                   headers=headers)
    check("an agent already holding the version is sent nothing", "gpo_document" not in r.get_json())
    r = agent.post("/api/agent/heartbeat",
                   json={"gpo_version": reply["gpo_version"], "gpo_features": ["lgpo", "bogus", 7]},
                   headers=headers)
    check("an agent updated to write the local GPO is noticed, unknown features dropped",
          r.status_code == 200 and gpo.agent_features(app.DB_PATH, "FIL-1") == ["lgpo"])
    root = client_for("root@example.com")
    result = root.get("/api/gpo/rsop/FIL-1").get_json()
    check("the resultant set says this PC's values go into its local GPO",
          result["agent_supports_registry"] and result["agent_writes_local_gpo"])
    check("...and not that it applies User Configuration, which it did not say",
          result["agent_applies_user"] is False)
    agent.post("/api/agent/heartbeat",
               json={"gpo_version": reply["gpo_version"], "gpo_features": ["user", "lgpo"]},
               headers=headers)
    result = root.get("/api/gpo/rsop/FIL-1").get_json()
    check("an agent 3.49 that applies User Configuration is noticed (phase 3)",
          gpo.agent_features(app.DB_PATH, "FIL-1") == ["lgpo", "user"]
          and result["agent_applies_user"])
    agent.post("/api/agent/heartbeat", json={"gpo_version": reply["gpo_version"]}, headers=headers)
    check("...and notices a downgrade too", gpo.agent_features(app.DB_PATH, "FIL-1") == [])

    root = client_for("root@example.com")
    version = first.get("config_version")
    r = root.put(f"/api/gpo/{policy['id']}", json=body(
        "Edge for Filadelfia", registry=[EDGE], agent={"metrics.report_interval_seconds": 42}))
    check("the policy is edited", r.status_code == 200)
    r = agent.post("/api/agent/heartbeat", json={"config_version": version}, headers=headers)
    reply = r.get_json()
    check("the config block is re-sent with the per-PC override",
          reply.get("config", {}).get("metrics.report_interval_seconds") == 42
          and reply.get("config_version") != version)
    r = agent.post("/api/agent/heartbeat", json={
        "gpo_version": "x", "gpo": {"states": [{"id": gpo.setting_id(EDGE["path"], EDGE["name"]),
                                                "status": "managed_elsewhere", "detail": "GPO"}]}},
        headers=headers)
    rsop = root.get("/api/gpo/rsop/FIL-1").get_json()
    check("an agent's report shows in the resultant set",
          rsop["registry"][0]["state"] == "managed_elsewhere")
    root.delete(f"/api/gpo/{policy['id']}")
    r = agent.post("/api/agent/heartbeat", json={"gpo_version": "x"}, headers=headers)
    values = r.get_json().get("gpo_document", {}).get("values")
    # Filadelfia blocks inheritance (test_scope_and_scripts), so the fleet-wide link above it
    # must not reach FIL-1 either -- until the block is lifted.
    check("with the OU's policy gone and inheritance blocked, nothing reaches the PC",
          values == [])
    root.put("/api/gpo/nodes", json={"node_kind": "ou", "node_key": FIL,
                                     "block_inheritance": False})
    r = agent.post("/api/agent/heartbeat", json={"gpo_version": "x"}, headers=headers)
    check("lifting the block lets the fleet-wide value through",
          [v["value"] for v in r.get_json().get("gpo_document", {}).get("values", [])] == [2])
    for remaining in root.get("/api/gpo").get_json()["gpos"]:
        root.delete(f"/api/gpo/{remaining['id']}")
    r = agent.post("/api/agent/heartbeat", json={"gpo_version": "x"}, headers=headers)
    check("with nothing left, the document is sent EMPTY rather than not at all",
          r.get_json().get("gpo_document") == {"values": []})


def test_admx_web():
    print("\n-- Administrative Templates --")
    import test_admx
    digest = admx.set_hash(test_admx.FILES)
    body = test_admx.zipped(test_admx.FILES)
    agent = app.app.test_client()
    r = agent.post(f"/api/agent/gpo/templates/{digest}", data=body)
    check("an upload without an agent token is refused", r.status_code == 401)
    agent_id, token = fleet.enroll_agent(app.DB_PATH, "ASU-1", "secret", "secret")
    headers = {"Authorization": f"Bearer {agent_id}:{token}"}
    r = agent.post(f"/api/agent/gpo/templates/{digest}", data=body, headers=headers)
    check("an enrolled agent's upload nobody asked for is refused", r.status_code == 400)
    r = agent.post("/api/agent/heartbeat", json={"admx_hash": digest}, headers=headers)
    check("a PC reporting an unknown set is asked for it", r.get_json().get("admx_wanted") is True)
    r = agent.post(f"/api/agent/gpo/templates/{'0' * 64}", data=body, headers=headers)
    check("an upload under the wrong hash is refused", r.status_code == 400)
    r = agent.post(f"/api/agent/gpo/templates/{digest}", data=body, headers=headers)
    check("the PC's upload is stored", r.status_code == 200 and r.get_json()["status"] == "stored")
    r = agent.post("/api/agent/heartbeat", json={"admx_hash": digest}, headers=headers)
    check("...and it is not asked again", "admx_wanted" not in r.get_json())

    viewer = client_for("viewer@example.com")
    check("a viewer scoped to other PCs is not told about a build only ASU-1 runs",
          viewer.get("/api/gpo/admx/sets").get_json()["sets"] == [])
    root = client_for("root@example.com")
    sets = root.get("/api/gpo/admx/sets").get_json()["sets"]
    check("an unrestricted operator sees it", [s["hash"] for s in sets] == [digest])
    r = viewer.get(f"/api/gpo/admx/{digest}/tree")
    check("the templates themselves are readable with view",
          r.status_code == 200 and len(r.get_json()["policies"]) == 4)
    r = viewer.get(f"/api/gpo/admx/{digest}/policy?ref=Microsoft.Policies.Edge:RestoreOnStartup")
    check("so is one setting's detail", r.status_code == 200 and r.get_json()["presentation"])
    check("an unknown setting is a 404",
          viewer.get(f"/api/gpo/admx/{digest}/policy?ref=nope").status_code == 404)
    r = viewer.post("/api/gpo", json={"name": "T", "settings": {"admx": [
        {"set": digest, "ref": "Microsoft.Policies.Edge:HideFirstRunExperience",
         "state": "enabled"}]}})
    check("writing a template setting still needs manage_group_policy", r.status_code == 403)
    r = root.post("/api/gpo", json={"name": "Template", "settings": {"admx": [
        {"set": digest, "ref": "Microsoft.Policies.Edge:HideFirstRunExperience",
         "state": "enabled"}]}})
    check("with it, a template setting saves through the policy route", r.status_code == 201)
    root.delete(f"/api/gpo/{r.get_json()['id']}")


def main():
    setup()
    test_gates()
    policy, _link = test_scope_and_scripts()
    test_script_edit_reach()
    test_heartbeat(policy)
    test_admx_web()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
