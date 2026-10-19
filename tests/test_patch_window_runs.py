"""Unit tests for issue #140 -- windows that create runs, and FleetHub owning Windows Update.

House pattern: a `check(name, cond)` counter plus a `__main__` that exits non-zero.

The silent failures this file exists to catch:

  * **Approving installs nothing, and nothing says so.** The bug the issue was filed for: a
    fleet with approvals and windows and no run sat unpatched. An auto-run window that
    creates no run when it opens is exactly that bug again, and looks identical from the
    console.

  * **A window that creates a run every minute.** The scheduler ticks every minute and a
    window is open for hours. Without the (window_id, window_start) dedupe a four-hour
    window makes 240 runs and dispatches each machine's patches 240 times.

  * **The take-over reaching a machine it must not.** An agent that never reported a control
    state (older than 3.47.0, or Linux) would fail the command as unknown; a PC whose keys
    Group Policy sets would be fought every ninety minutes forever. Both must get nothing.

  * **Repeating the take-over command every pass.** One command per machine per cooldown,
    or a PC whose report lags its command is sent one a minute.

Window tests run through `time.localtime`, so they build epochs from a known local date,
the way test_patches.py does.
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import patches

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


def local_epoch(year, month, day, hour, minute):
    return int(time.mktime((year, month, day, hour, minute, 0, 0, 0, -1)))


def offer(db_path, machine, *uids, now=1000):
    patches.ingest_inventory(db_path, machine, [{
        "uid": uid, "source": patches.SOURCE_WINDOWS_UPDATE, "kb": uid,
        "title": f"Update {uid}", "classification": "security", "reboot_required": True,
    } for uid in uids], now=now)


def window_runs(db_path, window_id):
    with patches.get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM patch_runs WHERE window_id = ? ORDER BY window_start",
            (window_id,))]


def control_commands(db_path, machine):
    with patches.get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM commands WHERE machine = ? AND type = ? ORDER BY created_at",
            (machine, patches.CONTROL_COMMAND_TYPE))]


def main():
    workdir = tempfile.mkdtemp(prefix="patch-window-tests-")
    db_path = os.path.join(workdir, "patches.db")
    try:
        fleet.init_fleet_db(db_path)
        patches.init_patches_db(db_path)

        # 2026-10-18 is a Sunday. Window: Sunday 23:00 for four hours.
        sunday_2300 = local_epoch(2026, 10, 18, 23, 0)
        sunday_2330 = local_epoch(2026, 10, 18, 23, 30)
        monday_0100 = local_epoch(2026, 10, 19, 1, 0)
        monday_0400 = local_epoch(2026, 10, 19, 4, 0)
        next_sunday_2300 = local_epoch(2026, 10, 25, 23, 0)

        # ------------------------------------------------------------- occurrences
        print("\n== Window occurrences ==")
        sunday = {"enabled": True, "days_mask": 1 << 6, "start_minute": 23 * 60,
                  "duration_minutes": 4 * 60}
        check("an open window reports the occurrence it is in",
              patches.window_occurrence(sunday, sunday_2330)
              == (sunday_2300, sunday_2300 + 4 * 3600))
        # The spill-over case: Monday 01:00 belongs to SUNDAY's opening, and must dedupe
        # against the run created at 23:00, not start a second one.
        check("the spill past midnight is the same occurrence, not a new one",
              patches.window_occurrence(sunday, monday_0100)
              == patches.window_occurrence(sunday, sunday_2300))
        check("a closed window has no occurrence",
              patches.window_occurrence(sunday, monday_0400) is None)
        check("window_is_open still agrees with it",
              patches.window_is_open(sunday, monday_0100)
              and not patches.window_is_open(sunday, monday_0400))

        # ------------------------------------------------------------- defaults
        print("\n== Switches default off ==")
        wid = patches.create_window(
            db_path, actor="op@x.com", name="Sunday night", days_mask=1 << 6,
            start_minute=23 * 60, duration_minutes=4 * 60,
            scope_kind=patches.SCOPE_MACHINES, machines=["PC1", "PC2", "PC3"])
        win = patches.get_window(db_path, wid)
        check("a new window does not install by itself", win["auto_run"] is False)
        check("a new window does not take over Windows Update",
              win["own_windows_update"] is False)
        check("running the schema init twice keeps the columns",
              patches.init_patches_db(db_path) is None
              and "auto_run" in patches.get_window(db_path, wid))

        offer(db_path, "PC1", "KB5000001", "KB5000002")
        offer(db_path, "PC2", "KB5000003")          # never approved
        offer(db_path, "PC3", "KB5000001")
        patches.set_approval(db_path, "KB5000001", patches.APPROVAL_APPROVED, actor="op@x.com")
        patches.set_approval(db_path, "KB5000002", patches.APPROVAL_DECLINED, actor="op@x.com")

        check("an open window with auto_run off creates nothing",
              patches.open_window_runs(db_path, sunday_2330) == []
              and window_runs(db_path, wid) == [])

        # ------------------------------------------------------------- auto runs
        print("\n== Windows create runs ==")
        patches.update_window(db_path, wid, actor="op@x.com", auto_run=True)
        check("an edit that does not mention the other switch leaves it alone",
              patches.get_window(db_path, wid)["own_windows_update"] is False)
        check("a closed auto-run window creates nothing",
              patches.open_window_runs(db_path, monday_0400) == [])
        created = patches.open_window_runs(db_path, sunday_2330)
        check("an open auto-run window creates exactly one run", len(created) == 1)
        run = patches.get_run(db_path, created[0])
        hosts = sorted(t["machine"] for t in run["targets"])
        check("it targets the machines with something approved", hosts == ["PC1", "PC3"])
        check("a machine with only unapproved updates is left out", "PC2" not in hosts)
        check("it installs everything approved, resolved at dispatch",
              run["selection"] == patches.SELECTION_APPROVED)
        check("it carries the occurrence as its window",
              run["window_start"] == sunday_2300
              and run["window_end"] == sunday_2300 + 4 * 3600)
        check("it restarts by the window's policy",
              run["reboot_policy"] == patches.REBOOT_IF_REQUIRED)
        check("its author names the window", run["created_by"] == "window:Sunday night")
        check("a declined update never reaches the resolved selection",
              [u["uid"] for u in patches.resolve_selection(db_path, run, "PC1")]
              == ["kb5000001"])

        check("the next tick in the same occurrence creates nothing",
              patches.open_window_runs(db_path, sunday_2330 + 60) == [])
        check("...nor does the spill past midnight",
              patches.open_window_runs(db_path, monday_0100) == [])
        check("one run for the whole night", len(window_runs(db_path, wid)) == 1)

        # PC1 is still pending in Sunday's open run when the next Sunday comes around (it
        # never came online). It must not be put into a second run alongside.
        offer(db_path, "PC4", "KB5000001")
        patches.update_window(db_path, wid, actor="op@x.com",
                              machines=["PC1", "PC2", "PC3", "PC4"])
        created = patches.open_window_runs(db_path, next_sunday_2300)
        check("the next occurrence makes its own run", len(created) == 1)
        hosts = sorted(t["machine"] for t in patches.get_run(db_path, created[0])["targets"])
        check("a machine already waiting in another open run is not added twice",
              hosts == ["PC4"])

        # An all-machine window reaches machines it never named.
        patches.create_window(db_path, actor="op@x.com", name="Every night",
                              days_mask=0b1111111, start_minute=1 * 60,
                              duration_minutes=60, scope_kind=patches.SCOPE_ALL)
        every = [w for w in patches.list_windows(db_path) if w["name"] == "Every night"][0]
        patches.update_window(db_path, every["id"], actor="op@x.com", auto_run=True)
        offer(db_path, "PC9", "KB5000001")
        made = patches.open_window_runs(db_path, local_epoch(2026, 10, 27, 1, 30))
        every_hosts = {t["machine"] for rid in made
                       for t in patches.get_run(db_path, rid)["targets"]}
        check("an all-machine window covers a PC nobody named", "PC9" in every_hosts)

        # Nothing approved anywhere: a window that opens must not leave an empty run behind.
        patches.clear_approval(db_path, "KB5000001")
        check("a window with nothing to install creates no run",
              patches.open_window_runs(db_path, local_epoch(2026, 10, 28, 1, 30)) == [])

        # ------------------------------------------------------------- control ingest
        print("\n== Who installs Windows updates ==")
        check("a state the hub does not know is ignored",
              patches.record_control(db_path, "PC1", {"state": "maybe"}) is False)
        check("a non-object control block is ignored",
              patches.record_control(db_path, "PC1", ["fleethub"]) is False)
        patches.record_control(db_path, "PC1", {"state": "windows", "outside_installs": [
            {"title": "Cumulative Update", "kb": "5000009", "installed_at": 1234,
             "client": "PSWindowsUpdate"},
            {"title": ""},                                   # dropped, nothing to show
        ]})
        state = patches.control_for(db_path, "PC1")
        check("the reported state is stored", state["state"] == patches.CONTROL_WINDOWS)
        check("an install outside FleetHub is kept for the console",
              state["outside_installs"] == [{"title": "Cumulative Update", "kb": "KB5000009",
                                             "installed_at": 1234,
                                             "client": "PSWindowsUpdate"}])
        offer(db_path, "PC1", "KB5000001", now=2000)
        check("an inventory ingest does not wipe the control state",
              patches.control_for(db_path, "PC1")["state"] == patches.CONTROL_WINDOWS)
        check("a machine that never reported has no state",
              patches.control_for(db_path, "PC3")["state"] is None)

        # ------------------------------------------------------------- reconcile
        print("\n== Taking Windows Update over ==")
        now = 50_000
        check("nothing is sent while no window asks for it",
              patches.reconcile_control(db_path, now=now) == 0)

        patches.update_window(db_path, wid, actor="op@x.com", own_windows_update=True)
        patches.record_control(db_path, "PC2", {"state": "managed_elsewhere"})
        patches.record_control(db_path, "PC4", {"state": "fleethub"})
        check("the window now wants its machines owned",
              patches.control_desired(patches.list_windows(db_path), "PC1"))

        sent = patches.reconcile_control(db_path, now=now)
        commands = control_commands(db_path, "PC1")
        check("a PC reporting `windows` is taken over", sent == 1 and len(commands) == 1)
        check("...with own=true",
              commands and '"own": true' in commands[0]["params_json"])
        check("a PC already owned is sent nothing", control_commands(db_path, "PC4") == [])
        check("a PC managed by Group Policy or Intune is sent nothing",
              control_commands(db_path, "PC2") == [])
        check("a PC whose agent never reported a state is sent nothing",
              control_commands(db_path, "PC3") == [])

        check("the next pass does not repeat it while it is outstanding",
              patches.reconcile_control(db_path, now=now + 60) == 0)
        fleet.claim_commands(db_path, "agent-PC1", "PC1")
        fleet.complete_command(db_path, commands[0]["id"], "agent-PC1", True, "fleethub")
        check("...nor inside the cooldown after it was answered",
              patches.reconcile_control(db_path, now=now + 120) == 0)

        # Drift: an administrator deleted the keys. The agent reports `windows` again, and
        # once the cooldown is over the hub takes it back.
        later = now + patches.CONTROL_COOLDOWN_SECONDS + 1
        with patches.get_conn(db_path) as conn:
            conn.execute("UPDATE commands SET created_at = ? WHERE machine = ?",
                         (now, "PC1"))
        check("drift is repaired after the cooldown",
              patches.reconcile_control(db_path, now=later) == 1)

        # Hand-back: the window stops owning, and an owned PC is released.
        patches.update_window(db_path, wid, actor="op@x.com", own_windows_update=False)
        released = patches.reconcile_control(db_path, now=later)
        pc4 = control_commands(db_path, "PC4")
        check("turning the switch off hands an owned PC back to Windows",
              released == 1 and pc4 and '"own": false' in pc4[-1]["params_json"])

        patches.update_window(db_path, wid, actor="op@x.com", own_windows_update=True,
                              enabled=False)
        check("a DISABLED window owns nothing",
              not patches.control_desired(patches.list_windows(db_path), "PC1"))

        check("set_update_control is a known command type",
              patches.CONTROL_COMMAND_TYPE in fleet.ALL_COMMANDS)
        check("...and cannot be saved as a favorite",
              _raises(ValueError, fleet.create_favorite, db_path, "op@x.com", "sneaky",
                      patches.CONTROL_COMMAND_TYPE, {"own": False}))

        summary = {r["machine"]: r for r in patches.control_summary(db_path)}
        check("the console summary lists reporting machines", {"PC1", "PC2", "PC4"}
              <= set(summary))
        check("...and names a managed-elsewhere PC as such",
              summary["PC2"]["state"] == patches.CONTROL_MANAGED_ELSEWHERE)

        patches.forget_machine(db_path, "PC1")
        check("forgetting a machine forgets its control state",
              patches.control_for(db_path, "PC1")["state"] is None)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
