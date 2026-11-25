"""The machine page's 7 d and 30 d History ranges (hub 1.189.0): multi-day windows on
/api/machines/<m>/history, the 15m and 1h buckets, and the CSV archive import per day.

**The silent failure this file exists to catch is a long range that LOOKS right and is not.**
Three ways, each of which renders a perfectly plausible chart:

  - a request that still carries `date` is clamped to that one day on the server, so "30 d"
    quietly draws today and nothing else;
  - a multi-day window that skips the CSV import shows a flat gap exactly where the archived
    days are, which reads as "the PC was off";
  - a resolution the server does not know falls back to auto, which for thirty days used to
    mean 5m and 8,640 points per metric -- a page that is merely slow, never broken.

Imports app the way test_history_metrics.py does (env + cwd set before import).
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-histwindow-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.chdir(_TMPDIR)
os.environ["ALLOWED_EMAILS"] = "tester@example.com"

import app  # noqa: E402
import console_session  # noqa: E402

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


MACHINE = "WINDOW-PC"
FMT = "%Y-%m-%d %H:%M:%S"


def _seed(ts, temp, cpu):
    """One reading straight into the table, on the test's own thread (test_dedup.py says
    why: the writer thread's flush timer otherwise races the assert)."""
    app.write_readings_batch([
        (app.to_timestamp_str(ts), app.to_epoch_seconds(ts), MACHINE, float(temp), None)
        + app._metric_values_tuple({"cpu_load_pct": cpu})
    ])


def _client():
    client = app.app.test_client()
    console_session.sign_in(client, "tester@example.com")
    return client


# First on purpose: every other test here requests windows reaching back 30 days, and a
# requested day with no archive file is recorded as imported for good (the rule the `date`
# path has always had). Run after them, this test's file would be written for a day already
# marked done, and never read.
def test_archived_days_are_imported_for_a_range():
    print("\n-- a multi-day window imports each day's CSV archive, bounded --")
    day = (datetime.now() - timedelta(days=12)).replace(hour=9, minute=0, second=0, microsecond=0)
    os.makedirs(app.LOG_DIR, exist_ok=True)
    with open(app.get_log_path(day.strftime("%Y-%m-%d")), "w", newline="") as f:
        f.write("timestamp,machine,temperature\n")
        f.write(f"{day.strftime(FMT)},ARCHIVED-PC,55.5\n")
    client = _client()
    now = datetime.now()
    resp = client.get("/api/machines/ARCHIVED-PC/history", query_string={
        "from": (now - timedelta(days=14)).strftime(FMT), "to": now.strftime(FMT),
        "resolution": "1h", "limit": "all", "metrics": "temp",
    })
    temps = ((resp.get_json() or {}).get("metrics") or {}).get("temp") or []
    check("the archived reading is in the 14-day window", any(abs(p["y"] - 55.5) < 0.01 for p in temps))

    # Past MAX_HISTORY_IMPORT_DAYS nothing is imported: a hand-typed window of years must
    # not read years of files in one request.
    far = (datetime.now() - timedelta(days=200)).replace(hour=9, minute=0, second=0, microsecond=0)
    far_day = far.strftime("%Y-%m-%d")
    with open(app.get_log_path(far_day), "w", newline="") as f:
        f.write("timestamp,machine,temperature\n")
        f.write(f"{far.strftime(FMT)},ARCHIVED-PC,66.6\n")
    client.get("/api/machines/ARCHIVED-PC/history", query_string={
        "from": (far - timedelta(days=1)).strftime(FMT), "to": now.strftime(FMT),
        "resolution": "1h", "metrics": "temp",
    })
    with app.get_db_conn() as conn:
        imported = conn.execute("SELECT 1 FROM imported_days WHERE day = ?", (far_day,)).fetchone()
    check("a 200-day window imports nothing", imported is None)


def test_new_buckets_and_auto_resolution():
    print("\n-- 15m and 1h are real buckets, and auto picks 1h only past eight days --")
    check("15m is a valid resolution", app.VALID_RESOLUTIONS.get("15m") == 900)
    check("1h is a valid resolution", app.VALID_RESOLUTIONS.get("1h") == 3600)
    check("an explicit 15m is honoured", app.pick_resolution("15m", 30 * 86400) == "15m")
    # The boundaries that existed before keep their old answers -- only long spans moved.
    check("auto, 2 days -> 1m (unchanged)", app.pick_resolution("auto", 2 * 86400) == "1m")
    check("auto, 7 days -> 5m (unchanged)", app.pick_resolution("auto", 7 * 86400) == "5m")
    check("auto, 30 days -> 1h", app.pick_resolution("auto", 30 * 86400) == "1h")


def test_undated_window_spans_days():
    print("\n-- a window with no `date` is not clamped to one day --")
    now = datetime.now().replace(microsecond=0)
    args = {"from": (now - timedelta(days=30)).strftime(FMT), "to": now.strftime(FMT),
            "resolution": "1h"}
    start, end, resolution, _ = app._resolve_history_window(args)
    check("the span is thirty days", abs((end - start) - 30 * 86400) <= 1)
    check("the requested 1h is kept", resolution == "1h")
    # The control: the same window WITH a date is clamped, which is why the 7 d and 30 d
    # requests must leave it out.
    dated = dict(args, date=now.strftime("%Y-%m-%d"))
    d_start, d_end, _, _ = app._resolve_history_window(dated)
    check("a dated request is clamped to its day", d_end - d_start <= 86400)


def test_thirty_days_hourly_through_the_endpoint():
    print("\n-- 30 d at 1h returns one point per hour with readings, across every day --")
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    # One reading per six hours for 29 days: 116 readings, each in its own hour bucket.
    for i in range(29 * 4):
        _seed(now - timedelta(hours=6 * i), 40 + (i % 10), 5 + (i % 7))
    client = _client()
    resp = client.get(f"/api/machines/{MACHINE}/history", query_string={
        "from": (now - timedelta(days=30)).strftime(FMT),
        "to": (now + timedelta(hours=1)).strftime(FMT),
        "resolution": "1h", "limit": "all", "metrics": "temp,cpu_load",
    })
    check("history answers 200", resp.status_code == 200)
    body = resp.get_json() or {}
    temps = (body.get("metrics") or {}).get("temp") or []
    check("the response says 1h", body.get("resolution") == "1h")
    check("every seeded hour is a point", len(temps) == 29 * 4)
    days = {p["x"][:10] for p in temps}
    check("points span more than 28 calendar days", len(days) >= 28)
    check("cpu_load came back too", len((body.get("metrics") or {}).get("cpu_load") or []) == 29 * 4)


def main():
    test_archived_days_are_imported_for_a_range()
    test_new_buckets_and_auto_resolution()
    test_undated_window_spans_days()
    test_thirty_days_hourly_through_the_endpoint()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
