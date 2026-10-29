"""HTTP-layer test for schedules_web.py -- the hub-time preview every schedule picker uses.

Wires the blueprint onto a minimal Flask app, avoiding app.py's OAuth boot -- same approach
as test_wake_web / test_bios_web.

The silent failure this exists to catch is **a picker that shows a next run the scheduler
will not take**: the browser rendering epochs in its own zone instead of the hub's, or a
preview that accepts a spec the settings validator later refuses (so the operator sees five
plausible runs, presses save, and gets an error -- or worse, the reverse). What is asserted:

  * **The answer carries the hub's UTC offset**, and the runs it lists are the ones
    schedule_spec computes -- the route is a window onto that module, not a second parser.
  * **A bad spec is a 400 with schedule_spec's own sentence**, so the picker can put
    "Hour: 24 is outside 0-23." beside the field.
  * **`view` is enough, and nothing less is.** A preview is a calculator; saving is gated
    where the schedule is stored.
  * **A JSON body is required** -- the CSRF rule fleet_web.py states.

Run from the repo root so `import schedules_web` resolves.
"""
import functools
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

import fleet
import permissions
import schedule_spec
import settings
from permissions_web import create_access
from schedules_web import create_schedules_blueprint, hub_utc_offset_seconds

from flask import Flask, session as flask_session

PASS = 0
FAIL = 0
CURRENT_USER = "super@x.com"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def main():
    global CURRENT_USER
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    try:
        fleet.init_fleet_db(db_path)
        permissions.init_permissions_db(db_path)
        settings.init_settings_db(db_path)
        settings.invalidate()
        permissions.create_group(db_path, "Viewers", capabilities=[permissions.VIEW],
                                 machines=None, members=["viewer@x.com"])

        app = Flask(__name__)
        app.secret_key = "test"
        access = create_access(db_path, {"super@x.com"})
        app.register_blueprint(create_schedules_blueprint(fake_login_required, access))

        @app.before_request
        def _seed_session():
            flask_session["user"] = {"email": CURRENT_USER}
        c = app.test_client()

        nightly = {"kind": "weekly", "days_mask": 0b1111111, "times": [180]}

        print("\n== the preview is schedule_spec's answer, in hub time ==")
        before = int(time.time())
        r = c.post("/api/schedules/preview", json={"spec": nightly})
        body = r.get_json()
        check("POST -> 200", r.status_code == 200)
        check("five runs by default", len(body["next"]) == 5)
        check("they are the module's runs",
              body["next"] == schedule_spec.next_fires(nightly, body["now"], 5))
        check("...in the future", all(t > before for t in body["next"]))
        check("the hub's UTC offset rides along",
              body["hub_utc_offset"] == hub_utc_offset_seconds(body["now"]))
        check("the cleaned spec is echoed back", body["spec"] == nightly)

        r = c.post("/api/schedules/preview",
                   json={"spec": {"kind": "cron", "expr": "@daily"}, "count": 99})
        check("an alias comes back expanded",
              r.get_json()["spec"] == {"kind": "cron", "expr": "0 0 * * *"})
        check("the count is capped", len(r.get_json()["next"]) == 10)

        anchor = before - 1800
        r = c.post("/api/schedules/preview", json={
            "spec": {"kind": "interval", "every_minutes": 60}, "anchor": anchor, "count": 2})
        check("an interval counts from the anchor it is given",
              r.get_json()["next"] == [anchor + 3600, anchor + 7200])
        # request.get_json() reads the non-standard Infinity and NaN literals: each used to
        # reach int() and answer 500 on a view-only route. Out of range = no anchor.
        for raw in ("Infinity", "-Infinity", "NaN", "1e300", "true"):
            r = c.post("/api/schedules/preview", content_type="application/json",
                       data='{"spec": {"kind": "interval", "every_minutes": 60}, '
                            f'"anchor": {raw}, "count": 1}}')
            check(f"anchor {raw} is ignored, not a 500 ({r.status_code})",
                  r.status_code == 200 and r.get_json()["next"][0] > before)

        r = c.post("/api/schedules/preview", json={
            "spec": {"kind": "window", "days_mask": 31, "start_minute": 480,
                     "duration_minutes": 600}, "purpose": "window"})
        check("a window previews its openings", r.status_code == 200
              and all(time.localtime(t).tm_hour == 8 for t in r.get_json()["next"]))

        print("\n== a bad spec is refused with a sentence ==")
        r = c.post("/api/schedules/preview",
                   json={"spec": {"kind": "cron", "expr": "0 24 * * *"}})
        check("400", r.status_code == 400)
        check("...naming the field", "Hour" in r.get_json()["error"])
        r = c.post("/api/schedules/preview",
                   json={"spec": {"kind": "cron", "expr": "0 0 30 2 *"}})
        check("a never-firing spec is refused here too", r.status_code == 400)
        r = c.post("/api/schedules/preview", json={"spec": nightly, "purpose": "window"})
        check("a weekly spec is not a window", r.status_code == 400)
        r = c.post("/api/schedules/preview", json={"spec": nightly, "purpose": "sometimes"})
        check("an unknown purpose is refused", r.status_code == 400)
        r = c.post("/api/schedules/preview", data="spec=x",
                   content_type="application/x-www-form-urlencoded")
        check("a form post is refused (CSRF)", r.status_code == 415)

        print("\n== view is enough, and nothing less is ==")
        CURRENT_USER = "viewer@x.com"
        check("a viewer may preview",
              c.post("/api/schedules/preview", json={"spec": nightly}).status_code == 200)
        CURRENT_USER = "nobody@x.com"
        check("someone with no capability may not",
              c.post("/api/schedules/preview", json={"spec": nightly}).status_code == 403)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
