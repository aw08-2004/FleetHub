"""A refresh must never land under the operator's hands (static/js/fleet-live.js, backups.js).

The silent failure this file exists to catch is **a dropdown that closes by itself**. A
native popup -- a <select>'s list, a time input's clock -- closes the instant its element is
replaced, and nothing in the DOM says it was open, so nothing errors and nothing logs. The
operator just sees the list vanish every few seconds and cannot pick anything. Hub 1.174.1
fixed two ways it happened, both caught from a screen recording rather than a report:

  * **Backups' run poll redrew the whole tab.** While `running` held it re-read every 4 s and
    called render(), rebuilding the schedule picker, the mode list and the destination list.
    The poll only fetches runs and the due time, so it must redraw only those.
  * **A live re-read had no idea a dropdown was open.** fleet-live.js deferred for an open
    <dialog> and a page's own busy(), and nothing else.

Both are guards that read as optional clean-up, so they are pinned here by source. The
behaviour itself was exercised against the real scripts in a browser when this was written.

Run from the repo root.
"""
import os
import re
import sys

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def code_only(js):
    """JS with its // line comments removed -- comments name what the code must not do."""
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


def function_body(code, name):
    """The text of `function name(...) { ... }`, by brace counting from its opening brace."""
    start = code.find(f"function {name}(")
    if start < 0:
        return ""
    i = code.find("{", start)
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i:j + 1]
    return ""


def test_fleet_live_waits_for_a_dropdown():
    print("\n-- fleet-live.js: a live re-read waits while a dropdown is in use --")
    code = code_only(read("hub", "static", "js", "fleet-live.js"))
    check("select and the date/time inputs count as popup controls",
          all(s in code for s in ("'select, input[type=\"date\"], input[type=\"time\"], '",
                                  "input[type=\"datetime-local\"]")))
    busy = function_body(code, "busy")
    check("busy() asks popupInUse() before the page's own check",
          0 <= busy.find("popupInUse()") < busy.find("opts.busy"))
    popup = function_body(code, "popupInUse")
    check("...only for a recently used control, so a select left focused cannot freeze a page",
          "POPUP_RECENT_MS" in popup and "lastInput" in popup)
    check("...and a plain text box is not one (it would stall every page with a search box)",
          not re.search(r"POPUP_CONTROLS\s*=\s*'[^;]*input,", code)
          and "input[type=\"text\"]" not in code)
    check("use is noticed in the capture phase, so stopPropagation cannot hide it",
          "owner.document.addEventListener(type, noteInput, true)" in code)


def test_backups_poll_redraws_only_runs():
    print("\n-- backups.js: the run poll and the live watch leave the schedule card alone --")
    code = code_only(read("hub", "static", "js", "backups.js"))
    poll = function_body(code, "schedulePoll")
    check("the poll redraws in place", "renderRunsInPlace();" in poll)
    check("...never the whole tab", "render();" not in poll)
    in_place = function_body(code, "renderRunsInPlace")
    check("in place means the runs card and the due line",
          "renderRunsCard()" in in_place and "schedule-due" in in_place)
    check("...and it can find the runs card", "card.dataset.runsCard = '';" in code)
    check("the live watch defers while a field on the tab is in use",
          "busy: operatorEditing" in code)
    editing = function_body(code, "operatorEditing")
    check("...including anywhere inside the schedule picker", ".schedule-picker" in editing)


def main():
    test_fleet_live_waits_for_a_dropdown()
    test_backups_poll_redraws_only_runs()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
