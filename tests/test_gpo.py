"""gpo.py -- Group Policy on the PC tree (roadmap #32).

The silent failures this file exists to catch:

  * **The wrong policy winning.** Precedence is Windows': the node nearest the PC wins, link
    order 1 wins within a node, Block inheritance drops non-enforced links from above, and an
    enforced link above beats everything below it. Get any of these backwards and a PC quietly
    carries the setting nobody meant it to, with every screen looking right.
  * **A registry value outside HKLM\\SOFTWARE\\Policies** reaching a document, or one inside a
    key FleetHub itself owns.
  * **A policy change that never reaches the agent**, because the config version did not move.
  * **A link outliving its author's scope**: a PC filed under the node later must not inherit a
    link an operator who could not reach it made.
  * **A run-once script running twice** -- or never again after it changed.
  * **Unlinking that does not unset**: the document must go EMPTY, not absent.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import gpo
import pc_tree
import permissions
import rules
import scripts
import settings

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


DOMAIN = "DC=ccmf,DC=com,DC=py"
FIL = f"OU=FILADELFIA,{DOMAIN}"
CAJAS = f"OU=Cajas,OU=Usuarios,{FIL}"


def policy(db, name, agent=None, registry=None, script_list=None, enabled=True):
    error, clean = gpo.validate({"name": name, "enabled": enabled, "settings": {
        "agent_settings": agent or {}, "registry": registry or [], "scripts": script_list or []}})
    assert error is None, error
    error, saved = gpo.save_gpo(db, clean, actor="t")
    assert error is None, error
    return saved


def reg(name, value, path="SOFTWARE\\Policies\\Microsoft\\Edge"):
    return {"path": path, "name": name, "type": "dword", "value": value}


def edge_value(db, machine, name="HideFirstRunExperience"):
    for entry in gpo.rsop(db, machine)["registry"]:
        if entry["name"] == name:
            return entry["value"], entry["gpo"]
    return None, None


def main():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, ad_ou TEXT,"
                         " ad_dn TEXT, companion_version TEXT)")
            conn.executemany("INSERT INTO machine_info (machine, ad_ou) VALUES (?, ?)", [
                ("CAJA-1", CAJAS), ("OFI-1", f"OU=Oficinas,OU=Usuarios,{FIL}"),
                ("ASU-1", f"OU=ASUNCION,{DOMAIN}"), ("WG-1", None)])
        fleet.init_fleet_db(db)
        permissions.init_permissions_db(db)
        pc_tree.init_pc_tree_db(db)
        rules.init_rules_db(db)
        scripts.init_scripts_db(db)
        settings.init_settings_db(db)
        gpo.init_gpo_db(db)
        gpo.init_gpo_db(db)          # idempotent
        gpo.invalidate()

        print("\n== Validation ==")
        bad = [
            ("a path outside the policy tree",
             reg("X", 1, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run")),
            ("Windows Update policy, which UpdateControl owns",
             reg("NoAutoUpdate", 1, "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU")),
            ("FleetHub's own key", reg("X", 1, "SOFTWARE\\Policies\\FleetHub")),
            ("a path that climbs out", reg("X", 1, "SOFTWARE\\Policies\\..\\Run")),
            ("the default value", reg("", 1)),
            ("a DWORD out of range", reg("X", 2 ** 32)),
            ("an unknown type", {"path": "SOFTWARE\\Policies\\X", "name": "Y", "type": "binary",
                                 "value": "00"}),
        ]
        for why, value in bad:
            error, _ = gpo.validate({"name": "x", "settings": {"registry": [value]}})
            check(f"refuses {why}", error is not None)
        error, clean = gpo.validate({"name": "x", "settings": {"registry": [
            {"path": "HKLM\\SOFTWARE\\Policies\\Microsoft\\Edge\\", "name": "Home",
             "type": "multi_string", "value": "a\nb"}]}})
        check("an HKLM\\ prefix and a trailing slash are normalised away",
              error is None and clean["settings"]["registry"][0]["path"]
              == "SOFTWARE\\Policies\\Microsoft\\Edge")
        check("a multi-string typed as lines becomes a list",
              clean["settings"]["registry"][0]["value"] == ["a", "b"])
        error, _ = gpo.validate({"name": "x", "settings": {"registry": [reg("A", 1), reg("a", 2)]}})
        check("the same value twice in one policy is refused (the registry ignores case)",
              error is not None)
        error, _ = gpo.validate({"name": "x", "settings": {
            "agent_settings": {"directory.enabled": True}}})
        check("only agent=True settings can be set by policy", error is not None)
        error, _ = gpo.validate({"name": "x", "settings": {
            "agent_settings": {"metrics.report_interval_seconds": 99999}}})
        check("an agent setting out of its bounds is refused", error is not None)
        error, clean = gpo.validate({"name": "x", "settings": {
            "agent_settings": {"metrics.report_interval_seconds": "30"}}})
        check("an agent setting is coerced by the settings registry",
              clean["settings"]["agent_settings"] == {"metrics.report_interval_seconds": 30})

        print("\n== Nothing linked, nothing applied ==")
        check("a PC with no links resolves to an empty, versioned document",
              gpo.resolve_for(db, "CAJA-1")["values"] == []
              and gpo.resolve_for(db, "CAJA-1")["version"])
        check("and no config overrides", gpo.config_overrides(db, "CAJA-1") == {})
        check("which leaves the config version untouched",
              gpo.config_version("base", {}) == "base")

        print("\n== The node nearest the PC wins ==")
        fleetwide = policy(db, "Fleet baseline", registry=[reg("HideFirstRunExperience", 1)],
                           agent={"metrics.report_interval_seconds": 10})
        filadelfia = policy(db, "Filadelfia", registry=[reg("HideFirstRunExperience", 2)])
        cajas_policy = policy(db, "Cajas", registry=[reg("HideFirstRunExperience", 3)],
                              agent={"metrics.report_interval_seconds": 60})
        gpo.create_link(db, fleetwide["id"], gpo.NODE_ROOT, "")
        gpo.create_link(db, filadelfia["id"], gpo.NODE_OU, FIL.upper())
        gpo.create_link(db, cajas_policy["id"], gpo.NODE_OU, CAJAS)
        check("every PC gets the fleet-wide policy", edge_value(db, "ASU-1")[0] == 1)
        check("an OU's policy beats the root's", edge_value(db, "OFI-1")[0] == 2)
        check("a deeper OU beats its parent", edge_value(db, "CAJA-1")[0] == 3)
        check("an OU link matches whatever case it was typed in", edge_value(db, "OFI-1")[1] == "Filadelfia")
        check("a PC outside every OU still gets the root's", edge_value(db, "WG-1")[0] == 1)
        check("agent settings resolve by the same rule",
              gpo.config_overrides(db, "CAJA-1") == {"metrics.report_interval_seconds": 60}
              and gpo.config_overrides(db, "ASU-1") == {"metrics.report_interval_seconds": 10})
        check("the overrides move the config version",
              gpo.config_version("base", {"a": 1}) != "base"
              and gpo.config_version("base", {"a": 1}) != gpo.config_version("base", {"a": 2}))

        print("\n== A hub folder comes after the AD chain ==")
        folder = pc_tree.create_folder(db, "Cajas especiales")
        pc_tree.file_machines(db, ["CAJA-1"], folder["id"])
        folder_policy = policy(db, "Folder", registry=[reg("HideFirstRunExperience", 4)])
        gpo.create_link(db, folder_policy["id"], gpo.NODE_FOLDER, folder["id"])
        check("the folder beats the deepest OU", edge_value(db, "CAJA-1")[0] == 4)

        print("\n== Link order 1 wins within a node ==")
        second = policy(db, "Second at root", registry=[reg("Other", 7)])
        first = policy(db, "First at root", registry=[reg("Other", 8)])
        link_second = gpo.create_link(db, second["id"], gpo.NODE_ROOT, "")
        gpo.create_link(db, first["id"], gpo.NODE_ROOT, "")
        check("with Second linked before First, Second is order 2 behind the baseline's 1",
              link_second["link_order"] == 2)
        check("...so Second (order 2) beats First (order 3)",
              edge_value(db, "ASU-1", "Other")[0] == 7)
        gpo.update_link(db, link_second["id"], move=1)
        check("moving it down hands the win to First", edge_value(db, "ASU-1", "Other")[0] == 8)

        print("\n== Block inheritance and enforcement ==")
        gpo.set_block_inheritance(db, gpo.NODE_OU, CAJAS, True)
        pc_tree.file_machines(db, ["CAJA-1"], None)
        check("blocking at Cajas drops Filadelfia's and the root's links",
              edge_value(db, "CAJA-1")[0] == 3 and edge_value(db, "CAJA-1", "Other")[0] is None)
        enforced_link = [l for l in gpo.list_links(db, fleetwide["id"])][0]
        gpo.update_link(db, enforced_link["id"], enforced=True)
        check("an enforced root link ignores the block and beats the OU below",
              edge_value(db, "CAJA-1")[0] == 1)
        gpo.update_link(db, enforced_link["id"], enforced=False)
        gpo.set_block_inheritance(db, gpo.NODE_OU, CAJAS, False)
        try:
            gpo.set_block_inheritance(db, gpo.NODE_ROOT, "", True)
            check("blocking at the top is refused", False)
        except ValueError:
            check("blocking at the top is refused", True)

        print("\n== Disabled policies and links do not apply ==")
        error, clean = gpo.validate({"name": "Cajas", "enabled": False,
                                     "settings": cajas_policy["settings"]})
        gpo.save_gpo(db, clean, gpo_id=cajas_policy["id"])
        check("a disabled policy is skipped", edge_value(db, "CAJA-1")[0] == 2)
        error, clean = gpo.validate({"name": "Cajas", "enabled": True,
                                     "settings": cajas_policy["settings"]})
        gpo.save_gpo(db, clean, gpo_id=cajas_policy["id"])
        cajas_link = gpo.list_links(db, cajas_policy["id"])[0]
        gpo.update_link(db, cajas_link["id"], enabled=False)
        check("a disabled link is skipped", edge_value(db, "CAJA-1")[0] == 2)
        gpo.update_link(db, cajas_link["id"], enabled=True)

        print("\n== A link keeps its author's scope ==")
        scoped = policy(db, "Scoped", registry=[reg("Scoped", 1)])
        gpo.create_link(db, scoped["id"], gpo.NODE_OU, FIL, author_scope={"OFI-1"})
        check("it applies to the PC its author could reach", edge_value(db, "OFI-1", "Scoped")[0] == 1)
        check("and not to a PC beneath the node outside that scope",
              edge_value(db, "CAJA-1", "Scoped")[0] is None)

        print("\n== The registry document ==")
        document = gpo.resolve_for(db, "CAJA-1")
        ids = {v["id"] for v in document["values"]}
        check("each value carries the shared id",
              gpo.setting_id("SOFTWARE\\Policies\\Microsoft\\Edge", "HideFirstRunExperience") in ids)
        check("the id matches the agent's (GroupPolicyTests.The_id_matches_the_hub)",
              gpo.setting_id("SOFTWARE\\Policies\\Microsoft\\Edge", "HomepageLocation")
              == "332aecccfafb5f3d")
        before = document["version"]
        gpo.delete_link(db, cajas_link["id"])
        check("unlinking moves the version", gpo.resolve_for(db, "CAJA-1")["version"] != before)
        gpo.create_link(db, cajas_policy["id"], gpo.NODE_OU, CAJAS)

        print("\n== Agent reports ==")
        sid = gpo.setting_id("SOFTWARE\\Policies\\Microsoft\\Edge", "HideFirstRunExperience")
        gpo.record_report(db, "CAJA-1", {"states": [
            {"id": sid, "status": "managed_elsewhere", "detail": "a GPO"},
            {"id": "x", "status": "invented", "detail": ""}]})
        states = gpo.registry_states(db, "CAJA-1")
        check("a known status is stored", states[sid]["status"] == "managed_elsewhere")
        check("an unknown status is dropped, not coerced", "x" not in states)
        gpo.record_report(db, "CAJA-1", {"states": [], "removed": [sid]})
        check("a removed value's row goes", sid not in gpo.registry_states(db, "CAJA-1"))
        check("a malformed report writes nothing", gpo.record_report(db, "CAJA-1", "nope") == 0)

        print("\n== Scripts run once per PC per version ==")
        with scripts.get_conn(db) as conn:
            conn.execute(
                "INSERT INTO scripts (name, label, description, shell, body, inputs_json,"
                " timeout_seconds, enabled, created_at, created_by, updated_at, updated_by)"
                " VALUES ('setup', 'Setup', '', 'powershell', 'Write-Output {{sys.machine}}',"
                " '[]', 60, 1, 0, 't', 0, 't')")
            conn.execute(
                "INSERT INTO scripts (name, label, description, shell, body, inputs_json,"
                " timeout_seconds, enabled, created_at, created_by, updated_at, updated_by)"
                " VALUES ('needs_reading', '', '', 'powershell', 'echo {{sys.uptime_days}}',"
                " '[]', 60, 1, 0, 't', 0, 't')")
        error, clean = gpo.validate({"name": "Setup policy", "settings": {
            "scripts": [{"name": "setup"}, {"name": "needs_reading"}]}}, scripts.specs(db))
        check("a script reference validates against the library", error is None)
        error, _ = gpo.validate({"name": "Bad", "settings": {"scripts": [{"name": "nope"}]}},
                                scripts.specs(db))
        check("an unknown script is refused", error is not None)
        error, setup_policy = gpo.save_gpo(db, clean)
        gpo.create_link(db, setup_policy["id"], gpo.NODE_OU, CAJAS)
        queued = gpo.script_tick(db, ["CAJA-1", "OFI-1"])
        check("one run is queued for the PC beneath the node", queued == 1)
        runs = {r["script_name"]: r for r in gpo.script_runs(db, "CAJA-1")}
        command = fleet.get_command(db, runs["setup"]["command_id"])
        check("the body is rendered with the PC name",
              command["params"]["script"] == "Write-Output CAJA-1")
        check("the command is issued as the policy", command["issued_by"] == f"gpo:{setup_policy['id']}")
        check("a script needing a reading is skipped with a reason, not sent with 'unknown'",
              runs["needs_reading"]["status"] == gpo.SCRIPT_SKIPPED
              and "sys.uptime_days" in runs["needs_reading"]["detail"])
        check("the other PC got nothing", gpo.script_runs(db, "OFI-1") == [])
        check("a second tick queues nothing", gpo.script_tick(db, ["CAJA-1"]) == 0)
        with fleet.get_conn(db) as conn:
            conn.execute("UPDATE commands SET status = ? WHERE id = ?",
                         (fleet.STATUS_DONE, runs["setup"]["command_id"]))
        gpo.script_tick(db, ["CAJA-1"])
        check("a finished run is recorded done",
              {r["script_name"]: r for r in gpo.script_runs(db, "CAJA-1")}["setup"]["status"]
              == gpo.SCRIPT_DONE)
        check("and is never run again", gpo.script_tick(db, ["CAJA-1"]) == 0)
        with scripts.get_conn(db) as conn:
            conn.execute("UPDATE scripts SET body = 'Write-Output v2' WHERE name = 'setup'")
        check("a changed script body runs again", gpo.script_tick(db, ["CAJA-1"]) == 1)
        with scripts.get_conn(db) as conn:
            conn.execute("UPDATE scripts SET body = 'Write-Output v3' WHERE name = 'setup'")
        check("a PC that cannot run scripts is not sent one",
              gpo.script_tick(db, ["CAJA-1"], capability_refusal=lambda m: "an Android device")
              == 0)
        skipped = [r for r in gpo.script_runs(db, "CAJA-1")
                   if r["script_name"] == "setup" and r["status"] == gpo.SCRIPT_SKIPPED]
        check("...and the run says why", skipped and skipped[0]["detail"] == "an Android device")
        check("gpos_using_script finds the policy for the delete guard",
              [p["id"] for p in gpo.gpos_using_script(db, "setup")] == [setup_policy["id"]])

        print("\n== Scope helpers ==")
        check("machines under the root is every PC",
              gpo.machines_under_node(db, gpo.NODE_ROOT, "") == ["ASU-1", "CAJA-1", "OFI-1", "WG-1"])
        check("machines under an OU is component-wise",
              gpo.machines_under_node(db, gpo.NODE_OU, FIL) == ["CAJA-1", "OFI-1"])
        check("machines_linked covers every node a policy is linked to",
              gpo.machines_linked(db, filadelfia["id"]) == ["CAJA-1", "OFI-1"])
        check("folders_linked finds a folder link for pc_tree's delete guard",
              gpo.folders_linked(db, [folder["id"]]) != [])

        print("\n== Deleting a policy unlinks it ==")
        gpo.delete_gpo(db, cajas_policy["id"])
        check("its links go with it", gpo.list_links(db, cajas_policy["id"]) == [])
        check("and the PC falls back to the next policy up", edge_value(db, "CAJA-1")[0] == 2)

        print("\n== Machine lifecycle ==")
        gpo.rename_machine(db, "CAJA-1", "CAJA-1B")
        check("a merge carries the script history to the survivor",
              gpo.script_runs(db, "CAJA-1B") != [] and gpo.script_runs(db, "CAJA-1") == [])
        gpo.forget_machine(db, "CAJA-1B")
        check("a deleted PC's history goes", gpo.script_runs(db, "CAJA-1B") == [])
    finally:
        try:
            os.remove(db)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
