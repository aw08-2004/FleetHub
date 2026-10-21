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
import refusals
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
SCRIPT_RETRY = "retry"
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


class PolicyRejected(ValueError):
    """A policy payload the model refuses. Raised by the small checks below and turned back
    into the (error, clean) pair at the one public entry point, validate() -- so each check
    stays a few lines long rather than one function returning from twenty places."""


def _dword(name, value):
    try:
        number = int(str(value).strip(), 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise PolicyRejected(f"{name}: a DWORD needs a whole number")
    if isinstance(value, bool) or not 0 <= number <= 0xFFFFFFFF:
        raise PolicyRejected(f"{name}: a DWORD is 0 to 4294967295")
    return number


def _multi_string(name, value):
    items = value if isinstance(value, list) else str(value or "").splitlines()
    items = [str(i) for i in items]
    if len(items) > MAX_MULTI_ITEMS:
        raise PolicyRejected(f"{name}: at most {MAX_MULTI_ITEMS} lines")
    if any(len(i) > MAX_STRING_CHARS or "\x00" in i for i in items):
        raise PolicyRejected(f"{name}: a line is too long or contains a NUL")
    return items


def _string(name, value):
    value = "" if value is None else str(value)
    if len(value) > MAX_STRING_CHARS or "\x00" in value:
        raise PolicyRejected(f"{name}: the text is longer than {MAX_STRING_CHARS} characters")
    return value


_VALUE_CLEANERS = {REG_DWORD: _dword, REG_MULTI_SZ: _multi_string,
                   REG_SZ: _string, REG_EXPAND_SZ: _string}


def _registry_value(raw):
    if not isinstance(raw, dict):
        raise PolicyRejected("a registry value must be an object")
    path = normalize_registry_path(raw.get("path"))
    error = registry_path_error(path)
    if error:
        raise PolicyRejected(error)
    name = str(raw.get("name") or "").strip()
    if not name:
        raise PolicyRejected("a registry value needs a value name (the default value cannot be set)")
    if len(name) > MAX_VALUE_NAME_CHARS or "\\" in name or any(ord(c) < 32 for c in name):
        raise PolicyRejected(f"the value name {name!r} is not valid")
    kind = str(raw.get("type") or "").strip()
    cleaner = _VALUE_CLEANERS.get(kind)
    if cleaner is None:
        raise PolicyRejected(f"the value type must be one of {', '.join(REGISTRY_TYPES)}")
    return {"id": setting_id(path, name), "path": path, "name": name, "type": kind,
            "value": cleaner(name, raw.get("value"))}


def _bounded_list(raw, what, limit):
    raw = raw or []
    if not isinstance(raw, list):
        raise PolicyRejected(f"{what} must be a list")
    if len(raw) > limit:
        raise PolicyRejected(f"too many {what} (limit {limit})")
    return raw


def _agent_settings(raw):
    clean = {}
    for key, value in (raw or {}).items():
        setting = settings.BY_KEY.get(str(key))
        if setting is None or not setting.agent:
            raise PolicyRejected(f"{key} is not a setting a policy can set")
        # coerce_and_validate raises ValueError with an operator-facing sentence; validate()
        # renders it through refusals.message, never str(e).
        value = settings.coerce_and_validate(setting, value)
        # None is "Not configured" -- the same as leaving the key out, not a value of its own.
        if value is not None:
            clean[setting.key] = value
    return clean


def _registry(raw):
    clean, seen = [], set()
    for entry in _bounded_list(raw, "registry values", MAX_REGISTRY_VALUES):
        value = _registry_value(entry)
        if value["id"] in seen:
            raise PolicyRejected(f"{value['path']}\\{value['name']} is set twice in this policy")
        seen.add(value["id"])
        clean.append(value)
    return clean


def _script_entry(raw, script_specs):
    entry = raw if isinstance(raw, dict) else {"name": raw}
    script_name = str(entry.get("name") or "").strip().lower()
    inputs = entry.get("inputs") or {}
    if script_specs is not None:
        error, inputs = scripts.validate_reference(script_specs, script_name, inputs)
        if error:
            raise PolicyRejected(error)
    if not script_name:
        raise PolicyRejected("a script entry needs a script")
    return {"name": script_name, "inputs": inputs or {}}


def _scripts(raw, script_specs):
    clean = []
    for entry in _bounded_list(raw, "scripts", MAX_SCRIPTS):
        entry = _script_entry(entry, script_specs)
        if any(s["name"] == entry["name"] for s in clean):
            raise PolicyRejected(f"the script {entry['name']} is listed twice")
        clean.append(entry)
    return clean


def _text(payload, key, what, limit, required=False):
    text = str(payload.get(key) or "").strip()
    if required and not text:
        raise PolicyRejected(f"a policy needs a {what}")
    if len(text) > limit:
        raise PolicyRejected(f"the {what} is too long (limit {limit})")
    return text


def validate(payload, script_specs=None):
    """Check a policy object. Returns (error, clean). Errors are English, like every other
    model-layer refusal; the web layer passes them through.

    **The error is rendered with refusals.message, never str(e).** A settings validator's
    ValueError is the one exception here this module did not raise itself, and message() is
    the filter every route shares -- it renders the sentence written, never a repr or a
    traceback (CodeQL py/stack-trace-exposure, alerts #179/#180 on the first cut of this).

    `script_specs` is scripts.specs(); passing None skips the script-reference check (tests)."""
    try:
        if not isinstance(payload, dict):
            raise PolicyRejected("a policy must be an object")
        raw_settings = payload.get("settings") or {}
        if not isinstance(raw_settings, dict):
            raise PolicyRejected("settings must be an object")
        return None, {
            "name": _text(payload, "name", "name", NAME_MAX, required=True),
            "description": _text(payload, "description", "description", DESCRIPTION_MAX),
            "enabled": bool(payload.get("enabled", True)),
            "settings": {"agent_settings": _agent_settings(raw_settings.get("agent_settings")),
                         "registry": _registry(raw_settings.get("registry")),
                         "scripts": _scripts(raw_settings.get("scripts"), script_specs)},
        }
    except ValueError as e:
        return refusals.message(e), None


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


def machines_running_script(db_path, script_name):
    """Every PC a policy could run `script_name` on: the union of machines_linked over every
    policy that lists it, sorted.

    What an EDIT to that library script reaches. Run-once is keyed on the body's hash, so
    changing the body queues the new one on each of these PCs at the next scheduler pass, with
    no policy edit involved -- which is why the script editor's scope must cover them
    (rules_web.save_one_script, raised in review on #162). The same superset machines_linked
    uses for a policy edit, for the same reason."""
    reached = set()
    for policy in gpos_using_script(db_path, script_name):
        reached.update(machines_linked(db_path, policy["id"]))
    return sorted(reached)


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


_SET_ENFORCED_SQL = "UPDATE gpo_links SET enforced = ? WHERE id = ?"
_SET_ENABLED_SQL = "UPDATE gpo_links SET enabled = ? WHERE id = ?"


def _move_link(conn, link, move):
    """Swap `link` with its neighbour one place up (-1) or down (+1) in its node's order, and
    renumber. A move past either end is a no-op rather than an error."""
    rows = conn.execute("SELECT id FROM gpo_links WHERE node_kind = ? AND node_key = ?"
                        " ORDER BY link_order, id",
                        (link["node_kind"], link["node_key"])).fetchall()
    ids = [r["id"] for r in rows]
    index = ids.index(link["id"])
    other = index + move
    if not 0 <= other < len(ids):
        return
    ids[index], ids[other] = ids[other], ids[index]
    conn.executemany("UPDATE gpo_links SET link_order = ? WHERE id = ?",
                     [(order, lid) for order, lid in enumerate(ids, start=1)])


def update_link(db_path, link_id, *, enforced=None, enabled=None, move=None,
                author_scope="keep"):
    """Toggle enforced/enabled, or move one place up (-1) or down (+1) in its node's order.
    `author_scope` other than "keep" re-stamps the scope, as rules.save_rule does on edit."""
    link = get_link(db_path, link_id)
    if link is None:
        return None
    flags = [(sql, 1 if value else 0)
             for sql, value in ((_SET_ENFORCED_SQL, enforced), (_SET_ENABLED_SQL, enabled))
             if value is not None]
    with get_conn(db_path) as conn:
        for sql, value in flags:
            conn.execute(sql, (value, link["id"]))
        if author_scope != "keep":
            scope_json = None if author_scope is None else json.dumps(sorted(author_scope))
            conn.execute("UPDATE gpo_links SET scope_json = ? WHERE id = ?",
                         (scope_json, link["id"]))
        if move in (-1, 1):
            _move_link(conn, link, move)
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


def _ad_ou_of(db_path, machine, row):
    if row is not None:
        return row.get("ad_ou")
    with get_conn(db_path) as conn:
        found = conn.execute("SELECT ad_ou FROM machine_info WHERE machine = ?",
                             (machine,)).fetchone()
    return found["ad_ou"] if found else None


def _ou_nodes(ad_ou):
    """The AD domain, then each OU down to `ad_ou`, as nodes. The domain is the run of
    trailing DC= components; a DN with none is rooted at its last component instead."""
    components = directory.split_dn(ad_ou or "")
    if not components:
        return []
    domain_len = 0
    for component in reversed(components):
        if not component.lower().startswith("dc="):
            break
        domain_len += 1
    start = len(components) - max(domain_len, 1)
    nodes = []
    for index in range(start, -1, -1):
        dn = ",".join(components[index:])
        nodes.append((NODE_OU, directory.normalize_dn(dn), dn))
    return nodes


def _folder_nodes(db_path, machine, parents, filed):
    folder_id = filed.get(machine) if filed is not None else pc_tree.folder_of(db_path, machine)
    if folder_id is None:
        return []
    parents = parents if parents is not None else pc_tree._parents(db_path)
    chain = list(reversed(pc_tree.ancestors(db_path, folder_id, parents))) + [folder_id]
    return [(NODE_FOLDER, str(fid), "") for fid in chain]


def nodes_for_machine(db_path, machine, row=None, parents=None, filed=None):
    """The tree nodes above `machine`, in PROCESSING order (farthest first):

        every PC -> the AD domain -> each OU down to the PC's own -> each hub folder down to
        the PC's own

    as [(kind, key, label)]. `row`, `parents` and `filed` let a caller resolving many PCs read
    machine_info and the folder tree once."""
    return ([(NODE_ROOT, "", "")] + _ou_nodes(_ad_ou_of(db_path, machine, row))
            + _folder_nodes(db_path, machine, parents, filed))


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


def _links_by_node(links):
    """{(kind, key): [links]} with each node's links in APPLY order: highest link order first,
    so link order 1 is applied last within its node and wins, as in GPMC."""
    by_node = {}
    for link in links:
        by_node.setdefault((link["node_kind"], link["node_key"]), []).append(link)
    for node_links in by_node.values():
        node_links.sort(key=lambda link: -link["link_order"])
    return by_node


def _block_cut(nodes, options):
    """The index of the deepest node blocking inheritance (0 when none does). Non-enforced
    links on nodes above it do not apply."""
    blocked = [index for index, (kind, key, _label) in enumerate(nodes)
               if options.get((kind, key), {}).get("block_inheritance")]
    return blocked[-1] if blocked else 0


def _usable(link, policies, machine):
    policy = policies.get(link["gpo_id"])
    return bool(link["enabled"] and policy is not None and policy["enabled"]
                and _in_scope(link, machine))


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
    by_node = _links_by_node(links)
    cut = _block_cut(nodes, options)
    indexed = list(enumerate(nodes))
    plain = [(link, index, label) for index, (kind, key, label) in indexed if index >= cut
             for link in by_node.get((kind, key), []) if not link["enforced"]]
    enforced = [(link, index, label) for index, (kind, key, label) in reversed(indexed)
                for link in by_node.get((kind, key), []) if link["enforced"]]
    return [{"link": link, "policy": policies[link["gpo_id"]], "depth": depth, "label": label}
            for link, depth, label in plain + enforced if _usable(link, policies, machine)]


def _merge(applied):
    """Fold the applied links' settings in apply order. Agent settings and registry values are
    last-wins; scripts are additive -- two policies each asking for a different script both
    mean it, and the same script from two policies runs once."""
    agent_settings, registry, script_entries = {}, {}, {}
    for item in applied:
        policy = item["policy"]
        winner = {"gpo_id": policy["id"], "gpo": policy["name"]}
        body = policy["settings"]
        agent_settings.update({key: {"value": value, **winner}
                               for key, value in (body.get("agent_settings") or {}).items()})
        registry.update({entry["id"]: {**entry, **winner} for entry in body.get("registry") or []})
        for entry in body.get("scripts") or []:
            script_entries.setdefault(entry["name"], {**entry, **winner})
    return agent_settings, registry, script_entries


def _public_applied(item):
    link = item["link"]
    return {"link_id": link["id"], "gpo_id": item["policy"]["id"], "gpo": item["policy"]["name"],
            "node_kind": link["node_kind"], "node_key": link["node_key"],
            "node_label": link["node_label"] or item["label"], "enforced": link["enforced"]}


def rsop(db_path, machine, **cached):
    """The Resultant Set of Policy for one PC: what every setting ends up as and which policy
    won it, plus the links considered. The console's "why is this PC like this?" answer."""
    applied = applied_links(db_path, machine, **cached)
    agent_settings, registry, script_entries = _merge(applied)
    return {
        "machine": machine,
        "links": [_public_applied(item) for item in applied],
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


def _clean_state(entry, machine, now):
    if not isinstance(entry, dict):
        return None
    sid = str(entry.get("id") or "")[:32]
    status = str(entry.get("status") or "")
    if not sid or status not in STATES:
        return None
    return (machine, sid, status, str(entry.get("detail") or "")[:500], now)


def record_report(db_path, machine, payload, now=None):
    """Store the per-value states an agent reported: {"states": [{id, status, detail}],
    "removed": [id, ...]}.

    Every entry is bounded and an unknown status is dropped rather than coerced -- the
    console reads these as statements about a PC's registry, and a made-up one is worse than
    a missing one. Returns the number of rows written."""
    states = payload.get("states") if isinstance(payload, dict) else None
    if not isinstance(states, list):
        return 0
    now = int(now or time.time())
    rows = [row for row in (_clean_state(e, machine, now)
                            for e in states[:MAX_REGISTRY_VALUES * 2]) if row]
    removed = [(machine, str(i)[:32]) for i in (payload.get("removed") or [])
               if isinstance(i, str)]
    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO machine_gpo_registry_state (machine, setting_id, status, detail,"
            " reported_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(machine, setting_id) DO UPDATE SET"
            " status = excluded.status, detail = excluded.detail,"
            " reported_at = excluded.reported_at", rows)
        conn.executemany("DELETE FROM machine_gpo_registry_state WHERE machine = ?"
                         " AND setting_id = ?", removed)
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


# What a PC name may look like before it is written into code that runs as SYSTEM: a hostname,
# and nothing a shell reads as syntax. Narrower than app.is_valid_machine_name, which also
# admits a space -- harmless on a page, but enough to split an unquoted argument in a script.
_SAFE_MACHINE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _render_script(script, inputs, machine):
    """The body with its inputs filled in, or (None, reason). Only `sys.machine` is offered
    as a variable: a policy script runs on joining a node, not on a condition, so the rule
    engine's live readings have no meaning here -- and an unresolvable name skips the run
    rather than sending "unknown" into code (rules.render_template_checked).

    **A PC whose name is not a plain hostname is skipped, not rendered** (raised in review on
    #162). The name is what the agent reported, substituted verbatim into a SYSTEM script.
    Ingest already refuses quotes and shell punctuation (app.is_valid_machine_name), but that
    check lives in another module, allows spaces, and a scheduler that fires on every PC
    joining a node is a wider blast radius than a rule firing on a condition -- so the check
    the code depends on sits beside the code that depends on it. Rejected: quoting the name
    for the shell, which would be a different escape per shell and still wrong inside a
    string the author already quoted."""
    if not _SAFE_MACHINE_NAME.match(str(machine or "")):
        return None, ("the PC name is not a plain hostname (letters, digits, '.', '_' or '-'), "
                      "so it is not written into a script")
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


def _settled(run, outcome, detail, now):
    """What a queued run becomes, as (status, detail, finished_at), or None while pending."""
    if outcome == "done":
        return SCRIPT_DONE, "", now
    if outcome == "failed":
        return SCRIPT_FAILED, detail, now
    if outcome != "expired":
        return None
    # Never reached the PC. Retry while attempts last; then it is a failure worth showing
    # rather than a silent forever-pending row.
    if run["attempts"] >= MAX_SCRIPT_ATTEMPTS:
        return SCRIPT_FAILED, "the PC did not pick the command up", now
    return SCRIPT_RETRY, "", None


def _settle_queued(db_path, now):
    """Move every queued run whose command finished to its outcome. The command reads happen
    outside any open connection: get_command opens its own."""
    with get_conn(db_path) as conn:
        pending = conn.execute("SELECT * FROM gpo_script_runs WHERE status = ?",
                               (SCRIPT_QUEUED,)).fetchall()
    updates = []
    for run in pending:
        result = _settled(run, *_command_outcome(db_path, run["command_id"]), now)
        if result:
            updates.append((*result, run["machine"], run["script_name"], run["script_hash"]))
    with get_conn(db_path) as conn:
        conn.executemany("UPDATE gpo_script_runs SET status = ?, detail = ?, finished_at = ?"
                         " WHERE machine = ? AND script_name = ? AND script_hash = ?", updates)


class _ScriptPass:
    """One scheduler pass's shared reads, so each PC's resolution touches no table twice."""

    def __init__(self, db_path, policies, ttl_seconds, now, capability_refusal):
        self.db_path = db_path
        self.ttl_seconds = ttl_seconds
        self.now = now
        self.capability_refusal = capability_refusal
        self.cached = {"links": list_links(db_path), "options": node_options(db_path),
                       "policies": {p["id"]: p for p in policies}}
        self.parents = pc_tree._parents(db_path)
        self.filed = pc_tree.assignments(db_path)
        with get_conn(db_path) as conn:
            self.rows = {r["machine"]: dict(r) for r in conn.execute(
                "SELECT machine, ad_ou FROM machine_info")}
            self.existing = {(r["machine"], r["script_name"], r["script_hash"]): dict(r)
                             for r in conn.execute("SELECT * FROM gpo_script_runs")}
        self.library = {}
        self.queued = 0

    def script(self, name):
        if name not in self.library:
            self.library[name] = scripts.get_script(self.db_path, name)
        return self.library[name]

    def wanted(self, machine):
        row = self.rows.get(machine, {"machine": machine, "ad_ou": None})
        nodes = nodes_for_machine(self.db_path, machine, row=row, parents=self.parents,
                                  filed=self.filed)
        return rsop(self.db_path, machine, nodes=nodes, **self.cached)["scripts"]

    def machine(self, machine):
        wanted = self.wanted(machine)
        refusal = self.capability_refusal(machine) if wanted and self.capability_refusal else None
        for entry in wanted:
            if self.queued >= SCRIPTS_PER_TICK:
                return
            self.entry(machine, entry, refusal)

    def entry(self, machine, entry, refusal):
        script = self.script(entry["name"])
        if script is None or not script["enabled"]:
            return
        key = (machine, entry["name"], script_hash(script))
        run = self.existing.get(key)
        if run is not None and run["status"] != SCRIPT_RETRY:
            return
        attempts = (run["attempts"] if run else 0) + 1
        body, reason = ((None, refusal) if refusal
                        else _render_script(script, entry.get("inputs"), machine))
        if body is None:
            _upsert_run(self.db_path, key, entry["gpo_id"], None, SCRIPT_SKIPPED, attempts,
                        reason, self.now, self.now)
            return
        try:
            command_id = fleet.create_command(
                self.db_path, machine, "run_script",
                {"script": body, "shell": script["shell"],
                 "timeout_seconds": script["timeout_seconds"]},
                # `gpo:<id>` -- stable across a rename, and the agent keys its shell session on
                # it the way it does on `rule:<id>`.
                issued_by=f"gpo:{entry['gpo_id']}", ttl_seconds=self.ttl_seconds)
        except ValueError as e:
            _upsert_run(self.db_path, key, entry["gpo_id"], None, SCRIPT_SKIPPED, attempts,
                        refusals.message(e), self.now, self.now)
            return
        _upsert_run(self.db_path, key, entry["gpo_id"], command_id, SCRIPT_QUEUED, attempts,
                    "", self.now, None)
        self.queued += 1


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
    _settle_queued(db_path, now)
    run = _ScriptPass(db_path, policies, ttl_seconds, now, capability_refusal)
    for machine in machines:
        if run.queued >= SCRIPTS_PER_TICK:
            break
        run.machine(machine)
    return run.queued


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
