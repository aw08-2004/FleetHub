"""Pins the remote viewer's text clipboard sync and its Cmd-as-Ctrl option (issue #148).

The silent failure this exists to catch is **a paste that never arrives, with no error
anywhere**. The hub relay refuses an upload batch holding any message over
MAX_UP_MESSAGE_BYTES -- the whole batch, the keystrokes queued with it included -- and it
measures the message as Python's json.dumps writes it, which turns every non-ASCII character
into a six-byte escape. A chunker that counted UTF-8 bytes, or characters, would pass every
English test on WebRTC and drop a German or Japanese paste on the relay. So the chunks
remote-clipboard.js really produces are pushed through remote_relay.push_up here.

The quieter ones:
  * the offer flag. The viewer shows its clipboard controls only when the agent's offer says
    `clipboard`; lose the flag on either side and the feature disappears for every operator.
  * the agent's limits. MAX_CHARS and the `{"t":"clip"` prefix the agent matches on are
    written twice, once per language; this reads both files rather than trusting either.
  * the Mac. Cmd arrives as MetaLeft, which the PC reads as the Windows key -- the likely
    reason the reporter's Ctrl+C "did nothing" inside the session.

Two halves, as in test_remote_keyboard.py: the JOINS always, the BEHAVIOUR where `node` is on
PATH (reported as skipped otherwise, not passed). A real browser on a real PC is still owed.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUB = ROOT / "hub"
AGENT_REMOTE = ROOT / "agent" / "src" / "TempMonitorAgent" / "Remote"
CLIP_JS = HUB / "static" / "js" / "remote-clipboard.js"

sys.path.insert(0, str(HUB))
import remote_relay as rr  # noqa: E402

PASS = 0
FAIL = 0


def read(path):
    return Path(path).read_text(encoding="utf-8")


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def js_const(src, name):
    m = re.search(rf"const {name} = (\d+);", src)
    return int(m.group(1)) if m else None


def test_joins():
    print("joins")
    clip_js = read(CLIP_JS)
    remote_js = read(HUB / "static" / "js" / "remote.js")
    partial = read(HUB / "templates" / "partials" / "_remote_viewer.html")
    machine_css = read(HUB / "static" / "css" / "machine.css")
    bridge_cs = read(AGENT_REMOTE / "ClipboardBridge.cs")
    helper_cs = read(AGENT_REMOTE / "RemoteHelper.cs")
    peer_cs = read(AGENT_REMOTE / "RemotePeer.cs")

    chunk = js_const(clip_js, "CHUNK_BYTES")
    check("a chunk is capped below the relay's per-message limit",
          chunk is not None and chunk < rr.MAX_UP_MESSAGE_BYTES)
    max_chars = js_const(clip_js, "MAX_CHARS")
    agent_max = re.search(r"public const int MaxChars = ([\d_]+);", bridge_cs)
    check("the viewer and the agent agree on the most text a paste carries",
          agent_max is not None and max_chars == int(agent_max.group(1).replace("_", "")))
    check("the agent recognises a chunk by the prefix the viewer's object literal produces",
          '"{\\"t\\":\\"clip\\""' in helper_cs and "{ t: 'clip', id, i, n" in clip_js)

    check("the agent's offer carries the clipboard flag",
          "clipboard = clipboardCapable" in peer_cs)
    check("...and only when the clipboard thread actually started",
          "clipboardCapable: clipboard.IsReady" in helper_cs)
    check("the viewer reads the flag from the offer",
          "clipboard.setCapable(!!sig.payload?.clipboard)" in remote_js)
    check("teardown hides the controls again", "clipboard.setCapable(false)" in remote_js)
    check("a copy on the PC is handed to the controller",
          "msg.t === 'clip'" in remote_js and "clipboard.received(msg)" in remote_js)

    for el in ("clip-copy", "clip-paste", "cmd-ctrl", "cmd-ctrl-wrap"):
        check(f"the partial carries data-remote=\"{el}\"", f'data-remote="{el}"' in partial)
        check(f"remote.js looks up {el}", f"q('{el}')" in remote_js)
    for el in ("clip-copy", "clip-paste", "cmd-ctrl-wrap"):
        tag = partial[partial.index(f'data-remote="{el}"'):].split(">")[0]
        check(f"{el} starts hidden", "hidden" in tag)
    check("a hidden .remote-toggle really is hidden", ".remote-toggle[hidden]" in machine_css)

    for page in ("remote.html", "sharing.html"):
        html = read(HUB / "templates" / page)
        at = html.find("js/remote-clipboard.js")
        check(f"{page} loads remote-clipboard.js before remote.js",
              0 <= at < html.find("js/remote.js"))

    # Input held behind a paste must still pass the View-only gate, and the paste itself must
    # not be held behind the keys it is releasing.
    send_input = remote_js[remote_js.index("function sendInput"):]
    send_input = send_input[:send_input.index("\n        }\n")]
    check("sendInput checks View only before it holds anything",
          send_input.index("els.viewOnly.checked") < send_input.index("heldInput"))
    check("the clipboard controller sends on sendControl, not the held sendInput",
          "send: sendControl," in remote_js[remote_js.index("RemoteClipboard.create"):][:200])
    check("Ctrl+V reads the clipboard before the V is sent",
          remote_js.index("pasteThenRelease();") < remote_js.index("sendKey(e, true);"))
    check("auto-repeat does not re-read the clipboard", "!e.repeat" in remote_js)

    remap = remote_js[remote_js.index("function remapCode"):][:400]
    check("Cmd is remapped only while the box is ticked",
          "if (!els.cmdCtrl.checked) return code;" in remap)
    check("MetaLeft becomes ControlLeft", "'MetaLeft') return 'ControlLeft'" in remap)
    check("keys held under a remapped Cmd are released with it (macOS sends no keyup)",
          "metaChord.forEach" in remote_js)

    keys = ("copy_from_pc", "copy_from_pc_title", "paste_to_pc", "paste_to_pc_title",
            "cmd_as_ctrl", "cmd_as_ctrl_title", "clip_copied", "clip_copy_ready", "clip_sent",
            "clip_truncated", "clip_read_denied", "clip_write_denied")
    for lang in ("en", "de", "es"):
        remote = json.loads(read(HUB / "locales" / f"{lang}.json"))["machine"]["remote"]
        missing = [k for k in keys if not remote.get(k)]
        check(f"{lang}.json has every clipboard string", not missing)


# Runs inside node: loads remote-clipboard.js against fake buttons and a fake clipboard, then
# prints a JSON report. The chunks go back to Python, which measures them the way the relay does.
NODE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

function button() {
    const handlers = {};
    const classes = new Set();
    return {
        hidden: false, disabled: false, handlers,
        classList: { toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); },
                     has: (c) => classes.has(c) },
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        click() { return Promise.all((handlers.click || []).map((fn) => fn({}))); },
    };
}

const window = {};
const ctx = { window, navigator: {}, t: (k) => k, setTimeout, Promise, Date, JSON, Math, console };
vm.runInNewContext(src, ctx);
const RC = window.RemoteClipboard;

(async () => {
    const out = {};
    const samples = {
        ascii: 'x'.repeat(RC.MAX_CHARS),
        control: '\u0001'.repeat(RC.MAX_CHARS),
        german: 'Grüße aus Köln, "zitiert" \\ Backslash\n'.repeat(1500),
        cjk: '日本語のテキスト'.repeat(4000),
        emoji: 'a\u{1F600}'.repeat(20000),
        empty: '',
    };
    out.chunks = {};
    for (const [name, text] of Object.entries(samples)) {
        out.chunks[name] = { text, msgs: RC.chunks(text, 42) };
    }
    out.truncSurrogate = RC.truncate('a\u{1F600}b', 2);

    // The controller.
    let viewOnly = false;
    const sent = [];
    const hints = [];
    let local = 'from here';
    let writeOk = true;
    let readHangs = false;
    const clipboard = {
        writeText: async (s) => { if (!writeOk) throw new Error('denied'); local = s; },
        readText: () => readHangs ? new Promise(() => {}) : Promise.resolve(local),
    };
    const copyButton = button();
    const pasteButton = button();
    const c = RC.create({ send: (m) => sent.push(m), viewOnly: () => viewOnly,
        hint: (h) => hints.push(h), copyButton, pasteButton, clipboard });

    out.hiddenBeforeOffer = copyButton.hidden && pasteButton.hidden;
    out.ignoredBeforeOffer = (await c.pasteBeforeKeys(), sent.length === 0);
    c.setCapable(true);
    out.shownAfterOffer = !copyButton.hidden && !pasteButton.hidden;

    // PC -> here, browser allows the write.
    await c.received({ t: 'clip', s: 'copied on pc', trunc: false });
    out.autoWritten = local === 'copied on pc';
    out.autoHint = hints[hints.length - 1];
    out.copyIdleAfterWrite = copyButton.disabled;

    // PC -> here, browser refuses until a click.
    writeOk = false;
    await c.received({ t: 'clip', s: 'second', trunc: true });
    out.refusedHint = hints[hints.length - 1];
    out.copyLitUp = !copyButton.disabled && copyButton.classList.has('btn--primary');
    writeOk = true;
    await copyButton.click();
    out.clickWrote = local === 'second';
    out.copyIdleAfterClick = copyButton.disabled;

    // here -> PC on Ctrl+V; a repeat of the same text is not resent.
    local = 'pasted here';
    sent.length = 0;
    await c.pasteBeforeKeys();
    out.pasteSent = sent.map((m) => m.s).join('');
    out.pasteMsgType = sent.length && sent[0].t;
    sent.length = 0;
    await c.pasteBeforeKeys();
    out.repeatNotResent = sent.length === 0;
    await pasteButton.click();
    out.buttonResends = sent.map((m) => m.s).join('') === 'pasted here';

    // View only stops a paste.
    viewOnly = true;
    c.refresh();
    out.pasteButtonDisabledInViewOnly = pasteButton.disabled;
    sent.length = 0;
    local = 'not allowed';
    await pasteButton.click();
    out.viewOnlyBlocks = sent.length === 0;
    viewOnly = false;

    // A read stuck on a permission prompt does not hold the keys forever.
    readHangs = true;
    const t0 = Date.now();
    await c.pasteBeforeKeys();
    out.timeoutMs = Date.now() - t0;

    // Teardown.
    c.setCapable(false);
    out.hiddenAfterTeardown = copyButton.hidden && pasteButton.hidden;

    process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_behaviour():
    print("behaviour")
    node = shutil.which("node")
    if not node:
        print("  [--] node not on PATH: behaviour half SKIPPED, joins only")
        return
    proc = subprocess.run([node, "-e", NODE_DRIVER, "--", str(CLIP_JS)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        print(proc.stderr)
    check("node driver ran", proc.returncode == 0)
    if proc.returncode != 0:
        return
    r = json.loads(proc.stdout)

    for name, sample in r["chunks"].items():
        msgs, text = sample["msgs"], sample["text"]
        check(f"{name}: the chunks join back to the original text",
              "".join(m["s"] for m in msgs) == text)
        check(f"{name}: every chunk numbers itself i of n under one id",
              all(m["t"] == "clip" and m["id"] == 42 and m["n"] == len(msgs) and m["i"] == i
                  for i, m in enumerate(msgs)))
        sizes = [len(json.dumps(m, separators=(",", ":"))) for m in msgs]
        check(f"{name}: every chunk fits the relay as json.dumps measures it "
              f"(largest {max(sizes)} of {rr.MAX_UP_MESSAGE_BYTES})",
              max(sizes) <= rr.MAX_UP_MESSAGE_BYTES)
        try:
            # None means "no relay open" -- reached only once every message passed the checks.
            accepted = rr.push_up("no-such-session", msgs) is None
        except ValueError:
            accepted = False
        check(f"{name}: remote_relay.push_up takes the whole paste", accepted)
        check(f"{name}: no chunk ends on half a surrogate pair",
              all(not (m["s"] and 0xD800 <= ord(m["s"][-1]) <= 0xDBFF) for m in msgs))
    check("a worst-case paste stays well inside the agent's chunk limit (1024)",
          len(r["chunks"]["control"]["msgs"]) < 200)
    check("truncate does not split a surrogate pair", r["truncSurrogate"] == "a")

    check("controls stay hidden until the offer says the agent can answer",
          r["hiddenBeforeOffer"])
    check("nothing is sent before the offer", r["ignoredBeforeOffer"])
    check("controls appear once the agent can answer", r["shownAfterOffer"])
    check("a copy on the PC lands on the local clipboard when the browser allows it",
          r["autoWritten"] and r["autoHint"] == "machine.remote.clip_copied")
    check("...and leaves Copy from PC idle", r["copyIdleAfterWrite"])
    check("a refused write says so and lights up Copy from PC",
          r["refusedHint"].startswith("machine.remote.clip_copy_ready") and r["copyLitUp"])
    check("a truncated copy says it was cut short",
          r["refusedHint"].endswith("machine.remote.clip_truncated"))
    check("the click writes the PC's text", r["clickWrote"] and r["copyIdleAfterClick"])
    check("Ctrl+V sends the local clipboard as clip chunks",
          r["pasteSent"] == "pasted here" and r["pasteMsgType"] == "clip")
    check("an unchanged clipboard is not resent on the next Ctrl+V", r["repeatNotResent"])
    check("Paste to PC resends on purpose", r["buttonResends"])
    check("View only disables Paste to PC", r["pasteButtonDisabledInViewOnly"])
    check("View only blocks a paste", r["viewOnlyBlocks"])
    check(f"a hung clipboard read releases the keys after the timeout ({r['timeoutMs']} ms)",
          1000 <= r["timeoutMs"] < 4000)
    check("teardown hides the controls", r["hiddenAfterTeardown"])


def main():
    print("remote clipboard sync and Cmd as Ctrl (issue #148)")
    test_joins()
    test_behaviour()
    print(f"==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
