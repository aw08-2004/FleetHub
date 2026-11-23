"""Live deployment progress -- the PDQ-style step view, Abort, Retry now and Redeploy
(roadmap #5, hub 1.181.0 / agent 3.53.0).

The silent failure this file exists to catch is a progress view that LIES while looking
fine:

  * a step painted on the wrong row, because the hub's skeleton counts payloads or steps
    differently from the agent that reports against it;
  * a finished step flipping back to "running" when a retried or keepalive POST lands late,
    or a keepalive waking every open console every few seconds;
  * one agent writing another's progress, or progress reopening a finished command;
  * a row reading "cancelled" over an installer that is still running -- or an abort that
    is quietly retried, or offered for an agent that will never hear it;
  * a step's log that holds the step before's tail, or is empty because it was pruned and
    nobody said so;
  * a scoped operator redeploying, aborting or reading machines outside their scope.

Model and HTTP layer in one module because they are one feature, the same way the agent's
StepProgressTests covers executor, reporter and stream together. Times are driven with an
explicit `now` wherever the model takes one.
"""
import functools
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import changes
import fleet
import packages
import permissions
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


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def soon():
    """A `now` not behind the real clock -- see test_packages.soon()."""
    return int(time.time()) + 1


def stepped_package(db_path, name="Driver pack"):
    """One file payload and three steps."""
    return packages.create_package(
        db_path, name=name,
        sources=[{"name": "drivers", "kind": packages.SOURCE_UPLOAD, "sha256": "b" * 64,
                  "file_name": "drivers.zip"}],
        steps=[{"kind": "extract", "archive": "{drivers}", "name": "Unpack"},
               {"kind": "pnputil", "path": "{extracted}", "name": "Install drivers"},
               {"kind": "winget", "id": "Vendor.Tool", "name": "Install the tool"}],
        install_command="", actor="op@x.com")


def legacy_package(db_path, name="7-Zip"):
    return packages.create_package(
        db_path, name=name,
        source={"kind": packages.SOURCE_UPLOAD, "sha256": "a" * 64, "file_name": "7z.msi"},
        install_command=r"C:\Windows\System32\msiexec.exe",
        install_args='/i "{file}" /qn LICENSEKEY=SECRET-123', actor="op@x.com")


def set_version(db_path, machine, version):
    with packages.get_conn(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS machine_info "
                     "(machine TEXT PRIMARY KEY, companion_version TEXT)")
        conn.execute("INSERT OR REPLACE INTO machine_info(machine, companion_version) "
                     "VALUES (?, ?)", (machine, version))


def target(db_path, deployment_id, machine):
    return next(t for t in packages.get_deployment(db_path, deployment_id)["targets"]
                if t["machine"] == machine)


def dispatch(db_path, deployment_id, machines, now):
    """Tick once and claim each machine's command; returns {machine: command_id}."""
    packages.tick(db_path, now=now)
    ids = {}
    for machine in machines:
        fleet.claim_commands(db_path, f"agent-{machine}", machine)
        ids[machine] = target(db_path, deployment_id, machine)["command_id"]
    return ids


def progress(db_path, command_id, machine, phase, ordinal, state, **kw):
    return packages.record_step_progress(
        db_path, command_id, f"agent-{machine}", phase=phase, ordinal=ordinal,
        state=state, **kw)


def finish(db_path, command_id, machine, success, output="done"):
    fleet.complete_command(db_path, command_id, f"agent-{machine}", success, output)


def model_tests(db_path):
    print("\n== The skeleton counts exactly as the agent does ==")
    stepped = packages.get_package(db_path, stepped_package(db_path))
    skeleton = packages.progress_skeleton(packages.build_command_params(stepped, {"id": "d"}))
    check("a stepped package is its downloads, its steps, then detection",
          [(e["phase"], e["ordinal"]) for e in skeleton] ==
          [("download", 1), ("step", 1), ("step", 2), ("step", 3), ("detect", 1)])
    mixed = packages.progress_skeleton({
        "sources": [{"name": "app", "kind": packages.SOURCE_WINGET, "id": "Vendor.App"},
                    {"name": "cfg", "kind": packages.SOURCE_URL, "ref": "https://x/c.xml"},
                    {"name": "drv", "kind": packages.SOURCE_UNC, "ref": r"\\s\d.zip"}],
        "steps": [{"kind": "run", "name": "go"}]})
    check("downloads are the FILE payloads only, numbered among themselves -- winget "
          "fetches its own, and the agent counts the same way",
          [(e["phase"], e["ordinal"], e["label"]) for e in mixed if e["phase"] == "download"]
          == [("download", 1, "https://x/c.xml"), ("download", 2, r"\\s\d.zip")])
    check("a legacy `multi` source is not a download",
          [e["phase"] for e in packages.progress_skeleton(
              {"source": {"kind": "multi"}, "steps": [{"kind": "run"}]})] == ["step", "detect"])
    check("a step is labelled with its name and kind",
          skeleton[2]["label"] == "Install drivers" and skeleton[2]["kind"] == "pnputil")
    check("a download is labelled with its file", skeleton[0]["label"] == "drivers.zip")

    legacy = packages.get_package(db_path, legacy_package(db_path))
    skeleton = packages.progress_skeleton(packages.build_command_params(legacy, {"id": "d"}))
    check("a pre-steps package is download, step 1 of 1, detection",
          [(e["phase"], e["ordinal"]) for e in skeleton] ==
          [("download", 1), ("step", 1), ("detect", 1)])
    check("its one step is labelled with the program alone",
          skeleton[1]["label"] == "msiexec.exe")
    check("...never with its arguments, which can carry a licence key",
          not any("SECRET" in str(e["label"]) for e in skeleton))
    check("garbage params give an empty-handed skeleton, not an exception",
          [e["phase"] for e in packages.progress_skeleton("nonsense")] == ["step", "detect"])

    print("\n== Stages ==")
    pkg = stepped["id"]
    t0 = soon()
    dep = packages.create_deployment(db_path, package_id=pkg, machines=["PC-1", "PC-2"],
                                     created_by="op@x.com", max_attempts=3,
                                     retry_backoff_seconds=600)
    check("a fresh target is queued", target(db_path, dep, "PC-1")["stage"] == "queued")
    check("...with no steps until it has a command", target(db_path, dep, "PC-1")["steps"] == [])
    later = packages.create_deployment(db_path, package_id=pkg, machines=["PC-9"],
                                       created_by="op@x.com", window_start=t0 + 3600)
    check("a target whose window has not opened is scheduled",
          target(db_path, later, "PC-9")["stage"] == "scheduled")

    packages.tick(db_path, now=t0)
    check("sent but not picked up is waiting",
          target(db_path, dep, "PC-1")["stage"] == "waiting")
    fleet.claim_commands(db_path, "agent-PC-1", "PC-1")
    fleet.claim_commands(db_path, "agent-PC-2", "PC-2")
    row = target(db_path, dep, "PC-1")
    check("picked up is running", row["stage"] == "running")
    check("the step list is drawn before the agent has said anything",
          len(row["steps"]) == 5 and all(s["state"] is None for s in row["steps"]))
    check("'x of y' counts steps, not downloads or detection", row["step_total"] == 3)
    check("an agent with no reported version is not offered step progress",
          row["agent_reports_steps"] is False)
    set_version(db_path, "PC-1", "3.53.0")
    set_version(db_path, "PC-2", "3.52.9")
    check("3.53.0 reports steps", target(db_path, dep, "PC-1")["agent_reports_steps"] is True)
    check("3.52.9 does not", target(db_path, dep, "PC-2")["agent_reports_steps"] is False)
    check("started_at is when the machine picked it up",
          target(db_path, dep, "PC-1")["started_at"] is not None)

    print("\n== Recording progress ==")
    cmd1 = target(db_path, dep, "PC-1")["command_id"]
    cmd2 = target(db_path, dep, "PC-2")["command_id"]
    check("another agent cannot write this one's progress",
          raises(PermissionError, packages.record_step_progress, db_path, cmd1,
                 "agent-PC-2", phase="step", ordinal=1, state="running"))
    check("an unknown command is a KeyError",
          raises(KeyError, progress, db_path, "nope", "PC-1", "step", 1, "running"))
    for bad in [dict(phase="install", ordinal=1, state="running"),
                dict(phase="step", ordinal=0, state="running"),
                dict(phase="step", ordinal=packages.MAX_STEPS + 1, state="running"),
                dict(phase="step", ordinal="two", state="running"),
                dict(phase="step", ordinal=1, state="done"),
                dict(phase="step", ordinal=1, state="ok", exit_code="zero"),
                dict(phase="step", ordinal=1, state="ok", exit_code=True)]:
        check(f"refused: {bad}", raises(ValueError, packages.record_step_progress, db_path,
                                        cmd1, "agent-PC-1", **bad))
    other = fleet.create_command(db_path, machine="PC-1", command_type="restart",
                                 params={}, issued_by="op@x.com")
    fleet.claim_commands(db_path, "agent-PC-1", "PC-1")
    check("progress is only taken for a deployment's command",
          raises(PermissionError, progress, db_path, other, "PC-1", "step", 1, "running"))

    first = progress(db_path, cmd1, "PC-1", "download", 1, "running", out_seq=0)
    check("the first event is a change", first["changed"] is True and first["cancel"] is False)
    check("it names the machine, for the change notice", first["machine"] == "PC-1")
    repeat = progress(db_path, cmd1, "PC-1", "download", 1, "running", out_seq=0)
    check("a repeated running event -- the keepalive -- is NOT a change",
          repeat["changed"] is False)
    check("download done",
          progress(db_path, cmd1, "PC-1", "download", 1, "ok", out_seq=2)["changed"])
    progress(db_path, cmd1, "PC-1", "step", 1, "running", out_seq=2)
    late = progress(db_path, cmd1, "PC-1", "download", 1, "running", out_seq=0)
    check("a late running for a finished phase changes nothing", late["changed"] is False)
    row = target(db_path, dep, "PC-1")
    check("...and the phase stays finished", row["steps"][0]["state"] == "ok")
    check("the current step is the furthest one reached",
          row["current_step"]["phase"] == "step" and row["current_step"]["ordinal"] == 1
          and row["current_step"]["state"] == "running")
    check("a finished phase has its timings",
          row["steps"][0]["started_at"] and row["steps"][0]["finished_at"])
    check("a long message is clipped",
          progress(db_path, cmd1, "PC-1", "step", 1, "ok", exit_code=0,
                   message="x" * 5000)["changed"]
          and len(target(db_path, dep, "PC-1")["steps"][1]["message"]) ==
          packages.MAX_PROGRESS_MESSAGE_CHARS)
    check("a terminal event can arrive without its running (a dropped POST)",
          progress(db_path, cmd1, "PC-1", "step", 2, "failed", exit_code=-2147024894,
                   message="exit code -2147024894 is not in [0, 259, 3010]")["changed"])
    row = target(db_path, dep, "PC-1")
    check("the failed step is the More Info",
          row["failed_step"] == {"phase": "step", "ordinal": 2, "label": "Install drivers",
                                 "kind": "pnputil", "state": "failed",
                                 "exit_code": -2147024894,
                                 "message": "exit code -2147024894 is not in [0, 259, 3010]"})
    check("a negative Windows exit code survives the round trip",
          row["steps"][2]["exit_code"] == -2147024894)

    print("\n== One step's log ==")
    for seq, text in enumerate(["dl-a\n", "dl-b\n", "unpack-a\n", "unpack-b\n", "pnp\n"]):
        fleet.append_command_output(db_path, cmd1, "agent-PC-1", seq, text)
    # download was 0..2, step 1 from 2 and ended at 4 (re-record its end with the seq).
    with packages.get_conn(db_path) as conn:
        conn.execute("UPDATE deployment_target_steps SET out_to_seq = 4 "
                     "WHERE command_id = ? AND phase = 'step' AND ordinal = 1", (cmd1,))
    out = packages.step_output(db_path, dep, "PC-1", "download", 1)
    check("a phase's log is exactly its own chunks",
          "".join(c["text"] for c in out["chunks"]) == "dl-a\ndl-b\n" and out["done"])
    out = packages.step_output(db_path, dep, "PC-1", "step", 1)
    check("the next phase starts where the last ended",
          "".join(c["text"] for c in out["chunks"]) == "unpack-a\nunpack-b\n")
    out = packages.step_output(db_path, dep, "PC-1", "step", 1, after_seq=2)
    check("the after_seq cursor works inside a phase",
          [c["seq"] for c in out["chunks"]] == [3] and out["next_seq"] == 4)
    progress(db_path, cmd1, "PC-1", "step", 3, "running", out_seq=4)
    out = packages.step_output(db_path, dep, "PC-1", "step", 3)
    check("a running phase's log is open-ended -- readable mid-run",
          [c["seq"] for c in out["chunks"]] == [4] and out["done"] is False)
    out = packages.step_output(db_path, dep, "PC-1", "step", 2)
    check("a phase reported with no chunk number has no log, rather than everyone's",
          out["chunks"] == [] and out["expired"] is False)
    check("a bad phase is refused",
          raises(ValueError, packages.step_output, db_path, dep, "PC-1", "install", 1))
    check("an unknown target is a KeyError",
          raises(KeyError, packages.step_output, db_path, dep, "PC-404", "step", 1))

    progress(db_path, cmd1, "PC-1", "step", 3, "ok", exit_code=0, out_seq=5)
    finish(db_path, cmd1, "PC-1", True)
    check("progress for a finished command is refused, not reopened",
          raises(PermissionError, progress, db_path, cmd1, "PC-1", "detect", 1, "running"))
    check("before the scheduler records it, the target is finishing",
          target(db_path, dep, "PC-1")["stage"] == "finishing")
    check("is_deploy_command knows a target's attempt",
          packages.is_deploy_command(db_path, cmd1)
          and not packages.is_deploy_command(db_path, other))
    fleet.prune_command_output(db_path, older_than=int(time.time()) + 10)
    out = packages.step_output(db_path, dep, "PC-1", "download", 1)
    check("a pruned log says it expired, not that there was nothing",
          out["expired"] is True and out["chunks"] == [])

    print("\n== Every attempt keeps its own steps ==")
    progress(db_path, cmd2, "PC-2", "download", 1, "failed", message="sha256 mismatch")
    finish(db_path, cmd2, "PC-2", False, "sha256 mismatch")
    packages.reconcile_once(db_path, now=t0 + 1)
    row = target(db_path, dep, "PC-2")
    check("a failed attempt with retries left waits out its backoff",
          row["status"] == "pending" and row["stage"] == "retry_wait")
    moved = packages.retry_now(db_path, dep, ["PC-2"], actor="op@x.com", now=t0 + 2)
    check("Retry now makes it due", moved == 1
          and target(db_path, dep, "PC-2")["next_attempt_at"] == t0 + 2)
    check("...and it is audited",
          "retry_deployment_now" in [r["action"] for r in audit_rows(db_path)])
    check("Retry now never pulls a WINDOW forward",
          packages.retry_now(db_path, later, ["PC-9"], now=t0 + 2) == 0
          and target(db_path, later, "PC-9")["stage"] == "scheduled")
    check("Retry now on an unknown deployment is a KeyError",
          raises(KeyError, packages.retry_now, db_path, "nope", ["PC-2"]))
    packages.dispatch_once(db_path, now=t0 + 3)
    fleet.claim_commands(db_path, "agent-PC-2", "PC-2")
    cmd2b = target(db_path, dep, "PC-2")["command_id"]
    check("the retry is a new command", cmd2b and cmd2b != cmd2)
    row = target(db_path, dep, "PC-2")
    check("...whose step list starts clean",
          all(s["state"] is None for s in row["steps"]) and row["failed_step"] is None)
    with packages.get_conn(db_path) as conn:
        kept = conn.execute("SELECT state FROM deployment_target_steps WHERE command_id = ?",
                            (cmd2,)).fetchone()
    check("...while the first attempt's failure is still on record",
          kept is not None and kept["state"] == "failed")
    finish(db_path, cmd2b, "PC-2", True)
    packages.reconcile_once(db_path, now=t0 + 4)

    print("\n== Abort ==")
    t1 = soon()
    dep = packages.create_deployment(
        db_path, package_id=pkg, machines=["A-PENDING", "A-WAIT", "A-NEW", "A-OLD",
                                           "A-LATE", "A-DONE"],
        created_by="op@x.com", max_attempts=3, retry_backoff_seconds=600)
    for m in ("A-NEW", "A-LATE"):
        set_version(db_path, m, "3.53.0")
    set_version(db_path, "A-OLD", "3.52.0")
    packages.tick(db_path, now=t1)
    # A-PENDING stays pending: put it back as if its window were later.
    with packages.get_conn(db_path) as conn:
        conn.execute("UPDATE deployment_targets SET status = 'pending', command_id = NULL "
                     "WHERE deployment_id = ? AND machine = 'A-PENDING'", (dep,))
    for m in ("A-NEW", "A-OLD", "A-LATE", "A-DONE"):
        fleet.claim_commands(db_path, f"agent-{m}", m)
    done_cmd = target(db_path, dep, "A-DONE")["command_id"]
    finish(db_path, done_cmd, "A-DONE", True)
    packages.reconcile_once(db_path, now=t1 + 1)

    outcome = packages.abort_targets(
        db_path, dep, ["A-PENDING", "A-WAIT", "A-NEW", "A-OLD", "A-LATE", "A-DONE"],
        actor="boss@x.com", now=t1 + 2)
    check("a pending target is cancelled at once", outcome["cancelled"] == ["A-PENDING"])
    check("...saying who did it",
          target(db_path, dep, "A-PENDING")["last_error"] == "aborted by boss@x.com")
    check("waiting and running-on-3.53 targets are aborting",
          sorted(outcome["aborting"]) == ["A-LATE", "A-NEW", "A-WAIT"])
    check("an agent that cannot hear an abort is refused, not pretended to",
          outcome["unsupported"] == ["A-OLD"]
          and target(db_path, dep, "A-OLD")["abort_requested_at"] is None
          and target(db_path, dep, "A-OLD")["stage"] == "running")
    check("a finished target is skipped", outcome["skipped"] == ["A-DONE"])
    check("a running target reads aborting, NOT cancelled, while it runs",
          target(db_path, dep, "A-NEW")["status"] == "in_flight"
          and target(db_path, dep, "A-NEW")["stage"] == "aborting")
    wait_cmd = target(db_path, dep, "A-WAIT")["command_id"]
    check("a command nobody picked up is retired on the spot",
          fleet.get_command(db_path, wait_cmd)["status"] == fleet.STATUS_EXPIRED)
    new_cmd = target(db_path, dep, "A-NEW")["command_id"]
    answer = progress(db_path, new_cmd, "A-NEW", "step", 1, "running")
    check("the agent hears the abort in the answer to its next event",
          answer["cancel"] is True)
    progress(db_path, new_cmd, "A-NEW", "step", 1, "aborted")
    finish(db_path, new_cmd, "A-NEW", False, "[deploy] ABORTED from the console")
    late_cmd = target(db_path, dep, "A-LATE")["command_id"]
    finish(db_path, late_cmd, "A-LATE", True)   # it finished before it heard
    packages.reconcile_once(db_path, now=t1 + 3)
    check("an aborted run ends cancelled, and is NOT retried despite attempts left",
          target(db_path, dep, "A-NEW")["status"] == "cancelled"
          and target(db_path, dep, "A-NEW")["last_error"] == "aborted by boss@x.com")
    check("an abort that never reached the queue ends cancelled too",
          target(db_path, dep, "A-WAIT")["status"] == "cancelled")
    check("an abort that arrived after the last step records the truth: succeeded",
          target(db_path, dep, "A-LATE")["status"] == "succeeded")
    check("the aborted step is the More Info",
          target(db_path, dep, "A-NEW")["failed_step"]["state"] == "aborted")
    check("aborting is a security-level audit row",
          any(r["action"] == "abort_deployment_targets" and r["level"] == fleet.LEVEL_SECURITY
              for r in audit_rows(db_path)))
    check("abort on an unknown deployment is a KeyError",
          raises(KeyError, packages.abort_targets, db_path, "nope", ["A-NEW"]))

    print("\n== Redeploy ==")
    requeued = packages.retry_deployment_failures(
        db_path, dep, actor="op@x.com", machines=["A-LATE", "A-NEW"], include_succeeded=True)
    check("Redeploy selected requeues those machines, succeeded ones included",
          requeued == 2 and target(db_path, dep, "A-LATE")["status"] == "pending")
    check("...with the abort flag cleared, so the new attempt is not born aborted",
          target(db_path, dep, "A-NEW")["abort_requested_at"] is None)
    check("the plain retry never requeues a success",
          packages.retry_deployment_failures(db_path, dep, machines=["A-DONE"]) == 0)
    requeued = packages.retry_deployment_failures(db_path, dep, machines=["A-WAIT"])
    check("a scope list limits the plain retry to those machines",
          requeued == 1 and target(db_path, dep, "A-PENDING")["status"] == "cancelled")
    check("an empty scope requeues nothing",
          packages.retry_deployment_failures(db_path, dep, machines=[]) == 0)


def audit_rows(db_path):
    with fleet.get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT action, level FROM audit_log")]


def web_tests(workdir):
    global CURRENT_USER
    log_dir = os.path.join(workdir, "weblogs")
    os.makedirs(log_dir)
    db_path = os.path.join(log_dir, "hub.db")
    fleet.init_fleet_db(db_path)
    settings.init_settings_db(db_path)
    settings.invalidate()
    packages.init_packages_db(db_path)
    permissions.init_permissions_db(db_path)
    permissions.invalidate()
    permissions.create_group(
        db_path, name="Hospital IT",
        capabilities=[permissions.VIEW, permissions.DEPLOY_PACKAGES],
        machines=["HOSPITAL-1"], members=["hospital@x.com"], actor="root@x.com")

    marks, pokes = [], []
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(create_packages_blueprint(
        db_path, log_dir, fake_login_required, create_access(db_path, {"root@x.com"}),
        hub_url="https://hub.example.com",
        on_change=lambda topic, machine: marks.append((topic, machine)),
        wake_scheduler=lambda: pokes.append(1)))

    @app.before_request
    def _seed_session():
        from flask import session
        session["user"] = {"email": CURRENT_USER}

    c = app.test_client()
    pkg = stepped_package(db_path)

    print("\n== HTTP: deploy, and the scheduler is woken ==")
    r = c.post("/api/deployments", json={"package_id": pkg, "machines": ["HOSPITAL-1", "HR-1"]})
    dep = r.get_json()["id"]
    check("creating a deployment wakes the scheduler", r.status_code == 201 and pokes)
    check("a target answers with its stage and steps",
          r.get_json()["targets"][0]["stage"] == "queued"
          and "steps" in r.get_json()["targets"][0])

    packages.tick(db_path, now=soon())
    hosp_agent, hosp_token = fleet.enroll_agent(db_path, "HOSPITAL-1", "s", "s")
    hr_agent, hr_token = fleet.enroll_agent(db_path, "HR-1", "s", "s")
    fleet.claim_commands(db_path, hosp_agent, "HOSPITAL-1")
    fleet.claim_commands(db_path, hr_agent, "HR-1")
    hosp_cmd = target(db_path, dep, "HOSPITAL-1")["command_id"]
    hr_cmd = target(db_path, dep, "HR-1")["command_id"]
    hosp_auth = {"Authorization": f"Bearer {hosp_agent}:{hosp_token}"}
    url = f"/api/agent/commands/{hosp_cmd}/progress"

    print("\n== HTTP: the agent's progress POST ==")
    check("no agent token is 401",
          c.post(url, json={"phase": "step", "ordinal": 1, "state": "running"}).status_code
          == 401)
    r = c.post(url, json={"phase": "step", "ordinal": 1, "state": "running", "out_seq": 0},
               headers=hosp_auth)
    check("a step event is recorded", r.status_code == 200 and r.get_json()["cancel"] is False)
    check("...and marks deployments for that machine",
          marks == [("deployments", "HOSPITAL-1")])
    c.post(url, json={"phase": "step", "ordinal": 1, "state": "running", "out_seq": 0},
           headers=hosp_auth)
    check("a keepalive marks nothing", len(marks) == 1)
    r = c.post(f"/api/agent/commands/{hr_cmd}/progress",
               json={"phase": "step", "ordinal": 1, "state": "running"}, headers=hosp_auth)
    check("another machine's command is 403", r.status_code == 403)
    r = c.post(url, json={"phase": "install", "ordinal": 1, "state": "running"},
               headers=hosp_auth)
    check("an unknown phase is 400", r.status_code == 400)
    check("an unknown command is 404",
          c.post("/api/agent/commands/nope/progress",
                 json={"phase": "step", "ordinal": 1, "state": "running"},
                 headers=hosp_auth).status_code == 404)
    check("the progress route is a stream to the change hook, which it marks itself",
          not changes.should_mark_request("POST", url,
                                          "/api/agent/commands/<command_id>/progress", 200))

    print("\n== HTTP: a scoped operator ==")
    CURRENT_USER = "hospital@x.com"
    r = c.get(f"/api/deployments/{dep}/targets/HOSPITAL-1/steps/step/1/output")
    check("an in-scope step log is readable", r.status_code == 200
          and r.get_json()["state"] == "running")
    r = c.get(f"/api/deployments/{dep}/targets/HR-1/steps/step/1/output")
    check("an out-of-scope step log is refused", r.status_code == 403)
    r = c.get(f"/api/deployments/{dep}/targets/HOSPITAL-1/steps/install/1/output")
    check("a bad phase in the URL is 400", r.status_code == 400)
    r = c.post(f"/api/deployments/{dep}/abort", json={"machines": ["HR-1"]})
    check("aborting an out-of-scope machine is refused", r.status_code == 403)
    check("...and nothing was flagged",
          target(db_path, dep, "HR-1")["abort_requested_at"] is None)
    r = c.post(f"/api/deployments/{dep}/retry", json={"machines": ["HR-1"]})
    check("redeploying an out-of-scope machine is refused", r.status_code == 403)
    r = c.post(f"/api/deployments/{dep}/retry-now", json={"machines": ["HR-1"]})
    check("Retry now on an out-of-scope machine is refused", r.status_code == 403)

    # Both fail; the scoped operator's plain Retry must only requeue their own.
    fleet.complete_command(db_path, hosp_cmd, hosp_agent, False, "boom")
    fleet.complete_command(db_path, hr_cmd, hr_agent, False, "boom")
    with packages.get_conn(db_path) as conn:
        conn.execute("UPDATE deployments SET max_attempts = 1 WHERE id = ?", (dep,))
    packages.reconcile_once(db_path, now=soon())
    r = c.post(f"/api/deployments/{dep}/retry")
    check("a scoped operator's plain retry requeues only their machines",
          r.status_code == 200 and r.get_json()["requeued"] == 1
          and target(db_path, dep, "HR-1")["status"] == "failed")
    check("...and only their rows come back",
          [t["machine"] for t in r.get_json()["targets"]] == ["HOSPITAL-1"])

    print("\n== HTTP: abort ==")
    set_version(db_path, "HOSPITAL-1", "3.53.0")
    pokes.clear()
    r = c.post(f"/api/deployments/{dep}/abort", json={"machines": ["HOSPITAL-1"]})
    check("aborting a pending target answers with the outcome",
          r.status_code == 200 and r.get_json()["abort"]["cancelled"] == ["HOSPITAL-1"])
    check("an abort with no machines is 400",
          c.post(f"/api/deployments/{dep}/abort", json={}).status_code == 400)
    check("an unknown deployment is 404",
          c.post("/api/deployments/nope/abort",
                 json={"machines": ["HOSPITAL-1"]}).status_code == 404)

    CURRENT_USER = "viewer@x.com"
    check("an operator without deploy_packages reads no step log",
          c.get(f"/api/deployments/{dep}/targets/HOSPITAL-1/steps/step/1/output")
          .status_code == 403)
    CURRENT_USER = "root@x.com"


def main():
    workdir = tempfile.mkdtemp(prefix="deploy-progress-tests-")
    db_path = os.path.join(workdir, "packages.db")
    try:
        fleet.init_fleet_db(db_path)
        packages.init_packages_db(db_path)
        packages.init_packages_db(db_path)   # the migration must be idempotent
        model_tests(db_path)
        web_tests(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
