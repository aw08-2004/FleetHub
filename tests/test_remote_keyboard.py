"""Pins the remote viewer's phone keyboard (issue #138).

The silent failure this exists to catch is the one the issue reported: a phone could watch a
PC and click on it, and never type a character into it. Keys only ever came from keydown on
the focused <video>, which no phone keyboard is attached to -- and every desktop browser,
with a real keyboard, kept working, so nobody at a PC would see it break again.

A second, quieter one: typed text must reach the agent as a CHARACTER (code '', key 'ä'),
which InputInjector injects with KEYEVENTF_UNICODE. Sent as a key position (code 'KeyZ') it
would type whatever the PC's layout has there -- a German phone's z arriving as y -- and @, €
and umlauts would not arrive at all. Nothing would error; the wrong letters would just appear.

Two halves, as in test_remote_touch.py:
  * the JOINS, always: the elements in the partial, the script order on both pages that
    borrow the viewer, the CSS, and the catalog keys.
  * the BEHAVIOUR, where `node` is on PATH: remote-keyboard.js is loaded against a few fake
    DOM objects and driven with input events, and the messages it would send to the PC are
    checked. Without node this half is reported as skipped, not passed. A real phone
    keyboard is still owed (ROADMAP.MD, Owed locally).
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent / "hub"
KBD_JS = HUB / "static" / "js" / "remote-keyboard.js"

PASS = 0
FAIL = 0


def read(*parts):
    return HUB.joinpath(*parts).read_text(encoding="utf-8")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def test_joins():
    remote_js = read("static", "js", "remote.js")
    kbd_js = read("static", "js", "remote-keyboard.js")
    machine_css = read("static", "css", "machine.css")
    partial = read("templates", "partials", "_remote_viewer.html")

    for el in ("kbd", "kbd-sink", "kbd-strip"):
        check(f"the partial carries data-remote=\"{el}\"", f'data-remote="{el}"' in partial)
        check(f"remote.js looks up {el}", f"q('{el}')" in remote_js)
    for mod in ("Control", "Alt", "Meta"):
        check(f"the strip has the {mod} sticky key", f'data-kbd-mod="{mod}"' in partial)
        check(f"remote-keyboard.js knows {mod}", f"{mod}: {{" in kbd_js)
    for key in ("Escape", "Tab", "Delete", "ArrowLeft", "ArrowUp", "ArrowDown", "ArrowRight"):
        check(f"the strip's {key} is a key remote-keyboard.js sends",
              f'data-kbd-key="{key}"' in partial and f"{key}: {{" in kbd_js)

    check("remote-keyboard.js exports window.RemoteKeyboard",
          "window.RemoteKeyboard =" in kbd_js)
    check("remote.js sends the keyboard's keys through sendInput (view-only honoured)",
          "send: sendInput," in remote_js[remote_js.index("RemoteKeyboard.create"):][:400])
    check("remote.js disposes the keyboard with the viewer",
          "keyboard.dispose()" in remote_js[remote_js.index("function dispose"):])
    for page in ("remote.html", "sharing.html"):
        html = read("templates", page)
        at = html.find("js/remote-keyboard.js")
        check(f"{page} loads remote-keyboard.js before remote.js",
              0 <= at < html.find("js/remote.js"))

    # The sink must stay RENDERED to take focus, and typeable under the stage's
    # user-select: none -- either mistake and the phone keyboard never opens.
    sink_rule = machine_css[machine_css.index(".remote-kbd__sink {"):]
    sink_rule = sink_rule[:sink_rule.index("}")]
    check("the sink is not display:none", "display: none" not in sink_rule)
    check("the sink is 16px, or iOS zooms the page into it", "font-size: 16px" in sink_rule)
    check("the sink re-enables user-select under the stage", "user-select: text" in sink_rule)
    check("the partial's sink is not [hidden]",
          "hidden" not in partial[partial.index('data-remote="kbd-sink"'):].split(">")[0])
    for cls in (".remote-kbd__fab[hidden]", ".remote-kbd__strip[hidden]", ".remote-kbd__key["):
        check(f"machine.css styles {cls}", cls in machine_css)

    keys = ("keyboard", "keyboard_title", "keyboard_keys", "key_esc", "key_tab", "key_ctrl",
            "key_alt", "key_win", "key_del", "key_left", "key_up", "key_down", "key_right")
    for lang in ("en", "de", "es"):
        remote = json.loads(read("locales", f"{lang}.json"))["machine"]["remote"]
        check(f"{lang}.json has every keyboard string", all(remote.get(k) for k in keys))


# Runs inside node. Fakes just enough DOM for remote-keyboard.js, then prints a JSON report.
NODE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

function target(extra) {
    const handlers = {};
    const attrs = {};
    return Object.assign({
        handlers, attrs, dataset: {}, style: {}, hidden: false,
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        removeEventListener() {},
        fire(type, e) { (handlers[type] || []).forEach((fn) => fn(e)); },
        setAttribute(k, v) { attrs[k] = String(v); },
        getAttribute(k) { return k in attrs ? attrs[k] : null; },
        setPointerCapture() {},
    }, extra);
}

const store = {};
const document = { activeElement: null };
const window = {
    localStorage: {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); },
    },
};
const ctx = { window, document, Math, JSON, console, isFinite, Number };
vm.runInNewContext(src, ctx);
const RK = window.RemoteKeyboard;
const out = {};

const stage = target({ clientWidth: 400, clientHeight: 800,
    getBoundingClientRect: () => ({ bottom: 800 }) });
const button = target({ offsetWidth: 44, offsetHeight: 44, offsetLeft: 0, offsetTop: 378 });
const sink = target({ value: '' });
sink.focus = () => { document.activeElement = sink; sink.fire('focus', {}); };
sink.blur = () => { document.activeElement = null; sink.fire('blur', {}); };
const mods = {};
for (const m of ['Control', 'Alt', 'Meta']) {
    mods[m] = target({ closest: null });
    mods[m].dataset.kbdMod = m;
}
const strip = target({
    querySelector: (sel) => {
        const m = /data-kbd-mod="(\w+)"/.exec(sel);
        return m ? mods[m[1]] : null;
    },
});
const stripKey = (value) => {
    const el = target();
    el.dataset.kbdKey = value;
    el.closest = () => el;
    return el;
};
for (const m of Object.values(mods)) m.closest = () => m;

// send is remote.js's sendInput, which View-only silences; release is its sendControl.
let viewOnly = false;
const sent = [];
const released = [];
const kb = RK.create({ stage, button, sink, strip, storageKey: 'kbd',
    send: (m) => { if (!viewOnly) sent.push(m); },
    release: (m) => { released.push(m); sent.push(m); } });
const pe = (type, x, y) => button.fire(type, { type, pointerId: 1, clientX: x, clientY: y,
    preventDefault() {}, stopPropagation() {} });
const beforeinput = (inputType, data) => {
    let prevented = false;
    sink.fire('beforeinput', { inputType, data, preventDefault() { prevented = true; } });
    return prevented;
};
const tapStrip = (el) => strip.fire('pointerdown', { target: el,
    preventDefault() {}, stopPropagation() {} });
const brief = () => sent.map((m) => [m.code, m.key, m.down]);

out.hiddenBeforeShow = button.hidden === false ? 'unset' : button.hidden;
kb.show();
out.shownAfterShow = button.hidden === false;
out.defaultLeft = button.style.left;
out.defaultTop = button.style.top;

// A tap opens the keyboard (focuses the sink) and shows the strip.
pe('pointerdown', 20, 400); pe('pointerup', 20, 400);
out.tapFocused = document.activeElement === sink;
out.stripShown = strip.hidden === false;

// Typing: characters go out as characters, not key positions.
sent.length = 0;
out.prevented = beforeinput('insertText', '@ä');
out.typed = brief();
sent.length = 0;
beforeinput('insertText', '\u{1F600}');
out.emoji = sent.length;

// A paste carries its text in dataTransfer with data null (Safari's autocorrect does too).
sent.length = 0;
sink.fire('beforeinput', { inputType: 'insertFromPaste', data: null,
    dataTransfer: { getData: (t) => (t === 'text/plain' ? 'ok' : '') },
    preventDefault() {} });
out.paste = brief();
sent.length = 0;
beforeinput('deleteContentBackward', null);
beforeinput('insertLineBreak', null);
out.editing = brief();

// Fallback: an input event with no beforeinput before it.
sent.length = 0;
sink.value = 'x';
sink.fire('input', {});
out.inputFallback = brief();
out.sinkCleared = sink.value === '';

// Ctrl (sticky) then c: Ctrl down, KeyC down/up, Ctrl up -- Ctrl+C, not a Unicode 'c'.
sent.length = 0;
released.length = 0;
tapStrip(mods.Control);
out.ctrlPressed = mods.Control.attrs['aria-pressed'];
out.latchSendsNothing = sent.length === 0;
beforeinput('insertText', 'c');
out.ctrlC = brief();
out.ctrlUpViaRelease = released.map((m) => [m.code, m.down]);
out.ctrlReleased = mods.Control.attrs['aria-pressed'];

// Ctrl then '-': Ctrl+Minus (zoom out), by key position like a letter.
sent.length = 0;
tapStrip(mods.Control);
beforeinput('insertText', '-');
out.ctrlMinus = brief();
// Without a modifier the same '-' is just a character.
sent.length = 0;
beforeinput('insertText', '-');
out.plainMinus = brief();

// A strip key.
sent.length = 0;
tapStrip(stripKey('Escape'));
out.esc = brief();

// A swipe along the strip must not reach the stage, whose touchmove refuser would stop it
// scrolling on a narrow phone.
let stopped = false;
strip.fire('touchmove', { stopPropagation() { stopped = true; }, preventDefault() {} });
out.stripSwipeKept = stopped;

// Win latched and then let go unused -- tapped twice, or the keyboard closed: nothing at all
// reaches the PC. A lone Win down-up there opens the Start menu.
sent.length = 0;
tapStrip(mods.Meta);
tapStrip(mods.Meta);
out.winToggled = brief();
tapStrip(mods.Meta);
pe('pointerdown', 20, 400); pe('pointerup', 20, 400);   // second tap closes the keyboard
out.closed = document.activeElement === null && strip.hidden === true;
out.blurRelease = brief();
out.blurUnlatched = mods.Meta.attrs['aria-pressed'];

// Drag: moves, does not toggle the keyboard, and is remembered.
pe('pointerdown', 20, 400); pe('pointermove', 200, 400); pe('pointerup', 200, 400);
out.dragNoFocus = document.activeElement === null;
out.saved = store.kbd ? JSON.parse(store.kbd) : null;
out.dragLeft = button.style.left;

// View-only ticked with Ctrl latched: the box is checked BEFORE setEnabled(false) runs.
// Nothing was ever pressed on the PC, so nothing can be stranded there.
pe('pointerdown', 200, 400); pe('pointerup', 200, 400);
tapStrip(mods.Control);
sent.length = 0;
viewOnly = true;
kb.setEnabled(false);
out.viewOnlyHidden = button.hidden === true;
out.viewOnlySent = sent.length;
out.viewOnlyUnlatched = mods.Control.attrs['aria-pressed'];

console.log(JSON.stringify(out));
"""


def test_behaviour():
    node = shutil.which("node")
    if not node:
        print("  [--] node not on PATH: behaviour half SKIPPED, joins only")
        return
    proc = subprocess.run(
        [node, "-e", NODE_DRIVER, "--", str(KBD_JS)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    if proc.returncode != 0:
        print(proc.stderr)
    check("node driver ran", proc.returncode == 0)
    if proc.returncode != 0:
        return
    r = json.loads(proc.stdout.strip().splitlines()[-1])

    check("show() reveals the button", r["shownAfterShow"])
    check("the button starts on the left edge, mid-height",
          r["defaultLeft"] == "0px" and r["defaultTop"] == "378px")
    check("a tap opens the keyboard", r["tapFocused"])
    check("the strip appears with the keyboard", r["stripShown"])
    check("typed text is taken over from the browser", r["prevented"])
    check("typed characters go out as characters (Unicode path), not key positions",
          r["typed"] == [["", "@", True], ["", "@", False], ["", "ä", True], ["", "ä", False]])
    check("a paste is read from dataTransfer when data is null",
          r["paste"] == [["", "o", True], ["", "o", False], ["", "k", True], ["", "k", False]])
    check("an emoji the agent cannot inject is dropped, not sent as half a pair",
          r["emoji"] == 0)
    check("backspace and return become Backspace and Enter",
          r["editing"] == [["Backspace", "Backspace", True], ["Backspace", "Backspace", False],
                           ["Enter", "Enter", True], ["Enter", "Enter", False]])
    check("an input event without beforeinput still types", r["inputFallback"]
          == [["", "x", True], ["", "x", False]])
    check("the hidden box is emptied after every event", r["sinkCleared"])
    check("Ctrl on the strip latches", r["ctrlPressed"] == "true")
    check("latching sends nothing to the PC until the next key", r["latchSendsNothing"])
    check("the modifier's key-up goes through release, which View-only does not stop",
          r["ctrlUpViaRelease"] == [["ControlLeft", False]])
    check("Ctrl then c is Ctrl+C by key position, and Ctrl is let go after",
          r["ctrlC"] == [["ControlLeft", "Control", True],
                         ["KeyC", "c", True], ["KeyC", "c", False],
                         ["ControlLeft", "Control", False]])
    check("the latch clears after one key", r["ctrlReleased"] == "false")
    check("Ctrl then - is Ctrl+Minus by key position, not a Unicode '-'",
          r["ctrlMinus"] == [["ControlLeft", "Control", True],
                             ["Minus", "-", True], ["Minus", "-", False],
                             ["ControlLeft", "Control", False]])
    check("a plain - with no modifier still goes out as a character",
          r["plainMinus"] == [["", "-", True], ["", "-", False]])
    check("a swipe on the strip is kept from the stage's touchmove refuser",
          r["stripSwipeKept"])
    check("Esc on the strip sends Escape",
          r["esc"] == [["Escape", "Escape", True], ["Escape", "Escape", False]])
    check("a second tap closes the keyboard and hides the strip", r["closed"])
    check("Win tapped on and off again sends nothing (no Start menu)", r["winToggled"] == [])
    check("closing the keyboard with Win latched sends nothing (no Start menu)",
          r["blurRelease"] == [])
    check("closing the keyboard clears the latch", r["blurUnlatched"] == "false")
    check("dragging the button does not toggle the keyboard", r["dragNoFocus"])
    check("the dragged position is remembered as fractions",
          r["saved"] is not None and 0 < r["saved"]["x"] <= 1)
    check("the button follows the drag", r["dragLeft"] == "180px")
    check("view-only hides the button", r["viewOnlyHidden"])
    check("ticking View-only with Ctrl latched sends nothing and clears the latch",
          r["viewOnlySent"] == 0 and r["viewOnlyUnlatched"] == "false")


def main():
    print("remote viewer phone keyboard (issue #138)")
    test_joins()
    test_behaviour()
    print(f"==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
