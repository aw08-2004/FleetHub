"""Model test for identity.py -- which physical PC a name belongs to.

**The silent failure this file exists to catch is two different PCs judged to be one.** A
SAME verdict hands a PC another machine's record and its enrolment, and nothing on screen
says so: the second PC simply reports where the first one used to. The likeliest ways to get
there are a firmware placeholder (the AMI UUID on half the whitebox boards in the building)
being treated as an identifier, and a cloned image's shared MachineGuid outvoting a BIOS
serial that says otherwise -- so both are pinned here.

The opposite failure, one PC judged to be two, is pinned too: a re-imaged PC gets a new
MachineGuid and must still match on its hardware.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import identity

PASS = 0
FAIL = 0

GUID_A = "{1B4E28BA-2FA1-11D2-883F-0016D3CCA427}"
GUID_B = "6fa459ea-ee8a-3ca4-894e-db77e160355e"
UUID_A = "4C4C4544-0042-3510-8048-B3C04F4E3532"
UUID_B = "4C4C4544-0042-3510-8048-B3C04F4E3533"
AMI_UUID = "03000200-0400-0500-0006-000700080009"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def ev(serial=None, guid=None, uuid=None):
    return identity.evidence(serial, guid, uuid)


def test_cleaning():
    print("\n-- placeholders identify nothing --")
    check("an AMI placeholder UUID is dropped", identity.clean_uuid(AMI_UUID) is None)
    check("an all-zero UUID is dropped",
          identity.clean_uuid("00000000-0000-0000-0000-000000000000") is None)
    check("a UUID of one repeated digit is dropped",
          identity.clean_uuid("1111111111111111" * 2) is None)
    check("garbage is not a GUID", identity.clean_uuid("not-a-guid") is None)
    check("braces and case do not make a GUID different",
          identity.clean_uuid(GUID_A) == identity.clean_uuid(GUID_A.strip("{}").lower()))
    check("an undashed /etc/machine-id reads as the same GUID dashed",
          identity.clean_uuid(GUID_B.replace("-", "")) == identity.clean_uuid(GUID_B))
    check("'To Be Filled By O.E.M.' is not a serial",
          identity.clean_serial("To Be Filled By O.E.M.") is None)
    check("a real serial is kept, trimmed and lower-cased",
          identity.clean_serial("  ABC123 ") == "abc123")


def test_compare():
    print("\n-- the hardware decides first, the install only when it cannot --")
    check("same serial: same PC",
          identity.compare(ev("SER1"), ev("ser1")) == identity.SAME)
    check("different serials: different PCs",
          identity.compare(ev("SER1"), ev("SER2")) == identity.DIFFERENT)
    check("a re-imaged PC (new GUID, same serial and UUID) is the same PC",
          identity.compare(ev("SER1", GUID_A, UUID_A), ev("SER1", GUID_B, UUID_A))
          == identity.SAME)
    check("a cloned image on other hardware (same GUID, different serial) is a different PC",
          identity.compare(ev("SER1", GUID_A), ev("SER2", GUID_A)) == identity.DIFFERENT)
    check("a matching serial with a conflicting UUID is a different PC",
          identity.compare(ev("SER1", uuid=UUID_A), ev("SER1", uuid=UUID_B))
          == identity.DIFFERENT)
    check("placeholder serials: the UUID decides",
          identity.compare(ev("Default string", uuid=UUID_A), ev("Default string", uuid=UUID_A))
          == identity.SAME)
    check("placeholder serial and placeholder UUID: the GUID decides",
          identity.compare(ev("Default string", GUID_A, AMI_UUID),
                           ev("Default string", GUID_A, AMI_UUID)) == identity.SAME)
    check("...and a different GUID there is a different PC",
          identity.compare(ev("Default string", GUID_A, AMI_UUID),
                           ev("Default string", GUID_B, AMI_UUID)) == identity.DIFFERENT)
    check("two whiteboxes with nothing usable are UNKNOWN, never SAME",
          identity.compare(ev("Default string", None, AMI_UUID),
                           ev("Default string", None, AMI_UUID)) == identity.UNKNOWN)
    check("an empty record is UNKNOWN", identity.compare({}, ev("SER1")) == identity.UNKNOWN)


def test_storage():
    print("\n-- written once, except by a redeem --")
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        identity.init_identity_db(db)
        identity.record(db, "PC-1", GUID_A, UUID_A)
        identity.record(db, "pc-1", GUID_B, UUID_B)
        with identity.get_conn(db) as conn:
            stored = identity.lookup(conn, "PC-1")
        check("a later report does not overwrite an established GUID or UUID",
              stored == {"machine_guid": identity.clean_uuid(GUID_A),
                         "smbios_uuid": identity.clean_uuid(UUID_A)})
        with identity.get_conn(db) as conn:
            identity.remember(conn, "PC-1", GUID_B, UUID_B, replace_guid=True)
            stored = identity.lookup(conn, "pc-1")
        check("a redeem replaces the GUID (a re-image) but never the UUID",
              stored == {"machine_guid": identity.clean_uuid(GUID_B),
                         "smbios_uuid": identity.clean_uuid(UUID_A)})
        identity.record(db, "PC-2", AMI_UUID, AMI_UUID)
        with identity.get_conn(db) as conn:
            check("placeholders are not stored at all", identity.lookup(conn, "PC-2") is None)

        identity.record(db, "OLD", GUID_A, UUID_B)
        identity.record(db, "NEW", None, UUID_A)
        identity.rename_machine(db, "OLD", "NEW")
        with identity.get_conn(db) as conn:
            merged = identity.lookup(conn, "NEW")
            gone = identity.lookup(conn, "OLD")
        check("a merge fills the survivor's blanks and keeps its own values",
              merged == {"machine_guid": identity.clean_uuid(GUID_A),
                         "smbios_uuid": identity.clean_uuid(UUID_A)})
        check("...and drops the merged-away row", gone is None)

        identity.forget_machine(db, "new")
        with identity.get_conn(db) as conn:
            check("a delete forgets the identifiers, in any case",
                  identity.lookup(conn, "NEW") is None)
    finally:
        try:
            os.remove(db)
        except OSError:
            pass


def main():
    test_cleaning()
    test_compare()
    test_storage()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
