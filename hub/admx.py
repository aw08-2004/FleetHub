"""Administrative Templates -- the ADMX/ADML catalogue behind the Group Policy editor (roadmap #32).

#32 shipped Group Policy with raw registry values only: "to find the value an ADMX setting
writes, look it up in the template's documentation". That works, and nobody does it. The owner
asked for the editor Windows has -- gpedit.msc's tree of Administrative Templates, each setting
Enabled / Disabled / Not configured with its option fields and explanation -- and this module
is the half of that the hub needs: read the templates, show them, and turn one setting's
state into the registry values the existing pipeline already delivers.

**The templates come from the PCs themselves.** Each Windows agent hashes its own
`C:\\Windows\\PolicyDefinitions` (the `.admx` files plus the `en-US`, `de-DE` and `es-ES`
`.adml`) and sends the hash on its heartbeat. The hub asks ONE PC per unknown hash to upload
the set, verifies the hash itself, parses it once and keeps the parsed catalogue on disk.
Every PC on one Windows build shares one set, so a fleet uploads a handful, not hundreds. A
Windows build's own templates are by definition the ones that match what it reads.

**An ADMX setting compiles to raw values, and the raw values are what is stored and sent.**
compile() is the one place a setting's state becomes `{path, name, type, value}` rows; gpo.py
validates those rows with the same allow-list as hand-entered ones and stores them beside the
setting. So a heartbeat never needs a catalogue, a deleted catalogue never changes a PC, and
the agent cannot tell an ADMX value from a typed one -- it does not need to.

What it refuses, on purpose:

  * **Keys outside `SOFTWARE\\Policies`.** Some Windows templates write elsewhere (they are
    "preferences", not policies). They are listed greyed with the reason; HKLM stays the trust
    root exactly as for raw values.
  * **QWORD values** (`longDecimal`) -- the pipeline has no QWORD type, so a setting using one is
    listed greyed rather than half-applied.
  * **User Configuration** until the agent can write the per-user local GPO (roadmap #32,
    phase 3). The tree shows the User half so the shape is gpedit's, greyed.
  * **`delete` values** (most "Disabled" states delete a value rather than write one). Until
    the agent writes the local GPO's Registry.pol, which can say "delete", such a value is
    left out -- the agent then removes it only if it wrote it. compile() reports them so the
    editor can say so.

Why parse on the hub at all, rather than send the PC's catalogue straight to the browser: the
browser would need the same rules to build values, and two implementations of "what does
Enabled with these options write" would drift. Rejected: bundling a template set in hub/
(stale the day it ships, and megabytes in every self-update) and operator uploads (the
owner chose the PCs' own set, 2026-10-29).

**XML is never parsed with a DOCTYPE.** The files come from an agent, which an attacker who
owns a PC controls; a DTD is how XML turns into entity expansion. A document containing one is
refused, the XML declaration is stripped (so an encoding claim cannot disagree with the
decoded text) and only then is it handed to ElementTree.

Kept free of Flask so it can be unit-tested in isolation.
"""
import collections
import gzip
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

# The template languages a PC sends, and the UI language each one serves. English is the
# fallback for every string, as it is for the hub's own catalogue.
LANGUAGES = {"en-US": "en", "de-DE": "de", "es-ES": "es"}
FALLBACK_LANGUAGE = "en"

#: The zip an agent uploads. Windows 11's own set is about 15 MB uncompressed for three
#: languages, under 4 MB zipped; 64 MB is a broken or hostile client.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 4000
#: Parsed sets kept in memory. The editor reads one at a time; two covers a switch between
#: builds without a re-read from disk on every click.
CACHED_SETS = 2
#: How long one PC holds the job of uploading a set before another PC is asked instead -- long
#: enough for a slow link to finish, short enough that a PC switched off mid-upload does not
#: stall the set for a day.
UPLOAD_CLAIM_SECONDS = 15 * 60
#: Template sets the hub keeps. A fleet runs a handful of Windows builds at once; past this,
#: the oldest sets no PC reports any more make room, and when every set is still reported the
#: upload is refused. Without it, an agent cycling through made-up sets -- each one a valid
#: upload under its own hash -- would fill the hub's disk (raised in review on #191).
MAX_SETS = 32

CLASS_MACHINE = "machine"
CLASS_USER = "user"
CLASS_BOTH = "both"

STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATES = (STATE_ENABLED, STATE_DISABLED)

# Why a setting cannot be set, as i18n slugs under gpo.admx.unsupported.
UNSUPPORTED_OUTSIDE = "outside_policy_tree"
UNSUPPORTED_QWORD = "qword"

POLICY_ROOT = "software\\policies\\"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ADMX_NAME = re.compile(r"^[a-z0-9._ -]{1,128}\.admx$", re.IGNORECASE)
_ADML_NAME = re.compile(r"^(en-us|de-de|es-es)/([a-z0-9._ -]{1,128}\.adml)$", re.IGNORECASE)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_STRING_REF = re.compile(r"^\$\((string|presentation)\.([^)]+)\)$")

_root = None
_cache = collections.OrderedDict()
_cache_lock = threading.Lock()
_claims = {}
_claims_lock = threading.Lock()
# {machine: hash} last written to machine_admx by this process -- see note_machine.
_reported = {}


class AdmxRejected(ValueError):
    """A template upload or a setting's options the hub refuses. The message is an
    operator-facing English sentence; callers render it through refusals.message."""


class AdmxHostile(AdmxRejected):
    """Not a broken template but an attempt -- a DTD. Fails the whole set rather than skipping
    the one file, see parse_set."""


def configure(root):
    """Where parsed catalogues live. Called once from app.py; tests point it at a temp dir."""
    global _root
    _root = root
    os.makedirs(root, exist_ok=True)
    with _cache_lock:
        _cache.clear()


def get_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_admx_db(db_path):
    """Create the tables if absent. Idempotent, like every other init_*_db."""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admx_sets (
                hash        TEXT PRIMARY KEY,
                files       INTEGER NOT NULL,
                policies    INTEGER NOT NULL,
                uploaded_at INTEGER NOT NULL,
                machine     TEXT
            )
            """
        )
        # Which set each PC last reported. What the editor's set picker counts, and what tells
        # the hub a hash is worth asking for at all.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS machine_admx (
                machine TEXT PRIMARY KEY,
                hash    TEXT NOT NULL,
                seen_at INTEGER NOT NULL
            )
            """
        )


# ================================================================ the set hash
def set_hash(files):
    """The hash of a template set: `files` is {relative path: bytes}. **The agent computes the
    same thing** (PolicyDefinitionsReader.cs), so a change here is a change there:

        sha256 over, for each file sorted by its lower-cased relative path with forward
        slashes, `<path>\\n<sha256 hex of its bytes>\\n`

    The hub recomputes it from what was uploaded, so a PC cannot file a set under somebody
    else's hash."""
    digest = hashlib.sha256()
    by_path = {path.lower(): content for path, content in files.items()}
    for path in sorted(by_path):
        content = by_path[path]
        digest.update(f"{path}\n{hashlib.sha256(content).hexdigest()}\n".encode("utf-8"))
    return digest.hexdigest()


def clean_hash(value):
    text = str(value or "").strip().lower()
    return text if _HASH.match(text) else None


# ================================================================ XML
def _decode(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8")


def _xml(raw, name):
    """Parse one template file with no DTD. See the module docstring."""
    try:
        text = _decode(raw)
    except UnicodeDecodeError:
        raise AdmxRejected(f"{name} is not UTF-8 or UTF-16 text")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise AdmxHostile(f"{name} declares a DTD, which a template never needs")
    try:
        return ET.fromstring(_XML_DECL.sub("", text, count=1))
    except ET.ParseError:
        raise AdmxRejected(f"{name} is not well-formed XML")


def _local(node):
    return node.tag.rsplit("}", 1)[-1]


def _children(node, name):
    return [c for c in node if _local(c) == name]


def _child(node, name):
    for c in node:
        if _local(c) == name:
            return c
    return None


def _section(node, name):
    """A child that may be missing, as an element to iterate either way. Explicit, because an
    Element's truth value is its child count, not whether it exists."""
    found = _child(node, name)
    return found if found is not None else ET.Element(name)


def _bool(node, attr, default=False):
    value = node.get(attr)
    return default if value is None else value.strip().lower() == "true"


def _int(node, attr, default):
    try:
        return int(node.get(attr, default))
    except (TypeError, ValueError):
        return default


# ================================================================ ADML (strings)
def _string_table(root):
    resources = _child(root, "resources")
    table = _child(resources, "stringTable") if resources is not None else None
    return {s.get("id"): (s.text or "") for s in (table if table is not None else [])
            if s.get("id")}


def _child_text(node, name):
    found = _child(node, name)
    return (found.text or "") if found is not None else ""


def _text_box(node, default_tag):
    """A textBox or comboBox: its label and default live in child elements, not its text."""
    item = {"kind": "textbox", "ref": node.get("refId"), "label": _child_text(node, "label"),
            "default": _child_text(node, default_tag)}
    if default_tag == "default":
        item["suggestions"] = [(s.text or "") for s in _children(node, "suggestion")]
    return item


# Presentation elements whose label is their own text: ADML tag -> (editor kind, how to read
# the default). gpedit's own defaults: a decimal box starts at 1, a dropdown at its first item.
_SIMPLE_PRESENTATION = {
    "checkBox": ("checkbox", lambda n: _bool(n, "defaultChecked")),
    "decimalTextBox": ("decimal", lambda n: _int(n, "defaultValue", 1)),
    "longDecimalTextBox": ("decimal", lambda n: _int(n, "defaultValue", 1)),
    "dropdownList": ("dropdown", lambda n: _int(n, "defaultItem", 0)),
    "listBox": ("listbox", None),
    "multiTextBox": ("multitext", None),
}


def _presentation_item(node):
    kind = _local(node)
    if kind == "text":
        return {"kind": "text", "label": (node.text or "").strip()}
    if kind == "textBox":
        return _text_box(node, "defaultValue")
    if kind == "comboBox":
        return _text_box(node, "default")
    if kind not in _SIMPLE_PRESENTATION:
        return None
    editor_kind, read_default = _SIMPLE_PRESENTATION[kind]
    item = {"kind": editor_kind, "ref": node.get("refId"), "label": (node.text or "").strip()}
    if read_default is not None:
        item["default"] = read_default(node)
    return item


def _presentations(root):
    resources = _child(root, "resources")
    table = _child(resources, "presentationTable") if resources is not None else None
    result = {}
    for presentation in (table if table is not None else []):
        items = [_presentation_item(c) for c in presentation]
        result[presentation.get("id")] = [i for i in items if i]
    return result


def _resolve(text, strings):
    match = _STRING_REF.match((text or "").strip())
    if not match:
        return text or ""
    return strings.get(match.group(2), match.group(2))


# ================================================================ ADMX (definitions)
class _File:
    """One .admx file being read: its target namespace and its prefix table, which every
    `ref` inside it is resolved through."""

    def __init__(self, root):
        namespaces = _child(root, "policyNamespaces")
        self.prefixes = {}
        self.namespace = ""
        for node in (namespaces if namespaces is not None else []):
            if _local(node) in ("target", "using"):
                self.prefixes[node.get("prefix", "")] = node.get("namespace", "")
                if _local(node) == "target":
                    self.namespace = node.get("namespace", "")

    def ref(self, text):
        text = (text or "").strip()
        if ":" in text:
            prefix, name = text.split(":", 1)
            return f"{self.prefixes.get(prefix, prefix)}:{name}"
        return f"{self.namespace}:{text}"


def _value(node):
    """A <value>/<enabledValue>/<trueValue>... body as {type, value}, or None when absent."""
    if node is None:
        return None
    for child in node:
        kind = _local(child)
        if kind == "decimal":
            return {"type": "dword", "value": _int(child, "value", 0)}
        if kind == "string":
            return {"type": "string", "value": child.text or ""}
        if kind == "delete":
            return {"type": "delete", "value": None}
        if kind == "longDecimal":
            return {"type": "qword", "value": child.get("value")}
    return None


def _value_list(node, default_key):
    """An enabledList / trueList / valueList as [{key, name, type, value}]."""
    if node is None:
        return []
    key = node.get("defaultKey") or default_key
    ops = []
    for item in _children(node, "item"):
        value = _value(_child(item, "value"))
        if value is not None:
            ops.append({"key": item.get("key") or key, "name": item.get("valueName") or "", **value})
    return ops


def _element(node, key, strings_ref):
    kind = _local(node)
    base = {"id": node.get("id"), "key": node.get("key") or key,
            "value_name": node.get("valueName") or ""}
    if kind == "boolean":
        return {**base, "type": "boolean",
                "true": _value(_child(node, "trueValue")), "false": _value(_child(node, "falseValue")),
                "true_list": _value_list(_child(node, "trueList"), base["key"]),
                "false_list": _value_list(_child(node, "falseList"), base["key"])}
    if kind == "decimal":
        return {**base, "type": "decimal", "min": _int(node, "minValue", 0),
                "max": _int(node, "maxValue", 9999), "required": _bool(node, "required"),
                "store_as_text": _bool(node, "storeAsText")}
    if kind == "longDecimal":
        return {**base, "type": "long_decimal"}
    if kind == "text":
        return {**base, "type": "text", "max_length": _int(node, "maxLength", 1023),
                "required": _bool(node, "required"), "expandable": _bool(node, "expandable")}
    if kind == "multiText":
        return {**base, "type": "multi_text", "max_length": _int(node, "maxLength", 1023),
                "max_strings": _int(node, "maxStrings", 0), "required": _bool(node, "required")}
    if kind == "enum":
        items = []
        for item in _children(node, "item"):
            items.append({"display": strings_ref(item.get("displayName")),
                          "value": _value(_child(item, "value")),
                          "list": _value_list(_child(item, "valueList"), base["key"])})
        return {**base, "type": "enum", "required": _bool(node, "required"), "items": items}
    if kind == "list":
        return {**base, "type": "list", "prefix": node.get("valuePrefix"),
                "explicit": _bool(node, "explicitValue"), "expandable": _bool(node, "expandable"),
                "additive": _bool(node, "additive")}
    return None


def _keys_of(policy):
    """Every key a policy can write, for the outside-the-policy-tree check."""
    keys = {policy["key"]}
    for op in policy["enabled_list"] + policy["disabled_list"]:
        keys.add(op["key"])
    for element in policy["elements"]:
        keys.add(element["key"])
        for op in element.get("true_list", []) + element.get("false_list", []):
            keys.add(op["key"])
        for item in element.get("items", []):
            keys.update(op["key"] for op in item["list"])
    return keys


def _types_of(policy):
    values = [policy["enabled_value"], policy["disabled_value"]]
    values += policy["enabled_list"] + policy["disabled_list"]
    for element in policy["elements"]:
        if element["type"] == "long_decimal":
            return {"qword"}
        values += [element.get("true"), element.get("false")]
        values += element.get("true_list", []) + element.get("false_list", [])
        for item in element.get("items", []):
            values.append(item["value"])
            values += item["list"]
    return {v["type"] for v in values if v}


def _unsupported(policy):
    if any(not (k or "").lower().startswith(POLICY_ROOT) for k in _keys_of(policy)):
        return UNSUPPORTED_OUTSIDE
    if "qword" in _types_of(policy):
        return UNSUPPORTED_QWORD
    return None


def _policy(node, file, strings_ref):
    key = node.get("key") or ""
    elements_node = _child(node, "elements")
    elements = [e for e in (_element(c, key, strings_ref) for c in
                            (elements_node if elements_node is not None else [])) if e]
    category = _child(node, "parentCategory")
    policy = {
        "ref": file.ref(node.get("name")),
        "class": {"Machine": CLASS_MACHINE, "User": CLASS_USER}.get(node.get("class"), CLASS_BOTH),
        "category": file.ref(category.get("ref")) if category is not None else None,
        "key": key, "value_name": node.get("valueName") or "",
        "enabled_value": _value(_child(node, "enabledValue")),
        "disabled_value": _value(_child(node, "disabledValue")),
        "enabled_list": _value_list(_child(node, "enabledList"), key),
        "disabled_list": _value_list(_child(node, "disabledList"), key),
        "elements": elements,
        "display": strings_ref(node.get("displayName")),
        "explain": strings_ref(node.get("explainText")),
    }
    reference = _STRING_REF.match(node.get("presentation") or "")
    policy["presentation_id"] = reference.group(2) if reference else None
    policy["unsupported"] = _unsupported(policy)
    return policy


def _definitions(name, raw, languages):
    """One .admx and its .adml per language -> (categories, policies)."""
    root = _xml(raw, name)
    file = _File(root)
    tables = {lang: _string_table(adml) for lang, adml in languages.items()}

    def strings_ref(text):
        return {lang: _resolve(text, table) for lang, table in tables.items()}

    categories = {}
    for node in _children(_section(root, "categories"), "category"):
        parent = _child(node, "parentCategory")
        ref = file.ref(node.get("name"))
        categories[ref] = {"ref": ref, "parent": file.ref(parent.get("ref")) if parent is not None else None,
                           "name": strings_ref(node.get("displayName"))}
    presentations = {lang: _presentations(adml) for lang, adml in languages.items()}
    policies = {}
    for node in _children(_section(root, "policies"), "policy"):
        policy = _policy(node, file, strings_ref)
        pid = policy.pop("presentation_id")
        policy["presentation"] = {lang: table.get(pid, []) for lang, table in presentations.items()}
        policies[policy["ref"]] = policy
    return categories, policies


def parse_set(files):
    """{relative path: bytes} -> the catalogue {"categories": {...}, "policies": {...}}.

    A file that fails to parse is skipped and named in `skipped`, rather than failing the set:
    one vendor's broken template should not hide Windows' own. A DTD anywhere fails the set --
    that is not a broken file, it is an attempt."""
    admx = {k: v for k, v in files.items() if k.lower().endswith(".admx")}
    adml = _adml_index(files)
    categories, policies, skipped = {}, {}, []
    for name in sorted(admx):
        found = _parse_file(name, admx[name], adml)
        if found is None:
            skipped.append(name)
            continue
        categories.update(found[0])
        policies.update(found[1])
    _drop_dangling(categories, policies)
    return {"categories": categories, "policies": policies, "skipped": skipped}


def _adml_index(files):
    """{(ui language, lower-cased file name): (path, bytes)} for every .adml in the set."""
    codes = {code.lower(): ui for code, ui in LANGUAGES.items()}
    index = {}
    for path, raw in files.items():
        match = _ADML_NAME.match(path)
        if match:
            index[(codes[match.group(1).lower()], match.group(2).lower())] = (path, raw)
    return index


def _parse_file(name, raw, adml):
    """One .admx with its .adml -> (categories, policies), or None when it is skipped: no
    English strings, or not well-formed. An AdmxHostile is raised through, never skipped."""
    stem = name.rsplit(".", 1)[0].lower() + ".adml"
    try:
        languages = {lang: _xml(adml[(lang, stem)][1], adml[(lang, stem)][0])
                     for lang in LANGUAGES.values() if (lang, stem) in adml}
        if FALLBACK_LANGUAGE not in languages:
            return None
        return _definitions(name, raw, languages)
    except AdmxHostile:
        raise
    except AdmxRejected:
        return None


def _drop_dangling(categories, policies):
    """A parent or category from a file not in the set -- usually Windows' own, referenced by a
    vendor template -- puts the node at the top rather than losing it."""
    for category in categories.values():
        if category["parent"] not in categories:
            category["parent"] = None
    for policy in policies.values():
        if policy["category"] not in categories:
            policy["category"] = None


# ================================================================ upload and storage
def _catalog_path(digest):
    """Where a set's catalogue lives. `digest` is always clean_hash()'s 64 hex characters, so it
    cannot leave `_root`; the containment check is said again here anyway, on the joined path,
    because that is the form a reader (and CodeQL's py/path-injection, alerts #201-#203 on the
    first cut of this) can see is safe without tracing every caller."""
    root = os.path.realpath(_root)
    path = os.path.realpath(os.path.join(root, f"{digest}.json.gz"))
    if not path.startswith(root + os.sep):
        raise AdmxRejected("that is not a template set")
    return path


def _read_zip(path):
    """The uploaded zip as {relative path: bytes}, every entry checked before it is read."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise AdmxRejected("the upload is not a zip archive")
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES:
            raise AdmxRejected("the upload has too many files")
        if sum(e.file_size for e in entries) > MAX_TOTAL_BYTES:
            raise AdmxRejected("the upload is larger than this hub accepts")
        files = {}
        for entry in entries:
            name = entry.filename
            if entry.is_dir():
                continue
            if not (_ADMX_NAME.match(name) or _ADML_NAME.match(name)):
                # Never a path the hub writes to -- nothing here is extracted to disk -- but a
                # name outside the two shapes is not a template, and a sender that includes one
                # is not the agent.
                raise AdmxRejected(f"the upload contains {name[:80]!r}, which is not a template")
            if entry.file_size > MAX_FILE_BYTES:
                raise AdmxRejected(f"{name} is larger than a template can be")
            with archive.open(entry) as handle:
                content = handle.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise AdmxRejected(f"{name} is larger than a template can be")
            files[name] = content
    if not any(k.lower().endswith(".admx") for k in files):
        raise AdmxRejected("the upload contains no .admx files")
    return files


def _spool(stream, length):
    incoming = os.path.join(_root, "incoming")
    os.makedirs(incoming, exist_ok=True)
    path = os.path.join(incoming, uuid.uuid4().hex + ".zip")
    written = 0
    try:
        with open(path, "wb") as handle:
            while written < length:
                chunk = stream.read(min(1024 * 1024, length - written))
                if not chunk:
                    break
                written += len(chunk)
                handle.write(chunk)
            if stream.read(1):
                raise AdmxRejected("the body is longer than its Content-Length")
    except BaseException:
        _remove(path)
        raise
    return path


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def ingest(db_path, machine, claimed, stream, length, now=None):
    """Take one PC's template upload. Returns `"stored"` or `"duplicate"`; raises
    AdmxRejected for anything refused (the web layer maps the length cases to 411/413).

    Verified before anything is kept: every zip entry's name and size, every file's XML, and
    the set's hash recomputed from the bytes, which must equal the one the PC claimed."""
    digest = clean_hash(claimed)
    if digest is None:
        raise AdmxRejected("hash must be 64 hex characters")
    if length is None or length <= 0:
        raise AdmxRejected("a Content-Length is required")
    if length > MAX_UPLOAD_BYTES:
        raise AdmxRejected("that upload is larger than this hub accepts")
    if has_set(db_path, digest):
        return "duplicate"
    now = int(now or time.time())
    if not holds_claim(digest, machine, now):
        # The heartbeat asks ONE PC per unknown set (note_machine); an upload nobody asked
        # for is refused before a byte is spooled. Without this, any enrolled agent -- or a
        # stolen agent token -- could store set after set under hashes of its own making
        # (raised in review on #191).
        raise AdmxRejected("this PC was not asked for that template set")
    path = _spool(stream, length)
    try:
        files = _read_zip(path)
    finally:
        _remove(path)
    if set_hash(files) != digest:
        raise AdmxRejected("the upload does not match the hash it was sent under")
    catalog = parse_set(files)
    _make_room(db_path)
    _write_catalog(digest, catalog)
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admx_sets (hash, files, policies, uploaded_at, machine)"
            " VALUES (?, ?, ?, ?, ?)",
            (digest, len(files), len(catalog["policies"]), now, machine))
    with _claims_lock:
        _claims.pop(digest, None)
    return "stored"


def holds_claim(digest, machine, now=None):
    """Whether `machine` is the PC currently asked to upload `digest` (note_machine)."""
    now = int(now or time.time())
    with _claims_lock:
        holder, until = _claims.get(digest, (None, 0))
    return holder == machine and until > now


def _make_room(db_path):
    """Keep at most MAX_SETS - 1 sets before one more is stored: drop the oldest sets no PC
    reports any more, and refuse when every set is still in use. A dropped set's policies keep
    working -- they store compiled values -- and a PC that reports it again re-uploads it."""
    with get_conn(db_path) as conn:
        held = conn.execute("SELECT COUNT(*) FROM admx_sets").fetchone()[0]
        if held < MAX_SETS:
            return
        unused = [r["hash"] for r in conn.execute(
            "SELECT hash FROM admx_sets WHERE hash NOT IN (SELECT hash FROM machine_admx)"
            " ORDER BY uploaded_at LIMIT ?", (held - MAX_SETS + 1,)).fetchall()]
        if held - len(unused) >= MAX_SETS:
            raise AdmxRejected("the hub holds as many template sets as it keeps, all in use")
        conn.executemany("DELETE FROM admx_sets WHERE hash = ?", [(h,) for h in unused])
    for digest in unused:
        _remove(_catalog_path(digest))
        with _cache_lock:
            _cache.pop(digest, None)


def _write_catalog(digest, catalog):
    path = _catalog_path(digest)
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(catalog, handle, separators=(",", ":"))
    os.replace(temporary, path)
    with _cache_lock:
        _cache.pop(digest, None)


def catalog(digest):
    """The parsed catalogue for a set, or None when the hub does not hold it."""
    digest = clean_hash(digest)
    if digest is None or _root is None:
        return None
    with _cache_lock:
        if digest in _cache:
            _cache.move_to_end(digest)
            return _cache[digest]
    try:
        with gzip.open(_catalog_path(digest), "rt", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    with _cache_lock:
        _cache[digest] = loaded
        while len(_cache) > CACHED_SETS:
            _cache.popitem(last=False)
    return loaded


def has_set(db_path, digest):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT 1 FROM admx_sets WHERE hash = ?", (digest,)).fetchone()
    return row is not None and os.path.exists(_catalog_path(digest))


# ================================================================ heartbeat
def note_machine(db_path, machine, claimed, now=None):
    """Record the set a PC reported. Returns whether this PC should upload it: only for a set
    the hub lacks, and only for the one PC currently holding the claim on that set."""
    digest = clean_hash(claimed)
    if digest is None:
        return False
    now = int(now or time.time())
    # Written only when a PC's set CHANGES: this runs on every heartbeat of every Windows PC,
    # and a template set changes a few times a year. The memo is per process, so a restart
    # costs one write per PC and nothing else.
    if _reported.get(machine) != digest:
        with get_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO machine_admx (machine, hash, seen_at) VALUES (?, ?, ?)"
                " ON CONFLICT(machine) DO UPDATE SET hash = excluded.hash,"
                " seen_at = excluded.seen_at", (machine, digest, now))
        _reported[machine] = digest
    if has_set(db_path, digest):
        return False
    with _claims_lock:
        holder, until = _claims.get(digest, (None, 0))
        if holder not in (None, machine) and until > now:
            return False
        _claims[digest] = (machine, now + UPLOAD_CLAIM_SECONDS)
    return True


def forget_machine(db_path, machine):
    _reported.pop(machine, None)
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM machine_admx WHERE machine = ?", (machine,))


def list_sets(db_path, in_scope=None):
    """The sets the hub holds that a PC the caller can see reports, most-reported first:
    [{hash, files, policies, uploaded_at, machines, os}]. `machines` counts only PCs `in_scope`
    accepts, so the picker never says how many PCs outside the caller's scope run a build, and
    a set none of them reports -- a build since upgraded away, or one outside their scope -- is
    left out; `os` is a label from one of those PCs."""
    with get_conn(db_path) as conn:
        sets = [dict(r) for r in conn.execute("SELECT * FROM admx_sets").fetchall()]
        reported = conn.execute(
            "SELECT a.machine, a.hash, i.os_caption, i.os_build FROM machine_admx a"
            " LEFT JOIN machine_info i ON i.machine = a.machine").fetchall()
    by_hash = {}
    for row in reported:
        if in_scope is None or in_scope(row["machine"]):
            by_hash.setdefault(row["hash"], []).append(row)
    result = []
    for entry in sets:
        rows = by_hash.get(entry["hash"], [])
        if not rows:
            continue
        label = next((" ".join(filter(None, [r["os_caption"], r["os_build"]])) for r in rows
                      if r["os_caption"] or r["os_build"]), "")
        result.append({"hash": entry["hash"], "files": entry["files"],
                       "policies": entry["policies"], "uploaded_at": entry["uploaded_at"],
                       "machines": len(rows), "os": label})
    result.sort(key=lambda s: (-s["machines"], -s["uploaded_at"]))
    return result


# ================================================================ reading for the editor
def _text(strings, lang):
    if not strings:
        return ""
    return strings.get(lang) or strings.get(FALLBACK_LANGUAGE) or ""


def tree(digest, lang):
    """The category tree and every policy's one-line entry, in one language."""
    loaded = catalog(digest)
    if loaded is None:
        return None
    categories = [{"ref": c["ref"], "parent": c["parent"], "name": _text(c["name"], lang)}
                  for c in loaded["categories"].values()]
    policies = [{"ref": p["ref"], "category": p["category"], "class": p["class"],
                 "name": _text(p["display"], lang), "unsupported": p["unsupported"]}
                for p in loaded["policies"].values()]
    categories.sort(key=lambda c: c["name"].lower())
    policies.sort(key=lambda p: p["name"].lower())
    return {"hash": digest, "categories": categories, "policies": policies}


def _public_element(element, lang):
    public = {k: v for k, v in element.items()
              if k not in ("true", "false", "true_list", "false_list", "items")}
    if element["type"] == "enum":
        public["items"] = [_text(item["display"], lang) for item in element["items"]]
    return public


def policy_detail(digest, ref, lang):
    """One policy as the editor's dialog needs it: name, explanation, option fields."""
    loaded = catalog(digest)
    policy = loaded["policies"].get(ref) if loaded else None
    if policy is None:
        return None
    presentation = policy["presentation"].get(lang) or policy["presentation"].get(FALLBACK_LANGUAGE) or []
    return {"ref": ref, "class": policy["class"], "name": _text(policy["display"], lang),
            "explain": _text(policy["explain"], lang), "key": policy["key"],
            "value_name": policy["value_name"], "unsupported": policy["unsupported"],
            "category": policy["category"],
            "elements": [_public_element(e, lang) for e in policy["elements"]],
            "presentation": presentation}


def find_policy(digest, ref):
    loaded = catalog(digest)
    return loaded["policies"].get(ref) if loaded else None


# ================================================================ compile
def _op_row(op, key=None, name=None):
    return {"path": key if key is not None else op["key"],
            "name": name if name is not None else op["name"],
            "type": op["type"], "value": op["value"]}


class _Rows:
    """What one setting writes: `rows` to set, `deletes` the `delete` values left out (see the
    module docstring)."""

    def __init__(self):
        self.rows, self.deletes = [], []

    def add(self, row):
        if row["type"] == "delete":
            self.deletes.append({"path": row["path"], "name": row["name"]})
        elif row["type"] == "qword":
            raise AdmxRejected("this setting writes a QWORD value, which policy cannot set")
        else:
            self.rows.append(row)

    def value(self, key, name, value, default_type="dword", default_value=None):
        if value is None:
            if name:
                self.add({"path": key, "name": name, "type": default_type, "value": default_value})
            return
        self.add({"path": key, "name": name, "type": value["type"], "value": value["value"]})

    def ops(self, ops):
        for op in ops:
            self.add(_op_row(op))


def _option(options, element):
    return options.get(element["id"]) if isinstance(options, dict) else None


def _compile_boolean(out, element, raw):
    checked = bool(raw)
    out.value(element["key"], element["value_name"], element["true" if checked else "false"],
              default_value=1 if checked else 0)
    out.ops(element["true_list" if checked else "false_list"])


def _compile_decimal(out, element, raw):
    if raw in (None, ""):
        if element["required"]:
            raise AdmxRejected(f"{element['id']} needs a number")
        return
    try:
        number = int(str(raw).strip())
    except ValueError:
        raise AdmxRejected(f"{element['id']} needs a whole number")
    if not element["min"] <= number <= element["max"]:
        raise AdmxRejected(f"{element['id']} must be between {element['min']} and {element['max']}")
    out.add({"path": element["key"], "name": element["value_name"],
             "type": "string" if element["store_as_text"] else "dword",
             "value": str(number) if element["store_as_text"] else number})


def _compile_text(out, element, raw):
    text = "" if raw is None else str(raw)
    if not text:
        if element["required"]:
            raise AdmxRejected(f"{element['id']} needs a value")
        return
    if len(text) > element["max_length"]:
        raise AdmxRejected(f"{element['id']} is longer than {element['max_length']} characters")
    out.add({"path": element["key"], "name": element["value_name"],
             "type": "expand_string" if element["expandable"] else "string", "value": text})


def _compile_multi_text(out, element, raw):
    lines = raw if isinstance(raw, list) else str(raw or "").splitlines()
    lines = [str(line) for line in lines if str(line) != ""]
    if not lines:
        if element["required"]:
            raise AdmxRejected(f"{element['id']} needs at least one line")
        return
    if element["max_strings"] and len(lines) > element["max_strings"]:
        raise AdmxRejected(f"{element['id']} takes at most {element['max_strings']} lines")
    out.add({"path": element["key"], "name": element["value_name"], "type": "multi_string",
             "value": lines})


def _compile_enum(out, element, raw):
    if raw in (None, ""):
        if element["required"]:
            raise AdmxRejected(f"{element['id']} needs a choice")
        return
    try:
        index = int(raw)
    except (TypeError, ValueError):
        index = -1
    if not 0 <= index < len(element["items"]):
        raise AdmxRejected(f"{element['id']} is not one of its choices")
    item = element["items"][index]
    out.value(element["key"], element["value_name"], item["value"])
    out.ops(item["list"])


def _compile_list(out, element, raw):
    entries = raw if isinstance(raw, list) else []
    kind = "expand_string" if element["expandable"] else "string"
    for index, entry in enumerate(entries, start=1):
        if element["explicit"]:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2 or not str(entry[0]).strip():
                raise AdmxRejected(f"{element['id']} needs a name and a value on every line")
            name, value = str(entry[0]).strip(), str(entry[1])
        elif element["prefix"] is not None:
            name, value = f"{element['prefix']}{index}", str(entry)
        else:
            name = value = str(entry)
        if value == "" and not element["explicit"]:
            continue
        out.add({"path": element["key"], "name": name, "type": kind, "value": value})


_COMPILERS = {"boolean": _compile_boolean, "decimal": _compile_decimal, "text": _compile_text,
              "multi_text": _compile_multi_text, "enum": _compile_enum, "list": _compile_list}


def compile_policy(policy, state, options):
    """What one Administrative Template setting writes in `state` with `options`:
    {"rows": [{path, name, type, value}], "deletes": [{path, name}]}.

    Windows' rules (the ADMX schema reference): Enabled writes `enabledValue` -- or DWORD 1 when
    the policy names a value but gives none -- then `enabledList`, then each option element.
    Disabled writes `disabledValue` -- or DWORD 0 -- then `disabledList`, and deletes the option
    elements' values. Raises AdmxRejected on options the template would refuse."""
    if state not in STATES:
        raise AdmxRejected("a template setting is enabled or disabled")
    if policy.get("unsupported"):
        raise AdmxRejected(f"{policy['ref']} cannot be set by policy here")
    out = _Rows()
    enabled = state == STATE_ENABLED
    out.value(policy["key"], policy["value_name"],
              policy["enabled_value" if enabled else "disabled_value"],
              default_value=1 if enabled else 0)
    out.ops(policy["enabled_list" if enabled else "disabled_list"])
    for element in policy["elements"]:
        if not enabled:
            if element["value_name"]:
                out.deletes.append({"path": element["key"], "name": element["value_name"]})
            continue
        compiler = _COMPILERS.get(element["type"])
        if compiler is None:
            raise AdmxRejected(f"{element['id']} is an option type policy cannot set")
        compiler(out, element, _option(options, element))
    return {"rows": out.rows, "deletes": out.deletes}
