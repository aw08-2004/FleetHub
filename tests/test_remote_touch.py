"""Pins the remote viewer's phone support: the virtual cursor, pinch-zoom and the fullscreen
fallback (issue #133).

The silent failure this exists to catch is the one the issue reported: on an iPhone the
Fullscreen button did NOTHING -- requestFullscreen is undefined on a div there, so the call
threw before its own .catch() could show a message -- and a pinch zoomed the page rather than
the picture, leaving a desktop too small to click. Every desktop browser kept working, so no
operator at a PC would ever see either break again.

Two halves, as in test_mobile_nav.py:
  * the JOINS, always: the cursor element, the CSS classes remote.js toggles, touch-action on
    the stage, and the script order on the Remote page. Rename one and the phone path dies
    quietly with every other test green.
  * the BEHAVIOUR, where `node` is on PATH: remote-touch.js is loaded against a few fake DOM
    objects and driven with pointer events, and the messages it would send to the PC are
    checked -- a tap clicks at the cursor, not at the finger. Without node this half is
    reported as skipped, not passed. There is still no browser here; the real gestures need
    a phone.
"""
import json
import os
import shutil
import subprocess
import sys

PASS = 0
FAIL = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "hub")
STATIC = os.path.join(HUB, "static")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def read(*parts):
    with open(os.path.join(HUB, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_joins():
    remote_js = read("static", "js", "remote.js")
    touch_js = read("static", "js", "remote-touch.js")
    machine_css = read("static", "css", "machine.css")
    base_css = read("static", "css", "base.css")
    partial = read("templates", "partials", "_remote_viewer.html")
    remote_html = read("templates", "remote.html")

    # Fullscreen: feature-detected, with a fallback, never an unguarded call.
    check("remote.js no longer calls stage.requestFullscreen() unguarded",
          "els.stage.requestFullscreen()" not in remote_js)
    check("remote.js knows the prefixed API (iPad Safari)",
          "webkitRequestFullscreen" in remote_js and "webkitfullscreenchange" in remote_js)
    for cls, css_name, css in (("remote-stage--pseudo-full", "machine.css", machine_css),
                               ("app-frames__frame--takeover", "base.css", base_css)):
        check(f"remote.js toggles {cls}", f"'{cls}'" in remote_js)
        check(f"{css_name} styles .{cls}", f".{cls}" in css)
    check("pagehide clears the takeover on the shell's frame",
          "if (pseudoFull) setPseudoFullscreen(false);" in
          remote_js[remote_js.index("function onPageHide"):])

    # Touch: the cursor element, the stage that refuses browser gestures, the script order.
    check("the partial carries the cursor element", 'data-remote="cursor"' in partial)
    check("remote.js looks the cursor up", "cursor: q('cursor')" in remote_js)
    stage_rule = machine_css[machine_css.index(".remote-stage {"):]
    stage_rule = stage_rule[:stage_rule.index("}")]
    check(".remote-stage has touch-action: none", "touch-action: none" in stage_rule)
    check("machine.css styles the cursor", ".remote-stage__cursor {" in machine_css)
    check("remote-touch.js exports window.RemoteTouch", "window.RemoteTouch =" in touch_js)
    touch_at = remote_html.find("js/remote-touch.js")
    check("the Remote page loads remote-touch.js before remote.js",
          0 <= touch_at < remote_html.find("js/remote.js"))
    check("remote.js drops the mouse events iOS replays after a tap",
          remote_js.count("if (replayed())") == 3)

    en = json.loads(read("locales", "en.json"))
    check("the gesture hint is in the catalog",
          bool(en["machine"]["remote"].get("touch_hint")))


# Runs inside node. Fakes just enough DOM for remote-touch.js, then prints a JSON report.
NODE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

function target(extra) {
    const handlers = {};
    return Object.assign({
        handlers,
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        fire(type, e) { (handlers[type] || []).forEach((fn) => fn(e)); },
    }, extra);
}

const window = {};
const ctx = { window, performance, setTimeout, clearTimeout, Math, console };
vm.runInNewContext(src, ctx);
const RT = window.RemoteTouch;
const out = {};

// Geometry. A 1920x1080 picture in an 800x600 box: letterboxed top and bottom.
const g = RT.geometry;
const box = g.pictureBox({ w: 800, h: 600 }, 1920, 1080);
out.box = box;
out.clampLow = g.clampView({ scale: 0.5, tx: 50, ty: 50 }, { w: 800, h: 600 });
out.clampHigh = g.clampView({ scale: 2, tx: -5000, ty: 10 }, { w: 800, h: 600 });
out.zoomCentre = g.zoomAbout({ scale: 1, tx: 0, ty: 0 }, { w: 800, h: 600 }, 2, 400, 300);
out.follow = g.followPoint({ scale: 2, tx: 0, ty: 0 }, { w: 800, h: 600 }, 600, 300, 24);

// The controller, on an 800x450 box exactly the picture's shape, so local == remote * size.
// One fresh set of fakes per controller: a disposed one still has its listeners on the
// element it was given, which in a page is fine (the element goes with it) and here would
// answer every later event twice.
const sent = [];
let firstTouch = 0;
let video, cursor;
function rig() {
    video = target({
        clientWidth: 800, clientHeight: 450, videoWidth: 1600, videoHeight: 900,
        offsetLeft: 0, offsetTop: 0, style: {}, setPointerCapture() {},
    });
    const stage = target({ getBoundingClientRect: () => ({ left: 0, top: 0 }) });
    cursor = { style: {}, hidden: true };
    return RT.create({
        stage, video, cursor, size: () => ({ w: 0, h: 0 }),
        send: (m) => sent.push(m), onFirstTouch: () => { firstTouch++; },
    });
}
const touch = rig();
const ev = (type, id, x, y, pointerType) => {
    video.fire(type, { type, pointerId: id, clientX: x, clientY: y,
        pointerType: pointerType || 'touch', preventDefault() {} });
};
const clicks = () => sent.filter((m) => m.t === 'd' || m.t === 'u');

// A mouse is not a touch: the controller must leave it entirely to remote.js.
ev('pointerdown', 9, 10, 10, 'mouse');
ev('pointerup', 9, 10, 10, 'mouse');
out.mouseIgnored = sent.length === 0 && cursor.hidden && !touch.recentlyTouched();

// Tap far from the centre: the click lands at the CURSOR (centre), not the finger.
ev('pointerdown', 1, 700, 400); ev('pointerup', 1, 700, 400);
out.tap = clicks().map((m) => [m.t, m.b, m.x, m.y]);
out.cursorShown = !cursor.hidden;
out.firstTouch = firstTouch;
out.recentlyTouched = touch.recentlyTouched();

// Drag one finger 80px right, 45px down: the cursor moves by that much of the picture.
sent.length = 0;
ev('pointerdown', 2, 100, 100); ev('pointermove', 2, 140, 120); ev('pointermove', 2, 180, 145);
ev('pointerup', 2, 180, 145);
out.dragClicks = clicks().length;
const moves = sent.filter((m) => m.t === 'm');
out.dragEnd = moves.length ? [moves[moves.length - 1].x, moves[moves.length - 1].y] : null;
out.cursorLeft = cursor.style.left;

// Two-finger tap: a right-click at the cursor.
sent.length = 0;
ev('pointerdown', 3, 300, 200); ev('pointerdown', 4, 340, 200);
ev('pointerup', 3, 300, 200); ev('pointerup', 4, 340, 200);
out.twoFingerTap = clicks().map((m) => [m.t, m.b]);

// Pinch apart about the centre: the view zooms, and nothing at all is sent to the PC.
sent.length = 0;
ev('pointerdown', 5, 380, 225); ev('pointerdown', 6, 420, 225);
ev('pointermove', 5, 360, 225); ev('pointermove', 6, 440, 225);
ev('pointerup', 5, 360, 225); ev('pointerup', 6, 440, 225);
out.pinchSent = sent.length;
out.pinchTransform = video.style.transform;

touch.reset();
out.resetTransform = video.style.transform;
touch.dispose();

// Hold still, then drag: the button goes down after the hold and up at the end.
sent.length = 0;
const touch2 = rig();
ev('pointerdown', 7, 200, 200);
setTimeout(() => {
    ev('pointermove', 7, 260, 200);
    ev('pointerup', 7, 260, 200);
    out.hold = clicks().map((m) => [m.t, m.b]);
    touch2.dispose();
    console.log(JSON.stringify(out));
}, 650);
"""


def test_behaviour():
    node = shutil.which("node")
    if not node:
        print("  [--] node not on PATH: behaviour half SKIPPED, joins only")
        return
    proc = subprocess.run(
        [node, "-e", NODE_DRIVER, "--", os.path.join(STATIC, "js", "remote-touch.js")],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        print(proc.stderr)
    check("node driver ran", proc.returncode == 0)
    if proc.returncode != 0:
        return
    r = json.loads(proc.stdout.strip().splitlines()[-1])

    near = lambda a, b: abs(a - b) < 1e-6
    check("pictureBox letterboxes a wide picture top and bottom",
          near(r["box"]["x"], 0) and near(r["box"]["w"], 800) and near(r["box"]["y"], 75)
          and near(r["box"]["h"], 450))
    check("clampView never zooms out past 1 or pans off the edge",
          r["clampLow"] == {"scale": 1, "tx": 0, "ty": 0})
    check("clampView keeps a 2x view covering its box",
          r["clampHigh"] == {"scale": 2, "tx": -800, "ty": 0})
    check("zoomAbout keeps the pinch centre still",
          r["zoomCentre"] == {"scale": 2, "tx": -400, "ty": -300})
    check("followPoint pans a cursor near the right edge back into view",
          near(r["follow"]["tx"], -(2 * 600 - (800 - 24))))

    check("a mouse pointer is left alone", r["mouseIgnored"])
    check("a tap clicks at the cursor (centre), not under the finger",
          r["tap"] == [["d", 0, 0.5, 0.5], ["u", 0, 0.5, 0.5]])
    check("the cursor appears on the first touch", r["cursorShown"] and r["firstTouch"] == 1)
    check("mouse events right after a touch are flagged as replayed", r["recentlyTouched"])
    check("a one-finger drag moves without clicking", r["dragClicks"] == 0)
    check("the cursor moves by the finger's travel, as a share of the picture",
          r["dragEnd"] is not None and near(r["dragEnd"][0], 0.5 + 80 / 800)
          and near(r["dragEnd"][1], 0.5 + 45 / 450))
    check("the cursor element follows it",
          r["cursorLeft"].endswith("px") and near(float(r["cursorLeft"][:-2]), 480))
    check("a two-finger tap is a right-click",
          r["twoFingerTap"] == [["d", 2], ["u", 2]])
    check("a pinch sends nothing to the PC", r["pinchSent"] == 0)
    check("a pinch zooms the picture", "scale(2)" in r["pinchTransform"])
    check("reset() unzooms", r["resetTransform"] == "")
    check("hold then drag presses and releases the left button",
          r["hold"] == [["d", 0], ["u", 0]])


def main():
    print("remote viewer touch + fullscreen fallback (issue #133)")
    test_joins()
    test_behaviour()
    print(f"==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
