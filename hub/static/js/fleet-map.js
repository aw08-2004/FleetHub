// The fleet map (roadmap #23 phase C): every device in scope that has a known position.
//
// **A last-known-position map, and the page has to say so.** Nothing here is live. Each point
// is where a device was the last time an operator asked, which for most devices is never and
// for the rest is whenever somebody last needed to know. A map that looked live would be read
// as live, so the age of every fix is on screen and stale ones are drawn differently.
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

    filterEl.addEventListener('input', draw);
    load();
    // New fixes, and machines going on- or offline (the marker style follows status).
    window.FleetLive.watch(['location', 'status', 'machines'], load, { minInterval: 5000 });
})();
