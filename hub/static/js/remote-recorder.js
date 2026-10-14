// Session recording (roadmap #19): records what ONE viewer is showing and streams it to the hub.
//
// The browser records, not the agent: the viewer already holds the decoded picture -- the
// WebRTC MediaStream, or the canvas the hub relay draws into -- so a MediaRecorder on that
// stream costs the PC nothing. See hub/recordings.py for the whole design; the parts that
// shape this file are:
//
//   * NOTHING IS RECORDED UNTIL THE PC CONFIRMS ITS BADGE. Start asks the hub, the hub asks
//     the PC to show "Recording Screen", and only once the hub reports `recording` does the
//     MediaRecorder start. An agent too old to show a badge never confirms, and the attempt
//     fails here after BADGE_WAIT_MS with a sentence saying so.
//   * TEN MINUTES AT A TIME. The deadline is the hub's; this file runs the 60-second
//     countdown against the hub's clock (server_time) and offers Extend. The countdown is
//     drawn in the console, never on the PC, so it is never in the video.
//   * CHUNKS GO UP IN ORDER, ONE AT A TIME, numbered. A webm file is one stream, so the hub
//     refuses a gap, and a retried chunk that already landed is accepted without being stored
//     twice. A 409 is the hub saying the recording ended without us (the session ended, the
//     badge went, the deadline passed) and carries the reason.
//   * ONE STREAM PER RECORDING. If the viewer's picture is replaced -- WebRTC giving way to the
//     hub relay, or the new helper's track after a session switch -- the recording ends with
//     `stream_changed` rather than splicing a second webm header into the file, which would
//     leave everything after it unplayable. What is recorded is the newest LIVE video track,
//     wrapped in a stream of its own: after a switch remote.js adds the new helper's track to
//     the same MediaStream, so watching the stream object alone would never notice.
//   * A SESSION SWITCH CONTINUES THE RECORDING AS ITS NEXT PART. Switching user or signing in on
//     the PC relaunches the helper, which ends the recording at the hub (`helper_restarted`).
//     Ending there is right -- the old badge went with the old helper -- but stopping there is
//     not what the operator asked for, so after either of CONTINUE_REASONS this waits for the
//     new picture and starts a continuation: same reason, no dialog, and the same badge gate
//     as any start, so the new session shows "Recording Screen" before anything is kept. An
//     ending the operator chose (Stop, closing the viewer) never continues. See
//     hub/recordings.py for the series, the shared clock and what was rejected.
//
// A factory, like remote.js: window.RemoteRecorder.create(...) per viewer, no globals.
(function () {
    'use strict';

    const TIMESLICE_MS = 2000;
    const VIDEO_BITS_PER_SECOND = 2500000;
    const BADGE_WAIT_MS = 25000;
    const BADGE_POLL_MS = 1000;
    const STATUS_POLL_MS = 5000;
    const TICK_MS = 1000;
    const COUNTDOWN_SECONDS = 60;
    const MAX_CHUNK_RETRIES = 3;
    const MIME_CANDIDATES = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
    // recordings.CONTINUE_REASONS. A continuation waits CONTINUE_SETTLE_MS before looking for
    // the new picture -- the old track can still read `live` for a moment after its helper is
    // gone -- and gives up after CONTINUE_WAIT_MS, inside the hub's 120-second window.
    const CONTINUE_REASONS = new Set(['helper_restarted', 'stream_changed']);
    const CONTINUE_SETTLE_MS = 3000;
    const CONTINUE_WAIT_MS = 60000;

    // Why a recording ended, one LITERAL key per code: the i18n test only sees literal t()
    // calls, and tests/test_recordings_web.py checks this list against recordings.END_REASONS
    // so a reason added on the hub cannot reach the console untranslated.
    const END_REASON_TEXT = {
        stopped: () => t('recordings.end_reason.stopped'),
        time_limit: () => t('recordings.end_reason.time_limit'),
        session_ended: () => t('recordings.end_reason.session_ended'),
        helper_restarted: () => t('recordings.end_reason.helper_restarted'),
        badge_failed: () => t('recordings.end_reason.badge_failed'),
        badge_timeout: () => t('recordings.end_reason.badge_timeout'),
        stream_changed: () => t('recordings.end_reason.stream_changed'),
        size_limit: () => t('recordings.end_reason.size_limit'),
    };

    // `detail` is the PC's own reason, when it sent one (a badge that would not go up says
    // which Win32 call failed). Appended as plain text: it comes from the agent, and every
    // caller puts this string into textContent.
    function endReasonText(code, detail) {
        const text = (END_REASON_TEXT[code] || END_REASON_TEXT.stopped)();
        return detail ? `${text} (${detail})` : text;
    }

    function pickMime() {
        if (typeof MediaRecorder !== 'function') return null;
        for (const mime of MIME_CANDIDATES) {
            try { if (MediaRecorder.isTypeSupported(mime)) return mime; } catch (e) { /* next */ }
        }
        return null;
    }

    function supported() { return pickMime() !== null; }

    /** The newest video track still delivering frames, or null. */
    function liveTrack(stream) {
        const tracks = stream?.getVideoTracks ? stream.getVideoTracks() : [];
        for (let i = tracks.length - 1; i >= 0; i--) {
            if (tracks[i].readyState === 'live') return tracks[i];
        }
        return null;
    }

    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    /** A fetch that keeps the status: a 409 here is information, not a failure to report. */
    async function call(method, url, body, raw) {
        const init = { method };
        if (raw !== undefined) {
            init.headers = { 'Content-Type': 'application/octet-stream' };
            init.body = raw;
        } else if (body !== undefined) {
            init.headers = { 'Content-Type': 'application/json' };
            init.body = JSON.stringify(body);
        }
        const response = await fetch(url, init);
        const data = await response.json().catch(() => ({}));
        return { status: response.status, ok: response.ok, data };
    }

    /** opts.machine     the PC
     *  opts.sessionId   () => the viewer's remote session id
     *  opts.stream      () => the MediaStream the viewer is showing right now
     *  opts.ready       () => whether the viewer is showing a live picture (optional)
     *  opts.onState     (state, detail) -- 'starting' | 'recording' | 'stopping' |
     *                   'continuing' | 'ended' | 'failed'; detail carries {continuing} while
     *                   starting, {seconds} while recording, {reason} at the end and while
     *                   continuing, {error} on failure
     *  opts.onCountdown (secondsLeft | null) -- a number only inside the last minute */
    function create(opts) {
        const base = '/api/remote/recordings/';
        let rec = null;
        let recorder = null;
        let recordedStream = null;
        let sourceStream = null;   // the viewer's stream the recorded track was taken from
        let recordedTrack = null;
        let seq = 0;
        let queue = Promise.resolve();
        let clockOffset = 0;       // server seconds minus local seconds
        let tickTimer = null;
        let lastStatusPoll = 0;
        let state = 'idle';
        let finishing = null;

        function setState(next, detail) {
            state = next;
            if (opts.onState) opts.onState(next, detail || {});
        }

        function serverNow() { return Date.now() / 1000 + clockOffset; }

        function adopt(data) {
            if (!data?.id) return;
            rec = data;
            if (data.server_time) clockOffset = data.server_time - Date.now() / 1000;
        }

        /** `continues` is the id of the part this one follows (continueAfter); only then may
         *  it start from `continuing`. */
        async function start(reason, continues) {
            const from = continues ? ['continuing'] : ['idle', 'ended', 'failed'];
            if (!from.includes(state)) return;
            const mime = pickMime();
            if (!mime) {
                setState('failed', { error: t('recordings.unsupported') });
                return;
            }
            seq = 0;
            queue = Promise.resolve();
            finishing = null;
            // Forget the last recording before asking for the next, so a Stop pressed while
            // the hub is answering cancels THIS one (finish) rather than re-stopping that one.
            rec = null;
            setState('starting', { continuing: !!continues });
            if (!await requestRecording(reason, mime, continues)) return;
            if (await confirmBadge(Date.now() + BADGE_WAIT_MS)) beginRecording(mime);
        }

        /** Ask the hub for the recording. True once it exists and is still wanted. */
        async function requestRecording(reason, mime, continues) {
            const res = await call('POST',
                `/api/remote/${encodeURIComponent(opts.machine)}/recordings`,
                { session_id: opts.sessionId(), reason, mime, continues: continues || null });
            if (!res.ok) {
                if (state === 'starting') {                 // not cancelled meanwhile
                    setState('failed', { error: res.data.error || t('common.hub_error',
                                                                   { status: res.status }) });
                }
                return false;
            }
            adopt(res.data);
            if (state !== 'starting') {
                // Stopped while the hub was creating it: end it there too, or the PC raises a
                // badge for a recording nobody is making until the hub's sweep notices.
                await call('POST', base + encodeURIComponent(rec.id) + '/stop', {});
                return false;
            }
            return true;
        }

        /** Wait for the PC to confirm its badge. Not a moment longer than needed: every second
         *  here is a second the operator is looking at "waiting for the PC". True once it is
         *  up; false when this start is over, already reported (stopped meanwhile, refused,
         *  or out of time). Recurses rather than looping, as attempt() does. */
        async function confirmBadge(giveUp) {
            await sleep(BADGE_POLL_MS);
            if (state !== 'starting') return false;          // stopped meanwhile
            const poll = await call('GET', base + encodeURIComponent(rec.id));
            if (poll.ok) adopt(poll.data);
            if (rec.status === 'recording') return true;
            if (rec.status === 'failed' || rec.status === 'ended') {
                setState('failed', { error: endReasonText(rec.end_reason, rec.end_detail) });
                return false;
            }
            if (Date.now() < giveUp) return confirmBadge(giveUp);
            await call('POST', base + encodeURIComponent(rec.id) + '/stop', {});
            setState('failed', { error: endReasonText('badge_timeout') });
            return false;
        }

        function beginRecording(mime) {
            sourceStream = opts.stream();
            recordedTrack = liveTrack(sourceStream);
            if (!recordedTrack) {
                finish('stream_changed');
                return;
            }
            recordedStream = new MediaStream([recordedTrack]);
            try {
                recorder = new MediaRecorder(recordedStream,
                    { mimeType: mime, videoBitsPerSecond: VIDEO_BITS_PER_SECOND });
            } catch (e) {
                finish('stopped');
                setState('failed', { error: t('recordings.unsupported') });
                return;
            }
            recorder.ondataavailable = (e) => {
                if (e.data?.size) enqueue(e.data);
            };
            recorder.start(TIMESLICE_MS);
            setState('recording', { seconds: 0 });
            tickTimer = setInterval(tick, TICK_MS);
        }

        /** Each chunk carries the id of the recording it was cut from. Once a session switch
         *  has started the next part, `rec` is that part -- and the old recorder's last chunk,
         *  or a retry still in flight, must neither land in it nor be mistaken for its answer. */
        function enqueue(blob) {
            const n = seq++;
            const id = rec.id;
            queue = queue.then(() => upload(id, n, blob));
        }

        /** What one chunk upload's answer means: true when it is settled one way or the
         *  other, false when it is worth another try (the network, or a 5xx). */
        function settled(id, res) {
            if (!rec || rec.id !== id) return true;         // a part that has already ended
            if (!res) return false;                         // network: retry
            if (res.ok) {
                if (res.data.deadline) rec.deadline = res.data.deadline;
                if (res.data.server_time) clockOffset = res.data.server_time - Date.now() / 1000;
                return true;
            }
            if (res.status === 409) {
                // Ended at the hub. Say why, and stop recording into nothing. While stopping,
                // finish() is already on its way to saying so.
                adopt(res.data.recording);
                if (state === 'recording') ended(rec.end_reason);
                return true;
            }
            if (res.status >= 500 || res.status === 0) return false;
            stopLocal();
            setState('failed', { error: res.data.error
                || t('common.hub_error', { status: res.status }) });
            return true;
        }

        /** One chunk, retried with a growing pause until settled or out of attempts. Chunks
         *  go up in order and one at a time ON PURPOSE -- the hub appends to one stream -- and
         *  the retry recurses rather than looping, which says the same thing without an await
         *  inside a loop. */
        async function attempt(id, blob, tries) {
            const url = `${base}${encodeURIComponent(id)}/chunks/`;
            const res = await call('PUT', url + blob.seq, undefined, blob.data).catch(() => null);
            if (settled(id, res)) return;
            if (tries >= MAX_CHUNK_RETRIES) {
                stopLocal();
                setState('failed', { error: t('recordings.upload_failed') });
                return;
            }
            await sleep(1000 * (tries + 1));
            await attempt(id, blob, tries + 1);
        }

        async function upload(id, n, data) {
            if (!rec || rec.id !== id) return;
            if (state !== 'recording' && state !== 'stopping') return;
            await attempt(id, { seq: n, data }, 0);
        }

        async function tick() {
            if (state !== 'recording' || !rec) return;
            // The viewer's picture is not the one being recorded any more (WebRTC -> hub
            // relay, or a new helper's track after a session switch): this part is over.
            const source = opts.stream();
            if (source !== sourceStream || liveTrack(source) !== recordedTrack) {
                finish('stream_changed');
                return;
            }
            const left = Math.round(rec.deadline - serverNow());
            if (opts.onCountdown) opts.onCountdown(left <= COUNTDOWN_SECONDS ? Math.max(0, left) : null);
            if (left <= 0) {
                finish('time_limit');
                return;
            }
            setState('recording', {
                seconds: Math.max(0, Math.round(serverNow() - (rec.confirmed_at || serverNow()))),
            });
            // An ending the hub decided (the session ended, the badge could not follow the
            // lock screen) reaches us on the next chunk anyway; this finds it sooner.
            if (Date.now() - lastStatusPoll > STATUS_POLL_MS) {
                lastStatusPoll = Date.now();
                const poll = await call('GET', base + encodeURIComponent(rec.id)).catch(() => null);
                if (poll?.ok) {
                    adopt(poll.data);
                    if (rec.status !== 'recording' && state === 'recording') {
                        stopLocal();
                        ended(rec.end_reason);
                    }
                }
            }
        }

        function stopLocal() {
            if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
            if (opts.onCountdown) opts.onCountdown(null);
            if (recorder && recorder.state !== 'inactive') {
                try { recorder.stop(); } catch (e) { /* already stopped */ }
            }
            recorder = null;
            recordedStream = null;
            sourceStream = null;
            recordedTrack = null;
        }

        /** An ending the hub decided (a 409, the status poll). */
        function ended(reason) {
            stopLocal();
            endedOrContinue(reason || 'stopped');
        }

        function endedOrContinue(reason) {
            if (rec && CONTINUE_REASONS.has(reason)) continueAfter(rec);
            else setState('ended', { reason });
        }

        /** Wait for the new helper's picture, then start the next part of `prev`. Gives up --
         *  ending as `prev` did -- if the viewer's session goes, or no picture arrives in
         *  CONTINUE_WAIT_MS. A Stop meanwhile is finish()'s `continuing` case. */
        function continueAfter(prev) {
            const session = opts.sessionId();
            const giveUp = Date.now() + CONTINUE_WAIT_MS;
            setState('continuing', { reason: prev.end_reason });
            void sleep(CONTINUE_SETTLE_MS).then(() => awaitPicture(prev, session, giveUp));
        }

        /** One look for the new picture, and another BADGE_POLL_MS later until giveUp. */
        async function awaitPicture(prev, session, giveUp) {
            if (state !== 'continuing') return;              // stopped meanwhile
            if (!session || opts.sessionId() !== session || Date.now() >= giveUp) {
                setState('ended', { reason: prev.end_reason });
                return;
            }
            const ready = opts.ready ? opts.ready() : true;
            if (ready && liveTrack(opts.stream())) {
                await start(prev.reason, prev.id);
                return;
            }
            await sleep(BADGE_POLL_MS);
            await awaitPicture(prev, session, giveUp);
        }

        /** Stop recording: flush the last chunk, then tell the hub. Resolves once the hub has
         *  the whole file. Safe to call more than once; later calls wait on the first. */
        function finish(reason) {
            // Between parts there is nothing to flush: the last part has ended at the hub, and
            // the next one has not been asked for. Stop means stop -- no next part. Checked
            // before `finishing`, which still holds the finish that ended the last part.
            if (state === 'continuing') {
                setState('ended', { reason: 'stopped' });
                return Promise.resolve();
            }
            if (finishing) return finishing;
            // Stopped while the hub is still creating it: start() ends it when the answer
            // comes back and finds the state moved on.
            if (state === 'starting' && !rec) {
                setState('failed', { error: t('recordings.cancelled') });
                return Promise.resolve();
            }
            if (!rec || (state !== 'recording' && state !== 'starting')) {
                return Promise.resolve();
            }
            const wasStarting = state === 'starting';
            setState('stopping');
            finishing = (async () => {
                if (!wasStarting && recorder && recorder.state !== 'inactive') {
                    // The final dataavailable fires before 'stop', so the last chunk is
                    // queued by the time the promise below resolves.
                    await new Promise((resolve) => {
                        recorder.addEventListener('stop', resolve, { once: true });
                        try { recorder.stop(); } catch (e) { resolve(); }
                    });
                }
                stopLocal();
                await queue.catch(() => {});
                const res = await call('POST', base + encodeURIComponent(rec.id) + '/stop',
                                       { reason: reason || 'stopped' }).catch(() => null);
                if (res?.ok) adopt(res.data);
                const final = rec?.end_reason || reason;
                if (wasStarting) {
                    setState('failed', { error: t('recordings.cancelled') });
                } else if (reason === 'stopped') {
                    // The operator's Stop, even when the hub had already ended it for a
                    // session switch a moment earlier: they asked for no more recording.
                    setState('ended', { reason: final });
                } else {
                    endedOrContinue(final);
                }
            })();
            return finishing;
        }

        async function extend() {
            if (state !== 'recording' || !rec) return;
            const res = await call('POST', base + encodeURIComponent(rec.id) + '/extend', {});
            if (res.ok) {
                adopt(res.data);
                if (opts.onCountdown) opts.onCountdown(null);
            } else if (res.status === 409 && res.data.recording) {
                adopt(res.data.recording);
                if (rec.status !== 'recording') ended(rec.end_reason);
            }
        }

        return {
            start,
            stop: (reason) => finish(reason || 'stopped'),
            extend,
            isActive: () => ['starting', 'recording', 'stopping', 'continuing'].includes(state),
        };
    }

    window.RemoteRecorder = { create, supported, endReasonText };
})();
