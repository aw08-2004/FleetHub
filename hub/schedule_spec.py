"""When something runs: one parser and one clock for every schedule in the hub.

Operators asked for "run the rules, the backups, the reports on a schedule, or on a cron
timer". Before this module the hub had exactly two ideas of time: an interval counted from
the last attempt (backups, `backup.*_interval_hours`) and the weekly maintenance window
(patches.py). Neither can say "every night at 02:00" -- the interval drifts by however long
the last run took to be noticed, and a window is a span, not an instant. This module is the
third idea, and it is meant to be the last: **every feature that runs on a schedule stores a
spec in its own row and asks this module the two questions a scheduler needs** -- "has a slot
passed since I last ran?" (`latest_slot`) and "when will it next run?" (`next_fires`).

A SPEC is a small JSON object, validated here and nowhere else:

    purpose "fire" -- a set of instants:
      {"kind": "weekly",   "days_mask": 31, "times": [180, 720]}   minutes past local midnight
      {"kind": "interval", "every_minutes": 360}                     elapsed time from an anchor
      {"kind": "cron",     "expr": "0 3 * * 0"}

    purpose "window" -- a set of minutes during which something may act:
      {"kind": "window", "days_mask": 31, "start_minute": 480, "duration_minutes": 600}
      {"kind": "cron",   "expr": "* 8-17 * * 1-5"}                  every matching minute

`days_mask` is bit 0 = Monday, the same mask patches.maintenance_windows has always stored
and policy-time.js draws. Cron numbers Sunday as 0. **The two conventions meet in exactly
one function, `cron_dow_to_mask`**, because the off-by-one between them is the bug that
reboots the helpdesk on a Monday morning instead of a Sunday night.

**Everything is in the HUB's local time**, for the reason patches.py gives: one helpdesk, one
clock, and a schedule written in the operator's own time is the one they can reason about.
The browser may sit in another zone, so the console never computes a next run itself -- it
asks the hub (`POST /api/schedules/preview`).

**DST.** A calendar slot is a wall-clock time, built per day with mktime, so "03:00" stays
03:00 across the change. A slot the spring-forward skips fires at whatever instant mktime
normalises it to (once); a slot the fall-back repeats fires once, because mktime returns one
instant for it and the per-day list is de-duplicated. An `interval` spec counts elapsed time
and ignores DST entirely, which is exactly what `interval_hours` always did. Every function
takes an injectable `clock` (anything with `localtime` and `mktime`, default the `time`
module) so the DST behaviour is tested on Windows, which has no `time.tzset`.

**Rejected: croniter / APScheduler.** croniter is ~200 lines of value behind a new dependency
in a hub that self-updates by copying files, and it still knows nothing about Monday-first
masks, windows, or an injected clock. APScheduler brings its own threads and job store, a
second scheduler beside the `tick()` threads app.py already runs and tests by passing a
clock. Rejected too: a seconds field, `@reboot`, and Quartz's `L W # ?` -- the first has no
meaning against a 15-60 s scheduler tick, the second has no meaning for a hub that restarts
on every self-update, and the last are a dialect operators would have to look up.

**Cron's day-of-month / day-of-week rule is Vixie's OR**, because that is what every crontab
an operator has ever copied means: when BOTH fields are restricted, a day matches if EITHER
does (`0 0 13 * 5` is "the 13th, and every Friday"). A field counts as unrestricted when its
text starts with `*`, so `*/2` in day-of-month is still unrestricted -- also Vixie's rule,
also surprising, and pinned by tests/test_schedule_spec.py.

Pure functions, no tables, no Flask: each feature keeps its own spec, history and gate. See
ROADMAP.MD -> *Schedules* for why there is no central jobs table.
"""
import datetime
import time

PURPOSE_FIRE = "fire"
PURPOSE_WINDOW = "window"
PURPOSES = (PURPOSE_FIRE, PURPOSE_WINDOW)

KIND_WEEKLY = "weekly"
KIND_INTERVAL = "interval"
KIND_CRON = "cron"
KIND_WINDOW = "window"
KINDS_BY_PURPOSE = {
    PURPOSE_FIRE: (KIND_WEEKLY, KIND_INTERVAL, KIND_CRON),
    PURPOSE_WINDOW: (KIND_WINDOW, KIND_CRON),
}

DAY_MINUTES = 24 * 60
ALL_DAYS = 0b1111111
MAX_TIMES_PER_DAY = 24
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 366 * DAY_MINUTES
MAX_CRON_CHARS = 200
# How far a search walks before it concludes a spec never fires. Eight years covers every
# leap-day schedule (`0 0 29 2 *` fires at least once in any eight consecutive years), and it
# bounds the work for one that never fires at all (`0 0 30 2 *`), which validate() refuses.
SEARCH_DAYS = 8 * 366

_MONTH_NAMES = {name: i + 1 for i, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_DOW_NAMES = {name: i for i, name in enumerate(
    ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}
_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}


class ScheduleError(ValueError):
    """A spec the hub refuses. The message is shown verbatim beside the field."""


# ================================
# CRON
# ================================
class Cron:
    """A parsed five-field expression: sets of allowed values plus the two star flags the
    day-of-month / day-of-week OR rule needs."""

    __slots__ = ("expr", "minutes", "hours", "doms", "months", "dows", "dom_star", "dow_star")

    def __init__(self, expr, minutes, hours, doms, months, dows, dom_star, dow_star):
        self.expr = expr
        self.minutes = minutes
        self.hours = hours
        self.doms = doms
        self.months = months
        self.dows = dows            # 0 = Sunday, as cron writes it; 7 folded into 0
        self.dom_star = dom_star
        self.dow_star = dow_star

    def day_matches(self, date):
        if date.month not in self.months:
            return False
        dom_ok = date.day in self.doms
        dow_ok = (date.weekday() + 1) % 7 in self.dows      # Python Monday=0 -> cron Sunday=0
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok

    def minutes_of_day(self):
        return sorted(h * 60 + m for h in self.hours for m in self.minutes)


def _parse_value(text, low, high, names, field):
    token = text.upper()
    if names and token in names:
        return names[token]
    if not token.isdigit():
        raise ScheduleError(f"{field}: '{text}' is not a number.")
    value = int(token)
    if not low <= value <= high:
        raise ScheduleError(f"{field}: {value} is outside {low}-{high}.")
    return value


def _parse_field(text, low, high, field, names=None):
    """One cron field to a frozenset. Accepts `*`, `a`, `a-b`, lists, and `/step` on `*`,
    a range or a single start (`5/15` = 5,20,35,50)."""
    if not text:
        raise ScheduleError(f"{field} is empty.")
    values = set()
    for part in text.split(","):
        if not part:
            raise ScheduleError(f"{field}: empty entry in '{text}'.")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text or part.endswith("/"):
            if not step_text.isdigit() or int(step_text) == 0:
                raise ScheduleError(f"{field}: step '{step_text}' must be a positive number.")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            start = _parse_value(a, low, high, names, field)
            end = _parse_value(b, low, high, names, field)
            if start > end:
                raise ScheduleError(f"{field}: range '{base}' runs backwards.")
        else:
            start = _parse_value(base, low, high, names, field)
            end = high if step_text else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expr):
    """Parse a five-field cron expression (or an @alias) into a Cron. Raises ScheduleError."""
    text = " ".join(str(expr or "").split())
    if not text:
        raise ScheduleError("The cron expression is empty.")
    if len(text) > MAX_CRON_CHARS:
        raise ScheduleError("The cron expression is too long.")
    lowered = text.lower()
    if lowered.startswith("@"):
        if lowered == "@reboot":
            raise ScheduleError("@reboot is not supported: the hub restarts on every update.")
        if lowered not in _ALIASES:
            raise ScheduleError(f"Unknown cron alias '{text}'.")
        text = _ALIASES[lowered]
    # Quartz's `L W # ?` need no special case: none is a number or a name, so the field
    # parser refuses them with the token in the message.
    fields = text.split(" ")
    if len(fields) != 5:
        raise ScheduleError(f"A cron expression has five fields (minute hour day month weekday); "
                            f"this one has {len(fields)}.")
    minute, hour, dom, month, dow = fields
    dows = _parse_field(dow, 0, 7, "Weekday", _DOW_NAMES)
    if 7 in dows:
        dows = (dows - {7}) | {0}
    return Cron(
        expr=" ".join(fields),
        minutes=_parse_field(minute, 0, 59, "Minute"),
        hours=_parse_field(hour, 0, 23, "Hour"),
        doms=_parse_field(dom, 1, 31, "Day of month"),
        months=_parse_field(month, 1, 12, "Month", _MONTH_NAMES),
        dows=dows,
        dom_star=dom.startswith("*"),
        dow_star=dow.startswith("*"),
    )


def cron_dow_to_mask(dows):
    """Cron weekdays (0 = Sunday) as a days_mask (bit 0 = Monday). The ONE place the two
    numberings meet -- see the module docstring."""
    mask = 0
    for d in dows:
        mask |= 1 << ((int(d) + 6) % 7)
    return mask


def mask_to_cron_dows(mask):
    """The inverse of cron_dow_to_mask."""
    return frozenset((bit + 1) % 7 for bit in range(7) if mask & (1 << bit))


# ================================
# VALIDATION
# ================================
def _int(value, name, low, high):
    """A whole number in range. "180" and 180.0 are accepted -- JSON clients are sloppy
    about types, as settings.coerce_and_validate notes -- but 180.5 and True are not."""
    if isinstance(value, bool):
        raise ScheduleError(f"{name} must be a whole number.")
    if isinstance(value, str):
        value = value.strip()
        if not value.lstrip("-").isdigit():
            raise ScheduleError(f"{name} must be a whole number.")
        number = int(value)
    elif isinstance(value, (int, float)) and int(value) == value:
        number = int(value)
    else:
        raise ScheduleError(f"{name} must be a whole number.")
    if not low <= number <= high:
        raise ScheduleError(f"{name} must be between {low} and {high}.")
    return number


def _days_mask(value):
    mask = _int(value, "Days", 0, ALL_DAYS)
    if mask == 0:
        raise ScheduleError("Pick at least one day.")
    return mask


def clean(spec, purpose=PURPOSE_FIRE):
    """The normalised spec, or raise ScheduleError. Unknown keys are dropped, so what is
    stored is exactly what this module reads back."""
    if purpose not in PURPOSES:
        raise ScheduleError(f"Unknown schedule purpose '{purpose}'.")
    if not isinstance(spec, dict):
        raise ScheduleError("A schedule must be an object.")
    kind = spec.get("kind")
    if kind not in KINDS_BY_PURPOSE[purpose]:
        raise ScheduleError(f"'{kind}' is not a schedule kind here; "
                            f"use one of {', '.join(KINDS_BY_PURPOSE[purpose])}.")
    if kind == KIND_WEEKLY:
        raw_times = spec.get("times")
        if not isinstance(raw_times, (list, tuple)) or not raw_times:
            raise ScheduleError("Add at least one time of day.")
        times = sorted({_int(t, "Time of day", 0, DAY_MINUTES - 1) for t in raw_times})
        if len(times) > MAX_TIMES_PER_DAY:
            raise ScheduleError(f"At most {MAX_TIMES_PER_DAY} times a day; "
                                f"use a cron expression for more.")
        return {"kind": kind, "days_mask": _days_mask(spec.get("days_mask")), "times": times}
    if kind == KIND_INTERVAL:
        every = _int(spec.get("every_minutes"), "Interval", MIN_INTERVAL_MINUTES,
                     MAX_INTERVAL_MINUTES)
        return {"kind": kind, "every_minutes": every}
    if kind == KIND_WINDOW:
        return {"kind": kind,
                "days_mask": _days_mask(spec.get("days_mask")),
                "start_minute": _int(spec.get("start_minute"), "Start", 0, DAY_MINUTES - 1),
                "duration_minutes": _int(spec.get("duration_minutes"), "Duration", 1,
                                         7 * DAY_MINUTES)}
    return {"kind": KIND_CRON, "expr": parse_cron(spec.get("expr")).expr}


def validate(spec, purpose=PURPOSE_FIRE, now=None, clock=None):
    """(error, cleaned). Error is None when the spec is good.

    Beyond shape, a spec that would NEVER fire is refused -- `0 0 30 2 *` parses perfectly
    and is the schedule nobody notices is dead until the backup they needed is not there.
    """
    try:
        cleaned = clean(spec, purpose)
    except ScheduleError as e:
        return str(e), None
    now = int(time.time() if now is None else now)
    if cleaned["kind"] != KIND_INTERVAL and not _next_calendar(
            cleaned, now, 1, clock or time):
        return "This schedule never fires.", None
    return None, cleaned


# ================================
# CALENDAR WALK
# ================================
def _day_plan(spec):
    """(day_matches(date), minutes_of_day) for a calendar spec."""
    if spec["kind"] == KIND_CRON:
        cron = parse_cron(spec["expr"])
        return cron.day_matches, cron.minutes_of_day()
    mask = spec["days_mask"]
    minutes = spec["times"] if spec["kind"] == KIND_WEEKLY else [spec["start_minute"]]
    return (lambda date: bool(mask & (1 << date.weekday()))), list(minutes)


def _instants(date, minutes, clock):
    """The epochs of `minutes` on local `date`, de-duplicated and sorted. One mktime per
    slot with isdst=-1, so a wall-clock time stays a wall-clock time across DST."""
    out = set()
    for mod in minutes:
        out.add(int(clock.mktime((date.year, date.month, date.day,
                                  mod // 60, mod % 60, 0, 0, 0, -1))))
    return sorted(out)


def _local_date(epoch, clock):
    stamp = clock.localtime(epoch)
    return datetime.date(stamp.tm_year, stamp.tm_mon, stamp.tm_mday)


def _next_calendar(spec, after, n, clock):
    matches, minutes = _day_plan(spec)
    found = []
    date = _local_date(after, clock) - datetime.timedelta(days=1)  # a slot past midnight-DST
    for _ in range(SEARCH_DAYS + 2):
        if matches(date):
            for s in _instants(date, minutes, clock):
                if s > after and (not found or s > found[-1]):
                    found.append(s)
                    if len(found) >= n:
                        return found
        date += datetime.timedelta(days=1)
    return found


def _latest_calendar(spec, now, anchor, clock):
    matches, minutes = _day_plan(spec)
    date = _local_date(now, clock) + datetime.timedelta(days=1)
    floor = _local_date(anchor, clock) - datetime.timedelta(days=1) if anchor else None
    for _ in range(SEARCH_DAYS + 2):
        if floor is not None and date < floor:
            return None
        if matches(date):
            for s in reversed(_instants(date, minutes, clock)):
                if s <= now:
                    return s if anchor is None or s > anchor else None
        date -= datetime.timedelta(days=1)
    return None


# ================================
# THE QUESTIONS A SCHEDULER ASKS
# ================================
def latest_slot(spec, now, anchor=None, clock=None):
    """The latest slot `s` with anchor < s <= now, or None when no slot has passed since
    the anchor.

    This is the whole of "is it due?": a scheduler stores when it last ran (or when the
    schedule was armed) as the anchor, and a slot strictly after it is owed. Because the
    answer comes from stored state rather than from "did the tick land on the minute",
    a hub that was down across 03:00 and a tick that ran at 03:00:40 are the same case,
    and each feature decides for itself whether a late slot still runs (backups: always;
    a reboot: only within a grace) by comparing the returned slot with `now`.

    An `interval` spec has no calendar, so it needs the anchor: the slots are
    anchor + k * every. With no anchor it returns None -- "never ran" is the caller's
    question (backups treat it as due at once).
    """
    clock = clock or time
    now = int(now)
    if spec["kind"] == KIND_INTERVAL:
        if anchor is None:
            return None
        step = spec["every_minutes"] * 60
        k = (now - int(anchor)) // step
        return int(anchor) + k * step if k >= 1 else None
    return _latest_calendar(spec, now, None if anchor is None else int(anchor), clock)


def next_fires(spec, after, n=5, anchor=None, clock=None):
    """The next `n` slots strictly after `after`. For `interval`, slots are counted from
    `anchor` (default `after`, i.e. "from now")."""
    clock = clock or time
    after = int(after)
    if spec["kind"] == KIND_INTERVAL:
        step = spec["every_minutes"] * 60
        base = int(after if anchor is None else anchor)
        k = max(1, (after - base) // step + 1)
        return [base + (k + i) * step for i in range(n)]
    return _next_calendar(spec, after, n, clock)


def min_gap_seconds(spec, now, sample=64, clock=None):
    """The shortest gap between consecutive slots over the next `sample` of them. Used to
    refuse a schedule tighter than something else's floor (a command cooldown, a scan
    spacing) instead of letting the floor silently skip runs."""
    if spec["kind"] == KIND_INTERVAL:
        return spec["every_minutes"] * 60
    fires = next_fires(spec, now, sample, clock=clock)
    gaps = [b - a for a, b in zip(fires, fires[1:])]
    return min(gaps) if gaps else SEARCH_DAYS * 86400


def window_occurrence(spec, now, clock=None):
    """(start, end) epochs of the opening of a `window`-purpose spec that contains `now`, or
    None when it is closed.

    For `window` kind this is patches.window_occurrence's rule exactly, spill-over past
    midnight included: 01:00 Monday is inside Sunday's 22:00 + 4 h window. For a cron
    window it is the matching minute itself, (minute, minute + 60).
    """
    clock = clock or time
    now = int(now)
    stamp = clock.localtime(now)
    if spec["kind"] == KIND_CRON:
        cron = parse_cron(spec["expr"])
        date = datetime.date(stamp.tm_year, stamp.tm_mon, stamp.tm_mday)
        if (cron.day_matches(date) and stamp.tm_hour in cron.hours
                and stamp.tm_min in cron.minutes):
            start = now - stamp.tm_sec
            return start, start + 60
        return None
    minute_of_day = stamp.tm_hour * 60 + stamp.tm_min
    start = spec["start_minute"]
    duration = spec["duration_minutes"]
    today = stamp.tm_wday
    mask = spec["days_mask"]

    def _bounds(day_offset):
        opened = int(clock.mktime((stamp.tm_year, stamp.tm_mon, stamp.tm_mday + day_offset,
                                   0, start, 0, 0, 0, -1)))
        return opened, opened + duration * 60

    if mask & (1 << today) and start <= minute_of_day < start + duration:
        return _bounds(0)
    # Spill-over from an opening on an earlier day. A window may be up to a week long, so
    # look back as far as its duration reaches, nearest day first.
    for back in range(1, (start + duration - 1) // DAY_MINUTES + 1):
        day = (today - back) % 7
        if mask & (1 << day) and minute_of_day + back * DAY_MINUTES < start + duration:
            return _bounds(-back)
    return None


def is_active(spec, now, clock=None):
    return window_occurrence(spec, now, clock) is not None


def next_openings(spec, after, n=5, clock=None):
    """The next `n` times a `window`-purpose spec opens, for the preview.

    A cron window opens at a matching minute whose previous minute does not match, so
    "* 8-17 * * 1-5" opens once a weekday at 08:00, not 600 times.
    """
    clock = clock or time
    after = int(after)
    if spec["kind"] == KIND_WINDOW:
        return _next_calendar(spec, after, n, clock)
    cron = parse_cron(spec["expr"])
    minutes = cron.minutes_of_day()
    matching = set(minutes)
    found = []
    date = _local_date(after, clock) - datetime.timedelta(days=1)
    for _ in range(SEARCH_DAYS + 2):
        if cron.day_matches(date):
            ran_past_midnight = (DAY_MINUTES - 1 in matching
                                 and cron.day_matches(date - datetime.timedelta(days=1)))
            for mod in minutes:
                if (mod - 1 in matching) if mod else ran_past_midnight:
                    continue
                s = int(clock.mktime((date.year, date.month, date.day,
                                      mod // 60, mod % 60, 0, 0, 0, -1)))
                if s > after and (not found or s > found[-1]):
                    found.append(s)
                    if len(found) >= n:
                        return found
        date += datetime.timedelta(days=1)
    return found


def preview(spec, purpose, now, n=5, anchor=None, clock=None):
    """What the console's picker shows: the next runs (or openings), in epochs."""
    if purpose == PURPOSE_WINDOW:
        return next_openings(spec, now, n, clock)
    return next_fires(spec, now, n, anchor=anchor, clock=clock)
