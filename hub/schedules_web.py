"""Flask HTTP surface for schedules -- a thin layer over schedule_spec.py, registered as a
Blueprint from app.py.

**One route today: the preview.** Every schedule picker in the console (Backups now; rules,
reports and discovery as they gain schedules) sends the spec it is holding here and shows
what comes back. The browser never works out a next run itself, for two reasons:

  * **The browser is not in the hub's time zone.** Schedules run on the hub's local clock
    (schedule_spec.py's docstring says why). A technician on a laptop set to another zone,
    or a browser that has not caught up with a DST rule change, would be shown a "next run"
    an hour off from the one the scheduler will actually take -- a preview that lies is
    worse than none.
  * **One implementation.** A JS cron parser beside the Python one is two dialects of
    "what does `0 0 13 * 5` mean", and the first difference between them is found by an
    operator whose backup ran on the wrong night.

So the answer carries the hub's UTC offset, and the picker renders each epoch in that
offset, labelled "hub time".

**Gate: `view`.** Previewing a spec reads nothing about the fleet and changes nothing -- it
is a calculator. Saving a schedule is gated where the schedule lives (`manage_backups` on
the Backups page, `manage_rules` on a rule), which is the gate that matters.

The CSRF note from fleet_web.py applies verbatim: the body is read with
request.get_json(silent=True), which requires Content-Type: application/json.
"""
import time

from flask import Blueprint, jsonify, request

import permissions
import refusals
import schedule_spec

MAX_PREVIEW = 10
# An anchor past this (2100-01-01) is not a last run: it is a typo or an attack. The bound
# also keeps out the `Infinity` and `NaN` literals request.get_json() accepts, which reached
# int() and turned a view-only route into a 500 (found in review, PR #189).
MAX_ANCHOR = 4102444800


def hub_utc_offset_seconds(now=None):
    """The hub's UTC offset at `now`, DST included -- what the picker shifts epochs by."""
    return int(time.localtime(now if now is not None else time.time()).tm_gmtoff)


def create_schedules_blueprint(login_required, access):
    """No `db_path`, unlike its siblings: the preview reads nothing stored. The /schedule
    overview (Schedules, phase 6) is the first route here that will need it."""
    bp = Blueprint("schedules", __name__)
    can_view = access.require(permissions.VIEW)

    @bp.route("/api/schedules/preview", methods=["POST"])
    @login_required
    @can_view
    def preview():
        """The next runs of a spec, in epochs, plus the hub's offset to render them in.

        Body: {spec, purpose: "fire"|"window", anchor?: epoch, count?: 1-10}. An invalid
        spec is a 400 carrying schedule_spec's own sentence, which the picker shows beside
        the field -- "Hour: 24 is outside 0-23." is the message, not a status code.
        """
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "A JSON body is required."}), 415
        purpose = data.get("purpose") or schedule_spec.PURPOSE_FIRE
        if purpose not in schedule_spec.PURPOSES:
            return jsonify({"error": f"Unknown schedule purpose '{purpose}'."}), 400
        now = int(time.time())
        error, spec = schedule_spec.validate(data.get("spec"), purpose, now=now)
        if error:
            return refusals.refuse(schedule_spec.ScheduleError(error))
        try:
            count = max(1, min(MAX_PREVIEW, int(data.get("count") or 5)))
        except (TypeError, ValueError, OverflowError):  # OverflowError: count Infinity
            count = 5
        anchor = data.get("anchor")
        anchor = (int(anchor) if isinstance(anchor, (int, float)) and not isinstance(anchor, bool)
                  and 0 < anchor < MAX_ANCHOR else None)
        return jsonify({
            "spec": spec,
            "next": schedule_spec.preview(spec, purpose, now, count, anchor=anchor),
            "now": now,
            "hub_utc_offset": hub_utc_offset_seconds(now),
        }), 200

    return bp
