"""If / Else and Stop steps, payload download conditions, and the condition grammar behind
them (roadmap #5, hub 1.187.0 / agent 3.54.0).

The silent failure this file exists to catch is **an Else branch taken on a machine nobody
identified**. Every way the hub can cause it is checked here, because none of them fails a
deploy -- each installs the generic driver pack, reports green, and is found months later:

  * a condition the hub stores in a shape the agent reads differently (a literal left as
    "16" against a number, a version compared as text, an unknown key riding along);
  * a step numbered one way here and another on the agent, so an untaken branch shifts every
    later step onto the wrong row of the console;
  * a `{drivers}` bound in only one branch and used after the If -- a path called literally
    "{drivers}" on the machines that took the other one;
  * an agent too old to know what an If is being sent one anyway;
  * a preview that answers from a guess, or for a machine the operator cannot see.

The agent half of the same promise is agent/tests/.../PackageConditionTests.cs.
"""
import functools
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import fleet
import package_conditions as pc
import packages
import permissions
import rules
import settings
from flask import Flask
from packages_web import create_packages_blueprint
from permissions_web import create_access

PASS = 0
FAIL = 0
CURRENT_USER = "root@x.com"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def refused(fn, *args, contains="", **kwargs):
    """True when `fn` raises ValueError mentioning `contains`."""
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        if contains and contains not in str(e):
            print(f"       refused, but with: {e}")
            return False
        return True
    return False


def soon():
    return int(time.time()) + 1


def leaf(var, cmp, value=None):
    node = {"var": var, "cmp": cmp}
    if value is not None:
        node["value"] = value
    return node


def run(args, **extra):
    return {"kind": "run", "command": "cmd.exe", "args": args, **extra}


def model_if(then, otherwise=None, **extra):
    return {"kind": "if", "condition": leaf("hw.model", "contains", "Latitude"),
            "then_steps": then, "else_steps": otherwise or [], **extra}


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


# ============================== the condition grammar ==============================

def test_grammar():
    print("\n== conditions: validation ==")
    ctx = lambda **kw: pc.Context(**kw)   # noqa: E731

    node = pc.validate(leaf("hw.memory_gb", ">=", "16"), ctx())
    check("a number literal is coerced once, at save time", node["value"] == 16)
    node = pc.validate(leaf("os.version", ">=", "10.0.22631"), ctx())
    check("a version literal is kept as text", node["value"] == "10.0.22631")
    check("a version that is not dotted numbers is refused",
          refused(pc.validate, leaf("os.version", ">=", "Windows 11"), ctx(),
                  contains="not a version"))
    check("an ordered operator on text is refused",
          refused(pc.validate, leaf("hw.model", ">", "a"), ctx(), contains="cannot be used"))
    check("an unknown variable is refused",
          refused(pc.validate, leaf("hw.colour", "==", "red"), ctx(), contains="unknown variable"))
    check("a chassis is one of its five words",
          refused(pc.validate, leaf("hw.chassis", "==", "notebook"), ctx(), contains="one of"))
    node = pc.validate(leaf("hw.model", "in", "Latitude 5440, Latitude 7440"), ctx())
    check("'a, b' typed into the value box is a list",
          node["value"] == ["Latitude 5440", "Latitude 7440"])
    node = pc.validate({**leaf("hw.model", "==", "x"), "extra": "dropped"}, ctx())
    check("unknown keys are dropped, not carried to the agent", "extra" not in node)
    check("an empty group is refused",
          refused(pc.validate, {"op": "and", "nodes": []}, ctx(), contains="at least one"))
    check("'not' takes exactly one clause",
          refused(pc.validate, {"op": "not", "nodes": [leaf("hw.model", "is_known")] * 2}, ctx()))

    probe = pc.validate({"probe": "registry_value", "root": "hklm", "key": r"SOFTWARE\X",
                         "name": "V"}, ctx())
    check("a registry check is shaped exactly like a detection rule",
          probe == {"probe": "registry_value", "root": "HKLM", "key": r"SOFTWARE\X", "name": "V"})
    check("a PowerShell check's timeout is bounded",
          refused(pc.validate, {"probe": "powershell", "script": "exit 0",
                                "timeout_seconds": 9999}, ctx()))
    check("a check may not open a variable nothing provides",
          refused(pc.validate, {"probe": "file_exists", "path": r"{drivers}\x.inf"},
                  ctx(bound={"work"}), contains="{drivers}"))
    check("a download condition cannot run machine checks",
          refused(pc.validate, {"probe": "file_exists", "path": "C:\\x"},
                  ctx(allow_probes=False), contains="If step"))
    check("a download condition cannot read a step result",
          refused(pc.validate, leaf("step.last_exit_code", "==", 0),
                  ctx(allow_runtime=False), contains="no step has run"))

    print("\n== conditions: the hub's evaluation (preview) ==")
    facts = {"hw.model": "Latitude 5440", "hw.manufacturer": "Dell Inc.",
             "os.version": "10.0.22631", "os.build": 22631}
    ev = lambda node: pc.evaluate(pc.validate(node, ctx()), facts)   # noqa: E731
    check("text equality ignores case, as the agent's does",
          ev(leaf("hw.manufacturer", "==", "DELL INC.")) is True)
    check("versions compare as versions: 10.0.22631 >= 10.0.9200",
          ev(leaf("os.version", ">=", "10.0.9200")) is True)
    check("...where text would have said the opposite", ("10.0.22631" >= "10.0.9200") is False)
    check("an absent value is UNKNOWN, not false",
          ev(leaf("hw.model_name", "==", "x")) is pc.UNKNOWN)
    check("...so != on it is UNKNOWN too, not true",
          ev(leaf("hw.model_name", "!=", "x")) is pc.UNKNOWN)
    check("a definite false settles an and next to an unknown",
          ev({"op": "and", "nodes": [leaf("hw.model_name", "==", "x"),
                                     leaf("hw.model", "==", "Precision")]}) is False)
    check("a machine check is UNKNOWN to the hub",
          ev({"probe": "file_exists", "path": "C:\\x"}) is pc.UNKNOWN)
    check("compare_versions refuses a BIOS string rather than reading it as 0",
          pc.compare_versions("N3KET60W (1.40)", "1.0") is None)


# ============================== steps ==============================

def test_steps():
    print("\n== steps: If / Stop validation ==")
    plan = packages.validate_steps([
        model_if([run("/c echo a")], [run("/c echo b")]),
        run("/c echo after"),
    ])
    check("an If keeps its two lanes", len(plan[0]["then_steps"]) == 1 and len(plan[0]["else_steps"]) == 1)
    check("on_unknown defaults to fail and is not stored", "on_unknown" not in plan[0])
    numbering = [(o, s["kind"], d, p, b) for o, s, d, p, b in packages.iter_steps(plan)]
    check("numbering is depth-first: If, Then, Else, after",
          numbering == [(1, "if", 0, None, None), (2, "run", 1, 1, "then"),
                        (3, "run", 1, 1, "else"), (4, "run", 0, None, None)])

    check("an If needs a condition",
          refused(packages.validate_steps, [{"kind": "if", "then_steps": [run("x")]}],
                  contains="needs a condition"))
    check("an If needs at least one step",
          refused(packages.validate_steps, [model_if([])], contains="at least one step"))
    check("an error names the depth-first step number",
          refused(packages.validate_steps,
                  [model_if([run("/c a")], [{"kind": "run"}])], contains="step 3"))
    check("a step after a Stop can never run, and is refused",
          refused(packages.validate_steps,
                  [{"kind": "stop", "outcome": "success"}, run("/c x")],
                  contains="can never run"))
    check("a Stop is success or failure",
          refused(packages.validate_steps, [{"kind": "stop", "outcome": "maybe"}]))
    nested = [run("x")]
    for _ in range(packages.MAX_STEP_DEPTH):
        nested = [model_if(nested)]
    check("Ifs nest MAX_STEP_DEPTH deep", len(packages.validate_steps(nested)) == 1)
    check("...and no deeper",
          refused(packages.validate_steps, [model_if(nested)], contains="nested at most"))
    too_many = [model_if([run(str(i)) for i in range(packages.MAX_STEPS)])]
    check("MAX_STEPS counts the steps inside Ifs",
          refused(packages.validate_steps, too_many, contains="counting the ones inside"))

    print("\n== steps: variables across branches ==")
    extract = {"kind": "extract", "archive": "{pack}", "save_as": "drivers"}
    use = {"kind": "pnputil", "path": "{drivers}"}
    check("a name bound in only one branch is not bound after the If",
          refused(packages.validate_steps, [model_if([extract]), use], ["pack"],
                  contains="{drivers}"))
    plan = packages.validate_steps([model_if([extract], [dict(extract)]), use], ["pack"])
    check("...but bound in both branches it is", plan[1]["kind"] == "pnputil")
    plan = packages.validate_steps(
        [model_if([extract], [{"kind": "stop", "outcome": "failure"}]), use], ["pack"])
    check("a branch that Stops does not count against the other", plan[1]["path"] == "{drivers}")
    check("continue_on_error on the If keeps only what was bound before it",
          refused(packages.validate_steps,
                  [model_if([extract], [dict(extract)], continue_on_error=True), use], ["pack"],
                  contains="{drivers}"))
    plan = packages.validate_steps([run(r"/c copy {work}\{model}.cfg C:\x")])
    check("{model} is a machine variable a step may use", plan[0]["args"].endswith("C:\\x"))
    check("a payload may not take a machine variable's name",
          refused(packages.validate_source, {"name": "model", "kind": "url",
                                             "ref": "https://x/y.zip"}, contains="reserved"))
    check("nor may an extract step's save_as",
          refused(packages.validate_steps,
                  [{"kind": "extract", "archive": "{pack}", "save_as": "arch"}], ["pack"],
                  contains="already taken"))


def test_packages(db_path):
    print("\n== packages: payload download conditions ==")
    sources = [
        {"name": "dell", "kind": "url", "ref": "https://x/dell.zip",
         "only_if": leaf("hw.manufacturer", "starts_with", "Dell")},
        {"name": "lenovo", "kind": "url", "ref": "https://x/lenovo.zip",
         "only_if": json.dumps(leaf("hw.manufacturer", "starts_with", "Lenovo"))},
    ]
    steps = [{"kind": "if", "condition": leaf("hw.manufacturer", "starts_with", "Dell"),
              "then_steps": [{"kind": "extract", "archive": "{dell}", "save_as": "drivers"}],
              "else_steps": [{"kind": "extract", "archive": "{lenovo}", "save_as": "drivers"}]},
             {"kind": "pnputil", "path": "{drivers}"}]
    pkg_id = packages.create_package(db_path, name="Drivers", sources=sources, steps=steps,
                                     actor="op@x.com")
    pkg = packages.get_package(db_path, pkg_id)
    check("a download condition is stored and read back",
          pkg["sources"][1]["only_if"]["value"] == "Lenovo")
    check("a payload only an If's lane opens counts as used", len(pkg["sources"]) == 2)
    check("a winget reference cannot have a download condition",
          refused(packages.validate_source,
                  {"name": "w", "kind": "winget", "ref": "X.Y", "only_if": leaf("hw.model", "is_known")},
                  contains="winget"))
    check("a single-command package cannot have one",
          refused(packages.create_package, db_path, name="One",
                  source={"kind": "url", "ref": "https://x/a.msi",
                          "only_if": leaf("hw.model", "is_known")},
                  install_command="msiexec", install_args="/i {file}", contains="needs steps"))
    check("an Android package cannot have one either",
          refused(packages.create_package, db_path, name="Phone",
                  sources=[{"name": "app", "kind": "upload", "sha256": "c" * 64,
                            "only_if": leaf("hw.model", "is_known")}],
                  steps=[{"kind": "apk", "apk": "{app}"}], contains="Android"))
    check("an apk hidden inside an If still makes the package Android -- and is refused",
          refused(packages.create_package, db_path, name="Phone2",
                  sources=[{"name": "app", "kind": "upload", "sha256": "c" * 64}],
                  steps=[model_if([{"kind": "apk", "apk": "{app}"}])], contains="Android"))

    print("\n== packages: custom fields ==")
    err, _ = rules.save_field(db_path, "site", "Site", "text", actor="root")
    check("a custom field exists for the test", err is None)
    pkg2 = packages.create_package(
        db_path, name="Site tools", steps=[{"kind": "if",
                                            "condition": leaf("field.site", "==", "Hospital"),
                                            "then_steps": [run("/c echo h")]}], actor="op@x.com")
    check("a condition may read a custom field the hub knows", pkg2 is not None)
    check("...and an unknown one is refused by name",
          refused(packages.create_package, db_path, name="Bad field",
                  steps=[{"kind": "if", "condition": leaf("field.nope", "==", "x"),
                          "then_steps": [run("/c x")]}], contains="no custom field called 'nope'"))
    check("the assistant's pre-check, with no database, lets a field clause through",
          packages.check_definition(
              name="Pre", steps=[{"kind": "if", "condition": leaf("field.nope", "==", "x"),
                                  "then_steps": [run("/c x")]}])[2][0]["kind"] == "if")

    print("\n== packages: needs agent 3.54.0 ==")
    plain = packages.get_package(db_path, packages.create_package(
        db_path, name="Plain", steps=[run("/c echo")], actor="op@x.com"))
    check("a plain step list needs no new agent", not packages.needs_conditions_agent(plain))
    check("an If does", packages.needs_conditions_agent(pkg))
    with_fact = packages.get_package(db_path, packages.create_package(
        db_path, name="Fact", steps=[run("/c echo {model}")], actor="op@x.com"))
    check("so does a {model} in a step", packages.needs_conditions_agent(with_fact))
    check("hub variables are only the AD / custom-field ones",
          packages.hub_condition_variables(packages.get_package(db_path, pkg2)) == {"field.site"}
          and packages.hub_condition_variables(pkg) == set())
    return pkg_id, pkg2


def set_machine(db_path, machine, version, model="Latitude 5440"):
    with packages.get_conn(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS machine_info (machine TEXT PRIMARY KEY, "
                     "model TEXT, manufacturer TEXT, os_caption TEXT, os_version TEXT, "
                     "os_build TEXT, os_arch TEXT, companion_version TEXT, updated_at TEXT)")
        conn.execute("INSERT OR REPLACE INTO machine_info VALUES (?, ?, 'Dell Inc.', "
                     "'Microsoft Windows 11 Pro', '10.0.22631', '22631', '64-bit', ?, NULL)",
                     (machine, model, version))


def test_dispatch(db_path, pkg_id, site_pkg):
    print("\n== dispatch: the agent-version gate ==")
    set_machine(db_path, "OLD-1", "3.53.0")
    set_machine(db_path, "NEW-1", "3.54.0")
    dep = packages.create_deployment(db_path, package_id=pkg_id, machines=["OLD-1", "NEW-1"],
                                     created_by="op@x.com")
    packages.tick(db_path, now=soon())
    targets = {t["machine"]: t for t in packages.get_deployment(db_path, dep)["targets"]}
    check("an agent older than 3.54.0 is refused, not sent the job",
          targets["OLD-1"]["status"] == "failed" and targets["OLD-1"]["command_id"] is None)
    check("...and the reason names both versions",
          "3.54.0" in targets["OLD-1"]["last_error"] and "3.53.0" in targets["OLD-1"]["last_error"])
    check("a current agent is dispatched", targets["NEW-1"]["status"] == "in_flight")
    params = fleet.get_command(db_path, targets["NEW-1"]["command_id"])["params"]
    check("the download condition rides on the wire payload",
          params["sources"][0]["only_if"]["var"] == "hw.manufacturer")
    check("no facts are sent when the package reads none", "facts" not in params)

    print("\n== dispatch: hub facts ==")
    set_machine(db_path, "SITE-1", "3.54.0")
    rules.set_machine_field(db_path, "SITE-1", "site", "Hospital", actor="root")
    dep2 = packages.create_deployment(db_path, package_id=site_pkg, machines=["SITE-1"],
                                      created_by="op@x.com")
    packages.tick(db_path, now=soon())
    params = fleet.get_command(
        db_path, packages.get_deployment(db_path, dep2)["targets"][0]["command_id"])["params"]
    check("a custom field the package reads is resolved and sent",
          params.get("facts") == {"field.site": "Hospital"})
    check("...and nothing the agent reads itself rides along",
          not any(k.startswith("hw.") for k in params.get("facts", {})))

    print("\n== progress: flat ordinals and not_run ==")
    command_id = next(t for t in packages.get_deployment(db_path, dep)["targets"]
                      if t["machine"] == "NEW-1")["command_id"]
    agent = fleet.enroll_agent(db_path, "NEW-1", "s", "s")[0]
    fleet.claim_commands(db_path, agent, "NEW-1")
    events = [("download", 1, "running"), ("download", 1, "ok"), ("download", 2, "not_run"),
              ("step", 1, "running"), ("step", 1, "ok"), ("step", 2, "running"),
              ("step", 2, "ok"), ("step", 3, "not_run")]
    for phase, ordinal, state in events:
        packages.record_step_progress(db_path, command_id, agent, phase=phase,
                                      ordinal=ordinal, state=state)
    target = next(t for t in packages.get_deployment(db_path, dep)["targets"]
                  if t["machine"] == "NEW-1")
    steps = [(s["phase"], s["ordinal"], s.get("depth"), s.get("branch"), s["state"])
             for s in target["steps"]]
    check("the skeleton flattens the tree with depth and lane",
          steps[2:6] == [("step", 1, 0, None, "ok"), ("step", 2, 1, "then", "ok"),
                         ("step", 3, 1, "else", "not_run"), ("step", 4, 0, None, None)])
    check("a not_run phase is accepted and stored", steps[1][4] == "not_run")
    check("the untaken branch is not part of 'step x of y'", target["step_total"] == 3)
    check("...nor where the machine is", target["current_step"]["ordinal"] == 2)
    packages.record_step_progress(db_path, command_id, agent, phase="step", ordinal=4,
                                  state="running")
    target = next(t for t in packages.get_deployment(db_path, dep)["targets"]
                  if t["machine"] == "NEW-1")
    check("the step after a skipped Else is 'step 3 of 3', not 'step 4 of 3'",
          target["current_step"]["ordinal"] == 4 and target["current_step"]["position"] == 3
          and target["step_total"] == 3)


def test_web(workdir):
    global CURRENT_USER
    print("\n== HTTP: vocabulary and preview ==")
    log_dir = os.path.join(workdir, "weblogs")
    os.makedirs(log_dir)
    db_path = os.path.join(log_dir, "hub.db")
    fleet.init_fleet_db(db_path)
    settings.init_settings_db(db_path)
    settings.invalidate()
    packages.init_packages_db(db_path)
    rules.init_rules_db(db_path)
    permissions.init_permissions_db(db_path)
    permissions.invalidate()
    permissions.create_group(
        db_path, name="Hospital IT", capabilities=[permissions.VIEW, permissions.DEPLOY_PACKAGES],
        machines=["HOSPITAL-1"], members=["hospital@x.com"], actor="root@x.com")
    set_machine(db_path, "HOSPITAL-1", "3.54.0")
    set_machine(db_path, "HR-1", "3.54.0", model="Precision 3581")

    access = create_access(db_path, {"root@x.com"})
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(create_packages_blueprint(db_path, log_dir, fake_login_required,
                                                     access, hub_url="https://hub.example.com"))

    @app.before_request
    def _seed_session():
        from flask import session
        session["user"] = {"email": CURRENT_USER}

    c = app.test_client()
    vocab = c.get("/api/packages").get_json()["conditions"]
    names = {v["name"] for v in vocab["variables"]}
    check("the editor is served every variable", {"hw.model", "hw.model_name", "os.version"} <= names)
    check("...with a label, never the key",
          all(not v["label"].startswith("packages.") for v in vocab["variables"]))
    check("...and the operators its kind allows",
          next(v for v in vocab["variables"] if v["name"] == "os.version")["operators"][0] == ">")
    check("the minimum agent is served for the editor's note", vocab["min_agent"] == "3.54.0")

    r = c.post("/api/packages/conditions/preview", json={
        "machine": "HOSPITAL-1",
        "condition": {"op": "and", "nodes": [leaf("hw.model", "contains", "latitude"),
                                             leaf("os.version", ">=", "10.0.19045")]}})
    body = r.get_json()
    check("a preview answers from the hub's facts", r.status_code == 200 and body["result"] == "true")
    check("...and shows each clause's actual value",
          body["explain"]["nodes"][0]["actual"] == "Latitude 5440")
    r = c.post("/api/packages/conditions/preview", json={
        "machine": "HOSPITAL-1", "condition": {"probe": "file_exists", "path": "C:\\x"}})
    check("a machine check is 'unknown, decided on the machine', never a guess",
          r.get_json()["result"] == "unknown" and r.get_json()["explain"]["on_machine"] is True)
    r = c.post("/api/packages/conditions/preview",
               json={"machine": "HOSPITAL-1", "condition": leaf("hw.model", ">", "x")})
    check("an invalid condition is 400 with the reason",
          r.status_code == 400 and "cannot be used" in r.get_json()["error"])

    CURRENT_USER = "hospital@x.com"
    r = c.post("/api/packages/conditions/preview",
               json={"machine": "HR-1", "condition": leaf("hw.model", "is_known")})
    check("a scoped operator cannot preview a machine outside their scope", r.status_code == 403)
    check("...and is told nothing about it", "Precision" not in r.get_data(as_text=True))
    r = c.post("/api/packages/conditions/preview",
               json={"machine": "HOSPITAL-1", "condition": leaf("hw.model", "is_known")})
    check("...but can preview their own", r.status_code == 200)
    CURRENT_USER = "viewer@x.com"
    r = c.post("/api/packages/conditions/preview",
               json={"machine": "HOSPITAL-1", "condition": leaf("hw.model", "is_known")})
    check("an operator without deploy_packages is refused", r.status_code == 403)
    CURRENT_USER = "root@x.com"


def main():
    workdir = tempfile.mkdtemp(prefix="package-conditions-tests-")
    db_path = os.path.join(workdir, "packages.db")
    try:
        fleet.init_fleet_db(db_path)
        packages.init_packages_db(db_path)
        packages.init_packages_db(db_path)   # the only_if_json migration must be idempotent
        rules.init_rules_db(db_path)
        test_grammar()
        test_steps()
        pkg_id, site_pkg = test_packages(db_path)
        test_dispatch(db_path, pkg_id, site_pkg)
        test_web(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
