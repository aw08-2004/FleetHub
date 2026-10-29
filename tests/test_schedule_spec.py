"""schedule_spec.py -- the one parser and clock behind every scheduled thing in the hub.

The silent failure this file exists to catch is **a schedule that saves, shows a next run,
and then fires at the wrong time or never** -- the backup that ran on Monday instead of
Sunday because two weekday numberings met in the wrong place, the 03:00 reboot that fired
twice on the night the clocks went back, the `0 0 30 2 *` that parses perfectly and is dead.
Nothing in the console looks wrong in any of those cases; the run simply is not where the
operator put it.

What is pinned:

  * **Cron parses the way a copied crontab means it**: steps, ranges, lists, names, Sunday
    as 0 and 7, the @aliases -- and Vixie's day-of-month OR day-of-week rule, including the
    surprising `*/2` counts-as-unrestricted case.
  * **Refusals name the problem**: six fields, out-of-range values, a zero step, `@reboot`,
    Quartz syntax, and a spec that never fires.
  * **Sunday=0 meets bit-0=Monday in one function, correctly.**
  * **latest_slot is "anchor < slot <= now"**, exclusive at the anchor and inclusive at now,
    for calendar and interval specs alike -- the whole of "is it due?".
  * **DST**, through an injected clock (Windows has no time.tzset): a skipped wall-clock
    time fires once, a repeated one fires once, and 03:00 stays 03:00.
  * **A `window` spec agrees with patches.window_occurrence** minute for minute, spill-over
    past midnight included, so active hours and maintenance windows cannot disagree.

Run from the repo root so `import schedule_spec` resolves.
"""
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))

import patches
import schedule_spec as ss

PASS = 0
FAIL = 0

HOUR = 3600
DAY = 86400
_EPOCH = datetime.datetime(1970, 1, 1)


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


class ShiftClock:
    """A zone whose UTC offset is `before` until the epoch `switch` and `after` from then on.

    mktime follows glibc's isdst=-1 behaviour closely enough for the property under test: a
    local time in the spring-forward gap is read with the old offset (so it lands an hour
    later on the wall), and one in the fall-back overlap resolves to its FIRST instant.
    """

    def __init__(self, switch, before, after):
        self.switch, self.before, self.after = switch, before, after

    def localtime(self, epoch):
        offset = self.before if epoch < self.switch else self.after
        dt = _EPOCH + datetime.timedelta(seconds=int(epoch) + offset)
        return time.struct_time((dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
                                 dt.weekday(), dt.timetuple().tm_yday, 0))

    def mktime(self, t):
        y, mo, d, h, mi, s = t[:6]
        naive = (datetime.datetime(y, mo, 1)
                 + datetime.timedelta(days=d - 1, hours=h, minutes=mi, seconds=s))
        local = int((naive - _EPOCH).total_seconds())
        e_before, e_after = local - self.before, local - self.after
        if e_before < self.switch:
            return e_before
        if e_after >= self.switch:
            return e_after
        return e_before


# A fixed UTC-3 zone: deterministic whatever this machine's own zone is.
FIXED = ShiftClock(10 ** 12, -3 * HOUR, -3 * HOUR)


def at(clock, y, mo, d, h=0, mi=0):
    return int(clock.mktime((y, mo, d, h, mi, 0, 0, 0, -1)))


def local(clock, epoch):
    s = clock.localtime(epoch)
    return (s.tm_year, s.tm_mon, s.tm_mday, s.tm_hour, s.tm_min)


def cron(expr):
    return {"kind": "cron", "expr": expr}


def refused(spec, purpose="fire"):
    error, cleaned = ss.validate(spec, purpose, now=at(FIXED, 2026, 11, 1), clock=FIXED)
    return error is not None and cleaned is None


def test_cron_parses_like_a_crontab():
    print("\n== cron parses the way a copied crontab means it ==")
    c = ss.parse_cron("*/15 * * * *")
    check("*/15 is every quarter hour", c.minutes == {0, 15, 30, 45})
    check("5/20 starts at 5 and steps to the end", ss.parse_cron("5/20 * * * *").minutes == {5, 25, 45})
    check("a stepped range", ss.parse_cron("0 8-18/2 * * *").hours == {8, 10, 12, 14, 16, 18})
    check("a list", ss.parse_cron("0,30 * * * *").minutes == {0, 30})
    check("weekday names", ss.parse_cron("0 0 * * MON-FRI").dows == {1, 2, 3, 4, 5})
    check("month names, any case", ss.parse_cron("0 0 1 jan,DEC *").months == {1, 12})
    check("Sunday is both 0 and 7", ss.parse_cron("0 3 * * 7").dows == {0}
          and ss.parse_cron("0 3 * * 0").dows == {0})
    check("@daily is midnight", ss.parse_cron("@daily").expr == "0 0 * * *")
    check("@weekly is Sunday midnight", ss.parse_cron("@weekly").expr == "0 0 * * 0")
    check("extra whitespace is folded", ss.parse_cron("  0   3 * *  0 ").expr == "0 3 * * 0")


def test_cron_refusals_name_the_problem():
    print("\n== a bad expression is refused, with a reason ==")
    for expr in ("", "0 3 * *", "0 0 3 * * 0", "60 * * * *", "0 24 * * *", "0 0 0 * *",
                 "0 0 * 13 *", "0 0 * * 8", "*/0 * * * *", "5-1 * * * *", "@reboot",
                 "@fortnightly", "0 0 L * *", "0 0 ? * MON", "0 0 * * 5#3", "0 0 15W * *",
                 "x * * * *", "1,,2 * * * *"):
        try:
            ss.parse_cron(expr)
            check(f"refuses {expr!r}", False)
        except ss.ScheduleError as e:
            check(f"refuses {expr!r}: {e}", bool(str(e)))
    check("a spec that can never fire is refused", refused(cron("0 0 30 2 *")))
    check("...and the leap day is not", not refused(cron("0 0 29 2 *")))


def test_vixie_or_rule():
    print("\n== day-of-month OR day-of-week, the way cron has always meant it ==")
    after = at(FIXED, 2026, 11, 1) - 1
    spec = cron("0 0 13 * 5")
    fires = [f for f in ss.next_fires(spec, after, 12, clock=FIXED)
             if local(FIXED, f)[1] == 11]
    want = {d for d in range(1, 31)
            if datetime.date(2026, 11, d).weekday() == 4 or d == 13}
    check("both restricted: the 13th AND every Friday",
          {local(FIXED, f)[2] for f in fires} == want)
    fires = ss.next_fires(cron("0 0 */2 * 5"), after, 6, clock=FIXED)
    check("*/2 in day-of-month counts as unrestricted, so only Fridays",
          all(datetime.date(*local(FIXED, f)[:3]).weekday() == 4 for f in fires))
    fires = ss.next_fires(cron("0 0 1 * *"), after, 3, clock=FIXED)
    check("day-of-week '*' leaves day-of-month in charge",
          [local(FIXED, f)[1:3] for f in fires] == [(11, 1), (12, 1), (1, 1)])


def test_leap_day():
    print("\n== a leap-day schedule finds its next leap year ==")
    fires = ss.next_fires(cron("0 0 29 2 *"), at(FIXED, 2026, 11, 1), 2, clock=FIXED)
    check("next 29 February is 2028", local(FIXED, fires[0])[:3] == (2028, 2, 29))
    check("...then 2032", local(FIXED, fires[1])[:3] == (2032, 2, 29))


def test_weekday_numbering_meets_once():
    print("\n== Sunday=0 and bit0=Monday meet in one function ==")
    check("cron Sunday is mask bit 6", ss.cron_dow_to_mask({0}) == 1 << 6)
    check("cron Monday is mask bit 0", ss.cron_dow_to_mask({1}) == 1)
    check("Mon-Fri is 31", ss.cron_dow_to_mask(range(1, 6)) == 0b0011111)
    check("round trip, every day", all(
        ss.mask_to_cron_dows(ss.cron_dow_to_mask({d})) == {d} for d in range(7)))
    sunday = cron("0 3 * * 0")
    weekly = {"kind": "weekly", "days_mask": 1 << 6, "times": [180]}
    after = at(FIXED, 2026, 11, 1)
    check("cron 'Sunday' and the picker's Sunday are the same instants",
          ss.next_fires(sunday, after, 5, clock=FIXED)
          == ss.next_fires(weekly, after, 5, clock=FIXED))
    check("...and they are Sundays", all(
        datetime.date(*local(FIXED, f)[:3]).weekday() == 6
        for f in ss.next_fires(weekly, after, 5, clock=FIXED)))


def test_weekly_and_validation():
    print("\n== the picker's specs, cleaned ==")
    error, spec = ss.validate({"kind": "weekly", "days_mask": 31, "times": [720, "180", 180.0],
                               "junk": 1}, now=at(FIXED, 2026, 11, 1), clock=FIXED)
    check("times are de-duplicated, sorted, and strings accepted",
          error is None and spec == {"kind": "weekly", "days_mask": 31, "times": [180, 720]})
    saturday = at(FIXED, 2026, 10, 31, 12)
    fires = ss.next_fires(spec, saturday, 3, clock=FIXED)
    check("from a Saturday: Mon 03:00, Mon 12:00, Tue 03:00",
          [local(FIXED, f)[2:4] for f in fires] == [(2, 3), (2, 12), (3, 3)])
    check("no days is refused", refused({"kind": "weekly", "days_mask": 0, "times": [0]}))
    check("no times is refused", refused({"kind": "weekly", "days_mask": 1, "times": []}))
    check("25 times a day is refused",
          refused({"kind": "weekly", "days_mask": 1, "times": list(range(0, 1500, 60))}))
    check("a fractional minute is refused",
          refused({"kind": "weekly", "days_mask": 1, "times": [180.5]}))
    check("True is not a minute", refused({"kind": "weekly", "days_mask": 1, "times": [True]}))
    check("minute 1440 is refused", refused({"kind": "weekly", "days_mask": 1, "times": [1440]}))
    check("an interval under five minutes is refused",
          refused({"kind": "interval", "every_minutes": 4}))
    check("a window is not a fire spec", refused({"kind": "window", "days_mask": 1,
                                                  "start_minute": 0, "duration_minutes": 60}))
    check("a weekly spec is not a window", refused(
        {"kind": "weekly", "days_mask": 1, "times": [0]}, "window"))
    check("a non-object is refused", refused("0 3 * * *"))
    error, spec = ss.validate(cron("@daily"), now=at(FIXED, 2026, 11, 1), clock=FIXED)
    check("an alias is stored as its expansion", spec == cron("0 0 * * *"))


def test_latest_slot():
    print("\n== latest_slot: anchor < slot <= now ==")
    daily = cron("0 3 * * *")
    three = at(FIXED, 2026, 11, 2, 3)
    check("exactly at the slot, it is due",
          ss.latest_slot(daily, three, anchor=three - 60, clock=FIXED) == three)
    check("a second before, the previous day's slot",
          ss.latest_slot(daily, three - 1, anchor=None, clock=FIXED) == three - DAY)
    check("an anchor ON the slot means it has been served",
          ss.latest_slot(daily, three + 60, anchor=three, clock=FIXED) is None)
    check("hub down three days: one slot owed, the latest",
          ss.latest_slot(daily, three + 3 * DAY + 600, anchor=three, clock=FIXED)
          == three + 3 * DAY)
    every = {"kind": "interval", "every_minutes": 60}
    check("interval with no anchor is the caller's question",
          ss.latest_slot(every, 10_000, anchor=None) is None)
    check("interval: not yet", ss.latest_slot(every, 1000 + HOUR - 1, anchor=1000) is None)
    check("interval: exactly one interval", ss.latest_slot(every, 1000 + HOUR, anchor=1000)
          == 1000 + HOUR)
    check("interval: the latest whole step", ss.latest_slot(every, 1000 + 2 * HOUR + 100,
                                                            anchor=1000) == 1000 + 2 * HOUR)
    check("interval next_fires counts from the anchor",
          ss.next_fires(every, 1000 + 100, 2, anchor=1000) == [1000 + HOUR, 1000 + 2 * HOUR])
    check("min gap of two times twenty minutes apart",
          ss.min_gap_seconds({"kind": "weekly", "days_mask": 127, "times": [180, 200]},
                             three, clock=FIXED) == 20 * 60)
    check("min gap of an interval", ss.min_gap_seconds(every, 0) == HOUR)


def test_dst():
    print("\n== DST: a wall-clock slot fires once, and stays on the wall clock ==")
    # Spring forward: at local 00:00 on 4 October 2026 the clock jumps to 01:00
    # (UTC-4 -> UTC-3, the old Paraguayan rule). 00:30 that night does not exist.
    std = ShiftClock(0, -4 * HOUR, -4 * HOUR)
    switch = at(std, 2026, 10, 4, 0)
    spring = ShiftClock(switch, -4 * HOUR, -3 * HOUR)
    fires = ss.next_fires(cron("30 0 * * *"), at(spring, 2026, 10, 1), 6, clock=spring)
    days = [local(spring, f)[:3] for f in fires]
    check("the skipped 00:30 still fires, once, that day",
          days.count((2026, 10, 4)) == 1 and len(set(days)) == len(days))
    check("strictly increasing", all(a < b for a, b in zip(fires, fires[1:])))
    fires = ss.next_fires(cron("0 3 * * *"), at(spring, 2026, 10, 1), 6, clock=spring)
    check("03:00 stays 03:00 on both sides of the change",
          all(local(spring, f)[3:] == (3, 0) for f in fires))
    check("the night of the change is 23 hours long",
          sorted({b - a for a, b in zip(fires, fires[1:])}) == [23 * HOUR, DAY])

    # Fall back: at local 00:00 on 22 March 2026 the clock returns to 23:00 the day before
    # (UTC-3 -> UTC-4). 23:30 on 21 March happens twice.
    dst = ShiftClock(0, -3 * HOUR, -3 * HOUR)
    switch = at(dst, 2026, 3, 22, 0)
    fall = ShiftClock(switch, -3 * HOUR, -4 * HOUR)
    fires = ss.next_fires(cron("30 23 * * *"), at(fall, 2026, 3, 19), 6, clock=fall)
    check("the repeated 23:30 fires once",
          [local(fall, f)[:3] for f in fires].count((2026, 3, 21)) == 1)
    check("never two runs within a few hours", min(b - a for a, b in zip(fires, fires[1:]))
          >= DAY)
    every = {"kind": "interval", "every_minutes": 360}
    check("an interval ignores DST entirely",
          ss.next_fires(every, switch - HOUR, 3, anchor=switch - HOUR, clock=fall)
          == [switch + 5 * HOUR, switch + 11 * HOUR, switch + 17 * HOUR])


def test_windows_agree_with_patches():
    print("\n== a window spec and a maintenance window agree, minute for minute ==")
    start = int(time.mktime((2026, 11, 2, 0, 0, 0, 0, 0, -1)))
    for days_mask, start_minute, duration in ((1 << 6, 22 * 60, 240),   # Sun 22:00 + 4 h
                                              (0b0011111, 8 * 60, 600),  # Mon-Fri 08-18
                                              (0b1000001, 23 * 60, 1500)):  # > a day
        window = {"enabled": True, "days_mask": days_mask, "start_minute": start_minute,
                  "duration_minutes": duration}
        spec = {"kind": "window", "days_mask": days_mask, "start_minute": start_minute,
                "duration_minutes": duration}
        mismatches = 0
        for step in range(0, 8 * DAY, 15 * 60):
            now = start + step
            if patches.window_occurrence(window, now) != ss.window_occurrence(spec, now):
                mismatches += 1
        check(f"mask {days_mask:07b} {start_minute}+{duration}: no disagreements",
              mismatches == 0)


def test_cron_windows():
    print("\n== a cron window is active on its matching minutes and opens once a day ==")
    spec = cron("* 8-17 * * 1-5")
    monday = at(FIXED, 2026, 11, 2)
    check("Monday 08:00 is inside", ss.is_active(spec, monday + 8 * HOUR, clock=FIXED))
    check("Monday 17:59 is inside", ss.is_active(spec, monday + 18 * HOUR - 60, clock=FIXED))
    check("Monday 18:00 is outside", not ss.is_active(spec, monday + 18 * HOUR, clock=FIXED))
    check("Saturday 10:00 is outside",
          not ss.is_active(spec, monday + 5 * DAY + 10 * HOUR, clock=FIXED))
    openings = ss.next_openings(spec, monday, 5, clock=FIXED)
    check("it opens at 08:00 on five consecutive weekdays",
          [local(FIXED, o)[2:4] for o in openings] == [(2, 8), (3, 8), (4, 8), (5, 8), (6, 8)])
    night = cron("* 22-23,0-5 * * *")
    openings = ss.next_openings(night, monday, 2, clock=FIXED)
    check("a window across midnight opens at 22:00, not again at 00:00",
          [local(FIXED, o)[3] for o in openings] == [22, 22])


def main():
    test_cron_parses_like_a_crontab()
    test_cron_refusals_name_the_problem()
    test_vixie_or_rule()
    test_leap_day()
    test_weekday_numbering_meets_once()
    test_weekly_and_validation()
    test_latest_slot()
    test_dst()
    test_windows_agree_with_patches()
    test_cron_windows()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
