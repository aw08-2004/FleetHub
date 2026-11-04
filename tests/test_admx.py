"""admx.py -- Administrative Templates for the Group Policy editor (roadmap #32).

The silent failures this file exists to catch:

  * **A template setting that writes something other than what gpedit would.** Enabled with no
    `enabledValue` is DWORD 1, Disabled is DWORD 0, a list writes `prefix1..n`, an enum writes
    its item's value AND its value list. Get one backwards and the PC carries the opposite of
    what the editor showed, with every screen agreeing it is right.
  * **A template that reaches outside HKLM\\SOFTWARE\\Policies** -- it must be listed as
    unsettable, never compiled.
  * **An upload filed under a hash it does not have**, which would let one PC replace the
    templates every other PC on that build is edited with.
  * **XML with a DTD parsed at all.** The files come from an agent.
  * **A later policy's "Disabled" leaving the earlier "Enabled" options behind** -- Windows
    replaces a setting whole, and the merge must too.
  * **The whole fleet uploading the same set at once**, instead of the one PC holding the claim.
  * **An upload nobody asked for being stored**, which would let one agent fill the hub's disk
    with sets of its own making; and the stored sets growing without bound.
  * **A Disabled setting's deletes lost** (phase 2) -- or sent to an agent too old to write the
    local GPO, whose document would then move over rows it can never apply.
  * **A User Configuration value landing in HKLM** (phase 3): sent without its class, sent to an
    agent that would read it as a machine row, or merged with the machine half of the same
    template -- and any machine id moving because User Configuration now exists.
"""
import io
import os
import sqlite3
import sys
import tempfile
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import admx
import fleet
import gpo
import pc_tree
import permissions
import rules
import scripts
import settings

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


NS = 'xmlns="http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions"'

EDGE_ADMX = f"""<?xml version="1.0" encoding="utf-8"?>
<policyDefinitions revision="1.0" schemaVersion="1.0" {NS}>
  <policyNamespaces>
    <target prefix="edge" namespace="Microsoft.Policies.Edge"/>
    <using prefix="windows" namespace="Microsoft.Policies.Windows"/>
  </policyNamespaces>
  <resources minRequiredRevision="1.0"/>
  <categories>
    <category name="edge" displayName="$(string.edge)"/>
    <category name="startup" displayName="$(string.startup)"><parentCategory ref="edge"/></category>
  </categories>
  <policies>
    <policy name="HideFirstRunExperience" class="Machine" displayName="$(string.Hide)"
            explainText="$(string.Hide_Explain)" key="SOFTWARE\\Policies\\Microsoft\\Edge"
            valueName="HideFirstRunExperience">
      <parentCategory ref="edge"/>
      <enabledValue><decimal value="1"/></enabledValue>
      <disabledValue><decimal value="0"/></disabledValue>
    </policy>
    <policy name="RestoreOnStartup" class="Both" displayName="$(string.Restore)"
            explainText="$(string.Restore_Explain)" presentation="$(presentation.Restore)"
            key="SOFTWARE\\Policies\\Microsoft\\Edge">
      <parentCategory ref="startup"/>
      <elements>
        <enum id="RestoreOnStartup" valueName="RestoreOnStartup" required="true">
          <item displayName="$(string.Restore_NTP)"><value><decimal value="5"/></value></item>
          <item displayName="$(string.Restore_URLs)"><value><decimal value="4"/></value>
            <valueList><item valueName="RestoreOnStartupUserURLsEnabled"><value><decimal value="1"/></value></item></valueList>
          </item>
        </enum>
        <list id="URLs" key="SOFTWARE\\Policies\\Microsoft\\Edge\\RestoreOnStartupURLs" valuePrefix=""/>
        <decimal id="Delay" valueName="StartupDelay" minValue="0" maxValue="60"/>
        <text id="Home" valueName="HomepageLocation" expandable="true"/>
        <boolean id="Ask" valueName="AskBeforeClose"/>
      </elements>
    </policy>
    <policy name="ProxyPreference" class="User" displayName="$(string.Proxy)"
            key="SOFTWARE\\Policies\\Microsoft\\Edge" valueName="ProxyMode">
      <parentCategory ref="edge"/>
    </policy>
    <policy name="ShellTweak" class="Machine" displayName="$(string.Shell)"
            key="SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer" valueName="NoDrives">
      <parentCategory ref="windows:WindowsComponents"/>
    </policy>
  </policies>
</policyDefinitions>
"""


def adml(strings):
    rows = "\n".join(f'      <string id="{k}">{v}</string>' for k, v in strings.items())
    return f"""<?xml version="1.0" encoding="utf-8"?>
<policyDefinitionResources revision="1.0" schemaVersion="1.0" {NS}>
  <displayName/><description/>
  <resources>
    <stringTable>
{rows}
    </stringTable>
    <presentationTable>
      <presentation id="Restore">
        <dropdownList refId="RestoreOnStartup" defaultItem="1">Action on startup</dropdownList>
        <listBox refId="URLs">Sites to open</listBox>
        <decimalTextBox refId="Delay" defaultValue="5">Delay</decimalTextBox>
        <textBox refId="Home"><label>Home page</label></textBox>
        <checkBox refId="Ask" defaultChecked="true">Ask before closing</checkBox>
      </presentation>
    </presentationTable>
  </resources>
</policyDefinitionResources>
"""


EN = {"edge": "Microsoft Edge", "startup": "Startup", "Hide": "Hide the First-run experience",
      "Hide_Explain": "Hides it.", "Restore": "Action to take on startup",
      "Restore_Explain": "What opens.", "Restore_NTP": "Open a new tab",
      "Restore_URLs": "Open a list of URLs", "Proxy": "Proxy mode", "Shell": "Hide drives"}
DE = {**EN, "Hide": "Ersteinrichtung ausblenden"}

FILES = {"msedge.admx": EDGE_ADMX.encode("utf-8"),
         "en-US/msedge.adml": adml(EN).encode("utf-8"),
         "de-DE/msedge.adml": adml(DE).encode("utf-16")}


def zipped(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def upload(db, machine, digest, files):
    body = zipped(files)
    return admx.ingest(db, machine, digest, io.BytesIO(body), len(body))


def refused(fn):
    try:
        fn()
    except admx.AdmxRejected as e:
        return str(e)
    return None


def test_parse():
    print("\n-- parsing a set --")
    catalog = admx.parse_set(FILES)
    policies = catalog["policies"]
    hide = policies.get("Microsoft.Policies.Edge:HideFirstRunExperience")
    check("a policy is keyed by its target namespace and name", hide is not None)
    check("its strings resolve per language, from UTF-8 and UTF-16 ADML alike",
          hide["display"] == {"en": "Hide the First-run experience",
                              "de": "Ersteinrichtung ausblenden"})
    check("a category's parent resolves through the file's own prefix",
          catalog["categories"]["Microsoft.Policies.Edge:startup"]["parent"]
          == "Microsoft.Policies.Edge:edge")
    check("a category from a file not in the set leaves the policy at the top, not lost",
          policies["Microsoft.Policies.Edge:ShellTweak"]["category"] is None)
    check("a key outside SOFTWARE\\Policies is listed as unsettable",
          policies["Microsoft.Policies.Edge:ShellTweak"]["unsupported"] == admx.UNSUPPORTED_OUTSIDE)
    check("class Both and User are kept apart",
          policies["Microsoft.Policies.Edge:RestoreOnStartup"]["class"] == admx.CLASS_BOTH
          and policies["Microsoft.Policies.Edge:ProxyPreference"]["class"] == admx.CLASS_USER)
    restore = policies["Microsoft.Policies.Edge:RestoreOnStartup"]
    check("the presentation lists the option fields in order",
          [i["kind"] for i in restore["presentation"]["en"]]
          == ["dropdown", "listbox", "decimal", "textbox", "checkbox"])

    dtd = dict(FILES)
    dtd["msedge.admx"] = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + EDGE_ADMX.split("?>", 1)[1].encode()
    check("a DTD anywhere fails the whole set", "DTD" in (refused(lambda: admx.parse_set(dtd)) or ""))
    broken = dict(FILES)
    broken["other.admx"] = b"<policyDefinitions"
    broken["en-US/other.adml"] = adml(EN).encode()
    check("one malformed vendor file is skipped, not fatal",
          admx.parse_set(broken)["skipped"] == ["other.admx"])


def test_compile():
    print("\n-- what a setting writes --")
    policies = admx.parse_set(FILES)["policies"]
    hide = policies["Microsoft.Policies.Edge:HideFirstRunExperience"]
    check("Enabled writes enabledValue",
          admx.compile_policy(hide, "enabled", {})["rows"][0]["value"] == 1)
    check("Disabled writes disabledValue",
          admx.compile_policy(hide, "disabled", {})["rows"][0]["value"] == 0)
    proxy = dict(policies["Microsoft.Policies.Edge:ProxyPreference"], enabled_value=None)
    check("a named value with no enabledValue is DWORD 1, as in Windows",
          admx.compile_policy(proxy, "enabled", {})["rows"]
          == [{"path": "SOFTWARE\\Policies\\Microsoft\\Edge", "name": "ProxyMode",
               "type": "dword", "value": 1}])

    restore = policies["Microsoft.Policies.Edge:RestoreOnStartup"]
    out = admx.compile_policy(restore, "enabled", {"RestoreOnStartup": 1,
                                                   "URLs": ["https://a", "https://b"],
                                                   "Delay": "10", "Home": "%USERPROFILE%",
                                                   "Ask": False})
    rows = {(r["path"].rsplit("\\", 1)[-1], r["name"]): (r["type"], r["value"]) for r in out["rows"]}
    check("an enum writes its item's value", rows[("Edge", "RestoreOnStartup")] == ("dword", 4))
    check("...and its item's value list",
          rows[("Edge", "RestoreOnStartupUserURLsEnabled")] == ("dword", 1))
    check("a list with an empty prefix writes 1..n under its own key",
          rows[("RestoreOnStartupURLs", "1")] == ("string", "https://a")
          and rows[("RestoreOnStartupURLs", "2")] == ("string", "https://b"))
    check("a decimal is a DWORD", rows[("Edge", "StartupDelay")] == ("dword", 10))
    check("an expandable text is REG_EXPAND_SZ",
          rows[("Edge", "HomepageLocation")] == ("expand_string", "%USERPROFILE%"))
    check("an unchecked boolean writes 0", rows[("Edge", "AskBeforeClose")] == ("dword", 0))
    check("a decimal out of range is refused",
          refused(lambda: admx.compile_policy(restore, "enabled", {"RestoreOnStartup": 0,
                                                                  "Delay": 61})) is not None)
    check("a required enum without a choice is refused",
          refused(lambda: admx.compile_policy(restore, "enabled", {})) is not None)
    check("a negative enum index is refused, not read from the end",
          refused(lambda: admx.compile_policy(restore, "enabled", {"RestoreOnStartup": -1}))
          is not None)
    disabled = admx.compile_policy(restore, "disabled", {})
    check("Disabled writes no option values and reports the deletes it leaves out",
          disabled["rows"] == [] and {"path": "SOFTWARE\\Policies\\Microsoft\\Edge",
                                      "name": "StartupDelay"} in disabled["deletes"])
    check("an unsettable setting cannot be compiled",
          refused(lambda: admx.compile_policy(policies["Microsoft.Policies.Edge:ShellTweak"],
                                              "enabled", {})) is not None)


def test_upload(db):
    print("\n-- uploads --")
    digest = admx.set_hash(FILES)
    check("the set hash ignores the case of a path",
          admx.set_hash({k.upper(): v for k, v in FILES.items()}) == digest)
    check("an upload no heartbeat asked for is refused before it is read",
          "not asked" in (refused(lambda: upload(db, "PC-1", digest, FILES)) or ""))
    admx.note_machine(db, "PC-1", "0" * 64)
    check("an upload under another set's hash is refused",
          "does not match" in (refused(lambda: upload(db, "PC-1", "0" * 64, FILES)) or ""))
    bad = dict(FILES)
    bad["../evil.admx"] = b"x"
    admx.note_machine(db, "PC-1", admx.set_hash(bad))
    check("an entry that is not a template name is refused",
          "not a template" in (refused(lambda: upload(db, "PC-1", admx.set_hash(bad), bad)) or ""))
    check("no Content-Length is refused",
          refused(lambda: admx.ingest(db, "PC-1", digest, io.BytesIO(b""), None)) is not None)
    body = zipped(FILES)
    check("a body longer than its Content-Length is refused",
          refused(lambda: admx.ingest(db, "PC-1", digest, io.BytesIO(body + b"x"), len(body)))
          is not None)

    check("the first PC on a build is asked for it", admx.note_machine(db, "PC-1", digest))
    check("a second PC is not, while the first holds the claim",
          not admx.note_machine(db, "PC-2", digest))
    check("...and its upload is refused even though the set is wanted",
          "not asked" in (refused(lambda: upload(db, "PC-2", digest, FILES)) or ""))
    check("the claiming PC keeps being asked", admx.note_machine(db, "PC-1", digest))
    for n in range(50):
        admx.note_machine(db, "PC-6", f"{n:064x}")
    check("a PC reporting a new made-up hash every heartbeat holds one claim, not fifty",
          sum(1 for who, _ in admx._claims.values() if who == "PC-6") == 1)
    admx.note_machine(db, "PC-5", "e" * 64, now=1)
    admx.note_machine(db, "PC-4", "d" * 64)
    check("an expired claim is dropped", "e" * 64 not in admx._claims)
    check("a good upload is stored", upload(db, "PC-1", digest, FILES) == "stored")
    check("...and a repeat is a duplicate, not a second copy",
          upload(db, "PC-1", digest, FILES) == "duplicate")
    check("nobody is asked for a set the hub holds", not admx.note_machine(db, "PC-2", digest))
    sets = admx.list_sets(db)
    check("the set is listed with both PCs counted", sets[0]["machines"] == 2)
    check("a scoped caller counts only their own PCs",
          admx.list_sets(db, in_scope=lambda m: m == "PC-2")[0]["machines"] == 1)
    tree = admx.tree(digest, "de")
    check("the tree reads in the caller's language",
          any(p["name"] == "Ersteinrichtung ausblenden" for p in tree["policies"]))
    check("...falling back to English where a template has no translation",
          admx.tree(digest, "es")["policies"][0]["name"] in {n for n in EN.values()})
    admx.configure(admx._root)   # drop the in-memory cache: it must come back from disk
    detail = admx.policy_detail(digest, "Microsoft.Policies.Edge:RestoreOnStartup", "en")
    check("a setting's detail survives a restart", detail and detail["elements"][0]["items"]
          == ["Open a new tab", "Open a list of URLs"])
    return digest


def test_cap(db, digest):
    print("\n-- the stored sets are bounded --")
    # Patched rather than assigned, so the real cap comes back however this block exits.
    with mock.patch.object(admx, "MAX_SETS", 3):
        made = []
        for n in range(3):
            files = {**FILES, f"extra{n}.admx": FILES["msedge.admx"]}
            files[f"en-US/extra{n}.adml"] = FILES["en-US/msedge.adml"]
            other = admx.set_hash(files)
            admx.note_machine(db, "PC-9", other)
            made.append((other, upload(db, "PC-9", other, files)))
        held = {s["hash"] for s in admx.list_sets(db)}
        check("past the cap, the oldest set no PC reports makes room",
              made[2][1] == "stored" and made[0][0] not in held and admx.catalog(made[0][0]) is None)
        check("...and a set a PC still reports is kept", digest in held)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO machine_admx (machine, hash, seen_at) VALUES ('PC-8', ?, 0)",
                         (made[1][0],))
        files = {**FILES, "extra9.admx": FILES["msedge.admx"],
                 "en-US/extra9.adml": FILES["en-US/msedge.adml"]}
        admx.note_machine(db, "PC-7", admx.set_hash(files))
        check("when every set is still in use, a new one is refused",
              "all in use" in (refused(lambda: upload(db, "PC-7", admx.set_hash(files), files)) or ""))


def save(db, name, entries, registry=None):
    error, clean = gpo.validate({"name": name, "settings": {"admx": entries,
                                                            "registry": registry or []}})
    if error:
        return error, None
    return gpo.save_gpo(db, clean, actor="t")


def test_policies(db, digest):
    print("\n-- template settings in a policy --")
    restore = "Microsoft.Policies.Edge:RestoreOnStartup"
    error, enabled = save(db, "Startup on", [
        {"set": digest, "ref": restore, "state": "enabled",
         "options": {"RestoreOnStartup": 1, "URLs": ["https://a"], "Delay": "", "Home": "",
                     "Ask": True}}])
    check("a template setting saves", error is None)
    check("...stored with the values it compiled to",
          {v["name"] for v in enabled["settings"]["admx"][0]["values"]}
          == {"RestoreOnStartup", "RestoreOnStartupUserURLsEnabled", "1", "AskBeforeClose"})
    error, _ = save(db, "Twice", [{"set": digest, "ref": "Microsoft.Policies.Edge:HideFirstRunExperience",
                                   "state": "enabled"}],
                    registry=[{"path": "SOFTWARE\\Policies\\Microsoft\\Edge",
                               "name": "HideFirstRunExperience", "type": "dword", "value": 0}])
    check("one value set both raw and by a template in one policy is refused",
          error and "also sets" in error)
    error, _ = save(db, "Unknown", [{"set": "f" * 64, "ref": restore, "state": "enabled"}])
    check("a setting from a set the hub does not hold is refused", error is not None)

    error, disabled = save(db, "Startup off", [{"set": digest, "ref": restore, "state": "disabled"}])
    check("a Disabled setting saves", error is None)
    gpo.create_link(db, enabled["id"], gpo.NODE_ROOT, "")
    folder = pc_tree.create_folder(db, name="Cajas", actor="t")
    pc_tree.file_machines(db, ["CAJA-1"], folder["id"], actor="t")
    gpo.create_link(db, disabled["id"], gpo.NODE_FOLDER, folder["id"])
    gpo.invalidate()
    near_rows = gpo.rsop(db, "CAJA-1")["registry"]
    near = {v["name"] for v in near_rows if v["type"] != gpo.REG_DELETE}
    far = {v["name"] for v in gpo.rsop(db, "OFI-1")["registry"]}
    check("far from the folder, Enabled's values all apply",
          {"RestoreOnStartup", "1", "AskBeforeClose"} <= far)
    check("under it, the nearer Disabled replaces the setting whole -- no option value lingers",
          near == set())
    check("...and its deletes are rows of their own, winning by id over Enabled's values",
          {"RestoreOnStartup", "StartupDelay"}
          <= {v["name"] for v in near_rows if v["type"] == gpo.REG_DELETE})
    rsop = gpo.rsop(db, "CAJA-1")
    check("the resultant set names the template setting and its winner",
          rsop["admx"][0]["state"] == "disabled" and rsop["admx"][0]["gpo"] == "Startup off")
    document = gpo.resolve_for(db, "OFI-1")
    check("the agent's document carries plain values, nothing template-shaped",
          all(set(v) == {"id", "path", "name", "type", "value"} for v in document["values"]))

    old = gpo.resolve_for(db, "CAJA-1")
    lgpo = gpo.resolve_for(db, "CAJA-1", ["lgpo"])
    check("an agent that cannot write the local GPO is sent no deletes",
          all(v["type"] != gpo.REG_DELETE for v in old["values"]))
    check("...one that can is sent them, as plain rows, under a different version",
          any(v["type"] == gpo.REG_DELETE and v["name"] == "StartupDelay"
              and set(v) == {"id", "path", "name", "type", "value"} for v in lgpo["values"])
          and lgpo["version"] != old["version"])
    registry, templates = {}, {}
    gpo._merge_admx(registry, templates, [
        {"ref": "r", "name": "n", "state": "disabled", "options": {}, "values": [],
         "deletes": [{"path": "SOFTWARE\\Policies\\X", "name": "A"}]}], {"gpo_id": 1, "gpo": "p"})
    check("a delete stored by hub 1.172.0 as {path, name} still becomes a delete row",
          [(v["id"], v["type"]) for v in registry.values()]
          == [(gpo.setting_id("SOFTWARE\\Policies\\X", "A"), gpo.REG_DELETE)])


def test_user_configuration(db, digest):
    print("\n-- User Configuration (phase 3) --")
    edge = "SOFTWARE\\Policies\\Microsoft\\Edge"
    proxy = "Microsoft.Policies.Edge:ProxyPreference"
    restore = "Microsoft.Policies.Edge:RestoreOnStartup"
    check("a machine value's id has not moved, and the same path in HKCU is another value",
          gpo.setting_id(edge, "HomepageLocation") == "332aecccfafb5f3d"
          and gpo.setting_id(edge, "HomepageLocation", gpo.CLASS_USER) == "8271643f7b465887")

    error, user_only = save(db, "Proxy", [{"set": digest, "ref": proxy, "state": "enabled"}])
    rows = (user_only or {}).get("settings", {}).get("admx", [{}])[0].get("values", [])
    check("a User-only template saves, as User Configuration rows",
          error is None and rows and all(r.get("class") == gpo.CLASS_USER for r in rows)
          and rows[0]["id"] == gpo.setting_id(edge, "ProxyMode", gpo.CLASS_USER))
    error, _ = save(db, "Proxy as machine", [{"set": digest, "ref": proxy, "class": "machine",
                                              "state": "enabled"}])
    check("...and cannot be set in Computer Configuration", error and "Computer" in error)
    error, _ = save(db, "Hide as user", [{"set": digest, "ref": "Microsoft.Policies.Edge:HideFirstRunExperience",
                                          "class": "user", "state": "enabled"}])
    check("a Computer-only template cannot be set in User Configuration", error and "User" in error)

    both = {"RestoreOnStartup": 0}
    error, halves = save(db, "Both halves", [
        {"set": digest, "ref": restore, "class": "machine", "state": "enabled", "options": both},
        {"set": digest, "ref": restore, "class": "user", "state": "disabled"}])
    check("a template in both halves is two settings in one policy", error is None)
    error, _ = save(db, "Twice in User", [
        {"set": digest, "ref": restore, "class": "user", "state": "enabled", "options": both},
        {"set": digest, "ref": restore, "class": "user", "state": "disabled"}])
    check("...but one half twice is refused", error and "twice" in error)

    error, typed = save(db, "Typed", [], registry=[
        {"path": f"HKCU\\{edge}", "name": "X", "type": "dword", "value": 1},
        {"path": edge, "name": "X", "type": "dword", "value": 2}])
    stored = (typed or {}).get("settings", {}).get("registry", [])
    check("a typed HKCU path is a user row, and HKLM and HKCU at one path are two values",
          error is None and [r.get("class") for r in stored] == ["user", None])
    error, _ = save(db, "Contradiction", [], registry=[
        {"class": "user", "path": f"HKLM\\{edge}", "name": "X", "type": "dword", "value": 1}])
    check("a path naming HKLM on a User Configuration row is refused", error and "HKLM" in error)
    error, _ = save(db, "Other class", [], registry=[
        {"class": "group", "path": edge, "name": "X", "type": "dword", "value": 1}])
    check("a class that is neither half is refused", error is not None)

    # Machine Enabled from every PC's link; User Enabled too; then a folder that disables only
    # the User half of the same template for its PCs.
    error, user_on = save(db, "Restore for users", [
        {"set": digest, "ref": restore, "class": "user", "state": "enabled", "options": both}])
    error2, user_off = save(db, "No restore for users", [
        {"set": digest, "ref": restore, "class": "user", "state": "disabled"}])
    gpo.create_link(db, user_on["id"], gpo.NODE_ROOT, "")
    kiosk = pc_tree.create_folder(db, name="Kiosk", actor="t")
    pc_tree.file_machines(db, ["PC-1"], kiosk["id"], actor="t")
    gpo.create_link(db, user_off["id"], gpo.NODE_FOLDER, kiosk["id"])
    gpo.invalidate()
    result = gpo.rsop(db, "PC-1")
    machine_rows = [r for r in result["registry"] if gpo.row_class(r) == gpo.CLASS_MACHINE]
    user_rows = [r for r in result["registry"] if gpo.row_class(r) == gpo.CLASS_USER]
    check("a nearer User Disabled replaces the User half only; the Computer half keeps Enabled",
          error is None and error2 is None
          and any(r["name"] == "RestoreOnStartup" and r["type"] == "dword" for r in machine_rows)
          and user_rows and all(r["type"] == gpo.REG_DELETE for r in user_rows))
    check("...and the resultant set names both halves of the template",
          sorted(e["class"] for e in result["admx"] if e["ref"] == restore) == ["machine", "user"])

    old = gpo.resolve_for(db, "PC-1", ["lgpo"])
    new = gpo.resolve_for(db, "PC-1", ["lgpo", "user"])
    check("an agent before 3.49 is sent no User Configuration row",
          all("class" not in v for v in old["values"]))
    check("...one that applies it is sent them, marked, and the machine rows stay unmarked",
          any(v.get("class") == "user" for v in new["values"])
          and [v for v in new["values"] if "class" not in v] == old["values"]
          and new["version"] != old["version"])


def main():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    root = tempfile.mkdtemp(prefix="hub-admx-test-")
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE machine_info (machine TEXT PRIMARY KEY, ad_ou TEXT,"
                         " ad_dn TEXT, companion_version TEXT, os_caption TEXT, os_build TEXT)")
            conn.executemany("INSERT INTO machine_info (machine, os_caption, os_build)"
                             " VALUES (?, ?, ?)", [("CAJA-1", "Windows 11 Pro", "26200"),
                                                   ("OFI-1", None, None), ("PC-1", None, None),
                                                   ("PC-2", None, None)])
        fleet.init_fleet_db(db)
        permissions.init_permissions_db(db)
        pc_tree.init_pc_tree_db(db)
        rules.init_rules_db(db)
        scripts.init_scripts_db(db)
        settings.init_settings_db(db)
        gpo.init_gpo_db(db)
        admx.init_admx_db(db)
        admx.init_admx_db(db)        # idempotent
        admx.configure(root)
        gpo.invalidate()

        test_parse()
        test_compile()
        digest = test_upload(db)
        test_policies(db, digest)
        test_user_configuration(db, digest)
        test_cap(db, digest)
    finally:
        try:
            os.remove(db)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
