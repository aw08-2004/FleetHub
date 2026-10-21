"""Nested permission groups -- a member of a subgroup holds everything its parents grant.

The silent failures this file exists to catch:

  * **A subgroup member silently missing an inherited grant.** Nesting is only useful if
    "Hospital > Ward 3" members really do get Hospital's capabilities and machines; if the
    ancestor walk regresses, every one of them loses access with nothing on screen saying so.
  * **Inheritance leaking downwards or sideways.** A parent's members must NOT get a child's
    extra capabilities, and a sibling must not get its sibling's.
  * **A child saving its inherited grants as its own.** The inherited view sits beside the own
    fields and is never merged into them -- otherwise moving a group out from under its parent
    would leave it holding the parent's capabilities, a grant nobody made.
  * **A cycle or an over-deep chain failing OPEN.** Both are refused at write time; a row
    edited by hand into one must grant LESS, never more.
  * **An invite into an empty subgroup of a fleet-wide parent passing the creator ceiling.**
    The group's own row looks harmless, and joining it grants every machine.
  * **Deleting a group out from under its subgroups.** Refused, naming them.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import invites
import permissions

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
    except (ValueError, invites.InviteError) as e:
        return needle.lower() in str(e).lower()
    return False


def main():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        permissions.init_permissions_db(db)
        permissions.init_permissions_db(db)          # idempotent, including the ALTER
        fleet.init_fleet_db(db)
        permissions.invalidate()

        print("\n== A pre-nesting hub keeps every group at the top level ==")
        legacy = permissions.create_group(db, name="Legacy", capabilities=[permissions.VIEW],
                                          machines=["OLD-1"], members=["legacy@x.com"])
        check("an existing group has no parent", permissions.get_group(db, legacy)["parent_id"] is None)
        check("...and inherits nothing", permissions.get_group(db, legacy)["ancestors"] == [])

        print("\n== Members of a subgroup hold the parent's grants ==")
        hospital = permissions.create_group(
            db, name="Hospital", capabilities=[permissions.VIEW],
            machines=["HOSP-1", "HOSP-2"], members=["head@x.com"])
        ward = permissions.create_group(
            db, name="Ward 3", capabilities=[permissions.ISSUE_COMMANDS],
            machines=["WARD-1"], members=["nurse-it@x.com"], parent_id=hospital)
        admins = permissions.create_group(
            db, name="Ward 3 admins", capabilities=[permissions.REMOTE_CONTROL],
            members=["admin@x.com"], parent_id=ward)
        sibling = permissions.create_group(
            db, name="Ward 4", capabilities=[permissions.DEPLOY_PACKAGES],
            machines=["WARD4-1"], members=["w4@x.com"], parent_id=hospital)

        p = permissions.effective_permissions(db, "admin@x.com")
        check("a grandchild member gets the grandparent's capability",
              permissions.VIEW in p["capabilities"])
        check("...and the parent's", permissions.ISSUE_COMMANDS in p["capabilities"])
        check("...and its own", permissions.REMOTE_CONTROL in p["capabilities"])
        check("...and every ancestor's machines",
              p["machines"] == {"HOSP-1", "HOSP-2", "WARD-1"})
        check("the parents count as groups they are in",
              {g["id"] for g in p["groups"]} == {admins, ward, hospital})

        p = permissions.effective_permissions(db, "head@x.com")
        check("a parent's member does NOT get a child's capability",
              p["capabilities"] == {permissions.VIEW})
        check("...nor a child's machines", p["machines"] == {"HOSP-1", "HOSP-2"})

        p = permissions.effective_permissions(db, "w4@x.com")
        check("a sibling does not get its sibling's grants",
              permissions.ISSUE_COMMANDS not in p["capabilities"] and "WARD-1" not in p["machines"])

        print("\n== Inherited grants sit beside the own ones, never inside them ==")
        g = permissions.get_group(db, admins)
        check("own capabilities are only the group's own", g["capabilities"] == [permissions.REMOTE_CONTROL])
        check("inherited capabilities are listed separately",
              set(g["inherited_capabilities"]) == {permissions.VIEW, permissions.ISSUE_COMMANDS})
        check("inherited machines are listed separately",
              set(g["inherited_machines"]) == {"HOSP-1", "HOSP-2", "WARD-1"})
        check("ancestors are nearest first", g["ancestors"] == [ward, hospital])
        with permissions.get_conn(db) as conn:
            stored = conn.execute("SELECT capabilities_json FROM permission_groups WHERE id = ?",
                                  (admins,)).fetchone()[0]
        check("the stored row holds only the own capability", stored == '["remote_control"]')

        print("\n== Moving a group re-points what it inherits ==")
        permissions.update_group(db, admins, parent_id="", actor="root@x.com")
        p = permissions.effective_permissions(db, "admin@x.com")
        check("moved to the top level, it keeps only its own grant",
              p["capabilities"] == {permissions.REMOTE_CONTROL} and p["machines"] == set())
        permissions.update_group(db, admins, parent_id=ward, actor="root@x.com")
        check("moved back, the inheritance returns",
              permissions.VIEW in permissions.effective_permissions(db, "admin@x.com")["capabilities"])
        permissions.update_group(db, admins, name="Ward 3 admins", actor="root@x.com")
        check("an edit with parent_id omitted leaves the parent alone",
              permissions.get_group(db, admins)["parent_id"] == ward)
        with fleet.get_conn(db) as conn:
            details = [r[0] for r in conn.execute(
                "SELECT detail_json FROM audit_log WHERE action = 'permission_group.update'")]
        check("re-parenting is in the security audit trail",
              any('"parent_id"' in (d or "") for d in details))

        print("\n== A fleet-wide ancestor makes every descendant fleet-wide ==")
        everything = permissions.create_group(db, name="Everything", scope_mode=permissions.SCOPE_ALL,
                                              capabilities=[permissions.VIEW])
        empty_child = permissions.create_group(db, name="Looks harmless", parent_id=everything,
                                               members=["kid@x.com"])
        check("the child's member is unrestricted",
              permissions.effective_permissions(db, "kid@x.com")["machines"] is None)
        check("effective_grant reports the inherited 'all'",
              permissions.effective_grant(db, empty_child)["machines"] is None)
        check("members_of_machine lists a child under a fleet-wide parent",
              "kid@x.com" in permissions.members_of_machine(db, "ANY-PC"))

        print("\n== The invite ceiling counts inheritance ==")
        scoped = {"superuser": False, "capabilities": {permissions.VIEW}, "machines": {"HOSP-1"}}
        check("a scoped creator cannot invite into an empty child of a fleet-wide parent",
              refused(lambda: invites._check_creator_ceiling(db, [empty_child], scoped),
                      "every machine"))
        hosp_creator = {"superuser": False, "capabilities": {permissions.VIEW},
                        "machines": {"HOSP-1", "HOSP-2", "WARD-1"}}
        check("...nor into a child whose PARENT holds a capability they lack",
              refused(lambda: invites._check_creator_ceiling(db, [ward], hosp_creator),
                      "issue_commands"))
        check("a creator holding the whole chain may invite into it",
              not refused(lambda: invites._check_creator_ceiling(db, [hospital], hosp_creator)))

        print("\n== Cycles and depth are refused at write time ==")
        check("a group cannot be its own parent",
              refused(lambda: permissions.update_group(db, ward, parent_id=ward), "own parent"))
        check("a group cannot move beneath its own subgroup",
              refused(lambda: permissions.update_group(db, hospital, parent_id=admins), "subgroups"))
        check("a missing parent is refused",
              refused(lambda: permissions.create_group(db, name="Orphan", parent_id="nope"),
                      "no longer exists"))
        chain = [hospital]
        parent = hospital
        while len(chain) < permissions.MAX_GROUP_DEPTH:
            parent = permissions.create_group(db, name=f"Level {len(chain) + 1}", parent_id=parent)
            chain.append(parent)
        check("a chain exactly MAX_GROUP_DEPTH deep is allowed",
              len(permissions.get_group(db, chain[-1])["ancestors"]) == permissions.MAX_GROUP_DEPTH - 1)
        check("one more level is refused",
              refused(lambda: permissions.create_group(db, name="Too deep", parent_id=chain[-1]),
                      "levels deep"))
        check("moving a subtree that would end up too deep is refused",
              refused(lambda: permissions.update_group(db, ward, parent_id=chain[-2]), "levels deep"))

        print("\n== A hand-made cycle fails CLOSED ==")
        a = permissions.create_group(db, name="Cycle A", capabilities=[permissions.VIEW],
                                     machines=["A-1"], members=["a@x.com"])
        b = permissions.create_group(db, name="Cycle B", capabilities=[permissions.WIPE_DEVICE],
                                     machines=["B-1"], members=["b@x.com"], parent_id=a)
        with permissions.get_conn(db) as conn:
            conn.execute("UPDATE permission_groups SET parent_id = ? WHERE id = ?", (b, a))
        permissions.invalidate()
        pa = permissions.effective_permissions(db, "a@x.com")
        check("a group in a cycle inherits nothing",
              pa["capabilities"] == {permissions.VIEW} and pa["machines"] == {"A-1"})
        pb = permissions.effective_permissions(db, "b@x.com")
        check("...from either side", pb["capabilities"] == {permissions.WIPE_DEVICE})

        print("\n== A group with subgroups cannot be deleted ==")
        check("deleting a parent is refused, naming the child",
              refused(lambda: permissions.delete_group(db, hospital), "Ward 3"))
        check("...and nothing was deleted", permissions.get_group(db, hospital) is not None)
        permissions.delete_group(db, empty_child)
        check("a leaf deletes normally", permissions.get_group(db, empty_child) is None)
    finally:
        try:
            os.remove(db)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
