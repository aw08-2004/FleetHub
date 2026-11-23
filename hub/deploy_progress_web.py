"""Flask HTTP surface for a RUNNING deployment -- the PDQ-style live view (roadmap #5,
hub 1.183.0 + agent 3.53.0). The model half is packages.py's LIVE PROGRESS section.

Its own blueprint rather than more routes in packages_web.py: that factory is the package
library and the deploy/cancel/retry surface, and this is the agent's step stream plus the
actions an operator takes on machines mid-run. Two audiences, the same gates as there:

  * **Console** -- `deploy_packages`, and every machine named must be in the caller's
    scope. Abort and Retry now take a selection and refuse it whole if any machine is out
    of reach (packages_web.scoped_targets). A step's log names its machine in the URL, so
    it is gated by `access.require_machine`: not on `view` like the fleet terminal,
    because a deployment row must not become a way to read a machine the operator could
    not otherwise reach.

  * **Agent, step progress** (`/api/agent/commands/<id>/progress`) -- bearer agent auth,
    and the command must be a deployment this agent claimed and is still running (see
    packages.record_step_progress). Not in fleet_web.py beside the output stream, because
    what it records is a deployment's and fleet.py knows nothing of steps. It is in
    changes.UNMARKED_RULES and marks `deployments` itself, only when a phase actually
    moved: the agent repeats its current event as a keepalive, and a notice per keepalive
    would refetch every open progress view every few seconds for nothing.

Waking the scheduler after Abort or Retry now is app.py's job (it wakes on any successful
console write to this blueprint or to packages), so this file needs no hook for it.
"""
from flask import Blueprint, jsonify, request

import packages
import permissions
import permissions_web
import refusals
from packages_web import _bearer_agent, scoped_targets


def _visible_deployment(db_path, access, deployment_id):
    deployment = packages.get_deployment(db_path, deployment_id)
    if deployment is not None:
        deployment["targets"] = access.filter_rows(deployment["targets"])
    return deployment


def _act_on_selection(db_path, access, deployment_id, action, answer_key):
    """Shared body of Abort and Retry now: scope-check the selection whole, run `action`
    (a packages function taking db_path, deployment_id, machines, actor=), and answer with
    the deployment as this operator may see it, plus what the action returned."""
    data = request.get_json(silent=True) or {}
    targets, error, status = scoped_targets(access, data.get("machines"))
    if error:
        return jsonify({"error": error}), status
    try:
        result = action(db_path, deployment_id, targets, actor=permissions_web.current_actor())
    except KeyError:
        return jsonify({"error": packages.UNKNOWN_DEPLOYMENT}), 404
    deployment = _visible_deployment(db_path, access, deployment_id)
    deployment[answer_key] = result
    return jsonify(deployment), 200


def _step_output(db_path, deployment_id, machine, phase, ordinal):
    try:
        return jsonify(packages.step_output(
            db_path, deployment_id, machine, phase, ordinal,
            after_seq=request.args.get("after_seq", -1))), 200
    except KeyError:
        return jsonify({"error": "unknown deployment target"}), 404
    except ValueError as e:
        return refusals.refuse(e)


def _record_progress(db_path, command_id, on_change):
    agent_id, _ = _bearer_agent(db_path)
    if agent_id is None:
        return jsonify({"error": "agent authentication required"}), 401
    data = request.get_json(silent=True) or {}
    try:
        recorded = packages.record_step_progress(
            db_path, command_id, agent_id,
            phase=data.get("phase"), ordinal=data.get("ordinal"),
            state=data.get("state"), exit_code=data.get("exit_code"),
            message=data.get("message"), out_seq=data.get("out_seq"))
    except KeyError:
        return jsonify({"error": "unknown command"}), 404
    except PermissionError as e:
        return refusals.refuse(e, 403)
    except ValueError as e:
        return refusals.refuse(e)
    if recorded["changed"] and on_change is not None:
        try:
            on_change("deployments", recorded["machine"])
        except Exception as e:                      # noqa: BLE001
            print(f"[deploy_progress] could not mark a progress change: {e}")
    return jsonify({"status": "ok", "cancel": recorded["cancel"]}), 200


def create_deploy_progress_blueprint(db_path, login_required, access, on_change=None):
    """Build the blueprint. `on_change(topic, machine)` marks a change-notice topic for the
    progress POST, which the after-request hook deliberately skips (see the docstring);
    it is optional and never allowed to fail the request it rides on.

    The routes are thin on purpose: each body is a module-level function above, so this
    factory stays a list of URLs and gates a reviewer can read at a glance.
    """
    bp = Blueprint("deploy_progress", __name__)
    can_deploy = access.require(permissions.DEPLOY_PACKAGES)
    can_deploy_on = access.require_machine(permissions.DEPLOY_PACKAGES)

    @bp.route("/api/deployments/<deployment_id>/retry-now", methods=["POST"])
    @login_required
    @can_deploy
    def retry_deployment_now(deployment_id):
        """Skip the retry backoff for these targets (packages.retry_now)."""
        return _act_on_selection(db_path, access, deployment_id, packages.retry_now, "moved")

    @bp.route("/api/deployments/<deployment_id>/abort", methods=["POST"])
    @login_required
    @can_deploy
    def abort_deployment_targets(deployment_id):
        """Stop these targets, running ones included (packages.abort_targets). Answers which
        were cancelled, which are aborting, and which an older agent cannot be told to stop
        -- so the console can say so instead of implying they were."""
        return _act_on_selection(db_path, access, deployment_id, packages.abort_targets,
                                 "abort")

    @bp.route("/api/deployments/<deployment_id>/targets/<machine>/steps/<phase>/"
              "<int:ordinal>/output", methods=["GET"])
    @login_required
    @can_deploy_on
    def deployment_step_output(deployment_id, machine, phase, ordinal):
        """One step's output, PDQ's Output Log (packages.step_output)."""
        return _step_output(db_path, deployment_id, machine, phase, ordinal)

    @bp.route("/api/agent/commands/<command_id>/progress", methods=["POST"])
    def agent_command_progress(command_id):
        """One phase transition of a running deployment. The answer's `cancel` is the abort
        signal. A 403 tells the agent the command is over (or not its own) and it stops
        reporting -- the same contract as the output stream's."""
        return _record_progress(db_path, command_id, on_change)

    return bp
