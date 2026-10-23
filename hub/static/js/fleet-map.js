// The fleet map (roadmap #23 phase C): every device in scope that has a known position.
//
// **Last-known positions, refreshed only while somebody watches.** Each point is where a device
// was the last time it answered a locate. With Live on (the default for an operator who may
// locate), the page asks every online device in scope again on location.live_interval_seconds
// for as long as it is open and visible, and the points move as the answers arrive. Closed,
// hidden or paused, nothing is asked -- the hub has no timer of its own (see location.py). The
// age of every fix stays on screen and stale ones are drawn differently, because a point that
// stopped moving still has to say how old it is.
//
// **Live replaced a map that could locate a device once.** The Locate button used to exist
// only on the "not located yet" list, so a phone's first answer moved it to the located list,
// which had no button, and its point never moved again. Every located row now has one too.
//
// **The list beside the map is not decoration.** It is what works when the tile server is
// unreachable, what an operator reads on a narrow screen, and what carries the coordinates
// somebody pastes into a phone before walking out of the door.
//
// **It also lists the devices that are NOT on the map**, under "Not located yet", with what
// each one said the last time anybody asked. Before that list existed, a phone whose every
// locate came back "the permission has not been granted" was absent from the map and from
// the search alike, and the page said only that nothing had ever been located -- true, and
// no help at all to the operator wondering where eleven phones had gone.
(function () {
    'use strict';

    const host = document.getElementById('fleet-map');
    if (!host) return;

    const listEl = document.getElementById('fleet-map-list');
    const emptyEl = document.getElementById('fleet-map-empty');
    const countEl = document.getElementById('fleet-map-count');
    const filterEl = document.getElementById('fleet-map-filter');

    let view = null;
    let fixes = [];
    let unlocated = [];
    let canLocate = false;
    let liveSeconds = 60;
    let liveTimer = null;
    const liveEl = document.getElementById('fleet-map-live');
    const liveToggle = document.getElementById('fleet-map-live-toggle');
    const liveStatus = document.getElementById('fleet-map-live-status');

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function when(epoch) {
        return epoch ? new Date(epoch * 1000).toLocaleString() : '';
    }

    function matches(rows) {
        const needle = (filterEl.value || '').trim().toLowerCase();
        if (!needle) return rows;
        return rows.filter((f) => (f.machine || '').toLowerCase().includes(needle));
    }

    // What a device without a position last said, in words an operator can act on. The
    // device's own reason is shown verbatim when it gave one: it names the switch to flip
    // ("open the FleetHub Agent app on the device and grant location"), which nothing here
    // could say better.
    function unlocatedReason(row) {
        if (row.pending) return t('location.list.pending');
        if (!row.status) return t('location.list.never_asked');
        if (row.status === 'unavailable') {
            return row.detail
                ? t('location.list.unavailable_detail', { detail: row.detail })
                : t('location.list.unavailable');
        }
        return t('location.list.no_answer');
    }

    async function locate(machine, button) {
        button.disabled = true;
        try {
            const response = await fetch(
                `/api/location/machines/${encodeURIComponent(machine)}`,
                { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                button.textContent = data.error || t('location.failed');
                return;
            }
            // The answer arrives later, as a live 'location' event that reloads this page's
            // data; until then the row says the request is in flight.
            await load();
        } catch {
            // A network failure, not a refusal: there is no reason text to show, so the row
            // says the request did not go out and the button stays disabled until a reload.
            button.textContent = t('location.failed');
        }
    }

    function renderUnlocated(rows) {
        if (!rows.length) return;
        listEl.appendChild(el('h2', 'fleet-map__group', t('location.list.unlocated_heading')));
        rows.forEach((row) => {
            const item = el('div', 'fleet-map__row fleet-map__row--unlocated');
            const name = el('a', 'fleet-map__name', row.machine);
            name.href = `/machine/${encodeURIComponent(row.machine)}`;
            item.appendChild(name);
            item.appendChild(el('span', 'stat-card__meta', unlocatedReason(row)));
            if (row.received_at && !row.pending) {
                item.appendChild(el('span', 'stat-card__meta',
                    t('location.list.asked', { when: when(row.received_at) })));
            }
            if (canLocate && !row.pending) {
                const button = el('button', 'btn fleet-map__locate', t('location.list.locate'));
                button.type = 'button';
                button.addEventListener('click', () => locate(row.machine, button));
                item.appendChild(button);
            }
            listEl.appendChild(item);
        });
    }

    function renderList(rows, waiting) {
        listEl.replaceChildren();
        rows.forEach((fix) => {
            const row = el('button', 'fleet-map__row');
            row.type = 'button';
            row.appendChild(el('span', 'fleet-map__name', fix.machine));
            row.appendChild(el('span', 'stat-card__meta',
                fix.stale ? t('location.list.last_known', { when: when(fix.fixed_at) })
                          : t('location.list.fixed', { when: when(fix.fixed_at) })));
            row.appendChild(el('span', 'stat-card__meta map-popup__coords',
                `${fix.lat.toFixed(5)}, ${fix.lon.toFixed(5)}`));
            // Clicking a row frames that device rather than navigating away: the question the
            // page answers is "where are they", and losing the map to answer "which one is
            // this" would be the wrong trade.
            row.addEventListener('click', () => {
                if (view) view.show([fix]);
            });
            listEl.appendChild(row);
            // Beside the row rather than inside it: a button nested in the row's button is
            // invalid HTML, and a click on it would also frame the map.
            if (canLocate) {
                const button = el('button', 'btn fleet-map__locate', t('location.list.locate'));
                button.type = 'button';
                button.addEventListener('click', () => locate(fix.machine, button));
                listEl.appendChild(button);
            }
        });
        renderUnlocated(waiting);
    }

    function draw() {
        const rows = matches(fixes);
        const waiting = matches(unlocated);
        countEl.textContent = waiting.length
            ? t('location.map.count_with_unlocated', { count: rows.length, unlocated: waiting.length })
            : t('location.map.count', { count: rows.length });
        renderList(rows, waiting);
        const drew = view ? view.show(rows) : false;
        // The empty state distinguishes "nothing matches your filter", "devices exist but none
        // has a position yet" and "no device in your scope can be located at all" -- three
        // problems with three different answers.
        emptyEl.hidden = drew;
        if (rows.length === 0 && (filterEl.value || '').trim() && fixes.length) {
            emptyEl.textContent = t('location.map.no_match');
        } else if (unlocated.length) {
            emptyEl.textContent = t('location.map.none_yet', { count: unlocated.length });
        } else {
            emptyEl.textContent = fixes.length === 0 ? t('location.map.none')
                                                     : t('location.map.no_match');
        }
    }

    async function load() {
        let response;
        try {
            response = await fetch('/api/location/fleet');
        } catch (e) {
            emptyEl.textContent = t('location.map.unreachable');
            emptyEl.hidden = false;
            return;
        }
        if (!response.ok) {
            emptyEl.textContent = t('location.map.unreachable');
            emptyEl.hidden = false;
            return;
        }
        const data = await response.json();
        fixes = data.fixes || [];
        unlocated = data.unlocated || [];
        canLocate = !!data.can_locate;
        if (data.live?.interval_seconds) liveSeconds = data.live.interval_seconds;
        // Built after the first payload, because the tile URL is in it -- a map created before
        // the fetch would need its layer swapped afterwards for no gain.
        if (!view) {
            view = window.LocationMap.create(host, data.map || {}, {
                // A popup's "Open" goes to the device's own page, which is where its history,
                // its Locate button and everything else about it lives. A plain assignment
                // rather than an <a>: shell.js intercepts link clicks to route them through
                // the frame, and a link built inside a Leaflet popup is outside the DOM it
                // watches, so it would navigate the frame out of the shell.
                onOpen: (fix) => {
                    window.location.href = `/machine/${encodeURIComponent(fix.machine)}`;
                },
            });
        }
        draw();
    }

    // ---------------------------------------------------------------- live
    // One timer for the whole page, re-armed after each tick rather than setInterval, so a
    // slow hub cannot stack ticks. The hub spaces asks per DEVICE (location.recently_requested),
    // so this timer only has to be roughly right, and two open maps cost a phone nothing extra.
    function liveOn() {
        return !!(canLocate && liveToggle?.checked && !document.hidden);
    }

    function describeLive() {
        if (!liveEl) return;
        liveEl.hidden = !canLocate;
        if (!liveStatus) return;
        liveStatus.textContent = liveOn()
            ? t('location.map.live_on', { seconds: liveSeconds })
            : t('location.map.live_paused');
    }

    async function liveTick() {
        liveTimer = null;
        if (liveOn()) {
            try {
                const response = await fetch('/api/location/fleet/locate',
                    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
                if (response.ok) {
                    const data = await response.json().catch(() => ({}));
                    if (data.live?.interval_seconds) liveSeconds = data.live.interval_seconds;
                }
            } catch {
                // Offline for a moment. The next tick tries again; the answers that do arrive
                // come in as 'location' events either way.
            }
            await load();
        }
        describeLive();
        scheduleLive();
    }

    function scheduleLive() {
        if (liveTimer) clearTimeout(liveTimer);
        liveTimer = liveOn() ? setTimeout(startLive, liveSeconds * 1000) : null;
    }

    // liveTick never rejects -- its fetch is caught and load() reports its own failures on
    // the page -- so the promise is dropped on purpose rather than awaited by an event
    // handler that has nothing to do with the answer.
    function startLive() {
        void liveTick();
    }

    if (liveToggle) {
        liveToggle.addEventListener('change', () => {
            describeLive();
            if (liveOn()) startLive(); else scheduleLive();
        });
    }
    // Hidden tab: stop asking. Back in view: ask at once rather than waiting out the interval,
    // since whoever came back to the tab came back to see where things are now.
    document.addEventListener('visibilitychange', () => {
        describeLive();
        if (liveOn()) startLive(); else scheduleLive();
    });

    filterEl.addEventListener('input', draw);
    void load().then(() => {
        describeLive();
        if (liveOn()) startLive();
    });
    // New fixes, and machines going on- or offline (the marker style follows status).
    window.FleetLive.watch(['location', 'status', 'machines'], load, { minInterval: 5000 });
})();
