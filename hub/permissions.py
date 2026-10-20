"""Permission groups -- the hub's access-control model.

Until now the perimeter was flat: any address in ALLOWED_EMAILS could see every
machine and run code as SYSTEM on any of them. That cannot serve an IT group where
the Hospital operator manages Hospital PCs and the HR operator manages HR PCs, and
nothing else on the roadmap (package deploys, backups, remote control) can be
meaningfully authorized without a finer model. This module is that model.

  * A **permission group** is {name, capabilities, machine scope}. Capabilities are
    granular toggles (see CAPABILITIES); machine scope is either an explicit list of
    machines or "every machine".
  * A **user** (by email) belongs to zero or more groups. Their effective permission
    is the UNION across their groups -- capabilities union, machine scope union.
  * **Groups nest, the way Active Directory groups do.** A group may name a parent, and a
    member of the child holds the child's grants PLUS every ancestor's -- capabilities union,
    machine scope union, "all" anywhere up the chain wins. A child can only ADD to what its
    parent grants; it can never narrow it. That one direction is what keeps the model as
    readable as the flat one it extends: the effect of adding someone to "Hospital > Ward 3
    admins" is everything the page shows on that row and the rows above it, never a
    subtraction that depends on some other group. Rejected: "child narrows parent" (an
    intersection, which effective_permissions below already refuses for the reason it gives)
    and a separate container object for organising groups (a group with no grants of its own
    already IS a container, exactly as an OU is in ADUC).
  * **ALLOWED_EMAILS is the break-glass superuser list.** Membership grants every
    capability over every machine, bypassing groups entirely. It is both the
    bootstrap path (day one, before any group exists, someone has to be able to
    create the first one) and the safety net if group config is ever broken. That is
    why nothing here guards against "deleting the last admin group" -- such a guard
    would be protecting against a lockout that cannot happen.

"Admin" is deliberately NOT a hardcoded tier. It is just a group holding
MANAGE_PERMISSION_GROUPS, which is what lets an operator hand out a narrow slice of
admin (say, backups only) without handing out everything.

Two-layer enforcement, applied wherever a machine is touched:
  1. is there a session at all (app.py's login_required), and
  2. does the caller hold the capability, AND is the target machine in their scope.

Layer 2 gates READS as well as writes -- an HR tech should not *see* Hospital
machines in a list, not merely be blocked from acting on them. The Flask-facing half
of that (decorators, per-request caching, list filtering) lives in permissions_web.py;
this module stays Flask-free so it can be unit-tested in isolation, exactly like
fleet.py and settings.py.

Member rows carry an `ad_group_dn` column alongside `email`. It was added on day one
so that roadmap #4 would not need a schema migration on a table this security-critical,
and it is now live: a group can name directory groups as well as individual emails, and
membership in a named directory group grants that group exactly as email membership
does. See DIRECTORY_GROUP_CLAIMS below. The hub never queries a directory to establish
this -- it believes what the identity provider signed at sign-in, so a directory grant
is session-scoped where an email grant is stored.
"""
import json
import sqlite3
import threading
import time
import uuid

import fleet

# ================================
# CAPABILITIES
# ================================
# Each is an independent toggle an admin sets per group. Order is the order the admin
# UI renders them in, so it runs least- to most-privileged.
VIEW = "view"
# Reading the audit trail is two capabilities, not one. VIEW_AUDIT_LOG is the perimeter --
# it opens the tab and returns info + notice rows. VIEW_SECURITY_AUDIT is a MODIFIER on top
# of it, widening the read to security-level rows (permission changes, backup-key access,
# remote sessions, command execution, settings). On its own it grants nothing at all: a
# group holding only it still gets no tab and a 403 from the API, because the level filter
# is a widening of a read the first capability authorised.
VIEW_AUDIT_LOG = "view_audit_log"
VIEW_SECURITY_AUDIT = "view_security_audit"
ISSUE_COMMANDS = "issue_commands"
REMOTE_CONTROL = "remote_control"
DEPLOY_PACKAGES = "deploy_packages"
MANAGE_BACKUPS = "manage_backups"
# Writing firmware settings and (later) flashing a BIOS. Its own toggle rather than a reuse
# of DEPLOY_PACKAGES, because this is the one action in the product with no restore path:
# a failed flash bricks a machine, and there is no chain to re-pull or snapshot to roll back
# to. Folding it into "can push an installer" would have handed it silently, on the day it
# shipped, to everyone who already had that. READING the firmware inventory is deliberately
# NOT gated here -- that is VIEW, like a machine's model or its disks.
MANAGE_FIRMWARE = "manage_firmware"
# Approving patches, scheduling maintenance windows, and pushing a run outside one. Its own
# toggle rather than a reuse of DEPLOY_PACKAGES, because the two differ on the thing that
# actually costs somebody their afternoon: a package install happens when an operator aims
# it, while an approved patch reboots a machine on a schedule nobody is watching -- and an
# emergency push does it immediately, in the middle of the working day. Folding it into
# "can push an installer" would have handed that silently, on the day it shipped, to
# everyone who already had that.
#
# READING patch inventory and compliance is deliberately NOT gated here -- that is VIEW,
# like a machine's disks or its BIOS version. Knowing a PC is missing a security update is
# not a privilege; deciding what happens about it is.
MANAGE_PATCHES = "manage_patches"
# Reading an escrowed BitLocker recovery password (roadmap #19). Its own toggle, and the
# narrowest capability in this product on purpose: it grants the ability to take one specific
# secret out of the hub, and nothing else.
#
# **Not a reuse of MANAGE_BACKUPS**, even though the key sits in that module's secret store.
# Configuring where backups go and being handed the recovery password of any PC in scope are
# different powers, and the store is an implementation detail they happen to share -- folding
# them together would have granted this silently, on the day it shipped, to everyone who
# already had that. READING the encryption posture is deliberately NOT gated here: whether a
# laptop is encrypted is inventory, in the same sense its model and its disks are, and it is
# VIEW. Holding the key is the privilege; knowing the lock exists is not.
READ_RECOVERY_KEYS = "read_recovery_keys"
# Asking a device where it is (roadmap #23). **The one command-issuing feature in this product
# that does NOT reuse ISSUE_COMMANDS**, and the departure is deliberate rather than an
# oversight: wake, the Processes card and the file explorer all reuse it on the argument that
# each is less dangerous than the SYSTEM shell that gate already grants. That argument works
# because all three act on a MACHINE. This one acts on a person.
#
# "May reboot a PC" must not silently imply "may find out where an employee is." Folding this
# into ISSUE_COMMANDS would have handed it, on the day it shipped, to everyone who already had
# a reboot button -- which is the whole helpdesk.
#
# READING a device's last known position is deliberately NOT gated here: that is VIEW plus
# machine scope, like every other thing a machine reports about itself. Knowing where a device
# was when somebody last asked is not the privilege; making a device answer is.
LOCATE_DEVICE = "locate_device"
# Writing app and time policy for managed devices (roadmap #23). Its own capability rather than
# a reuse of MANAGE_SETTINGS, on the same argument MANAGE_RULES already makes for itself: a
# policy is not a setting. A setting is a threshold somebody tunes; a policy is a standing
# instruction that changes what a person's device will do, applied without anybody present, and
# it is the first thing in this product that can make a device less useful to the person
# holding it.
#
# READING a policy, and reading how a device is complying with one, is deliberately NOT gated
# here -- that is VIEW plus machine scope. Knowing that an app is blocked on a device you
# already administer is not a privilege; deciding that it should be is.
MANAGE_DEVICE_POLICY = "manage_device_policy"
# Locking a device's screen, and erasing it (roadmap #23 phase H). The largest blast radius in
# this product: `wipe_device` is a factory reset with no undo, no dry run and no partial
# version of itself, and it takes effect on a device somebody may be holding.
#
# Its own capability rather than ISSUE_COMMANDS, and the argument that keeps the other command
# features under that gate is exactly what excludes these two: "less dangerous than the SYSTEM
# shell it already grants" is true of a reboot and false of an erase. It is also not folded in
# with MANAGE_DEVICE_POLICY, though both act on managed devices, because a policy is reversible
# by editing it and this is not reversible at all.
#
# LOCK and WIPE share one capability rather than getting one each. They are different in
# weight -- a lock is the ordinary first move for a phone left on a train, and is undone by
# the person's own PIN -- but anybody trusted with the second is trusted with the first, and a
# separate `lock_device` capability would be one more row in the permissions UI that nobody
# would ever grant alone.
WIPE_DEVICE = "wipe_device"
# Creating and revoking onboarding codes (roadmap #29). A code is a one-time right to put a
# machine into this fleet. Its own capability rather than a reuse of MANAGE_SETTINGS, which is
# where the Android provisioning QR sits: that QR carries the shared enrollment secret itself,
# so it is configuration-grade trust, and a code carries nothing reusable. Setting up a new PC
# is the most ordinary thing a helpdesk technician does, and they should not need the hub's
# configuration to do it.
#
# Not machine-scoped, because a code names no machine until one spends it. Revoking a code
# does revoke the agent it minted -- an operator holding this may therefore take one machine
# off the fleet channel, the one their own code put there, which is visible in the list.
MANAGE_ONBOARDING = "manage_onboarding"
MANAGE_SETTINGS = "manage_settings"
MANAGE_USERS = "manage_users"
MANAGE_PERMISSION_GROUPS = "manage_permission_groups"
# Writing rules: conditions over machine data, and what happens when they match. Its own
# capability rather than a reuse of MANAGE_SETTINGS, because a rule is not a setting -- it is
# a standing instruction that acts on machines without anybody present, which is a different
# thing to hand out than a threshold.
#
# It is deliberately NOT sufficient on its own to write a rule that issues COMMANDS: that
# additionally requires ISSUE_COMMANDS, checked in rules.validate_actions. Someone who may
# raise alerts on a condition has not thereby been handed a fleet-wide reboot button.
# READING rules, and the custom fields they use, is VIEW.
MANAGE_RULES = "manage_rules"
# Writing device groups: saved, named target filters that rules, package deployments and the
# Devices list aim at (device_groups.py). Its own capability rather than a reuse of
# MANAGE_RULES, because editing a group silently changes what every rule AND every deployment
# aimed at it reaches -- including rules somebody else wrote. Folding it into "may write rules"
# would have handed that, on the day it shipped, to everyone who could already write one.
#
# READING a group, and USING one, is deliberately NOT gated here -- that is VIEW, and whoever
# uses a group still needs every machine it resolves to in their own scope.
MANAGE_DEVICE_GROUPS = "manage_device_groups"
# Creating PC folders and filing machines into them (pc_tree.py) -- the hub's own tree beside
# the AD OUs. Its own capability rather than a reuse of MANAGE_DEVICE_GROUPS, because a folder
# is a SCOPE as well as a target: a permission group scoped to a folder grants its members
# every PC filed beneath it, so moving a PC into a folder hands it to people. Folding that into
# "may edit a saved filter" would have handed it, on the day it shipped, to everyone who could
# already write one. The mover must still hold every PC they move in their own scope.
#
# READING the tree is deliberately NOT gated here -- that is VIEW, narrowed to the caller's
# scope like the Devices list.
MANAGE_PC_TREE = "manage_pc_tree"
# Writing Group Policy: policy objects, their links to the PC tree, and block-inheritance on a
# node (gpo.py, roadmap #32). Its own capability rather than MANAGE_SETTINGS or
# MANAGE_DEVICE_POLICY: a policy object writes into HKLM on every PC beneath where it is
# linked, applied without anybody present, and that is a different thing to hand out than a
# threshold or an Android app block. A policy that RUNS a library script additionally needs
# ISSUE_COMMANDS, exactly as a rule that does (gpo_web.py) -- a script is code as SYSTEM.
#
# READING policies, and a PC's resultant set of policy, is VIEW plus machine scope.
MANAGE_GROUP_POLICY = "manage_group_policy"
# Tickets opened from a PC (roadmap #31). READING them is its own capability rather than VIEW,
# because a ticket is not inventory: it is somebody's own words about their work, often with a
# picture of their screen, and an operator who may see that a PC exists has not thereby been
# shown what its user wrote to IT. MANAGING them -- replying, status, assignment, labels, the
# AI triage -- is a second one, so a group can watch the queue without answering it.
VIEW_TICKETS = "view_tickets"
MANAGE_TICKETS = "manage_tickets"

CAPABILITIES = (
    VIEW,
    VIEW_AUDIT_LOG,
    VIEW_SECURITY_AUDIT,
    ISSUE_COMMANDS,
    REMOTE_CONTROL,
    DEPLOY_PACKAGES,
    MANAGE_BACKUPS,
    MANAGE_FIRMWARE,
    MANAGE_PATCHES,
    READ_RECOVERY_KEYS,
    LOCATE_DEVICE,
    MANAGE_DEVICE_POLICY,
    WIPE_DEVICE,
    MANAGE_RULES,
    MANAGE_DEVICE_GROUPS,
    MANAGE_PC_TREE,
    MANAGE_GROUP_POLICY,
    VIEW_TICKETS,
    MANAGE_TICKETS,
    MANAGE_ONBOARDING,
    MANAGE_SETTINGS,
    MANAGE_USERS,
    MANAGE_PERMISSION_GROUPS,
)

# The label and description each capability is shown with live in the translation
# catalogs, under `permissions.capability.<name>.label` / `.description` --
# `permissions_web.permissions_capabilities` reads them in the caller's language, so the
# API stays self-describing while the admin UI reads in the operator's language.
#
# They used to be a CAPABILITY_LABELS dict here, which was the right home while the
# console was English-only. Two homes for one string is how a catalog rots, so there is
# now exactly one: `en.json` is the schema (a key absent from it does not exist), and
# tests/test_i18n.py fails if a capability in the tuple above has no entries -- a new
# capability cannot reach the UI nameless.
CAPABILITY_TEXT_KEY = "permissions.capability"

# Machine-scope resolution modes. "list" is the v1 explicit list; "all" is the
# fleet-wide group (a global auditor, or the group that replaces break-glass once a
# deployment stops relying on ALLOWED_EMAILS); "ad_ou" resolves the machine set from
# Active Directory OUs (roadmap #4) -- which is why this was a mode column from the
# start rather than an is_all_machines flag.
SCOPE_LIST = "list"
SCOPE_ALL = "all"
SCOPE_AD_OU = "ad_ou"
# "folder" is the same idea over the hub's own PC tree (pc_tree.py) instead of AD: the group
# stores folder ids and its machines are every PC filed in them or in a folder beneath. It
# exists for the PCs AD does not know about -- a hub with no directory, or the workgroup boxes
# in a domain shop -- and resolves at cache-build time exactly like ad_ou, for the same
# reason: only a write to the tree can change the answer, and every such write invalidates.
SCOPE_FOLDER = "folder"
SCOPE_MODES = (SCOPE_LIST, SCOPE_ALL, SCOPE_AD_OU, SCOPE_FOLDER)

# How deep the group tree may go. Not a performance limit -- resolving eight ancestors is
# nothing -- but a readability one: past this nobody can say what a leaf group grants without
# drawing it, and the whole argument for nesting is that the tree on screen IS the answer.
MAX_GROUP_DEPTH = 8

# An `ad_ou` group stores OU distinguished names and its machine list is DERIVED from
# them at cache-build time, rather than resolved per request. That is safe for exactly
# one reason, and it is worth being explicit about it: the only thing that can change
# which machines are in an OU is a directory sync, and directory.sync_once invalidates
# this cache when any machine's OU actually moves. A machine enrolling has no OU until
# the next sync either, so there is no window where the derived list is stale in a way a
# per-request resolve would have caught.
MAX_OU_CHARS = 1024

MAX_NAME_CHARS = 80

# ================================
# DIRECTORY GROUP MAPPING (roadmap #4)
# ================================
# A permission group can name directory groups as well as individual emails. At sign-in
# the issuer tells us which directory groups the user is in; any permission group that
# names one of them applies, exactly as though the user had been added by email.
#
# The claims below are checked in order and their values UNIONED, because "which claim
# carries group membership" is per-issuer and not something an admin should have to know:
# Entra puts security-group object ids in `groups` and app roles in `roles`; Okta,
# Keycloak and Authentik all use `groups`; `wids` carries Entra's built-in directory
# roles (the tenant-wide "Global Administrator" and friends), which is the one token an
# admin is likely to reach for on day one.
DIRECTORY_GROUP_CLAIMS = ("groups", "roles", "wids")

# What a token may look like is deliberately NOT constrained beyond this. Entra sends
# GUIDs, ADFS and on-prem issuers send distinguished names, others send plain names --
# and the whole point of the `ad_group_dn` column being opaque is that it works for all
# three without a mode flag. Comparison is casefolded because every one of those forms is
# case-insensitive at its source (hex GUIDs, LDAP DNs, and group names alike), so a
# mapping typed as `CN=Hospital IT,OU=Groups,DC=x` must match a claim spelling it
# `cn=hospital it,ou=groups,dc=x`.
MAX_DIRECTORY_GROUP_CHARS = 512


# ================================
# DB SETUP
# ================================
def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_permissions_db(db_path):
    """Create the permission tables if absent. Idempotent -- safe to call next to the
    other init_*_db() functions on every hub start."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_groups (
                id                TEXT PRIMARY KEY,
                name              TEXT NOT NULL,
                description       TEXT,
                capabilities_json TEXT NOT NULL,   -- JSON array of CAPABILITIES members
                scope_mode        TEXT NOT NULL DEFAULT 'list',
                created_at        INTEGER NOT NULL,
                updated_at        INTEGER NOT NULL,
                updated_by        TEXT
            )
            """
        )
        # Case-insensitive: "Hospital IT" and "hospital it" being two groups is a
        # configuration accident every time, never an intent.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_permission_groups_name "
            "ON permission_groups(name COLLATE NOCASE)"
        )
        # Machines are referenced by hostname, matching machine_info's primary key. A
        # row for a machine that no longer exists is harmless (it grants access to
        # nothing) and is deliberately not foreign-keyed: deleting a machine must not
        # silently rewrite an operator's group definition.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_group_machines (
                group_id TEXT NOT NULL,
                machine  TEXT NOT NULL,
                PRIMARY KEY (group_id, machine)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pg_machines_machine "
            "ON permission_group_machines(machine)"
        )
        # Exactly one of email / ad_group_dn is set per row. SQLite treats NULLs as
        # distinct in a UNIQUE index, so the two indexes below coexist happily with
        # the other column left NULL.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_group_members (
                group_id    TEXT NOT NULL,
                email       TEXT,
                ad_group_dn TEXT,
                added_at    INTEGER NOT NULL,
                added_by    TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_members_email "
            "ON permission_group_members(group_id, email)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_members_ad "
            "ON permission_group_members(group_id, ad_group_dn)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pg_members_lookup "
            "ON permission_group_members(email)"
        )
        # The OU scope list for a scope_mode='ad_ou' group (roadmap #4). A table of its
        # own rather than more rows in permission_group_machines: those hold resolved
        # hostnames and this holds the RULE that produces them, and overloading one table
        # with both would make "is this machine explicitly granted, or is it just in the
        # OU today?" unanswerable -- which is exactly the question asked when revoking.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_group_ous (
                group_id TEXT NOT NULL,
                ou       TEXT NOT NULL,
                PRIMARY KEY (group_id, ou)
            )
            """
        )
        # The folder scope list for a scope_mode='folder' group -- the pc_tree twin of the
        # table above, kept separate from it for the same reason that one is separate from
        # the machine list.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_group_folders (
                group_id  TEXT NOT NULL,
                folder_id INTEGER NOT NULL,
                PRIMARY KEY (group_id, folder_id)
            )
            """
        )
        # The parent link that makes groups nest. ALTER-if-missing, the pattern
        # directory.init_directory_db uses, because this table predates nesting by many
        # releases and every existing hub has rows in it. NULL is a top-level group, which is
        # what every one of those rows becomes -- nesting arrives with no change to anybody's
        # access. Not foreign-keyed, like the machine rows: a dangling parent is treated as
        # "no parent" by _build, and delete_group refuses to leave one anyway.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(permission_groups)")}
        if "parent_id" not in existing:
            conn.execute("ALTER TABLE permission_groups ADD COLUMN parent_id TEXT")


# ================================
# NORMALISATION & VALIDATION
# ================================
def normalize_email(email):
    """Emails are identity here, so they are compared lowercased and stripped -- the
    same normalisation app.py applies to ALLOWED_EMAILS and to the OAuth claim. Doing
    it in one place is what keeps 'Ann@x.com' from being a different operator."""
    return str(email or "").strip().lower()


def email_from_claims(user_info):
    """Pick the email out of an OIDC userinfo / ID-token payload, or "" if there isn't one.

    Lives here rather than in app.py because it is an AUTHORIZATION decision, not a bit of
    sign-in plumbing: whatever this returns is the identity every permission group, every
    audit row and every terminal session is then keyed on. It belongs next to
    normalize_email for the same reason that does -- one place decides who someone is.

    `email` is the standard claim and what Google always sends, but it is not guaranteed.
    Microsoft Entra ID omits it in plenty of tenants and puts the sign-in name in
    `preferred_username` or `upn` instead, so falling back is the difference between "Entra
    works" and "Entra signs in and is then refused for having no email".

    The "@" test is the guard that makes those fallbacks safe. On some issuers
    `preferred_username` is a bare username ("administrator"), and accepting one of those
    as an identity would let it collide with a granted address -- so anything that isn't
    shaped like an address is ignored rather than trusted.
    """
    for claim in ("email", "preferred_username", "upn"):
        value = normalize_email((user_info or {}).get(claim))
        if "@" in value:
            return value
    return ""


def normalize_directory_group(token):
    """Fold a directory-group token to its comparison form. See the note beside
    DIRECTORY_GROUP_CLAIMS for why this is casefold-and-strip and nothing more."""
    return str(token or "").strip().casefold()


def has_group_claim_overage(user_info):
    """Did the issuer decline to send the group list because it was too long?

    Entra stops emitting `groups` once a user is in more than ~200 of them and sends
    `_claim_names`/`_claim_sources` pointing at a Graph endpoint instead. Resolving that
    needs a Graph token this hub does not hold, so we do not pretend to -- but the caller
    must be able to tell "this user is in no mapped group" from "the issuer never told us
    which groups this user is in". Those two look identical downstream and only one of
    them is a configuration error, so an unreported overage is an admin staring at a
    correct mapping that appears to do nothing.
    """
    names = (user_info or {}).get("_claim_names") or {}
    if not isinstance(names, dict):
        return False
    return any(claim in names for claim in DIRECTORY_GROUP_CLAIMS)


def directory_groups_from_claims(user_info):
    """Every directory-group token an issuer asserted, normalised and deduplicated.

    Lives here beside email_from_claims for the same reason that does: this is an
    AUTHORIZATION input, not sign-in plumbing, and one place should decide what the hub
    believes about who someone is. Non-string entries are dropped rather than coerced --
    a claim shaped unexpectedly must grant nothing, not stringify into a token that might
    collide with a real mapping.
    """
    seen = []
    for claim in DIRECTORY_GROUP_CLAIMS:
        values = (user_info or {}).get(claim)
        # A single-valued claim is a bare string on several issuers, a list on the rest.
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            token = normalize_directory_group(value)
            if token and token not in seen:
                seen.append(token)
    return seen


def normalize_machine(machine):
    return str(machine or "").strip()


def _validate_name(name):
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("A group name is required.")
    if len(cleaned) > MAX_NAME_CHARS:
        raise ValueError(f"Group name must be at most {MAX_NAME_CHARS} characters.")
    return cleaned


def _validate_capabilities(capabilities):
    """Accepts a list of capability names, or a dict of {name: bool} as the admin form
    posts it. Returns a sorted list. Unknown names are an error rather than being
    dropped: silently ignoring a typo'd capability is how a group ends up quietly
    less privileged than the admin believes it is."""
    if isinstance(capabilities, dict):
        names = [k for k, v in capabilities.items() if v]
    elif isinstance(capabilities, (list, tuple, set, frozenset)):
        names = list(capabilities)
    elif capabilities is None:
        names = []
    else:
        raise ValueError("capabilities must be a list of capability names.")

    cleaned = []
    for name in names:
        text = str(name or "").strip()
        if text not in CAPABILITIES:
            raise ValueError(f"Unknown capability: {text!r}")
        if text not in cleaned:
            cleaned.append(text)
    return sorted(cleaned, key=CAPABILITIES.index)


def _validate_scope(scope_mode, machines):
    mode = str(scope_mode or SCOPE_LIST).strip().lower()
    if mode not in SCOPE_MODES:
        raise ValueError(f"Unknown scope mode: {mode!r}")
    cleaned = []
    for machine in (machines or []):
        name = normalize_machine(machine)
        if name and name not in cleaned:
            cleaned.append(name)
    # An explicit-list group with no machines is legal (a group being built up, or one
    # whose machines were decommissioned). It simply grants access to nothing.
    return mode, sorted(cleaned)


def _validate_ous(ous):
    """Normalise the OU distinguished names an `ad_ou` group scopes to.

    Stored in the admin's original case (it is displayed back to them), and compared
    casefolded component-wise by directory.ou_contains -- the same split between display
    form and comparison form the DN helpers over there use.
    """
    cleaned = []
    for ou in (ous or []):
        if not isinstance(ou, str):
            raise ValueError(f"{ou!r} is not an organizational unit name.")
        text = ou.strip()
        if not text:
            continue
        if len(text) > MAX_OU_CHARS:
            raise ValueError(f"Organizational unit names are limited to {MAX_OU_CHARS} "
                             f"characters.")
        # A DN has at least one `component=value`. Refusing anything else is what stops a
        # hostname being pasted into the OU field, where it would match nothing forever
        # while looking like a configured scope.
        if "=" not in text:
            raise ValueError(
                f"{ou!r} does not look like an organizational unit. Use its distinguished "
                f"name, e.g. OU=Clinical,DC=corp,DC=local.")
        if text not in cleaned:
            cleaned.append(text)
    return sorted(cleaned)


def _validate_folders(folders):
    """Normalise the PC-folder ids a `folder` group scopes to. Existence is NOT checked
    here: a folder deleted later must not make the group unsaveable, and a missing folder
    resolves to no machines, which is the closed direction. pc_tree.delete_folder refuses
    to delete a folder a group still names, so this only happens to rows edited by hand."""
    cleaned = []
    for folder in (folders or []):
        try:
            folder_id = int(folder)
        except (TypeError, ValueError):
            raise ValueError(f"{folder!r} is not a PC folder.")
        if folder_id <= 0:
            raise ValueError(f"{folder!r} is not a PC folder.")
        if folder_id not in cleaned:
            cleaned.append(folder_id)
    return sorted(cleaned)


def _validate_members(members):
    cleaned = []
    for member in (members or []):
        email = normalize_email(member)
        if not email:
            continue
        if "@" not in email:
            raise ValueError(f"{member!r} is not an email address.")
        if email not in cleaned:
            cleaned.append(email)
    return sorted(cleaned)


def _validate_directory_groups(tokens):
    """Normalise the directory-group tokens a group maps, refusing the shapes that
    would silently grant more than the admin meant.

    The only real hazard here is a token that normalises to empty or to something a
    claim can never carry: stored, it would sit in the UI looking like a live grant
    while matching nothing forever. Length is capped because an LDAP DN is the longest
    legitimate form and 512 characters is well past the longest real one.
    """
    cleaned = []
    for token in (tokens or []):
        if not isinstance(token, str):
            raise ValueError(f"{token!r} is not a directory group identifier.")
        normalized = normalize_directory_group(token)
        if not normalized:
            continue
        if len(normalized) > MAX_DIRECTORY_GROUP_CHARS:
            raise ValueError(
                f"Directory group identifiers are limited to "
                f"{MAX_DIRECTORY_GROUP_CHARS} characters.")
        if normalized not in cleaned:
            cleaned.append(normalized)
    return sorted(cleaned)


# ================================
# THE CACHE
# ================================
# Every authorized request resolves the caller's effective permissions, and a machine
# list filters per row -- so this sits in the hottest read path in the hub. Writes are
# vanishingly rare (an admin editing a group). Same copy-on-write discipline as
# settings.py, and the same caveat: it is per-process, correct under the single
# waitress process the hub runs as, and would need a version-row poll if the hub ever
# ran multiple workers. Readers take one atomic global read and never mutate what they
# get; writers build a complete new state and rebind in one assignment.

_state = None                    # dict: {"groups": {...}, "by_email": {...}}
_state_lock = threading.Lock()   # serialises writers and cold loads ONLY


def invalidate():
    """Drop the cache so the next read rebuilds from the DB. Called after every write
    here, and exposed for tests that write rows behind this module's back."""
    global _state
    with _state_lock:
        _state = None


def _build(db_path):
    groups = {}
    by_email = {}
    by_directory_group = {}
    with get_conn(db_path) as conn:
        for row in conn.execute(
            "SELECT id, name, description, capabilities_json, scope_mode, parent_id, "
            "created_at, updated_at, updated_by FROM permission_groups"
        ):
            try:
                capabilities = json.loads(row["capabilities_json"]) or []
            except (TypeError, ValueError):
                # A corrupt row must fail CLOSED (no capabilities), never open.
                capabilities = []
            groups[row["id"]] = {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                # Drop anything no longer a known capability, so removing one from
                # CAPABILITIES actually revokes it rather than leaving a live string.
                "capabilities": [c for c in capabilities if c in CAPABILITIES],
                "scope_mode": row["scope_mode"] if row["scope_mode"] in SCOPE_MODES else SCOPE_LIST,
                "parent_id": row["parent_id"] or None,
                "machines": [],
                "members": [],
                "directory_groups": [],
                "ous": [],
                "folders": [],
                # Filled in by _resolve_inheritance below, once every group's own scope is
                # known. `ancestors` is nearest-first.
                "ancestors": [],
                "inherited_capabilities": [],
                "inherited_all": False,
                "inherited_machines": [],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
            }
        for row in conn.execute(
            "SELECT group_id, machine FROM permission_group_machines ORDER BY machine"
        ):
            group = groups.get(row["group_id"])
            if group is not None:
                group["machines"].append(row["machine"])
        for row in conn.execute(
            "SELECT group_id, email FROM permission_group_members "
            "WHERE email IS NOT NULL ORDER BY email"
        ):
            group = groups.get(row["group_id"])
            if group is None:
                continue
            group["members"].append(row["email"])
            by_email.setdefault(row["email"], []).append(row["group_id"])
        # Directory-group members (roadmap #4). Rows are written already normalised, but
        # normalise on read too: the column predates this feature by several releases, so
        # a row could have been put there by hand or by an older migration.
        for row in conn.execute(
            "SELECT group_id, ad_group_dn FROM permission_group_members "
            "WHERE ad_group_dn IS NOT NULL ORDER BY ad_group_dn"
        ):
            group = groups.get(row["group_id"])
            if group is None:
                continue
            token = normalize_directory_group(row["ad_group_dn"])
            if not token:
                continue
            group["directory_groups"].append(token)
            by_directory_group.setdefault(token, []).append(row["group_id"])
        for row in conn.execute(
            "SELECT group_id, ou FROM permission_group_ous ORDER BY ou"
        ):
            group = groups.get(row["group_id"])
            if group is not None:
                group["ous"].append(row["ou"])
        for row in conn.execute(
            "SELECT group_id, folder_id FROM permission_group_folders ORDER BY folder_id"
        ):
            group = groups.get(row["group_id"])
            if group is not None:
                group["folders"].append(row["folder_id"])

    # Resolve every ad_ou group's OUs to the machines actually in them, ONCE per cache
    # build, so machine_in_scope and visible_machine_filter keep working unchanged
    # downstream. See the note by SCOPE_AD_OU for why a snapshot is correct here: only a
    # directory sync can change the answer, and it invalidates this cache when it does.
    ou_groups = [g for g in groups.values() if g["scope_mode"] == SCOPE_AD_OU]
    if ou_groups:
        _resolve_ou_scopes(db_path, ou_groups)
    folder_groups = [g for g in groups.values() if g["scope_mode"] == SCOPE_FOLDER]
    if folder_groups:
        _resolve_folder_scopes(db_path, folder_groups)

    # Last, because a group inherits its ancestors' RESOLVED scope -- an ad_ou parent hands
    # its children the machines in its OUs, not the OU strings.
    _resolve_inheritance(groups)

    return {"groups": groups, "by_email": by_email,
            "by_directory_group": by_directory_group}


def _ancestor_ids(groups, group_id):
    """The chain of parents above `group_id`, nearest first.

    A missing parent ends the chain there: the group is top-level from that point up, which
    is what it would be had the parent been deleted properly. A cycle, or a chain deeper than
    MAX_GROUP_DEPTH, returns NO ancestors at all. The writes below refuse to create either,
    so meeting one means a row was edited by hand -- and the answer to that must be LESS
    access, never more. Cutting a cycle part-way would still let every group in it inherit
    from the others, which is a grant nobody can see on screen.
    """
    chain = []
    seen = {group_id}
    current = groups.get(group_id)
    while current is not None and current.get("parent_id"):
        parent_id = current["parent_id"]
        if parent_id not in groups:
            break
        if parent_id in seen or len(chain) >= MAX_GROUP_DEPTH - 1:
            return []
        chain.append(parent_id)
        seen.add(parent_id)
        current = groups[parent_id]
    return chain


def _resolve_inheritance(groups):
    """Fill in each group's `ancestors` and what it inherits from them.

    Stored beside the group's OWN fields, never merged into them. The editor shows and saves
    the own fields, and a child that saved its inherited capabilities as its own would keep
    them after being moved out from under the parent -- a grant nobody made, which is the
    exact failure this whole module exists to prevent.
    """
    for group_id, group in groups.items():
        ancestors = _ancestor_ids(groups, group_id)
        own_caps = set(group["capabilities"])
        own_machines = set(group["machines"])
        inherited_caps = set()
        inherited_machines = set()
        inherited_all = False
        for ancestor_id in ancestors:
            ancestor = groups[ancestor_id]
            inherited_caps.update(ancestor["capabilities"])
            if ancestor["scope_mode"] == SCOPE_ALL:
                inherited_all = True
            else:
                inherited_machines.update(ancestor["machines"])
        group["ancestors"] = ancestors
        group["inherited_capabilities"] = sorted(inherited_caps - own_caps,
                                                 key=CAPABILITIES.index)
        group["inherited_all"] = inherited_all
        group["inherited_machines"] = sorted(inherited_machines - own_machines)


def _resolve_folder_scopes(db_path, folder_groups):
    """Fill in `machines` for each folder group from the PC tree. Imported lazily for the
    reason _resolve_ou_scopes gives, and failing CLOSED the same way: a tree that cannot be
    read grants nothing, never everything."""
    try:
        import pc_tree
    except Exception:
        return
    for group in folder_groups:
        try:
            group["machines"] = pc_tree.machines_in_folders(db_path, group["folders"])
        except sqlite3.Error:
            group["machines"] = []


def _resolve_ou_scopes(db_path, ou_groups):
    """Fill in `machines` for each ad_ou group from the fleet's current OUs.

    Imported here rather than at module scope: permissions.py is the access-control core
    and is imported by everything, while directory.py imports alerts and fleet -- a
    top-level import would make that a cycle. It is also the honest dependency direction,
    since AD is an optional feature this module works fine without.

    A failure to resolve leaves `machines` EMPTY, never unrestricted. If the AD columns
    are somehow unreadable, an ad_ou group must grant access to nothing rather than
    falling through to a wider scope.
    """
    try:
        import directory
    except Exception:
        return
    try:
        with get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT machine, ad_ou FROM machine_info "
                "WHERE ad_ou IS NOT NULL AND ad_ou <> ''").fetchall()
    except sqlite3.Error:
        # machine_info without the AD columns -- a hub that has not run
        # directory.init_directory_db yet. No OU data means no machines in scope.
        return
    fleet_ous = [(row["machine"], row["ad_ou"]) for row in rows]
    for group in ou_groups:
        matched = [machine for machine, ad_ou in fleet_ous
                   if any(directory.ou_contains(scope, ad_ou) for scope in group["ous"])]
        group["machines"] = sorted(matched)


def _get_state(db_path):
    global _state
    state = _state
    if state is None:
        with _state_lock:
            if _state is None:
                _state = _build(db_path)
            state = _state
    return state


# ================================
# READS
# ================================
def _copy_group(group):
    """A caller-owned copy of a cached group. Every list is copied too: callers (the API
    layer, the UI) mutate what they get, and the cache must never be one of them. One
    helper rather than three inline dict() calls, so a new list-valued field cannot be
    deep-copied on one read path and shared on another."""
    return dict(group,
                capabilities=list(group["capabilities"]),
                machines=list(group["machines"]),
                members=list(group["members"]),
                directory_groups=list(group["directory_groups"]),
                ous=list(group["ous"]),
                folders=list(group["folders"]),
                ancestors=list(group["ancestors"]),
                inherited_capabilities=list(group["inherited_capabilities"]),
                inherited_machines=list(group["inherited_machines"]))


def list_groups(db_path):
    """Every group, ordered by name. Returns copies."""
    groups = [_copy_group(g) for g in _get_state(db_path)["groups"].values()]
    return sorted(groups, key=lambda g: g["name"].lower())


def get_group(db_path, group_id):
    """One group, or None."""
    group = _get_state(db_path)["groups"].get(str(group_id or "").strip())
    return None if group is None else _copy_group(group)


def effective_grant(db_path, group_id):
    """What membership of ONE group actually hands somebody, inheritance included:

        {"capabilities": set, "machines": set|None}

    `machines` is None when the group or any ancestor is fleet-wide. This is the question
    every "may you hand this group out?" check has to ask -- invites, above all. Asking it of
    the group's own fields instead would let a scoped admin invite somebody into an empty
    child of a fleet-wide parent, which reads as harmless and grants everything.
    """
    group = _get_state(db_path)["groups"].get(str(group_id or "").strip())
    if group is None:
        return None
    capabilities = set(group["capabilities"]) | set(group["inherited_capabilities"])
    if group["scope_mode"] == SCOPE_ALL or group["inherited_all"]:
        return {"capabilities": capabilities, "machines": None}
    return {"capabilities": capabilities,
            "machines": set(group["machines"]) | set(group["inherited_machines"])}


def children_of(db_path, group_id):
    """The groups directly beneath `group_id`, as copies, ordered by name."""
    group_id = str(group_id or "").strip()
    found = [_copy_group(g) for g in _get_state(db_path)["groups"].values()
             if g["parent_id"] == group_id]
    return sorted(found, key=lambda g: g["name"].lower())


def _with_ancestors(state, groups):
    """`groups` plus every ancestor of each, deduplicated, ancestors after the groups that
    brought them in. This IS nesting at the point it takes effect: a member of a child
    group is a member of its parents, so the parents join their group list here and
    everything downstream unions them exactly as it unions any other group."""
    out = list(groups)
    seen = {g["id"] for g in out}
    for group in list(out):
        for ancestor_id in group["ancestors"]:
            if ancestor_id not in seen and ancestor_id in state["groups"]:
                seen.add(ancestor_id)
                out.append(_copy_group(state["groups"][ancestor_id]))
    return out


def groups_for_email(db_path, email):
    """The groups this user belongs to by email, as full group dicts."""
    state = _get_state(db_path)
    ids = state["by_email"].get(normalize_email(email), ())
    return [_copy_group(state["groups"][gid]) for gid in ids if gid in state["groups"]]


def groups_for_directory_groups(db_path, directory_groups):
    """The groups granted by the directory groups an issuer asserted for this session.

    Order is by permission-group name so the result is stable regardless of the order
    the issuer happened to list the user's groups in -- that ordering is not meaningful
    and letting it leak into the audit trail or the UI makes two identical sessions look
    different.
    """
    state = _get_state(db_path)
    ids = []
    for token in (directory_groups or []):
        for gid in state["by_directory_group"].get(normalize_directory_group(token), ()):
            if gid not in ids:
                ids.append(gid)
    found = [_copy_group(state["groups"][gid]) for gid in ids if gid in state["groups"]]
    return sorted(found, key=lambda g: g["name"].lower())


def mapped_directory_groups(db_path):
    """Every directory-group token any permission group maps, as a set.

    This is what lets sign-in store only the INTERSECTION of what the issuer claimed and
    what this hub actually maps. That matters: Flask sessions are signed cookies with a
    ~4 KB budget, and a user in 200 Entra groups carries 7 KB of GUIDs -- enough to break
    the cookie, and therefore the login, for exactly the users who are in the most groups.
    A claimed group nothing maps can never affect authorization, so dropping it at the
    door loses nothing and bounds the session by the admin's own configuration.
    """
    return set(_get_state(db_path)["by_directory_group"].keys())


def is_superuser(email, superusers):
    """Break-glass: membership in ALLOWED_EMAILS grants everything over everything."""
    return normalize_email(email) in {normalize_email(e) for e in (superusers or ())}


def effective_permissions(db_path, email, superusers=(), directory_groups=()):
    """What this user may actually do, as one dict:

        {"email", "superuser", "capabilities": set, "machines": set|None, "groups": [...]}

    `machines` is None when the user's scope is EVERY machine (a superuser, or a
    member of any scope_mode="all" group) -- deliberately None rather than a set of
    every hostname, so callers cannot accidentally freeze a snapshot of the fleet and
    then miss a machine that enrolled a second later. Callers must treat None as
    "unrestricted"; machine_in_scope() and visible_machine_filter() do.

    Note the union semantics: capabilities from one group apply to machines from
    another. That is the documented model (effective permission = union across
    groups), and it is what makes "give Ann command rights on the Hospital PCs" a
    matter of adding her to one group rather than editing a matrix. Where that is too
    coarse, the answer is a narrower group, not a per-group intersection -- an
    intersection model makes the effect of adding someone to a group depend on every
    other group they are in, which no one can reason about.

    `directory_groups` are the tokens the identity provider asserted for THIS SESSION
    (roadmap #4). They union in exactly like email membership -- a group reached by
    directory mapping is not a lesser grant, it is the same grant reached another way,
    which is what makes "map the Hospital IT Entra group once" a replacement for
    maintaining an email list by hand. They are a session input rather than a stored
    one because that is what they are: the hub never queries the directory, it believes
    what the issuer signed at sign-in, and that belief expires with the session.
    """
    email = normalize_email(email)
    if is_superuser(email, superusers):
        return {
            "email": email,
            "superuser": True,
            "capabilities": set(CAPABILITIES),
            "machines": None,
            "groups": [],
        }

    capabilities = set()
    machines = set()
    all_machines = False
    # Deduplicate by id: someone can be both an explicit member and in a mapped
    # directory group, and counting that group twice would double every list it appears
    # in on the "my permissions" page.
    groups = groups_for_email(db_path, email)
    seen_ids = {group["id"] for group in groups}
    for group in groups_for_directory_groups(db_path, directory_groups):
        if group["id"] not in seen_ids:
            seen_ids.add(group["id"])
            groups.append(group)
    # Nesting: every parent of a group someone is in counts as a group they are in. Done
    # once here rather than in each lookup above, so a parent reached both by email and by
    # a directory mapping is still counted once.
    groups = _with_ancestors(_get_state(db_path), groups)
    for group in groups:
        capabilities.update(group["capabilities"])
        if group["scope_mode"] == SCOPE_ALL:
            all_machines = True
        else:
            # Both "list" and "ad_ou" arrive here as a concrete machine list -- the OU
            # group's was derived at cache-build time. Union semantics are identical, so
            # an OU-scoped group composes with an explicit one without a special case.
            machines.update(group["machines"])
    return {
        "email": email,
        "superuser": False,
        "capabilities": capabilities,
        "machines": None if all_machines else machines,
        "groups": groups,
    }


def has_capability(permissions, capability):
    return capability in (permissions or {}).get("capabilities", ())


def machine_in_scope(permissions, machine):
    """Is this one machine inside the caller's scope? None scope means unrestricted."""
    scope = (permissions or {}).get("machines", set())
    if scope is None:
        return True
    return normalize_machine(machine) in scope


def visible_machine_filter(permissions):
    """A predicate for filtering a list of machine names down to the visible ones.
    Returns None when the caller is unrestricted, so hot paths can skip filtering
    entirely rather than running a no-op test per row."""
    scope = (permissions or {}).get("machines", set())
    if scope is None:
        return None
    return lambda machine: normalize_machine(machine) in scope


def members_of_machine(db_path, machine):
    """Every email that can reach `machine` through a group. Excludes superusers --
    they are not in the group tables at all. Used by the admin UI to answer "who has
    access to this box?".

    Also excludes anyone who reaches it through a mapped DIRECTORY group, and cannot do
    otherwise: the hub never queries the directory, so it has no way to enumerate that
    group's members -- it only ever learns that one person is in it, at their sign-in.
    Read this as "who has access by name", not as the complete list.

    A group reaches the box through its own scope OR an inherited one, so the members of a
    child group under a fleet-wide parent are listed too -- leaving them out would answer
    "who can touch this PC?" with a list missing exactly the people nesting added.
    """
    machine = normalize_machine(machine)
    emails = set()
    for group in _get_state(db_path)["groups"].values():
        if (group["scope_mode"] == SCOPE_ALL or group["inherited_all"]
                or machine in group["machines"] or machine in group["inherited_machines"]):
            emails.update(group["members"])
    return sorted(emails)


# ================================
# WRITES
# ================================
# Auditing lives here rather than in the HTTP layer (where settings_web.py puts it)
# because a permission change is the one edit whose record must exist no matter which
# caller made it -- a future CLI, an AD sync, a migration. fleet.audit never raises.

def _replace_machines(conn, group_id, machines):
    conn.execute("DELETE FROM permission_group_machines WHERE group_id = ?", (group_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO permission_group_machines(group_id, machine) VALUES (?, ?)",
        [(group_id, m) for m in machines],
    )


def _replace_members(conn, group_id, members, actor, now):
    conn.execute(
        "DELETE FROM permission_group_members WHERE group_id = ? AND email IS NOT NULL",
        (group_id,),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO permission_group_members(group_id, email, added_at, added_by) "
        "VALUES (?, ?, ?, ?)",
        [(group_id, e, now, actor) for e in members],
    )


def _replace_ous(conn, group_id, ous):
    conn.execute("DELETE FROM permission_group_ous WHERE group_id = ?", (group_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO permission_group_ous(group_id, ou) VALUES (?, ?)",
        [(group_id, o) for o in ous],
    )


def _replace_directory_groups(conn, group_id, tokens, actor, now):
    """The ad_group_dn half of the member table. Scoped to `ad_group_dn IS NOT NULL` so
    saving a group's directory mappings never touches its email members, and vice versa
    -- the two halves share a table but are edited by different fields of the form."""
    conn.execute(
        "DELETE FROM permission_group_members WHERE group_id = ? AND ad_group_dn IS NOT NULL",
        (group_id,),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO permission_group_members"
        "(group_id, ad_group_dn, added_at, added_by) VALUES (?, ?, ?, ?)",
        [(group_id, t, now, actor) for t in tokens],
    )


def _replace_folders(conn, group_id, folders):
    conn.execute("DELETE FROM permission_group_folders WHERE group_id = ?", (group_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO permission_group_folders(group_id, folder_id) VALUES (?, ?)",
        [(group_id, f) for f in folders],
    )


def _subtree_height(groups, group_id):
    """1 for a group with no children, 2 with children but no grandchildren, and so on.
    Bounded by the depth cap the writes enforce, and guarded against a hand-made cycle."""
    children = {}
    for gid, group in groups.items():
        if group["parent_id"]:
            children.setdefault(group["parent_id"], []).append(gid)
    height, level, seen = 0, [group_id], {group_id}
    while level:
        height += 1
        level = [c for gid in level for c in children.get(gid, ()) if c not in seen]
        seen.update(level)
    return height


def _validate_parent(db_path, group_id, parent_id):
    """Normalise a requested parent. "" and None both mean top-level.

    Refuses a parent that does not exist, the group itself, any group beneath it (a cycle
    -- the group would inherit from itself through its own children), and a placement that
    would push the deepest group in the moved subtree past MAX_GROUP_DEPTH. Moving a group
    moves its whole subtree, so the depth check is on the subtree, not the one group.
    """
    parent_id = str(parent_id or "").strip()
    if not parent_id:
        return None
    groups = _get_state(db_path)["groups"]
    if parent_id not in groups:
        raise ValueError("The parent group no longer exists.")
    if group_id and parent_id == group_id:
        raise ValueError("A group cannot be its own parent.")
    if group_id and group_id in ([parent_id] + _ancestor_ids(groups, parent_id)):
        raise ValueError("A group cannot be moved beneath one of its own subgroups.")
    parent_depth = len(_ancestor_ids(groups, parent_id)) + 1
    height = _subtree_height(groups, group_id) if group_id in groups else 1
    if parent_depth + height > MAX_GROUP_DEPTH:
        raise ValueError(f"Groups can nest at most {MAX_GROUP_DEPTH} levels deep.")
    return parent_id


def create_group(db_path, name, capabilities=(), machines=(), members=(),
                 scope_mode=SCOPE_LIST, description=None, directory_groups=(),
                 ous=(), folders=(), parent_id=None, actor="unknown"):
    """Create a group and return its id. Raises ValueError on invalid input or a
    duplicate name -- everything is validated before anything is written."""
    name = _validate_name(name)
    capabilities = _validate_capabilities(capabilities)
    scope_mode, machines = _validate_scope(scope_mode, machines)
    members = _validate_members(members)
    directory_groups = _validate_directory_groups(directory_groups)
    ous = _validate_ous(ous)
    folders = _validate_folders(folders)
    parent_id = _validate_parent(db_path, None, parent_id)
    description = (str(description).strip() or None) if description else None

    group_id = uuid.uuid4().hex
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO permission_groups(id, name, description, capabilities_json, "
                "scope_mode, parent_id, created_at, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (group_id, name, description, json.dumps(capabilities), scope_mode,
                 parent_id, now, now, actor),
            )
            _replace_machines(conn, group_id, machines)
            _replace_members(conn, group_id, members, actor, now)
            _replace_directory_groups(conn, group_id, directory_groups, actor, now)
            _replace_ous(conn, group_id, ous)
            _replace_folders(conn, group_id, folders)
    except sqlite3.IntegrityError:
        raise ValueError(f"A permission group named {name!r} already exists.")
    invalidate()
    fleet.audit(db_path, actor, "permission_group.create", name, {
        "group_id": group_id, "capabilities": capabilities,
        "scope_mode": scope_mode, "machines": machines, "members": members,
        "directory_groups": directory_groups, "ous": ous, "folders": folders,
        "parent_id": parent_id,
    }, level=fleet.LEVEL_SECURITY)
    return group_id


def update_group(db_path, group_id, name=None, capabilities=None, machines=None,
                 members=None, scope_mode=None, description=None,
                 directory_groups=None, ous=None, folders=None, parent_id=None,
                 actor="unknown"):
    """Patch a group in place. Every argument left as None is left untouched -- pass
    an empty list to actually clear machines or members, and "" as `parent_id` to move
    the group to the top level. Raises KeyError if the group is gone, ValueError on
    invalid input or a duplicate name."""
    group_id = str(group_id or "").strip()
    before = get_group(db_path, group_id)
    if before is None:
        raise KeyError(group_id)

    new_name = _validate_name(name) if name is not None else before["name"]
    new_caps = (_validate_capabilities(capabilities) if capabilities is not None
                else list(before["capabilities"]))
    new_mode, new_machines = _validate_scope(
        before["scope_mode"] if scope_mode is None else scope_mode,
        before["machines"] if machines is None else machines,
    )
    new_members = (_validate_members(members) if members is not None
                   else list(before["members"]))
    new_directory_groups = (_validate_directory_groups(directory_groups)
                            if directory_groups is not None
                            else list(before["directory_groups"]))
    new_ous = _validate_ous(ous) if ous is not None else list(before["ous"])
    new_folders = (_validate_folders(folders) if folders is not None
                   else list(before["folders"]))
    new_parent = (_validate_parent(db_path, group_id, parent_id) if parent_id is not None
                  else before["parent_id"])
    if description is None:
        new_description = before["description"]
    else:
        new_description = str(description).strip() or None

    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE permission_groups SET name = ?, description = ?, "
                "capabilities_json = ?, scope_mode = ?, parent_id = ?, updated_at = ?, "
                "updated_by = ? WHERE id = ?",
                (new_name, new_description, json.dumps(new_caps), new_mode, new_parent,
                 now, actor, group_id),
            )
            if machines is not None or scope_mode is not None:
                _replace_machines(conn, group_id, new_machines)
            if members is not None:
                _replace_members(conn, group_id, new_members, actor, now)
            if directory_groups is not None:
                _replace_directory_groups(conn, group_id, new_directory_groups,
                                          actor, now)
            if ous is not None or scope_mode is not None:
                _replace_ous(conn, group_id, new_ous)
            if folders is not None or scope_mode is not None:
                _replace_folders(conn, group_id, new_folders)
    except sqlite3.IntegrityError:
        raise ValueError(f"A permission group named {new_name!r} already exists.")
    invalidate()

    after = get_group(db_path, group_id)
    # Record only what actually moved -- a full before/after on every save buries the
    # one edit that mattered under six unchanged fields. `parent_id` is in the list
    # because re-parenting is a grant: every member of this group and of every group
    # beneath it gains the new parent's capabilities and scope in that one edit.
    changes = {}
    for field in ("name", "description", "capabilities", "scope_mode", "machines",
                  "members", "directory_groups", "ous", "folders", "parent_id"):
        if before.get(field) != after.get(field):
            changes[field] = {"from": before.get(field), "to": after.get(field)}
    if changes:
        fleet.audit(db_path, actor, "permission_group.update", after["name"],
                    {"group_id": group_id, "changes": changes},
                    level=fleet.LEVEL_SECURITY)
    return after


def delete_group(db_path, group_id, actor="unknown"):
    """Remove a group and its machine/member rows. Raises KeyError if unknown.

    No "you can't delete the last admin group" guard, deliberately: ALLOWED_EMAILS is
    break-glass and always retains every capability, so there is no lockout to
    prevent. See the module docstring.

    **A group with subgroups is refused** (ValueError), naming them. The alternatives were
    both silent grant changes: deleting the subtree removes access from people nobody looked
    at, and re-parenting the children onto the grandparent changes what they inherit. The
    operator moves or deletes the subgroups first, and each of those is its own audited edit.
    """
    group_id = str(group_id or "").strip()
    before = get_group(db_path, group_id)
    if before is None:
        raise KeyError(group_id)
    children = children_of(db_path, group_id)
    if children:
        names = ", ".join(c["name"] for c in children[:5])
        more = f" (and {len(children) - 5} more)" if len(children) > 5 else ""
        raise ValueError(f"{before['name']} still has subgroups: {names}{more}. "
                         f"Move or delete them first.")
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM permission_group_machines WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM permission_group_members WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM permission_group_ous WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM permission_group_folders WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM permission_groups WHERE id = ?", (group_id,))
    invalidate()
    fleet.audit(db_path, actor, "permission_group.delete", before["name"], {
        "group_id": group_id, "capabilities": before["capabilities"],
        "machines": before["machines"], "members": before["members"],
        "directory_groups": before["directory_groups"], "ous": before["ous"],
        "folders": before["folders"], "parent_id": before["parent_id"],
    }, level=fleet.LEVEL_SECURITY)
    return True


def groups_using_folder(db_path, folder_ids):
    """Every FOLDER-SCOPED group naming any of `folder_ids`, as [{id, name}]. Used by
    pc_tree to refuse deleting a folder a group still scopes to: the group would quietly
    grant nothing, which looks like a scope that is in force.

    Only scope_mode='folder' groups count. A group switched to another mode keeps its folder
    list (the editor does that so switching back loses nothing), but that list grants
    nothing while the mode is different -- and if it is switched back after the folder is
    gone, the missing id resolves to no machines, which is the closed direction."""
    wanted = {int(f) for f in folder_ids}
    found = [{"id": g["id"], "name": g["name"]}
             for g in _get_state(db_path)["groups"].values()
             if g["scope_mode"] == SCOPE_FOLDER and wanted.intersection(g["folders"])]
    return sorted(found, key=lambda g: g["name"].lower())


def known_emails(db_path):
    """Every email that is a member of any group, for the admin UI's picker."""
    return sorted(_get_state(db_path)["by_email"].keys())


def forget_machine(db_path, machine):
    """Drop a machine from every group's scope. Called when a machine is hard-deleted,
    so a hostname later reused by a different box doesn't silently inherit the old
    box's access grants."""
    machine = normalize_machine(machine)
    if not machine:
        return 0
    with get_conn(db_path) as conn:
        removed = conn.execute(
            "DELETE FROM permission_group_machines WHERE machine = ?", (machine,)
        ).rowcount or 0
    if removed:
        invalidate()
    return removed


def rename_machine(db_path, old_machine, new_machine):
    """Re-point group scopes from `old_machine` to `new_machine`. Called on a
    duplicate-serial merge: the survivor is the same physical box, so a group that
    granted access to the old hostname must keep granting it -- otherwise a merge
    silently removes machines from operators' scopes."""
    old_machine = normalize_machine(old_machine)
    new_machine = normalize_machine(new_machine)
    if not old_machine or not new_machine or old_machine == new_machine:
        return 0
    with get_conn(db_path) as conn:
        # OR IGNORE, then DELETE: a group already scoped to both names would otherwise
        # collide on the (group_id, machine) primary key.
        moved = conn.execute(
            "UPDATE OR IGNORE permission_group_machines SET machine = ? WHERE machine = ?",
            (new_machine, old_machine),
        ).rowcount or 0
        conn.execute("DELETE FROM permission_group_machines WHERE machine = ?", (old_machine,))
    invalidate()
    return moved
