"""Search, column filters and the phone card layout for every .data-table (hub 1.163.0).

The silent failures this file exists to catch:

  * **A filtered-out row still on screen on a phone.** The card layout sets `display: block` on
    every row, which outranks the UA's `[hidden]` rule -- without the explicit
    `tr[hidden] { display: none }` the filter "works" on a laptop and does nothing on a phone.
  * **A filter that outlives the day.** Filter state in localStorage would hide PCs tomorrow
    with nothing on screen saying why. It is sessionStorage, deliberately.
  * **A "Loading..." row filtered away**, leaving an empty table with no reason given.
  * **Devices' Export and Select all disagreeing with the screen.** Devices filters its own
    data (onChange) rather than letting the helper hide rendered rows; if it fell back to the
    auto mode, the CSV and the bulk bar would act on rows the filter had hidden.
  * **A table the helper must leave alone, enhanced anyway**: the alert card whose text is read
    as `:scope > table` (the scroll wrapper would break it), the printable report sheet, the
    header-less sensor table, the two-column ticket facts.
  * **A table class nothing styles.** events/patches/firmware built `class="table"`, which no
    stylesheet has, so they rendered as bare browser tables -- and the helper, which looks for
    .data-table, would not have found them either.

Run from the repo root.
"""
import json
import os
import re
import sys

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "hub", "static", "js")


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
    """JS with its // line comments removed -- the header names what the code must not do."""
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


def test_the_helper():
    print("\n-- table-tools.js: shape, safety, storage --")
    src = read("hub", "static", "js", "table-tools.js")
    code = code_only(src)
    check("an IIFE in strict mode", "(function () {" in code and "'use strict';" in code)
    check("no innerHTML -- cell text is whatever a host reported", "innerHTML" not in code)
    check("exposes FleetTable.enhance", "window.FleetTable = { enhance }" in code)
    check("filter state is sessionStorage", "sessionStorage" in code)
    check("...never localStorage", "localStorage" not in code)
    check("the filter row is its own row in the thead, never inside a th[data-sort]",
          "tr.className = 'data-table__filters'" in code
          and "table.tHead.appendChild(state.filterTr)" in code)
    check("a one-cell notice row is never hidden by a filter",
          "if (isNoticeRow(tr)) { tr.hidden = false; continue; }" in code)
    check("onChange mode hides nothing itself", "if (state.onChange) return;" in code)
    check("'off' and 'manual' tables are skipped by the auto scan",
          "table.dataset.tableTools === 'off'" in code and "table.dataset.tableTools === 'manual'" in code)
    check("the card class is added only to tables it enhanced",
          "table.classList.add('data-table--cards')" in code)


def test_loaded_on_every_page():
    print("\n-- base.html loads it before page scripts --")
    base = read("hub", "templates", "base.html")
    i = base.find("js/table-tools.js")
    check("base.html loads table-tools.js", i >= 0)
    check("...after i18n.js and common.js (it calls t() and tPlural())",
          base.find("js/i18n.js") < i and base.find("js/common.js") < i)
    check("...before page_scripts (Devices calls enhance at load)",
          i < base.find("{% block page_scripts %}"))


def test_catalog():
    print("\n-- every string it shows is in the catalog --")
    en = json.loads(read("hub", "locales", "en.json"))
    flat = {}

    def walk(prefix, node):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(key, v)
            else:
                flat[key] = v

    walk("", en)
    code = code_only(read("hub", "static", "js", "table-tools.js"))
    for key in sorted(set(re.findall(r"\bt\('([a-z0-9_.]+)'", code))):
        check(f"{key} is in en.json", key in flat)
    for key in sorted(set(re.findall(r"\btPlural\('([a-z0-9_.]+)'", code))):
        check(f"{key} has .one and .other", f"{key}.one" in flat and f"{key}.other" in flat)


def test_css():
    print("\n-- the card layout and the toolbar can actually hide things --")
    css = read("hub", "static", "css", "components.css")
    check("a filtered-out card is hidden on a phone",
          re.search(r"\.data-table--cards tbody tr\[hidden\]\s*\{\s*display:\s*none", css) is not None)
    check("hidden toolbar buttons stay hidden (.btn is inline-flex)",
          re.search(r"\.table-tools \[hidden\]", css) is not None)
    check("the card layout keys off .data-table--cards, at the 640px breakpoint",
          re.search(r"@media \(max-width: 640px\) \{\s*\.data-table--cards", css) is not None)
    check("Devices' stack rule no longer hides the thead the filter row lives in",
          ".data-table--stack:not(.data-table--cards) thead" in css)


def test_devices_filters_its_data():
    print("\n-- Devices: the filters narrow the data, so count/select-all/export follow --")
    page = read("hub", "templates", "inventory.html")
    js = code_only(read("hub", "static", "js", "inventory.js"))
    check("the table is data-table-tools=\"manual\"", 'data-table-tools="manual"' in page)
    check("inventory.js calls FleetTable.enhance with onChange",
          "FleetTable.enhance(" in js and "onChange(state)" in js)
    check("...and no second search box", "search: false" in js)
    check("matchesSearch consults the column filters",
          "columnText(row, key).toLowerCase().includes(value)" in js)


def test_opt_outs_and_modes():
    print("\n-- tables the helper leaves alone, and pages with their own search --")
    off = {
        "alerts.js": "table.dataset.tableTools = 'off'",
        "machine.js": "table.dataset.tableTools = 'off'",
        "report-sheet.js": "tbl.dataset.tableTools = 'off'",
    }
    for name, needle in off.items():
        check(f"{name} opts its table out", needle in read("hub", "static", "js", name))
    check("ticket.html's facts table opts out",
          'data-table-tools="off"' in read("hub", "templates", "ticket.html"))
    for name in ("audit.js", "users.js", "processes.js", "events.js"):
        check(f"{name} gets filters only (it has its own search)",
              "dataset.tableTools = 'filters'" in read("hub", "static", "js", name))
    check("tickets.html gets filters only",
          'data-table-tools="filters"' in read("hub", "templates", "tickets.html"))


def test_no_unstyled_table_class():
    print("\n-- no table is built with a class nothing styles --")
    bad = []
    for name in sorted(os.listdir(JS)):
        if name.endswith(".js"):
            with open(os.path.join(JS, name), encoding="utf-8") as fh:
                if "el('table', 'table')" in fh.read():
                    bad.append(name)
    check("no el('table', 'table') left" + (f" ({', '.join(bad)})" if bad else ""), not bad)


def main():
    test_the_helper()
    test_loaded_on_every_page()
    test_catalog()
    test_css()
    test_devices_filters_its_data()
    test_opt_outs_and_modes()
    test_no_unstyled_table_class()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
