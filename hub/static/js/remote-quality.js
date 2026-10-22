// The Auto preset of the remote viewer (roadmap #2): steps a relayed stream's quality down when
// the link cannot carry it, and back up once it can.
//
// Why it exists: the agent encodes at a fixed fps and bitrate whatever the link does -- it has
// no congestion control of its own -- so a phone on mobile data watching a 6 Mbps stream fell
// further behind every second, and skipping stale frames (remote_relay.py, remote-relay.js)
// only turns that into a jumpy picture. The fix for a link that is too slow is to send less.
//
// Fed by how far behind live each relayed batch is (X-Relay-Lag-Ms plus the body's transfer
// time). Decides only; remote.js sends the {t:'cfg'} that applies a rung. Kept free of the DOM
// so tests/test_remote_quality.py can drive it under node.
//
// The thresholds are deliberately slow to move:
//   * each rung change makes the agent rebuild its encoder and send a keyframe, which is itself
//     a burst a slow link has to swallow -- so a cooldown follows every change, and the samples
//     taken before it are forgotten;
//   * one late batch is noise (a GC pause, a cell handover), so stepping down needs two of the
//     last three samples to be late;
//   * stepping up is a guess that the link got better. A guess that comes straight back down
//     doubles the wait before the next one, so a link that is borderline settles on the rung it
//     can carry instead of rebuilding the encoder every twenty seconds.
// Rejected: estimating bandwidth from bytes per second and picking a bitrate to match. The
// long-poll's batches are too lumpy for a clean estimate, and lag is the thing the operator
// actually feels.
//
// WebRTC sessions report no lag (the browser plays that track itself), so Auto holds its
// starting rung there. Feeding it from getStats() is the open half -- see ROADMAP.MD #2.
(function () {
    'use strict';

    // Best first. START is where a session begins: one below the top, because a session that
    // opens on a slow link should not have to fall two rungs before it can be used.
    const LADDER = [
        { fps: 20, bitrate_kbps: 6000, scale: 100 },
        { fps: 15, bitrate_kbps: 2500, scale: 100 },
        { fps: 12, bitrate_kbps: 1500, scale: 75 },
        { fps: 10, bitrate_kbps: 800, scale: 50 },
        { fps: 6, bitrate_kbps: 400, scale: 50 },
    ];
    const START = 1;

    const DOWN_LAG_MS = 1000;
    const UP_LAG_MS = 300;
    const WINDOW = 3;
    const DOWN_HITS = 2;
    const COOLDOWN_MS = 5000;
    const UP_AFTER_MS = 15000;
    const UP_AFTER_MAX_MS = 120000;
    // A step down this soon after a step up means the step up was wrong.
    const FAILED_UP_MS = 30000;

    function create() {
        let rung = START;
        let recent = [];
        let changedAt = -Infinity;
        let lastMove = 0;          // -1 up, +1 down, 0 none yet
        let calmSince = null;
        let upAfter = UP_AFTER_MS;

        function settings() { return Object.assign({}, LADDER[rung]); }

        function move(next, now) {
            const direction = next > rung ? 1 : -1;
            if (direction > 0 && lastMove < 0 && now - changedAt < FAILED_UP_MS) {
                upAfter = Math.min(upAfter * 2, UP_AFTER_MAX_MS);
            }
            rung = next;
            lastMove = direction;
            changedAt = now;
            recent = [];
            calmSince = null;
            return settings();
        }

        /** One lag sample in milliseconds, taken at `now` (performance.now()). Returns the new
         *  rung's settings when the rung changes, otherwise null. */
        function sample(lagMs, now) {
            if (!Number.isFinite(lagMs)) return null;
            recent.push(lagMs);
            if (recent.length > WINDOW) recent.shift();
            if (lagMs < UP_LAG_MS) {
                if (calmSince === null) calmSince = now;
            } else {
                calmSince = null;
            }
            if (now - changedAt < COOLDOWN_MS) return null;
            const late = recent.filter((l) => l > DOWN_LAG_MS).length;
            if (late >= DOWN_HITS && rung < LADDER.length - 1) return move(rung + 1, now);
            if (calmSince !== null && now - calmSince >= upAfter && rung > 0) return move(rung - 1, now);
            return null;
        }

        function reset() {
            rung = START;
            recent = [];
            changedAt = -Infinity;
            lastMove = 0;
            calmSince = null;
            upAfter = UP_AFTER_MS;
        }

        return { sample, settings, reset, rung: () => rung };
    }

    window.RemoteQuality = { create, LADDER, START };
})();
