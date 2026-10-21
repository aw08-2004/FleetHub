"""The move_to_folder rule action (hub 1.162.0) -- a rule that files PCs into the PC tree.

The silent failures this file exists to catch:

  * **A grant nobody was allowed to make.** A folder can be a permission scope, so filing a PC
    hands it to every group scoped to that folder. Saving a rule that files PCs must need
    MANAGE_PC_TREE exactly as filing by hand does -- at the top level, inside a message's
    follow-up, and from any caller that never heard of the gate (it fails CLOSED).
  * **A default that unfiles.** `folder_id: null` means "out of every folder", which removes
    grants. It has to be something a rule author chose, never what a malformed value becomes.
  * **Churn.** A rule whose condition stays true fires again every cooldown; a PC already in
    the folder must not be rewritten, nor the permissions cache thrown away, nor live sockets
    re-resolved, each time.
  * **A rule filing into a folder that is gone.** Deleting a folder a rule files into must be
    refused (rules_using_folder), including from a follow-up; and a rule that meets a missing
    folder anyway must record a skip, not raise and take its other actions down with it.
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


NOW = 1_700_000_000


def move(folder_id):
    return {"type": "move_to_folder", "params": {"folder_id": folder_id}}


def ask_then(followup):
    """A show_message whose Yes runs `followup` -- the nested path a gate must not miss."""
    return {"type": "show_message",
            "params": {"title": "Move?", "body": "Move this PC?",
                       "buttons": [{"id": "yes"}, {"id": "no"}]},
            "on_response": {"yes": [followup]}}


def rule_with(actions, name="Mover"):
    return {"name": name, "target": {"include": [{"kind": "all"}]},
            "condition_text": "sys.uptime_days > 7", "actions": actions,
            "for_seconds": 0, "cooldown_seconds": 0}


def main():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, ad_ou TEXT, ad_dn TEXT)")
            conn.executemany("INSERT INTO machine_info (machine) VALUES (?)",
                             [("PC1",), ("PC2",)])
        fleet.init_fleet_db(db)
        permissions.init_permissions_db(db)
        pc_tree.init_pc_tree_db(db)
        rules.init_rules_db(db)
        permissions.invalidate()
        win11 = pc_tree.create_folder(db, "Win11", actor="t")
        other = pc_tree.create_folder(db, "Other", actor="t")

        print("\n== Saving needs MANAGE_PC_TREE, everywhere a rule can say it ==")
        err, _ = rules.validate_actions([move(win11["id"])], allow_pc_tree=False)
        check("refused without the capability", err is not None and "permission" in err)
        err, _ = rules.validate_actions([ask_then(move(win11["id"]))], allow_pc_tree=False)
        check("...and as a message's follow-up, which is still the rule granting it",
              err is not None and "permission" in err)
        err, _ = rules.validate_actions([ask_then(move(win11["id"]))], allow_pc_tree=True)
        check("...and it IS the gate that refused: the same message passes with it",
              err is None)
        err, _ = rules.validate_actions([move(win11["id"])])
        check("**fails closed**: a caller that never passes the flag is refused, not let through",
              err is not None)
        err, clean = rules.validate_actions([move(win11["id"])], allow_pc_tree=True)
        check("accepted with it", err is None and clean[0]["params"] == {"folder_id": win11["id"]})
        err, clean = rules.validate_actions([move(None)], allow_pc_tree=True)
        check("null is accepted, as an explicit unfile",
              err is None and clean[0]["params"] == {"folder_id": None})
        err, clean = rules.validate_actions([move(str(win11["id"]))], allow_pc_tree=True)
        check("a numeric string is normalised to the id", err is None
              and clean[0]["params"]["folder_id"] == win11["id"])
        for bad in (0, -3, "Win11", True, 1.5, [1]):
            err, _ = rules.validate_actions([move(bad)], allow_pc_tree=True)
            check(f"a malformed folder_id ({bad!r}) is refused, never read as unfile",
                  err is not None)
        err, _ = rules.save_rule(db, rule_with([move(win11["id"])]), actor="t", now=NOW)
        check("save_rule defaults closed too -- the AI drafter's commit path relies on it",
              err is not None)
        err, saved = rules.save_rule(db, rule_with([move(win11["id"])]), actor="t", now=NOW,
                                     allow_pc_tree=True)
        check("save_rule with the capability stores it", err is None and saved["id"])
        check("a move is not a command: no cooldown floor, no command kill switch",
              not rules.actions_include_command([move(win11["id"])])
              and rules.rule_can_run([move(win11["id"])], False) == (True, None))

        print("\n== Dispatch files the PC, once ==")
        cfg = {"actions_enabled": True}
        out = rules.dispatch_actions(db, [move(win11["id"])], "PC1", {}, rule=saved,
                                     now=NOW, config=cfg)
        check("the PC is filed into the folder", pc_tree.folder_of(db, "PC1") == win11["id"])
        check("...and the fire record says it moved, and from where",
              out[0].get("moved") is True and out[0].get("previous_folder_id") is None)
        with sqlite3.connect(db) as conn:
            filed_by = conn.execute(
                "SELECT filed_by FROM pc_folder_machines WHERE machine = 'PC1'").fetchone()[0]
        conn.close()
        check("...filed BY the rule, so the tree says who did it", filed_by == f"rule:{saved['id']}")
        out = rules.dispatch_actions(db, [move(win11["id"])], "PC1", {}, rule=saved,
                                     now=NOW + 60, config=cfg)
        check("a PC already there is moved: False -- no rewrite on every cooldown",
              out[0].get("moved") is False)
        rules.dispatch_actions(db, [move(other["id"])], "PC1", {}, rule=saved, now=NOW, config=cfg)
        check("a second folder moves it (a PC is in one folder at most)",
              pc_tree.folder_of(db, "PC1") == other["id"])
        out = rules.dispatch_actions(db, [move(None)], "PC1", {}, rule=saved, now=NOW, config=cfg)
        check("null takes it out of every folder",
              pc_tree.folder_of(db, "PC1") is None and out[0].get("moved") is True)
        out = rules.dispatch_actions(db, [move(None)], "PC2", {}, rule=saved, now=NOW, config=cfg)
        check("unfiling a PC that is in no folder is a no-op, not an error",
              out[0].get("moved") is False and "error" not in out[0])
        out = rules.dispatch_actions(db, [move(999), {"type": "snooze", "params": {"seconds": 60}}],
                                     "PC1", {}, rule=saved, now=NOW, config=cfg)
        check("a missing folder is a recorded skip, not a raise",
              out[0].get("skipped") and "error" not in out[0])
        check("...and the action after it still runs", "snoozed_until" in out[1])
        out = rules.dispatch_actions(db, [move(win11["id"])], "PC2", {}, rule=saved, now=NOW,
                                     config={"actions_enabled": False})
        check("the fleet-wide actions switch still stops it",
              out[0].get("skipped") and pc_tree.folder_of(db, "PC2") is None)

        print("\n== Live scope is re-pointed only when something moved ==")
        calls = []
        moved_rec = [{"type": "move_to_folder", "moved": True}]
        still_rec = [{"type": "move_to_folder", "moved": False}]
        check("a fire that moved a PC asks for a scope refresh", rules._moved_any(moved_rec))
        check("one that moved nothing does not", not rules._moved_any(still_rec))
        fire = {"rule": saved, "machine": "PC2", "variables": {}, "allowed": True,
                "block_reason": None}
        rules._fire(db, fire, now=NOW, config=cfg, on_scope_change=lambda: calls.append(1))
        check("_fire calls on_scope_change after a real move", calls == [1])
        rules._fire(db, fire, now=NOW + 1, config=cfg, on_scope_change=lambda: calls.append(1))
        check("...and not again when the PC was already there", calls == [1])

        print("\n== A folder a rule files into cannot be deleted ==")
        using = rules.rules_using_folder(db, [win11["id"]])
        check("rules_using_folder names a rule whose ACTION files into it",
              [r["id"] for r in using] == [saved["id"]])
        err, nested = rules.save_rule(db, rule_with([ask_then(move(other["id"]))], name="Asker"),
                                      actor="t", now=NOW, allow_pc_tree=True)
        check("a follow-up move saves", err is None)
        check("...and names its folder too",
              [r["id"] for r in rules.rules_using_folder(db, [other["id"]])] == [nested["id"]])
        check("a rule that unfiles names no folder",
              rules.rules_using_folder(db, [999]) == [])
    finally:
        # Windows keeps the WAL file open past the last connection; same as test_pc_tree.py.
        try:
            os.remove(db)
        except OSError:
            pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
