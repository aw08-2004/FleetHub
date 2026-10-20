"""Android packages (roadmap #5 on roadmap #23): an `apk` step, and the platform rule.

**The silent failure this file exists to catch is a package reaching the wrong kind of
machine.** An Android package aimed at a mixed group would hand a Windows agent a step kind it
cannot run, and a Windows package would land on a phone -- each "failing" only after it had
been sent, on a target the operator may never look at. So the platform is derived from the
recipe, an Android recipe is held to the rules a silent install needs (only apk steps, every
payload pinned, no Windows detection rule), and the scheduler retires a wrong-platform target
with the reason instead of dispatching it.

House pattern: `check(name, cond)` counters and a `main()` that exits non-zero.
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import capabilities
import fleet
import packages

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


def refused(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        return str(e)
    return None


APK_SOURCE = {"kind": packages.SOURCE_UPLOAD, "sha256": "b" * 64,
              "file_name": "app.apk", "file_size": 4321}


def make_apk_package(db_path, name="Example app", **overrides):
    kwargs = dict(name=name, source=APK_SOURCE,
                  steps=[{"kind": packages.STEP_APK, "apk": "{payload}"}],
                  actor="op@x.com")
    kwargs.update(overrides)
    return packages.create_package(db_path, **kwargs)


def make_msi_package(db_path, name="7-Zip"):
    return packages.create_package(
        db_path, name=name,
        source={"kind": packages.SOURCE_UPLOAD, "sha256": "a" * 64,
                "file_name": "7z.msi", "file_size": 1234},
        install_command="msiexec.exe", install_args='/i "{file}" /qn', actor="op@x.com")


def target(db_path, deployment_id, machine):
    with packages.get_conn(db_path) as conn:
        return dict(conn.execute(
            "SELECT * FROM deployment_targets WHERE deployment_id = ? AND machine = ?",
            (deployment_id, machine)).fetchone())


def main():
    workdir = tempfile.mkdtemp(prefix="pkg-android-tests-")
    db_path = os.path.join(workdir, "packages.db")
    try:
        fleet.init_fleet_db(db_path)
        packages.init_packages_db(db_path)
        capabilities.init_capabilities_db(db_path)

        print("\n== An apk recipe ==")
        pkg = packages.get_package(db_path, make_apk_package(db_path))
        check("an apk package is an Android package", pkg["platform"] == packages.PLATFORM_ANDROID)
        check("its step survives validation",
              pkg["steps"] == [{"kind": "apk", "apk": "{payload}"}])
        check("an msi package is a Windows package",
              packages.get_package(db_path, make_msi_package(db_path))["platform"]
              == packages.PLATFORM_WINDOWS)
        check("the params snapshot carries the apk step to the agent",
              packages.build_command_params(pkg, {"id": "d1"})["steps"][0]["kind"] == "apk")

        print("\n== What an Android recipe may not do ==")
        why = refused(make_apk_package, db_path, name="mixed",
                      steps=[{"kind": "apk", "apk": "{payload}"},
                             {"kind": "powershell", "script": "Write-Host hi"}])
        check("an apk step beside a Windows step is refused", why and "only have apk steps" in why)
        why = refused(make_apk_package, db_path, name="unpinned",
                      source={"kind": packages.SOURCE_URL, "ref": "https://cdn.example/app.apk"})
        check("an unpinned URL payload is refused", why and "pin its sha256" in why)
        check("a pinned URL payload is fine",
              refused(make_apk_package, db_path, name="pinned",
                      source={"kind": packages.SOURCE_URL, "ref": "https://cdn.example/app.apk",
                              "sha256": "c" * 64}) is None)
        why = refused(make_apk_package, db_path, name="unc",
                      source={"kind": packages.SOURCE_UNC, "ref": "\\\\srv\\share\\app.apk",
                              "sha256": "c" * 64})
        check("a UNC payload is refused -- a phone cannot read a share", why and "unc" in why)
        why = refused(make_apk_package, db_path, name="detect",
                      detection={"kind": packages.DETECT_FILE_EXISTS, "path": "C:\\x"})
        check("a Windows detection rule is refused", why and "detection" in why)
        why = refused(make_apk_package, db_path, name="empty", steps=[{"kind": "apk"}])
        check("an apk step must name its payload", why and "needs the payload" in why)
        why = refused(make_apk_package, db_path, name="typo",
                      steps=[{"kind": "apk", "apk": "{paylod}"}])
        check("...and a payload that exists", why and "paylod" in why)

        print("\n== An update is held to the same rules as a create ==")
        # The silent case: create passes the Android rules, and a later edit adds a Windows
        # step. update_package re-validates the whole recipe, so this must be refused too.
        why = refused(packages.update_package, db_path, pkg["id"],
                      steps=[{"kind": "apk", "apk": "{payload}"},
                             {"kind": "run", "command": "msiexec.exe", "args": "/i {payload}"}],
                      actor="op@x.com")
        check("adding a Windows step to an Android package is refused",
              why and "only have apk steps" in why)
        why = refused(packages.update_package, db_path, pkg["id"],
                      detection={"kind": packages.DETECT_FILE_EXISTS, "path": "C:\\x"},
                      actor="op@x.com")
        check("adding a detection rule to an Android package is refused",
              why and "detection" in why)
        why = refused(packages.update_package, db_path, pkg["id"],
                      sources=[{"name": "payload", "kind": packages.SOURCE_URL,
                                "ref": "https://cdn.example/app.apk"}],
                      actor="op@x.com")
        check("swapping in an unpinned payload is refused", why and "pin its sha256" in why)
        check("...and the stored package is unchanged",
              packages.get_package(db_path, pkg["id"])["steps"]
              == [{"kind": "apk", "apk": "{payload}"}])

        print("\n== The scheduler keeps each package on its own platform ==")
        capabilities.record_capabilities(db_path, "PHONE-1", {
            "platform": "android", "commands": [packages.COMMAND_TYPE], "features": []})
        # PC-1 reports nothing, like every Windows agent in the field.
        now = int(time.time()) + 1
        apk_deploy = packages.create_deployment(
            db_path, package_id=pkg["id"], machines=["PHONE-1", "PC-1"], created_by="op@x.com")
        msi_id = packages.get_package(db_path, make_msi_package(db_path, "Notepad++"))["id"]
        msi_deploy = packages.create_deployment(
            db_path, package_id=msi_id, machines=["PHONE-1", "PC-1"], created_by="op@x.com")
        dispatched = packages.dispatch_once(db_path, now=now)
        check("one command each: the phone gets the apk, the PC the msi", dispatched == 2)
        check("the apk reached the phone", target(db_path, apk_deploy, "PHONE-1")["command_id"])
        pc_apk = target(db_path, apk_deploy, "PC-1")
        check("the apk did NOT reach the PC, and the reason is recorded",
              pc_apk["status"] == packages.TARGET_FAILED
              and "not an Android device" in (pc_apk["last_error"] or ""))
        phone_msi = target(db_path, msi_deploy, "PHONE-1")
        check("the msi did NOT reach the phone, and says what would",
              phone_msi["status"] == packages.TARGET_FAILED
              and "apk steps" in (phone_msi["last_error"] or ""))
        check("the msi reached the PC", target(db_path, msi_deploy, "PC-1")["command_id"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
