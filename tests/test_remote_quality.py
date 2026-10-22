"""Pins how a relayed remote screen keeps up with a slow link (roadmap #2, hub 1.164.0): the
Auto preset (remote-quality.js) and the viewer's freeze-and-jump (remote-relay.js).

The silent failures this exists to catch are all "the picture is up, so it must be fine":
  * a phone on mobile data that keeps PLAYING seconds-old motion -- the viewer decoding every
    stale delta it is handed instead of holding still until a fresh keyframe;
  * Auto that never steps down (the lag header renamed, the callback never wired), so a slow
    link stays slow, or that steps down and never comes back, so a session that hit one bad
    patch stays at 0.4 Mbps for an hour;
  * Auto that oscillates -- every rung change rebuilds the agent's encoder and costs a
    keyframe, so a controller that flaps is itself the stutter it was meant to cure.
None of those fails a request or logs an error.

Two halves, as in test_remote_touch.py:
  * the JOINS, always: the Auto option is the default, the scripts load before remote.js, the
    hub sends the lag header the viewer reads, the catalog has the strings.
  * the BEHAVIOUR, where `node` is on PATH: the controller is driven with lag samples on a
    fake clock, and remote-relay.js with fake fetch / WebCodecs objects. Without node this half
    is reported as skipped, not passed.
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
JS = os.path.join(HUB, "static", "js")


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
    print("joins")
    partial = read("templates", "partials", "_remote_viewer.html")
    remote_js = read("static", "js", "remote.js")
    relay_js = read("static", "js", "remote-relay.js")
    web = read("remote_web.py")
    check("Auto is the preset a new viewer starts on",
          '<option value="auto" selected>' in partial and 'value="balanced" selected' not in partial)
    for page in ("remote.html", "sharing.html"):
        html = read("templates", page)
        at = html.find("js/remote-quality.js")
        check(f"{page} loads remote-quality.js before remote.js",
              0 <= at < html.find("js/remote.js"))
    check("remote.js feeds the relay's lag to the controller", "onLag: onRelayLag" in remote_js)
    check("the hub sends the header the viewer reads",
          'resp.headers["X-Relay-Lag-Ms"]' in web and "'X-Relay-Lag-Ms'" in relay_js)
    for lang in ("en", "de", "es"):
        remote = json.loads(read("locales", f"{lang}.json"))["machine"]["remote"]
        check(f"{lang}: the Auto label and its rung text are in the catalog",
              bool(remote.get("preset_auto")) and "{kbps}" in remote.get("auto_rung", ""))


QUALITY_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const window = {};
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'),
                   { window, Math, Number, Object });
const RQ = window.RemoteQuality;
const out = { ladder: RQ.LADDER, start: RQ.START };

// Feed `lag` every 200 ms for `ms`, from `t`; returns [new t, list of rungs moved to].
function feed(q, t, ms, lag) {
    const moves = [];
    for (let end = t + ms; t < end; t += 200) {
        if (q.sample(lag, t)) moves.push(q.rung());
    }
    return [t, moves];
}

let q = RQ.create();
let t = 0, m;
out.startRung = q.rung();
out.startSettings = q.settings();

// One late batch among good ones is noise.
q.sample(50, 0); q.sample(2500, 200); q.sample(50, 400); q.sample(50, 600);
out.oneSpike = q.rung();

// Sustained lag: steps down once, then waits out the cooldown before the next step.
q = RQ.create(); t = 0;
[t, m] = feed(q, t, 4800, 2000);
out.firstDown = m;
[t, m] = feed(q, t, 10000, 2000);
out.thenDown = m;
[t, m] = feed(q, t, 60000, 3000);
out.floor = q.rung();
out.floorSettings = q.settings();

// Calm: back up one rung per UP_AFTER window, never past the top.
q = RQ.create(); t = 0;
[t, m] = feed(q, t, 3000, 2000);
out.downBeforeRecover = q.rung();
[t, m] = feed(q, t, 14000, 50);
out.notYetUp = q.rung();
[t, m] = feed(q, t, 2000, 50);
out.upAfterCalm = q.rung();
[t, m] = feed(q, t, 120000, 50);
out.top = q.rung();

// A failed step up (straight back down) doubles the wait before the next one.
q = RQ.create(); t = 0;
[t, m] = feed(q, t, 1000, 2000);           // down to rung 2
[t, m] = feed(q, t, 16000, 50);            // calm: up to rung 1
out.upOnce = q.rung();
[t, m] = feed(q, t, 6000, 2000);           // the link could not carry it: down again
out.backDown = q.rung();
[t, m] = feed(q, t, 20000, 50);            // 20 s calm is no longer enough...
out.heldAfterFailedUp = q.rung();
[t, m] = feed(q, t, 12000, 50);            // ...30 s is
out.upAfterLongerWait = q.rung();

// reset() forgets the last session's link.
q.reset();
out.reset = q.rung();
out.nanIgnored = q.sample(NaN, 999999) === null;
console.log(JSON.stringify(out));
"""


RELAY_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

function frame(kind, key, text) {
    const payload = Buffer.from(text);
    const b = Buffer.alloc(6 + payload.length);
    b[0] = kind; b[1] = key ? 1 : 0; b.writeUInt32BE(payload.length, 2); payload.copy(b, 6);
    return b;
}
const V = (text, key) => frame(1, key, text);
const C = (text) => frame(2, false, text);

// Each batch is [lagHeader, records...]. SCENARIO picks the run.
const SCENARIOS = {
    mixed: [
        [50,   V('K1', true), V('d1')],
        [1500, V('d2'), V('d3')],
        [1600, V('d4')],
        [100,  C('{"t":"geom","w":1,"h":1}'), V('K2', true), V('d5')],
        [1200, V('K3', true), V('d6')],
    ],
    // A link too slow for the bitrate: every batch is late, and every one leads with the
    // newest keyframe the hub had.
    keyled: [
        [1500, V('K1', true), V('d1')],
        [1700, V('K2', true), V('d2')],
        [1600, V('K3', true), V('d3')],
    ],
};
const batches = SCENARIOS[process.env.SCENARIO || 'mixed'];
let served = 0;
const decoded = [];
const sentUp = [];
const lags = [];

class EncodedVideoChunk {
    constructor(init) { this.type = init.type; this.data = Buffer.from(init.data).toString(); }
}
class VideoDecoder {
    constructor() { this.decodeQueueSize = 0; }
    configure() {}
    decode(chunk) { decoded.push(chunk.data); }
    close() {}
}
const window = {
    VideoDecoder, EncodedVideoChunk,
    FleetApi: { postJson: async (url, body) => { sentUp.push(...body.messages); return {}; } },
};
const document = {
    createElement: () => ({
        getContext: () => ({ drawImage() {} }),
        captureStream: () => ({ getTracks: () => [] }),
    }),
};
function fetch() {
    if (served >= batches.length) return new Promise(() => {});
    const [lag, ...records] = batches[served++];
    const body = Buffer.concat(records);
    const headers = { 'X-Relay-Next': String(served * 10), 'X-Relay-State': 'open',
                      'X-Relay-Lag-Ms': String(lag) };
    return Promise.resolve({
        status: 200, ok: true,
        headers: { get: (n) => headers[n] ?? null },
        arrayBuffer: async () => body.buffer.slice(body.byteOffset, body.byteOffset + body.length),
    });
}
const ctx = {
    window, document, fetch, VideoDecoder, EncodedVideoChunk,
    performance, setTimeout, clearTimeout, console, Math, Number,
    DataView, Uint8Array, TextDecoder, AbortController, Promise, Object,
};
ctx.HTMLCanvasElement = function () {};
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), ctx);
window.RemoteRelay.create({
    video: { play: () => Promise.resolve() },
    codec: 'h264',
    downUrl: (after) => '/down?after=' + after,
    upUrl: () => '/up',
    onControl: () => {},
    onLag: (ms) => lags.push(ms),
});
setTimeout(() => {
    console.log(JSON.stringify({
        decoded, keyRequests: sentUp.filter((m) => m.t === 'key').length, lags,
    }));
    process.exit(0);
}, 300);
"""


def _node(node, driver, script, scenario=None):
    env = dict(os.environ, SCENARIO=scenario) if scenario else None
    result = subprocess.run([node, "-e", driver, script],
                            capture_output=True, text=True, timeout=30, env=env)
    if result.returncode != 0:
        print(result.stderr)
        return None
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_quality_behaviour(node):
    print("Auto quality controller")
    out = _node(node, QUALITY_DRIVER, os.path.join(JS, "remote-quality.js"))
    check("the controller ran under node", out is not None)
    if out is None:
        return
    ladder = out["ladder"]
    check("the ladder runs best to worst",
          all(a["bitrate_kbps"] > b["bitrate_kbps"] for a, b in zip(ladder, ladder[1:])))
    check("every rung is inside the agent's clamps (fps 1-60, 100-50000 kbps, 25-100 %)",
          all(1 <= r["fps"] <= 60 and 100 <= r["bitrate_kbps"] <= 50000
              and 25 <= r["scale"] <= 100 for r in ladder))
    check("a session starts one below the top", out["startRung"] == 1 == out["start"])
    check("one late batch is noise, not a step down", out["oneSpike"] == 1)
    check("sustained lag steps down exactly once at first", out["firstDown"] == [2])
    check("and again only after the cooldown", out["thenDown"] == [3, 4])
    check("it stops at the bottom rung", out["floor"] == len(ladder) - 1
          and out["floorSettings"] == ladder[-1])
    check("calm does not step up before the wait", out["notYetUp"] == out["downBeforeRecover"] == 2)
    check("15 s of calm steps up one rung", out["upAfterCalm"] == 1)
    check("and a link that stays good climbs to the top", out["top"] == 0)
    check("a step up that comes straight back down",
          out["upOnce"] == 1 and out["backDown"] == 2)
    check("makes the next step up wait longer", out["heldAfterFailedUp"] == 2)
    check("but not forever", out["upAfterLongerWait"] == 1)
    check("reset starts the next session from scratch", out["reset"] == 1)
    check("a missing lag sample is ignored", out["nanIgnored"])


def test_relay_behaviour(node):
    print("relay viewer freeze-and-jump")
    out = _node(node, RELAY_DRIVER, os.path.join(JS, "remote-relay.js"))
    check("the relay viewer ran under node", out is not None)
    if out is None:
        return
    decoded = out["decoded"]
    check("a live batch is decoded", decoded[:2] == ["K1", "d1"])
    check("a late batch that starts on a delta is NOT played -- the picture holds",
          not {"d2", "d3", "d4"} & set(decoded))
    check("it asks for a keyframe once, not on every late poll", out["keyRequests"] == 1)
    check("the next keyframe resumes the picture", "K2" in decoded and "d5" in decoded)
    check("a late batch that starts on a keyframe is shown -- it is the newest there is",
          decoded[-2:] == ["K3", "d6"])
    check("every batch with video reports its lag",
          len(out["lags"]) == 5 and out["lags"][1] >= 1500 and out["lags"][3] >= 100)

    # Review of #171: late batches that LEAD with a keyframe must not ask for another. Each
    # request rebuilds the agent's encoder for a bigger IDR that arrives just as late, and the
    # stream never settles; that link is Auto's to fix, through the lag it is still told.
    out = _node(node, RELAY_DRIVER, os.path.join(JS, "remote-relay.js"), scenario="keyled")
    check("the keyframe-led run ran under node", out is not None)
    if out is None:
        return
    check("late keyframe-led batches are shown, not frozen",
          out["decoded"] == ["K1", "d1", "K2", "d2", "K3", "d3"])
    check("and request no keyframe at all -- no encoder-rebuild loop", out["keyRequests"] == 0)
    check("while Auto still hears that they were late",
          len(out["lags"]) == 3 and min(out["lags"]) >= 1500)


def main():
    test_joins()
    node = shutil.which("node")
    if node:
        test_quality_behaviour(node)
        test_relay_behaviour(node)
    else:
        print("  [--] node not on PATH: behaviour half skipped, NOT passed")
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
