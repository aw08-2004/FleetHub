// Search, per-column filters and phone-width card labels for every .data-table (hub 1.163.0).
//
// WHY. Devices had one search box and nothing else; Device Groups, Watchdogs, Rules, Tickets
// and the forty-odd tables built in JS had neither. "Which of these PCs runs Windows 10 AND
// is a Dell" was a sort, a scan and a squint. And below 640px only Devices had a phone layout:
// every other wide table pushed the whole document sideways, nav and header with it.
//
// HOW. One helper, and no page script has to call it. A body MutationObserver finds every
// table.data-table as it is inserted -- most are built by page scripts after load, many are
// rebuilt on every refresh -- and gives it:
//   * a toolbar above it: one search box (any cell) and a Filters toggle,
//   * a second <thead> row, one input per labelled column; the filters AND together and AND
//     with the search box,
//   * data-label on every cell, copied off its header, for the card layout in components.css,
//   * a .table-scroll wrapper unless it already sits in one, so a wide table scrolls inside its
//     card rather than dragging the page.
// A MutationObserver on the tbody re-applies all of that when the page re-renders its rows,
// so a 30 s refresh neither clears the filter nor brings back rows it hid.
//
// The filter row is a separate row, never an <input> inside a th[data-sort]: a click in the
// box would sort the column, and tests/test_devices_web.py forbids it outright.
//
// MODES, per table, from data-table-tools:
//   (absent)   everything above. The toolbar only appears once the body has more than
//              MIN_ROWS rows -- a search box over three rows is noise.
//   "filters"  the column filters and their toggle, no search box: the page already has its
//              own search (Audit, Tickets, Users ...), and two boxes that filter differently
//              would be worse than one.
//   "off"      nothing at all, not even the card layout: key/value tables, the sensor
//              table, the printable report sheet, the duplicate-machine picker in an alert.
//   "manual"   not auto-enhanced; the page calls FleetTable.enhance() itself (Devices).
// Devices calls FleetTable.enhance() itself with onChange: it filters its own rows, so the
// count, select-all and CSV export all keep meaning "what is on screen".
//
// Filter state is sessionStorage, keyed by path + the table's position on the page.
// Rejected: localStorage. A column filter forgotten yesterday and still applied today hides
// PCs with nothing on screen saying why -- worse than retyping it.
//
// Built with createElement/textContent: cell text is machine names, serials and OS labels,
// i.e. whatever a host reported to the unauthenticated /api/report.
(function () {
    'use strict';

    const MIN_ROWS = 5;
    const STORAGE_PREFIX = 'fleethub.table:';
    const states = new WeakMap();      // table -> state
    const tableSeq = new WeakMap();    // table -> stable key, assigned on first sight
    // Which box last had focus, and whether it still does. A page that rebuilds its table
    // removes the focused box with it, and the browser fires no blur for a removed element --
    // so `focused` still being true when the replacement arrives means "was typing here".
    let focusMemo = { key: '', field: '', focused: false };

    function storageKey(table) {
        let key = tableSeq.get(table);
        if (!key) {
            const all = Array.from(document.querySelectorAll('table.data-table'));
            key = table.id || `${all.indexOf(table)}`;
            tableSeq.set(table, key);
        }
        return STORAGE_PREFIX + location.pathname + location.search + '#' + key;
    }

    function loadState(table) {
        try {
            const saved = JSON.parse(sessionStorage.getItem(storageKey(table)));
            if (saved && typeof saved === 'object') {
                return { query: String(saved.query || ''), columns: saved.columns || {}, open: !!saved.open };
            }
        } catch (e) { /* private mode, malformed */ }
        return { query: '', columns: {}, open: false };
    }

    function saveState(state) {
        try {
            sessionStorage.setItem(storageKey(state.table), JSON.stringify({
                query: state.query, columns: state.columns, open: state.open,
            }));
        } catch (e) { /* private mode */ }
    }

    /** The header row the columns are read from: the first row of the thead that is not ours. */
    function headerRow(table) {
        if (!table.tHead) return null;
        return Array.from(table.tHead.rows).find((r) => !r.classList.contains('data-table__filters')) || null;
    }

    /** [{ index, label, key }] -- one entry per grid column, colspans expanded. */
    function columns(table) {
        const row = headerRow(table);
        if (!row) return [];
        const out = [];
        for (const th of row.cells) {
            const label = th.textContent.trim();
            // A header holding a checkbox (select-all) is not a column anyone filters.
            const filterable = !!label && !th.querySelector('input, select, button');
            const key = th.dataset.sort || th.dataset.key || String(out.length);
            for (let i = 0; i < (th.colSpan || 1); i++) {
                out.push({ index: out.length, label, key, filterable: filterable && i === 0 });
            }
        }
        return out;
    }

    /** Cells of a body row by grid column, so a colspan in one row cannot shift the rest. */
    function cellsByColumn(tr) {
        const out = [];
        for (const td of tr.cells) {
            for (let i = 0; i < (td.colSpan || 1); i++) out.push(i === 0 ? td : null);
        }
        return out;
    }

    // A "Loading..." or "could not load" row: one cell across the table. Never filtered away
    // -- hiding the only thing saying why the table is empty would be the worst outcome here.
    function isNoticeRow(tr) {
        return tr.cells.length === 1 && tr.cells[0].colSpan > 1;
    }

    function dataRows(state) {
        return Array.from(state.table.tBodies).flatMap((tb) => Array.from(tb.rows))
            .filter((tr) => !tr.classList.contains('data-table__no-match'));
    }

    function labelCells(state) {
        const labels = state.cols.map((c) => c.label);
        for (const tr of dataRows(state)) {
            cellsByColumn(tr).forEach((td, i) => {
                if (td && labels[i] && !td.dataset.label) td.dataset.label = labels[i];
            });
        }
    }

    function active(state) {
        return !!state.query || Object.values(state.columns).some(Boolean);
    }

    // ---------------- Filtering (DOM mode) ----------------
    function apply(state) {
        if (state.onChange) return;
        const q = state.query.trim().toLowerCase();
        const colFilters = state.cols
            .map((c) => ({ index: c.index, value: String(state.columns[c.key] || '').trim().toLowerCase() }))
            .filter((f) => f.value);
        let rows = 0;
        let shown = 0;
        for (const tr of dataRows(state)) {
            if (isNoticeRow(tr)) { tr.hidden = false; continue; }
            rows++;
            const cells = cellsByColumn(tr);
            const text = (td) => (td ? td.textContent.toLowerCase() : '');
            const ok = (!q || cells.some((td) => text(td).includes(q)))
                && colFilters.every((f) => text(cells[f.index]).includes(f.value));
            tr.hidden = !ok;
            if (ok) shown++;
        }
        showNoMatch(state, rows > 0 && shown === 0 && active(state));
        updateCount(state, rows, shown);
        updateVisibility(state, rows);
    }

    function showNoMatch(state, on) {
        const body = state.table.tBodies[0];
        if (!body) return;
        let tr = body.querySelector(':scope > tr.data-table__no-match');
        if (!on) {
            tr?.remove();
            return;
        }
        if (!tr) {
            tr = document.createElement('tr');
            tr.className = 'data-table__no-match';
            const td = document.createElement('td');
            td.className = 'stat-card__meta';
            td.colSpan = Math.max(state.cols.length, 1);
            td.textContent = t('common.no_matches');
            tr.appendChild(td);
            body.appendChild(tr);
        }
    }

    function updateCount(state, rows, shown) {
        if (!state.countEl) return;
        state.countEl.textContent = active(state) && rows
            ? tPlural('common.table_count', rows, { visible: shown })
            : '';
    }

    /** The toolbar stays out of the way on a short table -- unless a filter is already on, in
     *  which case hiding the box would hide the reason rows are missing. */
    function updateVisibility(state, rows) {
        if (!state.toolbar || state.ownToolbar) return;
        state.toolbar.hidden = !(rows > MIN_ROWS || active(state));
    }

    // ---------------- Building ----------------
    function filterRow(state) {
        const tr = document.createElement('tr');
        tr.className = 'data-table__filters';
        tr.hidden = !state.open;
        for (const col of state.cols) {
            const th = document.createElement('th');
            if (col.filterable) {
                const input = document.createElement('input');
                input.type = 'search';
                input.className = 'input data-table__filter';
                input.autocomplete = 'off';
                input.spellcheck = false;
                input.placeholder = col.label;
                input.setAttribute('aria-label', t('common.table_filter_column', { column: col.label }));
                input.value = state.columns[col.key] || '';
                input.dataset.key = col.key;
                trackFocus(state, input, col.key);
                input.addEventListener('input', () => {
                    state.columns[col.key] = input.value;
                    changed(state);
                });
                th.appendChild(input);
            }
            tr.appendChild(th);
        }
        return tr;
    }

    function refreshButtons(state) {
        if (state.clearBtn) state.clearBtn.hidden = !active(state);
        if (state.toggleBtn) {
            const n = Object.values(state.columns).filter((v) => String(v || '').trim()).length;
            state.toggleBtn.querySelector('.table-tools__badge').textContent = n ? String(n) : '';
        }
    }

    function changed(state) {
        saveState(state);
        refreshButtons(state);
        if (state.onChange) state.onChange({ query: state.query, columns: { ...state.columns } });
        else apply(state);
    }

    function setOpen(state, open) {
        state.open = open;
        state.filterTr.hidden = !open;
        state.toggleBtn.setAttribute('aria-expanded', String(open));
        state.toggleBtn.classList.toggle('is-active', open);
        saveState(state);
        if (open) {
            const first = state.filterTr.querySelector('input');
            if (first) first.focus();
        }
    }

    function clearAll(state) {
        state.query = '';
        state.columns = {};
        if (state.searchInput) state.searchInput.value = '';
        for (const input of state.filterTr.querySelectorAll('input')) input.value = '';
        changed(state);
    }

    function trackFocus(state, input, field) {
        input.addEventListener('focus', () => {
            focusMemo = { key: storageKey(state.table), field, focused: true };
        });
        input.addEventListener('blur', () => { focusMemo.focused = false; });
    }

    function button(className, text) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = className;
        b.textContent = text;
        return b;
    }

    function buildToolbar(state, opts) {
        const bar = opts.toolbar || document.createElement('div');
        state.ownToolbar = !!opts.toolbar;
        if (!opts.toolbar) bar.className = 'table-tools';

        if (state.withSearch) {
            const input = document.createElement('input');
            input.type = 'search';
            input.className = 'input table-tools__search';
            input.autocomplete = 'off';
            input.placeholder = t('common.table_search');
            input.setAttribute('aria-label', t('common.table_search'));
            input.value = state.query;
            trackFocus(state, input, '');
            input.addEventListener('input', () => { state.query = input.value; changed(state); });
            state.searchInput = input;
            bar.appendChild(input);
        }

        const toggle = button('btn btn--ghost table-tools__toggle', t('common.table_filters'));
        toggle.setAttribute('aria-expanded', String(state.open));
        toggle.classList.toggle('is-active', state.open);
        const badge = document.createElement('span');
        badge.className = 'table-tools__badge';
        toggle.appendChild(badge);
        toggle.addEventListener('click', () => setOpen(state, !state.open));
        state.toggleBtn = toggle;

        const clear = button('btn btn--ghost table-tools__clear', t('common.table_clear'));
        clear.hidden = !active(state);
        clear.addEventListener('click', () => clearAll(state));
        state.clearBtn = clear;

        const count = document.createElement('span');
        count.className = 'stat-card__meta table-tools__count';
        count.setAttribute('aria-live', 'polite');
        state.countEl = state.onChange ? null : count;

        bar.append(toggle, clear);
        if (!state.onChange) bar.appendChild(count);
        return bar;
    }

    /** Put the table in a scroll box (unless it is in one) with the toolbar above that box. */
    function place(state) {
        const table = state.table;
        const parent = table.parentElement;
        if (!parent) return;
        const wrapped = parent.matches('.table-scroll, .report-table-wrap')
            && parent.children.length === 1;
        if (wrapped) {
            if (!state.ownToolbar) parent.before(state.toolbar);
            return;
        }
        const scroll = document.createElement('div');
        scroll.className = 'table-scroll';
        state.mutating = true;
        table.before(scroll);
        if (!state.ownToolbar) scroll.before(state.toolbar);
        scroll.appendChild(table);
        state.mutating = false;
    }

    /**
     * Enhance one table. Idempotent. opts:
     *   toolbar:  an existing element to put the Filters/Clear buttons in (Devices' toolbar)
     *   search:   false to leave the search box out (the page has its own)
     *   onChange: ({query, columns}) => void -- the page filters its own data; no row is hidden
     *             here. columns is keyed by each header's data-sort (or data-key).
     */
    function enhance(table, opts = {}) {
        if (!table || states.has(table)) return states.get(table) || null;
        const cols = columns(table);
        if (!cols.some((c) => c.filterable)) return null;
        const mode = table.dataset.tableTools || '';
        const saved = loadState(table);
        const state = {
            table,
            cols,
            ...saved,
            onChange: opts.onChange || null,
            withSearch: opts.search !== false && mode !== 'filters',
            mutating: false,
        };
        states.set(table, state);

        // A box on an earlier copy of this table (rebuilt by its page) had focus: hand the
        // caret to the same box on the new one, or typing stops mid-word on every refresh.
        const refocus = focusMemo.focused && focusMemo.key === storageKey(table)
            && (!document.activeElement || document.activeElement === document.body);

        // The card layout below 640px (components.css) keys off this class rather than off
        // .data-table itself: only a table we have labelled can be read as cards, and a
        // header row built without a <thead> would otherwise become a card of its own.
        table.classList.add('data-table--cards');
        state.filterTr = filterRow(state);
        if (!table.tHead) table.createTHead();
        table.tHead.appendChild(state.filterTr);
        state.toolbar = buildToolbar(state, opts);
        place(state);
        // Not changed(): a page in onChange mode reads the restored filters off the returned
        // state when it is ready, rather than being called back before its data has loaded.
        refreshButtons(state);
        labelCells(state);
        apply(state);
        if (refocus) {
            const target = focusMemo.field === ''
                ? state.searchInput
                : state.filterTr.querySelector(`input[data-key="${CSS.escape(focusMemo.field)}"]`);
            if (target) {
                target.focus();
                target.setSelectionRange(target.value.length, target.value.length);
            }
        }

        // The whole table, subtree: a page may add its tbody after inserting the table, and a
        // live reading rewriting one cell's text must re-run the filter on that row too.
        // Only childList, so the hidden attribute apply() writes on every row never wakes it.
        // The one child apply() adds or removes is the no-match row, and only when that state
        // flips -- so the run it wakes changes nothing and the loop ends there. Rejected: an
        // "applying" flag around apply(). Observer callbacks are microtasks, so the flag was
        // always false again by the time one ran, and it guarded nothing.
        const observer = new MutationObserver(() => {
            labelCells(state);
            apply(state);
        });
        observer.observe(table, { childList: true, subtree: true });
        return state;
    }

    // ---------------- Auto-enhance ----------------
    function scan(root) {
        if (!root?.querySelectorAll) return;
        const tables = root.matches?.('table.data-table')
            ? [root]
            : root.querySelectorAll('table.data-table');
        for (const table of tables) {
            if (table.dataset.tableTools === 'off' || table.dataset.tableTools === 'manual') continue;
            enhance(table);
        }
    }

    /** A page that rebuilds its table drops the old one -- and with it our wrapper's contents.
     *  Take the toolbar and the empty scroll box with it, or each refresh leaves another
     *  orphaned search box above the new table. */
    function sweep(node) {
        if (!node?.querySelectorAll) return;
        const tables = node.matches?.('table.data-table') ? [node] : node.querySelectorAll('table.data-table');
        for (const table of tables) {
            const state = states.get(table);
            if (!state || state.mutating || table.isConnected) continue;
            if (!state.ownToolbar && state.toolbar.isConnected) state.toolbar.remove();
        }
    }

    function start() {
        scan(document.body);
        new MutationObserver((records) => {
            for (const r of records) {
                r.removedNodes.forEach(sweep);
                r.addedNodes.forEach(scan);
            }
        }).observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();

    window.FleetTable = { enhance };
})();
