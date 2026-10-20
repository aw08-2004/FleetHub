"""Group Policy -- settings linked to the PC tree, inherited the way Windows GPOs are (roadmap #32).

The trees (pc_tree.py, permissions.py nesting) said WHERE a PC lives. This says what that
means for it: an administrator writes a policy object once, links it to a node of the tree --
every PC, an AD OU, a hub folder -- and every PC beneath that node gets it, with the nodes
closer to the PC winning. It is deliberately the Group Policy Management model an IT group
already knows, so the precedence rules below are Windows' rules, not new ones:

  * **Processing order.** Every PC first, then the AD domain and its OUs from the top down,
    then the hub folders from the top down. Later wins, so the node nearest the PC wins. Hub
    folders come AFTER the AD chain on purpose: a folder is the hub's own, more specific
    statement about a PC that AD files somewhere broader.
  * **Link order** within a node: order 1 wins, as in GPMC.
  * **Block inheritance** on a node drops every non-enforced link from the nodes above it.
  * **Enforced** links cannot be overridden from below and ignore block inheritance; between
    two enforced links, the one HIGHER in the tree wins -- again Windows' rule, and the only
    one under which "enforced" means something to the people above.

A policy object carries three kinds of setting, each delivered its own way:

  * **Agent settings** -- the `agent=True` hub settings (report interval, network collection,
    the tray...). Resolved per machine and merged into the heartbeat `config` block, so they
    work on every agent already in the field: the agent cannot tell a per-OU value from a
    fleet-wide one, and does not need to.
  * **Registry values** under `HKLM\\SOFTWARE\\Policies` only -- the tree Windows itself
    reserves for policy, and the one every ADMX template writes. Delivered as a versioned
    `gpo_document` on the heartbeat (the watchdog pattern), applied by the agent, which keeps
    its own copy of the same allow-list. **It never overwrites a value it did not write**: a
    value already there belongs to a domain GPO or a person, and is reported back as
    `managed_elsewhere` instead. Fighting real Group Policy every 90 minutes would be worse
    than not applying the setting.
  * **Scripts** from the library, run ONCE per PC per version of the script body, through the
    ordinary `run_script` command. Writing one needs `issue_commands` as well, exactly as a
    rule that runs one does.

**Every link remembers whose scope created it** (`author_scope`, the rules.scoped_targets
idea): a link made by an operator scoped to the Cajas PCs applies to those PCs only, even
after another PC is filed beneath the node. Without that, filing a PC (manage_pc_tree) would
silently hand it to a link somebody else wrote.

Rejected: an ADMX parser (large, and the allow-listed raw value is what every template
resolves to); authoring real Windows GPOs (the agent is not assumed to be domain-joined, and
SYSVOL is not ours to write); arbitrary registry paths (HKLM is the machine's trust root);
HKCU (per-user hives are only loaded while somebody is signed in, and "applies when Ana next
logs on" is a promise this agent cannot keep yet).

Authorization lives upstream in gpo_web.py. Kept free of Flask so it can be unit-tested in
isolation, like every other model module here.
"""
import hashlib
import json
import re
import sqlite3
import time

import directory
import fleet
import pc_tree
import rules
import scripts
import settings

NAME_MAX = 80
DESCRIPTION_MAX = 500
MAX_REGISTRY_VALUES = 200
MAX_SCRIPTS = 20
MAX_LINKS_PER_NODE = 50

NODE_ROOT = "root"
NODE_OU = "ou"
NODE_FOLDER = "folder"
NODE_KINDS = (NODE_ROOT, NODE_OU, NODE_FOLDER)

# ---------------------------------------------------------------- registry allow-list
# The agent enforces the same list (GroupPolicy/PolicyAllowList.cs). Twice on purpose: the hub
# check is what the operator sees at save time, and the agent check is what holds if the hub
# is ever wrong -- a hub is not a trust root for what LocalSystem writes into HKLM.
REGISTRY_ROOT = "SOFTWARE\\Policies\\"
# Inside the policy tree, keys something else on the machine already owns. WindowsUpdate is
# patches.py's (UpdateControl.cs writes it and would fight a second writer), and FleetHub's own
# key is where the agent keeps its markers.
REGISTRY_DENIED = (
    "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate",
    "SOFTWARE\\Policies\\FleetHub",
)
REG_DWORD = "dword"
REG_SZ = "string"
REG_EXPAND_SZ = "expand_string"
REG_MULTI_SZ = "multi_string"
REGISTRY_TYPES = (REG_DWORD, REG_SZ, REG_EXPAND_SZ, REG_MULTI_SZ)
MAX_PATH_CHARS = 255
MAX_VALUE_NAME_CHARS = 255
MAX_STRING_CHARS = 2048
MAX_MULTI_ITEMS = 64
_PATH_COMPONENT = re.compile(r"^[^\\/\x00-\x1f*?\"<>|]+$")

# What an agent reports per registry value. `refused` is the agent's own allow-list saying no,
# which only happens if the hub and agent disagree -- worth seeing, never worth retrying.
STATE_APPLIED = "applied"
STATE_MANAGED_ELSEWHERE = "managed_elsewhere"
STATE_ERROR = "error"
STATE_REFUSED = "refused"
STATES = (STATE_APPLIED, STATE_MANAGED_ELSEWHERE, STATE_ERROR, STATE_REFUSED)
STATE_TEXT_KEY = "gpo.state"

SCRIPT_QUEUED = "queued"
SCRIPT_DONE = "done"
SCRIPT_FAILED = "failed"
SCRIPT_SKIPPED = "skipped"
# A queued run whose command expired never reached the PC; it is retried rather than recorded
# as failed. Bounded so a PC that never comes back is not re-queued forever.
MAX_SCRIPT_ATTEMPTS = 3
# Commands queued per scheduler tick, fleet-wide. A policy linked to "every PC" must not turn
# into a burst of thousands of run_script rows in one transaction.
SCRIPTS_PER_TICK = 50


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_gpo_db(db_path):
    """Create the tables if absent. Idempotent, like every other init_*_db."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gpos (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL COLLATE NOCASE UNIQUE,
                description   TEXT NOT NULL DEFAULT '',
                enabled       INTEGER NOT NULL DEFAULT 1,
                settings_json TEXT NOT NULL,
                created_at    INTEGER NOT NULL,
                created_by    TEXT,
                updated_at    INTEGER NOT NULL,
                updated_by    TEXT
            )
            """
        )
        # node_key is the COMPARISON form: "" for every PC, the normalised DN for an OU (so two
        # spellings of one OU are one node), the folder id as text for a folder. node_label
        # keeps the OU in the case the admin's AD tools show it.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gpo_links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                gpo_id      INTEGER NOT NULL,
                node_kind   TEXT NOT NULL,
                node_key    TEXT NOT NULL,
                node_label  TEXT NOT NULL DEFAULT '',
                link_order  INTEGER NOT NULL,
                enforced    INTEGER NOT NULL DEFAULT 0,
                enabled     INTEGER NOT NULL DEFAULT 1,
                scope_json  TEXT,
                created_at  INTEGER NOT NULL,
                created_by  TEXT,
                UNIQUE (gpo_id, node_kind, node_key)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gpo_links_node "
                     "ON gpo_links(node_kind, node_key)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gpo_node_options (
                node_kind         TEXT NOT NULL,
                node_key          TEXT NOT NULL,
                block_inheritance INTEGER NOT NULL DEFAULT 0,
                updated_at        INTEGER NOT NULL,
                updated_by        TEXT,
                PRIMARY KEY (node_kind, node_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_gpo_registry_state (
                machine     TEXT NOT NULL,
                setting_id  TEXT NOT NULL,
                status      TEXT NOT NULL,
                detail      TEXT NOT NULL DEFAULT '',
                reported_at INTEGER NOT NULL,
                PRIMARY KEY (machine, setting_id)
            )
            """
        )
        # One row per (PC, script, body version). The hash is what makes "run once" survive an
        # edit correctly: a changed script body is a new hash, so it runs again; an unchanged
        # one never does, however often the policy is re-saved or re-linked.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gpo_script_runs (
                machine     TEXT NOT NULL,
                script_name TEXT NOT NULL,
                script_hash TEXT NOT NULL,
                gpo_id      INTEGER,
                command_id  TEXT,
                status      TEXT NOT NULL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                detail      TEXT NOT NULL DEFAULT '',
                queued_at   INTEGER,
                finished_at INTEGER,
                PRIMARY KEY (machine, script_name, script_hash)
            )
            """
        )
        # Which agents have said they can apply a registry document (they send gpo_version).
        # Presence, not an agent minor, is the capability -- the watchdog precedent -- and this
        # is what lets the console say "this PC's agent is too old for registry policy" instead
        # of showing a setting as pending forever.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_gpo_agent (
                machine   TEXT PRIMARY KEY,
                version   TEXT NOT NULL DEFAULT '',
                seen_at   INTEGER NOT NULL
            )
            """
        )


# ================================================================ the cache
# Every heartbeat resolves its PC's policy (the config block is compared on every one), so the
# policy objects, links and node options are cached and rebuilt only after a write here --
# the copy-on-write discipline permissions.py and settings.py use, with the same caveat: it is
# per-process, correct under the single waitress process the hub runs as. What is NOT cached
# is where a PC sits (its OU, its folder): those change through AD sync and pc_tree, and two
# indexed reads per heartbeat cost less than a second invalidation path would.
_cache = None


def invalidate():
    global _cache
    _cache = None


def _state(db_path):
    global _cache
    state = _cache
    if state is None:
        policies = list_gpos(db_path)
        state = {"links": list_links(db_path), "options": node_options(db_path),
                 "policies": {p["id"]: p for p in policies}}
        _cache = state
    return state


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()[:16]


# ================================================================ validation
def normalize_registry_path(path):
    """`HKLM\\SOFTWARE\\Policies\\X` and `SOFTWARE/Policies/X/` both become
    `SOFTWARE\\Policies\\X`. The hive prefix is accepted because it is what people paste
    out of regedit, and only HKLM is ever meant."""
    text = str(path or "").strip().replace("/", "\\").strip("\\")
    for prefix in ("HKEY_LOCAL_MACHINE\\", "HKLM:\\", "HKLM\\"):
        if text.upper().startswith(prefix):
            text = text[len(prefix):]
            break
    return "\\".join(part.strip() for part in text.split("\\"))


def registry_path_error(path):
    """Why `path` is not an allowed policy key, or None. Shared by validation and tests; the
    agent's PolicyAllowList.cs is the same rules in C#."""
    if not path:
        return "a registry value needs a key path"
    if len(path) > MAX_PATH_CHARS:
        return f"the key path is longer than {MAX_PATH_CHARS} characters"
    if not path.lower().startswith(REGISTRY_ROOT.lower()) or len(path) <= len(REGISTRY_ROOT):
        return "only keys under HKLM\\SOFTWARE\\Policies can be set by policy"
    parts = path.split("\\")
    if any(not part or part in (".", "..") or not _PATH_COMPONENT.match(part) for part in parts):
        return "the key path has an empty or invalid component"
    lowered = path.lower()
    for denied in REGISTRY_DENIED:
        d = denied.lower()
        if lowered == d or lowered.startswith(d + "\\"):
            return f"{denied} is managed by FleetHub itself and cannot be set by policy"
    return None


def setting_id(path, name):
    """A stable id for one registry value, the same on hub and agent: case-folded, because
    the registry is case-insensitive and two spellings are one value."""
    return hashlib.sha256(f"{path.lower()}|{name.lower()}".encode("utf-8")).hexdigest()[:16]


def _validate_registry_value(raw):
    if not isinstance(raw, dict):
        return "a registry value must be an object", None
    path = normalize_registry_path(raw.get("path"))
    error = registry_path_error(path)
    if error:
        return error, None
    name = str(raw.get("name") or "").strip()
    if not name:
        return "a registry value needs a value name (the default value cannot be set)", None
    if len(name) > MAX_VALUE_NAME_CHARS or "\\" in name or any(ord(c) < 32 for c in name):
        return f"the value name {name!r} is not valid", None
    kind = str(raw.get("type") or "").strip()
    if kind not in REGISTRY_TYPES:
        return f"the value type must be one of {', '.join(REGISTRY_TYPES)}", None
    value = raw.get("value")
    if kind == REG_DWORD:
        try:
            number = int(str(value).strip(), 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError):
            return f"{name}: a DWORD needs a whole number", None
        if isinstance(value, bool) or not 0 <= number <= 0xFFFFFFFF:
            return f"{name}: a DWORD is 0 to 4294967295", None
        value = number
    elif kind == REG_MULTI_SZ:
        items = value if isinstance(value, list) else str(value or "").splitlines()
        items = [str(i) for i in items]
        if len(items) > MAX_MULTI_ITEMS:
            return f"{name}: at most {MAX_MULTI_ITEMS} lines", None
        if any(len(i) > MAX_STRING_CHARS or "\x00" in i for i in items):
            return f"{name}: a line is too long or contains a NUL", None
        value = items
    else:
        value = "" if value is None else str(value)
        if len(value) > MAX_STRING_CHARS or "\x00" in value:
            return f"{name}: the text is longer than {MAX_STRING_CHARS} characters", None
    return None, {"id": setting_id(path, name), "path": path, "name": name,
                  "type": kind, "value": value}


def validate(payload, script_specs=None):
    """Check a policy object. Returns (error, clean). Errors are English, like every other
    model-layer refusal; the web layer passes them through.

    `script_specs` is scripts.specs(); passing None skips the script-reference check (tests)."""
    if not isinstance(payload, dict):
        return "a policy must be an object", None
    name = str(payload.get("name") or "").strip()
    if not name:
        return "a policy needs a name", None
    if len(name) > NAME_MAX:
        return f"the name is too long (limit {NAME_MAX})", None
    description = str(payload.get("description") or "").strip()
    if len(description) > DESCRIPTION_MAX:
        return f"the description is too long (limit {DESCRIPTION_MAX})", None

    raw_settings = payload.get("settings") or {}
    if not isinstance(raw_settings, dict):
        return "settings must be an object", None

    agent_settings = {}
    for key, raw in (raw_settings.get("agent_settings") or {}).items():
        setting = settings.BY_KEY.get(str(key))
        if setting is None or not setting.agent:
            return f"{key} is not a setting a policy can set", None
        try:
            value = settings.coerce_and_validate(setting, raw)
        except ValueError as e:
            return str(e), None
        if value is None:
            # "Not configured" -- the same as leaving the key out, not a value of its own.
            continue
        agent_settings[setting.key] = value

    registry = []
    seen = set()
    raw_registry = raw_settings.get("registry") or []
    if not isinstance(raw_registry, list):
        return "registry must be a list", None
    if len(raw_registry) > MAX_REGISTRY_VALUES:
        return f"too many registry values (limit {MAX_REGISTRY_VALUES})", None
    for raw in raw_registry:
        error, clean = _validate_registry_value(raw)
        if error:
            return error, None
        if clean["id"] in seen:
            return f"{clean['path']}\\{clean['name']} is set twice in this policy", None
        seen.add(clean["id"])
        registry.append(clean)

    script_entries = []
    raw_scripts = raw_settings.get("scripts") or []
    if not isinstance(raw_scripts, list):
        return "scripts must be a list", None
    if len(raw_scripts) > MAX_SCRIPTS:
        return f"too many scripts (limit {MAX_SCRIPTS})", None
    for raw in raw_scripts:
        entry = raw if isinstance(raw, dict) else {"name": raw}
        script_name = str(entry.get("name") or "").strip().lower()
        inputs = entry.get("inputs") or {}
        if script_specs is not None:
            error, inputs = scripts.validate_reference(script_specs, script_name, inputs)
            if error:
                return error, None
        if not script_name:
            return "a script entry needs a script", None
        if any(s["name"] == script_name for s in script_entries):
            return f"the script {script_name} is listed twice", None
        script_entries.append({"name": script_name, "inputs": inputs or {}})

    return None, {
        "name": name,
        "description": description,
        "enabled": bool(payload.get("enabled", True)),
        "settings": {"agent_settings": agent_settings, "registry": registry,
                     "scripts": script_entries},
    }


def has_scripts(policy):
    return bool((policy.get("settings") or {}).get("scripts"))


# ================================================================ policy objects
def _decode(row):
    policy = dict(row)
    try:
        policy["settings"] = json.loads(policy.pop("settings_json"))
    except (TypeError, ValueError):
        # A corrupt row fails CLOSED: a policy that sets nothing, never one that sets
        # something nobody wrote.
        policy["settings"] = {"agent_settings": {}, "registry": [], "scripts": []}
    policy["enabled"] = bool(policy["enabled"])
    return policy


def list_gpos(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM gpos ORDER BY name COLLATE NOCASE, id").fetchall()
    return [_decode(r) for r in rows]


def get_gpo(db_path, gpo_id):
    try:
        gpo_id = int(gpo_id)
    except (TypeError, ValueError):
        return None
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM gpos WHERE id = ?", (gpo_id,)).fetchone()
    return _decode(row) if row else None


def save_gpo(db_path, clean, gpo_id=None, actor=None):
    """Create (gpo_id None) or replace a validated policy. Returns (error, policy)."""
    now = int(time.time())
    try:
        with get_conn(db_path) as conn:
            if gpo_id is None:
                cur = conn.execute(
                    "INSERT INTO gpos (name, description, enabled, settings_json, created_at,"
                    " created_by, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (clean["name"], clean["description"], 1 if clean["enabled"] else 0,
                     json.dumps(clean["settings"]), now, actor, now, actor))
                gpo_id = cur.lastrowid
            else:
                cur = conn.execute(
                    "UPDATE gpos SET name = ?, description = ?, enabled = ?, settings_json = ?,"
                    " updated_at = ?, updated_by = ? WHERE id = ?",
                    (clean["name"], clean["description"], 1 if clean["enabled"] else 0,
                     json.dumps(clean["settings"]), now, actor, int(gpo_id)))
                if cur.rowcount == 0:
                    return "no such policy", None
    except sqlite3.IntegrityError:
        return "a policy with that name already exists", None
    invalidate()
    return None, get_gpo(db_path, gpo_id)


def delete_gpo(db_path, gpo_id):
    """Delete a policy and its links. Unlike a device group, being linked is not a reason to
    refuse: unlinking IS what deleting a policy is for, and the empty document the agent then
    receives is what lifts its registry values."""
    policy = get_gpo(db_path, gpo_id)
    if policy is None:
        return None
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM gpo_links WHERE gpo_id = ?", (policy["id"],))
        conn.execute("DELETE FROM gpos WHERE id = ?", (policy["id"],))
    invalidate()
    return policy


def gpos_using_script(db_path, script_name):
    """Every policy that runs `script_name`, as [{id, name}] -- for refusing the script's
    deletion, as rules_using_script does for rules."""
    wanted = str(script_name or "").strip().lower()
    return [{"id": p["id"], "name": f"policy {p['name']}"} for p in list_gpos(db_path)
            if any(s.get("name") == wanted for s in p["settings"].get("scripts") or [])]


# ================================================================ nodes and links
def node_key(kind, key):
    """The comparison form of a node. Raises ValueError for a node that cannot exist."""
    if kind == NODE_ROOT:
        return ""
    if kind == NODE_OU:
        normalized = directory.normalize_dn(key)
        if not normalized or "=" not in normalized:
            raise ValueError("an OU node needs its distinguished name")
        return normalized
    if kind == NODE_FOLDER:
        try:
            folder_id = int(key)
        except (TypeError, ValueError):
            raise ValueError("a folder node needs a folder")
        return str(folder_id)
    raise ValueError(f"node kind must be one of {', '.join(NODE_KINDS)}")


def _decode_link(row):
    link = dict(row)
    link["enforced"] = bool(link["enforced"])
    link["enabled"] = bool(link["enabled"])
    try:
        link["author_scope"] = (None if link.get("scope_json") is None
                                else set(json.loads(link.pop("scope_json"))))
    except (TypeError, ValueError):
        # Unreadable scope fails CLOSED: an empty scope applies the link to no PC.
        link["author_scope"] = set()
    link.pop("scope_json", None)
    return link


def list_links(db_path, gpo_id=None):
    with get_conn(db_path) as conn:
        if gpo_id is None:
            rows = conn.execute("SELECT * FROM gpo_links ORDER BY node_kind, node_key, link_order")
        else:
            rows = conn.execute("SELECT * FROM gpo_links WHERE gpo_id = ? ORDER BY id",
                                (int(gpo_id),))
        return [_decode_link(r) for r in rows.fetchall()]


def get_link(db_path, link_id):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM gpo_links WHERE id = ?", (int(link_id),)).fetchone()
    return _decode_link(row) if row else None


def links_for_node(db_path, kind, key):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM gpo_links WHERE node_kind = ? AND node_key = ?"
                            " ORDER BY link_order, id", (kind, key)).fetchall()
    return [_decode_link(r) for r in rows]


def _renumber(conn, kind, key):
    rows = conn.execute("SELECT id FROM gpo_links WHERE node_kind = ? AND node_key = ?"
                        " ORDER BY link_order, id", (kind, key)).fetchall()
    for order, row in enumerate(rows, start=1):
        conn.execute("UPDATE gpo_links SET link_order = ? WHERE id = ?", (order, row["id"]))


def create_link(db_path, gpo_id, kind, key, *, label="", enforced=False, author_scope=None,
                actor=None):
    """Link a policy to a node, last in its link order. Raises ValueError on a bad node, an
    unknown policy or a duplicate link. `author_scope` is the linker's machine set, or None
    for an unrestricted operator -- persisted, see the module docstring."""
    if get_gpo(db_path, gpo_id) is None:
        raise ValueError("no such policy")
    normalized = node_key(kind, key)
    if kind == NODE_FOLDER and pc_tree.get_folder(db_path, int(normalized)) is None:
        raise ValueError("that folder does not exist")
    if len(links_for_node(db_path, kind, normalized)) >= MAX_LINKS_PER_NODE:
        raise ValueError(f"a node can carry at most {MAX_LINKS_PER_NODE} links")
    now = int(time.time())
    scope_json = None if author_scope is None else json.dumps(sorted(author_scope))
    try:
        with get_conn(db_path) as conn:
            order = conn.execute(
                "SELECT COALESCE(MAX(link_order), 0) + 1 FROM gpo_links"
                " WHERE node_kind = ? AND node_key = ?", (kind, normalized)).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO gpo_links (gpo_id, node_kind, node_key, node_label, link_order,"
                " enforced, enabled, scope_json, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (int(gpo_id), kind, normalized,
                 str(label or (key if kind == NODE_OU else ""))[:1024],
                 order, 1 if enforced else 0, scope_json, now, actor))
            link_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("that policy is already linked here")
    invalidate()
    return get_link(db_path, link_id)


def update_link(db_path, link_id, *, enforced=None, enabled=None, move=None,
                author_scope="keep"):
    """Toggle enforced/enabled, or move one place up (-1) or down (+1) in its node's order.
    `author_scope` other than "keep" re-stamps the scope, as rules.save_rule does on edit."""
    link = get_link(db_path, link_id)
    if link is None:
        return None
    with get_conn(db_path) as conn:
        if enforced is not None:
            conn.execute("UPDATE gpo_links SET enforced = ? WHERE id = ?",
                         (1 if enforced else 0, link["id"]))
        if enabled is not None:
            conn.execute("UPDATE gpo_links SET enabled = ? WHERE id = ?",
                         (1 if enabled else 0, link["id"]))
        if author_scope != "keep":
            conn.execute("UPDATE gpo_links SET scope_json = ? WHERE id = ?",
                         (None if author_scope is None else json.dumps(sorted(author_scope)),
                          link["id"]))
        if move in (-1, 1):
            rows = conn.execute("SELECT id FROM gpo_links WHERE node_kind = ? AND node_key = ?"
                                " ORDER BY link_order, id",
                                (link["node_kind"], link["node_key"])).fetchall()
            ids = [r["id"] for r in rows]
            index = ids.index(link["id"])
            other = index + move
            if 0 <= other < len(ids):
                ids[index], ids[other] = ids[other], ids[index]
                for order, lid in enumerate(ids, start=1):
                    conn.execute("UPDATE gpo_links SET link_order = ? WHERE id = ?", (order, lid))
    invalidate()
    return get_link(db_path, link["id"])


def delete_link(db_path, link_id):
    link = get_link(db_path, link_id)
    if link is None:
        return None
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM gpo_links WHERE id = ?", (link["id"],))
        _renumber(conn, link["node_kind"], link["node_key"])
    invalidate()
    return link


def node_options(db_path):
    """{(kind, key): {"block_inheritance": bool}} for every node with options set."""
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM gpo_node_options").fetchall()
    return {(r["node_kind"], r["node_key"]): {"block_inheritance": bool(r["block_inheritance"])}
            for r in rows}


def set_block_inheritance(db_path, kind, key, block, actor=None):
    normalized = node_key(kind, key)
    if kind == NODE_ROOT:
        # There is nothing above every PC to block.
        raise ValueError("the top of the tree has nothing to block")
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO gpo_node_options (node_kind, node_key, block_inheritance, updated_at,"
            " updated_by) VALUES (?, ?, ?, ?, ?) ON CONFLICT(node_kind, node_key) DO UPDATE SET"
            " block_inheritance = excluded.block_inheritance, updated_at = excluded.updated_at,"
            " updated_by = excluded.updated_by",
            (kind, normalized, 1 if block else 0, int(time.time()), actor))
    invalidate()
    return {"node_kind": kind, "node_key": normalized, "block_inheritance": bool(block)}


def folders_linked(db_path, folder_ids):
    """Every link on any of `folder_ids`, as [{name}], for pc_tree's delete refusal."""
    wanted = {str(int(f)) for f in folder_ids}
    names = {p["id"]: p["name"] for p in list_gpos(db_path)}
    return [{"name": f"policy link {names.get(link['gpo_id'], link['gpo_id'])}"}
            for link in list_links(db_path)
            if link["node_kind"] == NODE_FOLDER and link["node_key"] in wanted]


def forget_folder(db_path, folder_id):
    """Drop a deleted folder's node options. Its links were refused at delete time."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM gpo_node_options WHERE node_kind = ? AND node_key = ?",
                     (NODE_FOLDER, str(int(folder_id))))
    invalidate()


def nodes_for_machine(db_path, machine, row=None, parents=None, filed=None):
    """The tree nodes above `machine`, in PROCESSING order (farthest first):

        every PC -> the AD domain -> each OU down to the PC's own -> each hub folder down to
        the PC's own

    as [(kind, key, label)]. `row`, `parents` and `filed` let a caller resolving many PCs read
    machine_info and the folder tree once."""
    nodes = [(NODE_ROOT, "", "")]
    if row is None:
        with get_conn(db_path) as conn:
            found = conn.execute("SELECT ad_ou FROM machine_info WHERE machine = ?",
                                 (machine,)).fetchone()
        ad_ou = found["ad_ou"] if found else None
    else:
        ad_ou = row.get("ad_ou")
    components = directory.split_dn(ad_ou or "")
    if components:
        domain_len = 0
        for component in reversed(components):
            if component.lower().startswith("dc="):
                domain_len += 1
            else:
                break
        start = len(components) - max(domain_len, 1)
        for index in range(start, -1, -1):
            dn = ",".join(components[index:])
            nodes.append((NODE_OU, directory.normalize_dn(dn), dn))
    folder_id = (filed.get(machine) if filed is not None
                 else pc_tree.folder_of(db_path, machine))
    if folder_id is not None:
        parents = parents if parents is not None else pc_tree._parents(db_path)
        chain = list(reversed(pc_tree.ancestors(db_path, folder_id, parents))) + [folder_id]
        for fid in chain:
            nodes.append((NODE_FOLDER, str(fid), ""))
    return nodes


def machines_under_node(db_path, kind, key):
    """Every PC beneath a node, sorted -- what a link there can reach before any author scope.
    Used by the web layer's scope check, so it resolves with the same containment the
    resolver uses: component-wise OUs, folder subtrees."""
    normalized = node_key(kind, key)
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT machine, ad_ou FROM machine_info").fetchall()
    if kind == NODE_ROOT:
        return sorted(r["machine"] for r in rows)
    if kind == NODE_OU:
        return sorted(r["machine"] for r in rows
                      if r["ad_ou"] and directory.ou_contains(normalized, r["ad_ou"]))
    return pc_tree.machines_in_folders(db_path, [int(normalized)])


def machines_linked(db_path, gpo_id):
    """Every PC beneath any node this policy is linked to, sorted -- what an edit or a delete
    of the policy can change, and so what the editor's scope must cover.

    Deliberately the whole reach of every link, ignoring enforcement, blocks, disabled links
    and author scopes: an edit can switch a disabled policy on, and the check has to cover
    where it will apply afterwards, not only where it applies now. A superset errs towards
    refusing, which is the direction a scope check should err in."""
    reached = set()
    for link in _state(db_path)["links"]:
        if link["gpo_id"] == int(gpo_id):
            try:
                reached.update(machines_under_node(db_path, link["node_kind"], link["node_key"]))
            except ValueError:
                continue
    return sorted(reached)


# ================================================================ resolution
def _in_scope(link, machine):
    scope = link.get("author_scope")
    return scope is None or machine in scope or machine.lower() in {m.lower() for m in scope}


def applied_links(db_path, machine, *, nodes=None, links=None, options=None, policies=None):
    """The links that apply to `machine`, in APPLY order -- later ones win a conflict.

    Windows' rules, as the module docstring sets out: non-enforced links farthest-first with
    link order 1 applied last within a node, cut at the deepest node blocking inheritance;
    then enforced links nearest-first, so the highest enforced link is applied last and wins.
    """
    if links is None or options is None or policies is None:
        cached = _state(db_path)
        links = cached["links"] if links is None else links
        options = cached["options"] if options is None else options
        policies = cached["policies"] if policies is None else policies
    if not links:
        # The common case on a hub that has never used this: no reads of where the PC sits.
        return []
    nodes = nodes if nodes is not None else nodes_for_machine(db_path, machine)

    by_node = {}
    for link in links:
        by_node.setdefault((link["node_kind"], link["node_key"]), []).append(link)

    cut = 0
    for index, (kind, key, _label) in enumerate(nodes):
        if options.get((kind, key), {}).get("block_inheritance"):
            cut = index

    def usable(link):
        policy = policies.get(link["gpo_id"])
        return (link["enabled"] and policy is not None and policy["enabled"]
                and _in_scope(link, machine))

    ordered = []
    for index, (kind, key, label) in enumerate(nodes):
        node_links = sorted(by_node.get((kind, key), []), key=lambda l: -l["link_order"])
        for link in node_links:
            if link["enforced"] or index < cut or not usable(link):
                continue
            ordered.append((link, index, label))
    for index in range(len(nodes) - 1, -1, -1):
        kind, key, label = nodes[index]
        node_links = sorted(by_node.get((kind, key), []), key=lambda l: -l["link_order"])
        for link in node_links:
            if link["enforced"] and usable(link):
                ordered.append((link, index, label))
    return [{"link": link, "policy": policies[link["gpo_id"]], "depth": depth, "label": label}
            for link, depth, label in ordered]


def rsop(db_path, machine, **cached):
    """The Resultant Set of Policy for one PC: what every setting ends up as and which policy
    won it, plus the links considered. The console's "why is this PC like this?" answer."""
    applied = applied_links(db_path, machine, **cached)
    agent_settings = {}
    registry = {}
    script_entries = {}
    for item in applied:
        policy = item["policy"]
        winner = {"gpo_id": policy["id"], "gpo": policy["name"]}
        body = policy["settings"]
        for key, value in (body.get("agent_settings") or {}).items():
            agent_settings[key] = {"value": value, **winner}
        for entry in body.get("registry") or []:
            registry[entry["id"]] = {**entry, **winner}
        # Scripts are additive, not "last wins": two policies each asking for a different script
        # both mean it. The same script from two policies runs once.
        for entry in body.get("scripts") or []:
            script_entries.setdefault(entry["name"], {**entry, **winner})
    return {
        "machine": machine,
        "links": [{"link_id": i["link"]["id"], "gpo_id": i["policy"]["id"],
                   "gpo": i["policy"]["name"], "node_kind": i["link"]["node_kind"],
                   "node_key": i["link"]["node_key"],
                   "node_label": i["link"]["node_label"] or i["label"],
                   "enforced": i["link"]["enforced"]} for i in applied],
        "agent_settings": agent_settings,
        "registry": sorted(registry.values(), key=lambda e: (e["path"].lower(), e["name"].lower())),
        "scripts": sorted(script_entries.values(), key=lambda e: e["name"]),
    }


def config_overrides(db_path, machine):
    """{setting key: value} a policy sets for this PC, for the heartbeat config block."""
    return {key: entry["value"] for key, entry in rsop(db_path, machine)["agent_settings"].items()}


def resolve_for(db_path, machine):
    """The registry document the agent applies: {"version", "values": [...]}. Always returns a
    document -- an EMPTY one when nothing applies, which is what tells the agent to remove the
    values it wrote (the watchdog/device-policy release valve)."""
    values = [{"id": e["id"], "path": e["path"], "name": e["name"], "type": e["type"],
               "value": e["value"]} for e in rsop(db_path, machine)["registry"]]
    return {"version": _hash(values), "values": values}


def config_version(base_version, overrides):
    """Fold a PC's policy overrides into its config version, so a policy change re-sends the
    config block. No overrides leaves the base untouched, so linking nothing changes nothing."""
    if not overrides:
        return base_version
    return hashlib.sha256(f"{base_version}|{_hash(overrides)}".encode("utf-8")).hexdigest()[:16]


# ================================================================ agent reports
def note_agent(db_path, machine, version, now=None):
    """Remember that this PC's agent can apply a registry document."""
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO machine_gpo_agent (machine, version, seen_at) VALUES (?, ?, ?)"
            " ON CONFLICT(machine) DO UPDATE SET version = excluded.version,"
            " seen_at = excluded.seen_at",
            (machine, str(version or "")[:64], int(now or time.time())))


def agent_supports(db_path, machine):
    with get_conn(db_path) as conn:
        return conn.execute("SELECT 1 FROM machine_gpo_agent WHERE machine = ?",
                            (machine,)).fetchone() is not None


def record_report(db_path, machine, payload, now=None):
    """Store the per-value states an agent reported: {"states": [{id, status, detail}]}.

    Every entry is bounded and an unknown status is dropped rather than coerced -- the
    console reads these as statements about a PC's registry, and a made-up one is worse than
    a missing one. Returns the number of rows written."""
    states = (payload or {}).get("states") if isinstance(payload, dict) else None
    if not isinstance(states, list):
        return 0
    now = int(now or time.time())
    rows = []
    for entry in states[:MAX_REGISTRY_VALUES * 2]:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("id") or "")[:32]
        status = str(entry.get("status") or "")
        if not sid or status not in STATES:
            continue
        rows.append((machine, sid, status, str(entry.get("detail") or "")[:500], now))
    removed = [str(i)[:32] for i in ((payload or {}).get("removed") or [])
               if isinstance(i, str)]
    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO machine_gpo_registry_state (machine, setting_id, status, detail,"
            " reported_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(machine, setting_id) DO UPDATE SET"
            " status = excluded.status, detail = excluded.detail,"
            " reported_at = excluded.reported_at", rows)
        for sid in removed:
            conn.execute("DELETE FROM machine_gpo_registry_state WHERE machine = ?"
                         " AND setting_id = ?", (machine, sid))
    return len(rows)


def registry_states(db_path, machine):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT setting_id, status, detail, reported_at"
                            " FROM machine_gpo_registry_state WHERE machine = ?",
                            (machine,)).fetchall()
    return {r["setting_id"]: dict(r) for r in rows}


def script_runs(db_path, machine):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM gpo_script_runs WHERE machine = ?"
                            " ORDER BY queued_at DESC", (machine,)).fetchall()
    return [dict(r) for r in rows]


# ================================================================ scripts
def script_hash(script):
    """What "the same script" means for run-once: the body, the shell and the timeout. A
    rename of the label or an edit to its description is not a reason to run it again."""
    return _hash({"body": script["body"], "shell": script["shell"],
                  "timeout": script["timeout_seconds"]})


def _render_script(script, inputs, machine):
    """The body with its inputs filled in, or (None, reason). Only `sys.machine` is offered
    as a variable: a policy script runs on joining a node, not on a condition, so the rule
    engine's live readings have no meaning here -- and an unresolvable name skips the run
    rather than sending "unknown" into code (rules.render_template_checked)."""
    variables = {"sys.machine": machine}
    namespace = dict(variables)
    for item in script["inputs"]:
        supplied = (inputs or {}).get(item["name"])
        raw = supplied if supplied not in (None, "") else item.get("default", "")
        value, gaps = rules.render_template_checked(raw, variables)
        if gaps:
            return None, f"unresolved input values: {', '.join(gaps)}"
        namespace[f"{scripts.INPUT_PREFIX}{item['name']}"] = value
    body, missing = rules.render_template_checked(script["body"], namespace)
    if missing:
        return None, (f"the script uses {', '.join(missing)}, which a policy script cannot "
                      f"fill in -- only sys.machine is available")
    return body, None


def _command_outcome(db_path, command_id):
    command = fleet.get_command(db_path, command_id) if command_id else None
    if command is None:
        return "expired", ""
    status = command.get("status")
    if status == fleet.STATUS_DONE:
        return "done", ""
    if status == fleet.STATUS_FAILED:
        # The tail, not the head: a script's last lines are where it says what went wrong.
        output = str((command.get("result") or {}).get("output") or "")
        return "failed", output[-500:]
    if status == fleet.STATUS_EXPIRED:
        return "expired", ""
    return "pending", ""


def script_tick(db_path, machines, ttl_seconds=fleet.DEFAULT_COMMAND_TTL_SECONDS, now=None,
                capability_refusal=None):
    """One pass of the script scheduler over `machines` (the ONLINE ones -- a command for an
    offline PC would only expire). Settles finished runs, then queues missing ones, at most
    SCRIPTS_PER_TICK. Returns the number of commands queued.

    `capability_refusal(machine)` returns a reason this PC cannot run a script (an Android
    device), or None; injected so this stays testable without the capabilities tables."""
    now = int(now or time.time())
    policies = list_gpos(db_path)
    if not any(p["enabled"] and has_scripts(p) for p in policies):
        return 0
    cached = {"links": list_links(db_path), "options": node_options(db_path),
              "policies": {p["id"]: p for p in policies}}
    parents = pc_tree._parents(db_path)
    filed = pc_tree.assignments(db_path)
    with get_conn(db_path) as conn:
        rows = {r["machine"]: dict(r) for r in conn.execute(
            "SELECT machine, ad_ou FROM machine_info")}
        pending = conn.execute("SELECT * FROM gpo_script_runs WHERE status = ?",
                               (SCRIPT_QUEUED,)).fetchall()

    # Settle what was queued before. Outside any open connection: get_command opens its own.
    settled = []
    for run in pending:
        outcome, detail = _command_outcome(db_path, run["command_id"])
        if outcome == "done":
            settled.append((SCRIPT_DONE, "", now, run))
        elif outcome == "failed":
            settled.append((SCRIPT_FAILED, detail, now, run))
        elif outcome == "expired":
            # Never reached the PC. Retry while attempts last; then it is a failure worth
            # showing rather than a silent forever-pending row.
            if run["attempts"] >= MAX_SCRIPT_ATTEMPTS:
                settled.append((SCRIPT_FAILED, "the PC did not pick the command up", now, run))
            else:
                settled.append(("retry", "", None, run))
    with get_conn(db_path) as conn:
        for status, detail, finished, run in settled:
            if status == "retry":
                conn.execute("UPDATE gpo_script_runs SET status = 'retry' WHERE machine = ?"
                             " AND script_name = ? AND script_hash = ?",
                             (run["machine"], run["script_name"], run["script_hash"]))
            else:
                conn.execute("UPDATE gpo_script_runs SET status = ?, detail = ?, finished_at = ?"
                             " WHERE machine = ? AND script_name = ? AND script_hash = ?",
                             (status, detail, finished, run["machine"], run["script_name"],
                              run["script_hash"]))
        existing = {(r["machine"], r["script_name"], r["script_hash"]): dict(r)
                    for r in conn.execute("SELECT * FROM gpo_script_runs")}

    library = {}
    queued = 0
    for machine in machines:
        if queued >= SCRIPTS_PER_TICK:
            break
        row = rows.get(machine, {"machine": machine, "ad_ou": None})
        nodes = nodes_for_machine(db_path, machine, row=row, parents=parents, filed=filed)
        wanted = rsop(db_path, machine, nodes=nodes, **cached)["scripts"]
        if not wanted:
            continue
        refusal = capability_refusal(machine) if capability_refusal else None
        for entry in wanted:
            if queued >= SCRIPTS_PER_TICK:
                break
            if entry["name"] not in library:
                library[entry["name"]] = scripts.get_script(db_path, entry["name"])
            script = library[entry["name"]]
            if script is None or not script["enabled"]:
                continue
            digest = script_hash(script)
            key = (machine, entry["name"], digest)
            run = existing.get(key)
            if run is not None and run["status"] != "retry":
                continue
            attempts = (run["attempts"] if run else 0) + 1
            body, reason = (None, refusal) if refusal else _render_script(
                script, entry.get("inputs"), machine)
            if body is None:
                _upsert_run(db_path, key, entry["gpo_id"], None, SCRIPT_SKIPPED, attempts,
                            reason, now, now)
                continue
            try:
                command_id = fleet.create_command(
                    db_path, machine, "run_script",
                    {"script": body, "shell": script["shell"],
                     "timeout_seconds": script["timeout_seconds"]},
                    # `gpo:<id>` -- stable across a rename, and the agent keys its shell
                    # session on it the way it does on `rule:<id>`.
                    issued_by=f"gpo:{entry['gpo_id']}", ttl_seconds=ttl_seconds)
            except ValueError as e:
                _upsert_run(db_path, key, entry["gpo_id"], None, SCRIPT_SKIPPED, attempts,
                            str(e)[:500], now, now)
                continue
            _upsert_run(db_path, key, entry["gpo_id"], command_id, SCRIPT_QUEUED, attempts,
                        "", now, None)
            queued += 1
    return queued


def _upsert_run(db_path, key, gpo_id, command_id, status, attempts, detail, queued_at,
                finished_at):
    machine, name, digest = key
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO gpo_script_runs (machine, script_name, script_hash, gpo_id, command_id,"
            " status, attempts, detail, queued_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(machine, script_name, script_hash) DO UPDATE SET"
            " gpo_id = excluded.gpo_id, command_id = excluded.command_id,"
            " status = excluded.status, attempts = excluded.attempts, detail = excluded.detail,"
            " queued_at = excluded.queued_at, finished_at = excluded.finished_at",
            (machine, name, digest, gpo_id, command_id, status, attempts, detail, queued_at,
             finished_at))


# ================================================================ machine lifecycle
def forget_machine(db_path, machine):
    with get_conn(db_path) as conn:
        for table in ("machine_gpo_registry_state", "gpo_script_runs", "machine_gpo_agent"):
            conn.execute(f"DELETE FROM {table} WHERE machine = ?", (machine,))


def rename_machine(db_path, old_machine, new_machine):
    """The survivor of a merge is the same box: its script history must follow it, or every
    run-once script runs a second time on a PC that already ran it."""
    if not old_machine or not new_machine or old_machine == new_machine:
        return
    with get_conn(db_path) as conn:
        for table in ("machine_gpo_registry_state", "gpo_script_runs", "machine_gpo_agent"):
            conn.execute(f"UPDATE OR IGNORE {table} SET machine = ? WHERE machine = ?",
                         (new_machine, old_machine))
            conn.execute(f"DELETE FROM {table} WHERE machine = ?", (old_machine,))
