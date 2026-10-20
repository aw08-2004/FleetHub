// FleetLive: the live console. One socket per browser tab, and every page re-reads what it
// shows the moment the hub says that thing changed.
//
// WHY. A page used to be fresh at the moment it loaded and then go quietly stale. Five pages
// polled every 30 seconds; the rest showed whatever was true when the operator arrived, so
// a ticket somebody else answered, an alert the rule evaluator raised or a machine that went
// offline stayed wrong until F5. The hub now pushes a CHANGE NOTICE ("topic X changed, for
// machine Y" -- hub/changes.py), and a page that watches that topic calls its own existing
// load function again.
//
// **A notice carries a name, never data.** The page refetches through the gated REST route
// it loaded from, so every capability and scope check keeps guarding the live view. That is
// the bargain tickets.js already struck (roadmap #31); this generalises it to every page.
//
// ONE SOCKET PER TAB, NOT PER FRAME. Socket.IO here is polling-only and every open socket
// holds a waitress thread (hub/app.py, where SocketIO is built). The shell document never
// reloads, so it owns the socket, and every framed page -- the transient one and the
// persistent Remote and Assistant frames -- subscribes through window.parent.FleetLive. A
// page opened on its own (no shell) owns a socket of its own. Rejected: a socket per page,
// which is what the Dashboard and the machine page each used to open.
//
// THROTTLED, AND PAUSED WHEN NOBODY IS LOOKING. watch() runs its callback at most once per
// `minInterval` (leading and trailing, so the first change shows at once and the last one is
// never lost), never twice concurrently, and not at all while the tab is hidden -- a hidden
// tab that missed a change catches up the moment it is shown. A reconnect re-runs every
// watcher, because notices sent while the socket was down are gone. While the socket is down
// a slow fallback tick stands in for it, which is what replaced the per-page setIntervals.
//
// NEVER UNDER THE OPERATOR'S HANDS. Most load functions re-render what they show, and a
// re-render that lands while somebody is half-way through an edit throws the edit away -- far
// worse than a stale number. So a refresh is deferred while any <dialog> on the page is open
// (every editor modal here is one), and while the page's own `busy()` says so (an inline
// form with unsaved values, an editor panel that is showing). It runs the moment that clears.
//
// ALSO HERE: the relative-time ticker. Any element written by fmtAgoInto() (common.js) is
// re-rendered every AGO_TICK_MS, so "last seen 2m ago" never sits there saying 2m for an
// hour. It runs in every document, framed or not, because each one owns its own elements.
//
// IIFE-wrapped, classic script, no bundler -- same as every other file in static/js.
(function () {
    'use strict';

    const DEFAULT_MIN_INTERVAL_MS = 2000;
    // How often a refresh deferred by a busy page (below) looks again.
    const BUSY_RETRY_MS = 2000;
    const FALLBACK_TICK_MS = 30000;
    const AGO_TICK_MS = 10000;

    /** The topbar's Live pill, in the document that owns the socket. */
    function setPill(state) {
        const pill = document.getElementById('socket-status');
        if (!pill || typeof setStatusPill !== 'function') return;
        pill.hidden = false;
        if (state === 'ok') setStatusPill(pill, 'ok', t('common.status.live'));
        else setStatusPill(pill, 'danger', t('common.status.offline'));
    }

    // ---------------- The hub: owns the socket (top document only) ----------------
    function createHub() {
        /** event name -> Set of callbacks. Raw socket events plus three synthetic ones:
         *  'resync' (reconnected, or the fallback tick), 'state' (connected/disconnected). */
        const listeners = new Map();
        let socket = null;
        let connected = false;
        let everConnected = false;
        let fallbackTimer = null;

        function on(event, fn) {
            if (!listeners.has(event)) listeners.set(event, new Set());
            listeners.get(event).add(fn);
            return () => { const set = listeners.get(event); if (set) set.delete(fn); };
        }

        function dispatch(event, payload) {
            const set = listeners.get(event);
            if (!set) return;
            for (const fn of Array.from(set)) {
                // A callback from a frame that navigated away without unsubscribing (a crash,
                // a browser that skipped pagehide) throws or touches a dead document. Drop it
                // rather than let one stale page stop every other page updating.
                try { fn(payload); } catch (e) { set.delete(fn); }
            }
        }

        function startFallback() {
            if (fallbackTimer) return;
            fallbackTimer = setInterval(() => dispatch('resync', { reason: 'fallback' }),
                FALLBACK_TICK_MS);
        }

        function stopFallback() {
            if (fallbackTimer) clearInterval(fallbackTimer);
            fallbackTimer = null;
        }

        function connect() {
            if (socket || typeof io !== 'function') {
                // No socket library (a page that does not load it): still keep pages fresh.
                if (!socket) startFallback();
                return;
            }
            socket = io({ transports: ['polling'], upgrade: false });
            socket.on('connect', () => {
                connected = true;
                stopFallback();
                setPill('ok');
                dispatch('state', { connected: true });
                // The first connect is the page's own initial load; a later one means notices
                // were missed while we were away, so everybody re-reads.
                if (everConnected) dispatch('resync', { reason: 'reconnect' });
                everConnected = true;
            });
            socket.on('disconnect', (reason) => {
                connected = false;
                setPill('danger');
                dispatch('state', { connected: false });
                startFallback();
                // The hub ends every socket after a permission change, so rooms are
                // re-resolved on the way back in (app.py, _drop_live_sockets). Socket.IO does
                // not retry a disconnect the SERVER chose, so this does; the reconnect then
                // re-runs every watcher, which is also how the sidebar picks the change up.
                if (reason === 'io server disconnect') setTimeout(() => socket.connect(), 500);
            });
            socket.on('connect_error', () => { setPill('danger'); startFallback(); });
            socket.onAny((event, payload) => dispatch(event, payload));
        }

        /** Drop and re-open the socket, so the server re-resolves this operator's rooms --
         *  used after a permission change (rooms are fixed at connect time, app.py). */
        function reconnect() {
            if (!socket) return;
            socket.disconnect();
            socket.connect();
        }

        return {
            on, connect, reconnect,
            isConnected: () => connected,
        };
    }

    // ---------------- The page-side API ----------------
    function topicMatches(topics, change) {
        return topics === '*' || topics.includes(change.topic);
    }

    /** Build the API a page uses, over `hub`. `owner` is the page's window: its own
     *  subscriptions are released when it goes away, so a navigated-away frame is never
     *  called back. */
    function createApi(hub, owner) {
        const releases = new Set();

        function track(off) {
            releases.add(off);
            return () => { off(); releases.delete(off); };
        }

        owner.addEventListener('pagehide', () => {
            for (const off of Array.from(releases)) { try { off(); } catch (e) { /* gone */ } }
            releases.clear();
        });

        /**
         * Run `refresh` whenever one of `topics` changes.
         *   topics:  an array of topic names (hub/changes.py), or '*' for every change.
         *   opts.machine:  only changes about this machine, plus fleet-wide ones (a notice
         *                  with no machine may concern anyone).
         *   opts.filter(change):  a further predicate, e.g. a ticket id.
         *   opts.minInterval:  ms between runs; default 2s.
         *   opts.temp:  also run on `new_temp` (for that machine, if opts.machine).
         *   opts.busy():  true while a refresh would clobber something the operator is
         *                 doing; the refresh waits. An open <dialog> always counts.
         * Returns an unsubscribe function.
         */
        function watch(topics, refresh, opts) {
            opts = opts || {};
            const minInterval = opts.minInterval || DEFAULT_MIN_INTERVAL_MS;
            let lastRun = 0;
            let timer = null;
            let inFlight = false;
            let again = false;
            let dirtyWhileHidden = false;

            function busy() {
                try {
                    if (owner.document.querySelector('dialog[open]')) return true;
                    return !!opts.busy?.();
                } catch (e) {
                    // A busy() that throws must not wedge the page forever: treat it as idle,
                    // and say so, since the page's own check is what is broken.
                    console.warn('[fleet-live] busy() threw; refreshing anyway', e);
                    return false;
                }
            }

            async function run() {
                timer = null;
                if (owner.document.hidden) { dirtyWhileHidden = true; return; }
                if (busy()) { timer = owner.setTimeout(run, BUSY_RETRY_MS); return; }
                if (inFlight) { again = true; return; }
                inFlight = true;
                lastRun = Date.now();
                try { await refresh(); } catch (e) { /* the page reports its own errors */ }
                inFlight = false;
                if (again) { again = false; schedule(); }
            }

            function schedule() {
                if (timer) return;
                const wait = Math.max(0, lastRun + minInterval - Date.now());
                timer = owner.setTimeout(run, wait);
            }

            function wanted(change) {
                if (!change || !topicMatches(topics, change)) return false;
                if (opts.machine && change.machine && change.machine !== opts.machine) return false;
                if (opts.filter && !opts.filter(change)) return false;
                return true;
            }

            const offs = [];
            offs.push(hub.on('changes', (msg) => {
                const list = msg?.changes || [];
                if (list.some(wanted)) schedule();
            }));
            // The tickets blueprint predates change notices and pushes its own two events;
            // they are the same thing as a `tickets` change.
            for (const event of ['ticket_new', 'ticket_updated']) {
                offs.push(hub.on(event, (msg) => {
                    const change = { topic: 'tickets', machine: msg?.machine, id: msg?.id };
                    if (wanted(change)) schedule();
                }));
            }
            if (opts.temp) {
                offs.push(hub.on('new_temp', (msg) => {
                    if (opts.machine && msg && msg.machine !== opts.machine) return;
                    schedule();
                }));
            }
            offs.push(hub.on('resync', () => schedule()));

            const onVisible = () => {
                if (!owner.document.hidden && dirtyWhileHidden) {
                    dirtyWhileHidden = false;
                    schedule();
                }
            };
            owner.document.addEventListener('visibilitychange', onVisible);

            return track(() => {
                offs.forEach((off) => off());
                owner.document.removeEventListener('visibilitychange', onVisible);
                if (timer) owner.clearTimeout(timer);
            });
        }

        /** Subscribe to a raw socket event ('new_temp', 'changes', 'state', ...). */
        function on(event, fn) {
            return track(hub.on(event, fn));
        }

        return {
            watch,
            on,
            onTemp: (fn) => on('new_temp', fn),
            reconnect: () => hub.reconnect(),
            isConnected: () => hub.isConnected(),
            __hub: hub,
        };
    }

    // ---------------- Which hub this document uses ----------------
    function parentHub() {
        try {
            if (window.parent !== window && window.parent.FleetLive?.__hub) {
                return window.parent.FleetLive.__hub;
            }
        } catch (e) { /* cross-origin parent: not our shell */ }
        return null;
    }

    function liveEnabled() {
        const meta = document.querySelector('meta[name="fleet-live"]');
        return !!meta && meta.getAttribute('content') === 'on';
    }

    let hub = parentHub();
    if (!hub) {
        hub = createHub();
        if (liveEnabled()) hub.connect();
    }
    window.FleetLive = createApi(hub, window);

    // ---------------- Relative-time ticker ----------------
    function tickAgo() {
        if (document.hidden || typeof fmtAgo !== 'function') return;
        for (const node of document.querySelectorAll('[data-ago-at]')) {
            node.textContent = fmtAgo(node.dataset.agoAt);
        }
    }
    setInterval(tickAgo, AGO_TICK_MS);
    document.addEventListener('visibilitychange', tickAgo);
})();
