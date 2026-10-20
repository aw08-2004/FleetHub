"""Group Policy over HTTP and on the heartbeat (roadmap #32).

The silent failures this file exists to catch:

  * **A write that reaches past the writer's scope.** Linking at a node, blocking inheritance
    and editing a linked policy each reach every PC beneath; a site-scoped operator must not be
    able to aim a policy at the whole fleet.
  * **A policy that runs code written without `issue_commands`.**
  * **A `view`-only route that writes**, and a resultant set readable for a PC outside scope.
  * **The heartbeat not carrying the result**: config overrides that never reach the agent, a
    registry document sent to an agent that never asked for one, or none sent to one that did.

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
    r = agent.post("/api/agent/heartbeat", json={"gpo_version": reply["gpo_version"]},
                   headers=headers)
    check("an agent already holding the version is sent nothing", "gpo_document" not in r.get_json())

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


def main():
    setup()
    test_gates()
    policy, _link = test_scope_and_scripts()
    test_heartbeat(policy)
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
