"""Which physical PC a name belongs to -- the evidence half of letting two PCs share a name.

**The hub keys every machine on its hostname, and a hostname is not an identity.** Two PCs
called `RECEPTION` are two machines, and one PC re-imaged and renamed is still one. The
owner's call (2026-11-02): a PC with a name the hub already knows may enrol, and the
hardware decides whether it is the same machine or a second one. This module is that
decision, and the table it reads from. Moving every table off the hostname key onto a
hub-minted `machine_id` is the larger half and follows; until it lands, a "different
machine" verdict can only refuse, and the same-name enrolment it exists for waits on it.

**Three identifiers, and they are not equals.**

  * `serial_number` (Win32_BIOS) and `smbios_uuid` (Win32_ComputerSystemProduct.UUID) are
    burned into the board. They survive a re-image, a rename and a disk swap, so they say
    which **hardware** this is.
  * `machine_guid` (HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid, /etc/machine-id on
    Linux) is minted by the OS installer. A re-image changes it, a cloned image copies it to
    every PC built from it. It says which **install** this is, which is evidence only when
    the hardware has nothing to say.

So `compare` asks the hardware first: any usable hardware identifier that differs means a
different PC, any that matches (with none differing) means the same one, and only when
neither side has a usable hardware identifier in common does the GUID decide. Rejected:
"all three must be equal" -- every re-imaged PC would come back as a stranger, which is the
commonest legitimate reason a known name enrols again. Rejected: GUID first -- a fleet built
from one un-sysprepped image would collapse into a single machine.

**Placeholders identify nothing.** Whiteboxes ship "To Be Filled By O.E.M.", AMI boards ship
the UUID `03000200-0400-0500-0006-000700080009`, and some VMs report all zeros. Treating
those as real would merge unrelated PCs, which is the failure app.is_valid_serial already
exists to prevent; the same rule is applied to the UUID and GUID here.

**Only two writers, and neither is an unsigned report.** These ids decide who may enrol
under a name, so whoever writes them first would decide it; /api/report still accepts
unsigned reports on a hub with enrolled-only telemetry off, which is anybody who can reach
the hub. So app records them from **signed** reports only, write-once (a blank is filled, an
established value is not overwritten), and a successful onboarding redeem writes them with
`authoritative=True`: that caller has spent a code and passed `compare`, a re-imaged PC has a
new GUID that should replace the old one, and a name with no record must not keep a row left
by a PC whose install failed after its redeem (both found in review). The cost: an
unenrolled PC that has only ever reported unsigned has nothing stored, and is refused as
before until it has been onboarded once.

Kept free of Flask so it can be unit-tested in isolation.
"""
import sqlite3
import time

# ================================
# VERDICTS
# ================================
SAME = "same"
DIFFERENT = "different"
UNKNOWN = "unknown"

# Shared with app._JUNK_SERIALS in spirit; repeated here rather than imported because app
# imports this module, and a model module must not import the composition root.
_JUNK_SERIALS = {
    "", "0", "none", "null", "n/a", "na", "not specified", "not applicable",
    "to be filled by o.e.m.", "to be filled by o.e.m", "default string",
    "system serial number", "chassis serial number", "unknown", "invalid",
    "empty", "123456789", "0123456789",
}

# SMBIOS UUIDs that firmware vendors ship unset. The AMI one is on a great many whitebox
# boards; the all-zero and all-F ones are what an unset field and some hypervisors report.
_JUNK_UUIDS = {
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "03000200-0400-0500-0006-000700080009",
}

MAX_ID_CHARS = 128


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_identity_db(db_path):
    """Create the identity table. Idempotent -- safe on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        ensure_table(conn)


def ensure_table(conn):
    """The table on an open connection, for callers (onboarding) whose own init must not
    depend on app having run first."""
    # NOCASE on the key because every other name lookup in the hub is case-insensitive: a
    # report from `desktop-x` describes the record `DESKTOP-X` (see app.delete_machine).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS machine_identity (
            machine       TEXT PRIMARY KEY COLLATE NOCASE,
            machine_guid  TEXT,
            smbios_uuid   TEXT,
            updated_at    INTEGER NOT NULL
        )
        """
    )


# ================================
# NORMALISATION
# ================================
def clean_serial(value):
    """The serial as identity compares it (trimmed, lower-cased), or None for a placeholder."""
    text = str(value or "").strip()
    if text.lower() in _JUNK_SERIALS or len(text) > MAX_ID_CHARS:
        return None
    return text.lower()


def clean_uuid(value):
    """A GUID/UUID as identity compares it, or None for a blank, a placeholder or garbage.

    Braces and case are dropped because Windows hands the same GUID back as `{ABC...}` from
    one API and `abc...` from another. Anything that is not 32 hex digits after that is not a
    GUID, and storing it would make a typo an identity."""
    text = str(value or "").strip().strip("{}").lower()
    if text in _JUNK_UUIDS:
        return None
    digits = text.replace("-", "")
    if len(digits) != 32 or any(c not in "0123456789abcdef" for c in digits):
        return None
    # A UUID whose every digit is the same is a placeholder whatever its dashes look like.
    if len(set(digits)) == 1:
        return None
    return "-".join((digits[:8], digits[8:12], digits[12:16], digits[16:20], digits[20:]))


def evidence(serial=None, machine_guid=None, smbios_uuid=None):
    """{serial, machine_guid, smbios_uuid}, each cleaned to a comparable value or None."""
    return {
        "serial": clean_serial(serial),
        "machine_guid": clean_uuid(machine_guid),
        "smbios_uuid": clean_uuid(smbios_uuid),
    }


# ================================
# THE DECISION
# ================================
def compare(known, presented):
    """SAME, DIFFERENT or UNKNOWN for two `evidence()` dicts -- see the module docstring.

    UNKNOWN is not "probably the same". It means nothing usable was on both sides, and a
    caller that has to grant something on it (onboarding) must treat it as a refusal."""
    known = known or {}
    presented = presented or {}
    hardware_match = False
    for key in ("serial", "smbios_uuid"):
        a, b = known.get(key), presented.get(key)
        if a and b:
            if a != b:
                return DIFFERENT
            hardware_match = True
    if hardware_match:
        return SAME
    a, b = known.get("machine_guid"), presented.get("machine_guid")
    if a and b:
        return SAME if a == b else DIFFERENT
    return UNKNOWN


# ================================
# STORAGE
# ================================
def lookup(conn, machine):
    """{machine_guid, smbios_uuid} stored for `machine` (any case), or None."""
    row = conn.execute(
        "SELECT machine_guid, smbios_uuid FROM machine_identity WHERE machine = ?",
        (machine,)).fetchone()
    if row is None:
        return None
    return {"machine_guid": row["machine_guid"], "smbios_uuid": row["smbios_uuid"]}


def remember(conn, machine, machine_guid=None, smbios_uuid=None, authoritative=False, now=None):
    """Store what `machine` presented. Write-once per field unless `authoritative`, when a
    presented value replaces the stored one -- see the module docstring for who may pass it.
    Values that do not clean to a usable identifier are dropped, never stored over a value."""
    guid = clean_uuid(machine_guid)
    uuid = clean_uuid(smbios_uuid)
    if guid is None and uuid is None:
        return
    now = int(now if now is not None else time.time())
    if authoritative:
        first, second = "excluded", "machine_identity"
    else:
        first, second = "machine_identity", "excluded"
    conn.execute(
        "INSERT INTO machine_identity(machine, machine_guid, smbios_uuid, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(machine) DO UPDATE SET "
        f"  machine_guid = COALESCE({first}.machine_guid, {second}.machine_guid), "
        f"  smbios_uuid = COALESCE({first}.smbios_uuid, {second}.smbios_uuid), "
        "  updated_at = excluded.updated_at",
        (machine, guid, uuid, now))


def record(db_path, machine, machine_guid=None, smbios_uuid=None):
    """remember() on its own connection -- the /api/report path."""
    if clean_uuid(machine_guid) is None and clean_uuid(smbios_uuid) is None:
        return
    with get_conn(db_path) as conn:
        remember(conn, machine, machine_guid, smbios_uuid)


def forget_machine(db_path, machine):
    """Drop a deleted machine's identifiers. Keeping them would make the next PC to take the
    name answer to a dead machine's hardware, which is exactly the refusal deletion exists to
    clear."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_identity WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """Fold identifiers into the survivor of a duplicate-serial merge. The survivor's own
    values win where both have one: it is the record still reporting."""
    with get_conn(db_path) as conn:
        old = lookup(conn, old_machine)
        if old is not None:
            remember(conn, new_machine, old["machine_guid"], old["smbios_uuid"])
        conn.execute("DELETE FROM machine_identity WHERE machine = ? AND machine <> ?",
                     (old_machine, new_machine))
