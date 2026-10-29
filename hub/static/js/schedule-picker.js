// The schedule picker: one control for "when does this run", shared by every page that
// stores a schedule_spec spec (the Backups page first; rules, reports and discovery next).
//
// **It never computes a next run itself.** Every change is sent to POST /api/schedules/preview
// and the answer is shown in the HUB's time, labelled with the hub's UTC offset. The browser
// may be in another zone, or behind on a DST rule, and a preview an hour off from the run the
// scheduler actually takes is worse than none -- see schedules_web.py. That also means one
// cron dialect in the product, not a JavaScript one beside the Python one.
//
// **A week starts on MONDAY and the wire format is a bitmask, bit 0 = Monday**, the same mask
// patches.maintenance_windows stores and policy-time.js draws. The cron box is the only place
// Sunday is 0, and the server is the only place the two are converted.
//
// **onChange only ever sees a spec the server accepted.** The picker holds the draft, the
// preview validates it, and only then does the owning page get it to save -- so a page cannot
// autosave a half-typed cron expression and be told off for it on every keystroke.
//
// Built with createElement/textContent only, like every other console script.
(function () {
    'use strict';

    const PREVIEW_DELAY_MS = 400;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    /** Monday first, matching the mask. Seven literal keys, because tests/test_i18n.py only
     *  scans literals -- a key built from the day number would be invisible to it. */
    function dayNames() {
        return [t('schedule.day.mon'), t('schedule.day.tue'), t('schedule.day.wed'),
                t('schedule.day.thu'), t('schedule.day.fri'), t('schedule.day.sat'),
                t('schedule.day.sun')];
    }

    function clock(minutes) {
        const value = Math.max(0, Math.min(1439, Number(minutes) || 0));
        return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
    }

    function minutesOf(text) {
        const match = /^(\d{1,2}):(\d{2})$/.exec(String(text || ''));
        if (!match) return null;
        return Number(match[1]) * 60 + Number(match[2]);
    }

    function offsetLabel(seconds) {
        const sign = seconds < 0 ? '−' : '+';
        const abs = Math.abs(seconds);
        const hours = String(Math.floor(abs / 3600)).padStart(2, '0');
        const minutes = String(Math.floor((abs % 3600) / 60)).padStart(2, '0');
        return `UTC${sign}${hours}:${minutes}`;
    }

    /** An epoch as the hub's wall clock: shift by the hub's offset, then format as UTC. */
    function hubTime(epoch, offset) {
        return new Date((epoch + offset) * 1000).toLocaleString(window.HUB_LANG || undefined, {
            timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short',
            hour: '2-digit', minute: '2-digit',
        });
    }

    function modeOf(spec, allowNone) {
        if (!spec) return allowNone ? 'none' : 'weekly';
        return spec.kind;
    }

    /**
     * mount(container, options) -> { get() }
     *
     * options:
     *   purpose    'fire' (instants) or 'window' (a span when something may act)
     *   value      the stored spec, or null
     *   allowNone  offer a "not scheduled" mode, whose meaning the page states in noneLabel
     *   noneLabel  the caption of that mode ("Every N hours, as set above")
     *   kinds      which kinds to offer; default all for the purpose
     *   onChange   called with a server-accepted spec, or null for "none"
     */
    function mount(container, options) {
        const opts = options || {};
        const purpose = opts.purpose === 'window' ? 'window' : 'fire';
        const kinds = opts.kinds || (purpose === 'window' ? ['window', 'cron']
                                                         : ['weekly', 'interval', 'cron']);
        const onChange = typeof opts.onChange === 'function' ? opts.onChange : function () {};

        // The draft, per mode, so flipping between modes does not throw away what was typed.
        const initial = opts.value || null;
        let mode = modeOf(initial, !!opts.allowNone);
        const draft = {
            weekly: { days_mask: 0b0011111, times: [120] },
            interval: { every: 6, unit: 'hours' },
            window: { days_mask: 0b0011111, start: 480, end: 1080 },
            cron: { expr: '' },
        };
        if (initial && initial.kind === 'weekly') {
            draft.weekly = { days_mask: initial.days_mask, times: initial.times.slice() };
        } else if (initial && initial.kind === 'interval') {
            const hours = initial.every_minutes % 60 === 0;
            draft.interval = { every: hours ? initial.every_minutes / 60 : initial.every_minutes,
                               unit: hours ? 'hours' : 'minutes' };
        } else if (initial && initial.kind === 'window') {
            draft.window = { days_mask: initial.days_mask, start: initial.start_minute,
                             end: (initial.start_minute + initial.duration_minutes) % 1440 };
        } else if (initial && initial.kind === 'cron') {
            draft.cron = { expr: initial.expr };
        }

        const root = el('div', 'schedule-picker');
        const modeRow = el('div', 'flex flex-wrap items-center gap-2');
        const modeSelect = el('select', 'input');
        const modeLabels = {
            none: opts.noneLabel || t('schedule.mode.none'),
            weekly: t('schedule.mode.weekly'),
            interval: t('schedule.mode.interval'),
            window: t('schedule.mode.window'),
            cron: t('schedule.mode.cron'),
        };
        (opts.allowNone ? ['none'] : []).concat(kinds).forEach((kind) => {
            const option = el('option', null, modeLabels[kind]);
            option.value = kind;
            if (kind === mode) option.selected = true;
            modeSelect.appendChild(option);
        });
        modeRow.appendChild(modeSelect);
        root.appendChild(modeRow);

        const body = el('div', 'flex flex-wrap items-center gap-2');
        root.appendChild(body);
        const previewEl = el('p', 'setting__default');
        root.appendChild(previewEl);
        const errorEl = el('div', 'setting__error');
        root.appendChild(errorEl);
        container.appendChild(root);

        let accepted = initial;
        let timer = null;
        let seq = 0;

        function daysControl(state) {
            const wrap = el('div', 'flex flex-wrap items-center gap-2');
            dayNames().forEach((label, bit) => {
                const item = el('label', 'stat-card__meta');
                const box = el('input', 'checkbox');
                box.type = 'checkbox';
                box.checked = !!(state.days_mask & (1 << bit));
                box.addEventListener('change', () => {
                    state.days_mask = box.checked ? (state.days_mask | (1 << bit))
                                                  : (state.days_mask & ~(1 << bit));
                    changed();
                });
                item.appendChild(box);
                item.appendChild(document.createTextNode(' ' + label));
                wrap.appendChild(item);
            });
            return wrap;
        }

        function timeInput(value, onValue) {
            const input = el('input', 'input');
            input.type = 'time';
            input.value = clock(value);
            input.addEventListener('change', () => {
                const minutes = minutesOf(input.value);
                if (minutes !== null) { onValue(minutes); changed(); }
            });
            return input;
        }

        function renderBody() {
            body.replaceChildren();
            if (mode === 'none') return;
            if (mode === 'weekly') {
                body.appendChild(daysControl(draft.weekly));
                const times = el('div', 'flex flex-wrap items-center gap-2');
                body.appendChild(el('span', 'setting__label', t('schedule.at')));
                draft.weekly.times.forEach((minutes, index) => {
                    times.appendChild(timeInput(minutes, (v) => { draft.weekly.times[index] = v; }));
                    if (draft.weekly.times.length > 1) {
                        const remove = el('button', 'btn btn--ghost', '×');
                        remove.type = 'button';
                        remove.title = t('schedule.remove_time');
                        remove.setAttribute('aria-label', t('schedule.remove_time'));
                        remove.addEventListener('click', () => {
                            draft.weekly.times.splice(index, 1);
                            renderBody();
                            changed();
                        });
                        times.appendChild(remove);
                    }
                });
                const add = el('button', 'btn btn--ghost', t('schedule.add_time'));
                add.type = 'button';
                add.addEventListener('click', () => {
                    draft.weekly.times.push(12 * 60);
                    renderBody();
                    changed();
                });
                times.appendChild(add);
                body.appendChild(times);
            } else if (mode === 'interval') {
                body.appendChild(el('span', 'setting__label', t('schedule.every')));
                const number = el('input', 'input');
                number.type = 'number';
                number.min = '1';
                number.value = String(draft.interval.every);
                number.addEventListener('change', () => {
                    draft.interval.every = Number(number.value);
                    changed();
                });
                body.appendChild(number);
                const unit = el('select', 'input');
                [['minutes', t('schedule.unit.minutes')], ['hours', t('schedule.unit.hours')]]
                    .forEach(([value, label]) => {
                        const option = el('option', null, label);
                        option.value = value;
                        if (value === draft.interval.unit) option.selected = true;
                        unit.appendChild(option);
                    });
                unit.addEventListener('change', () => { draft.interval.unit = unit.value; changed(); });
                body.appendChild(unit);
            } else if (mode === 'window') {
                body.appendChild(daysControl(draft.window));
                body.appendChild(el('span', 'setting__label', t('schedule.from')));
                body.appendChild(timeInput(draft.window.start, (v) => { draft.window.start = v; }));
                body.appendChild(el('span', 'setting__label', t('schedule.until')));
                body.appendChild(timeInput(draft.window.end, (v) => { draft.window.end = v; }));
            } else if (mode === 'cron') {
                const input = el('input', 'input');
                input.type = 'text';
                input.autocomplete = 'off';
                input.spellcheck = false;
                input.placeholder = purpose === 'window' ? '* 8-17 * * 1-5' : '0 3 * * 0';
                input.value = draft.cron.expr;
                input.setAttribute('aria-label', t('schedule.mode.cron'));
                input.addEventListener('input', () => { draft.cron.expr = input.value; changed(); });
                body.appendChild(input);
                body.appendChild(el('p', 'setting__default', t('schedule.cron_help')));
            }
        }

        function specFromDraft() {
            if (mode === 'weekly') {
                return { kind: 'weekly', days_mask: draft.weekly.days_mask,
                         times: draft.weekly.times.slice() };
            }
            if (mode === 'interval') {
                const every = Number(draft.interval.every) || 0;
                return { kind: 'interval',
                         every_minutes: draft.interval.unit === 'hours' ? every * 60 : every };
            }
            if (mode === 'window') {
                const start = draft.window.start;
                // An end before the start wraps past midnight: 22:00 until 06:00 is eight hours.
                let duration = (draft.window.end - start + 1440) % 1440;
                if (duration === 0) duration = 1440;
                return { kind: 'window', days_mask: draft.window.days_mask, start_minute: start,
                         duration_minutes: duration };
            }
            return { kind: 'cron', expr: draft.cron.expr };
        }

        // `initial` describes what is already stored: it must not count as a change, or
        // merely opening the page would save the schedule back over itself.
        async function refreshPreview(initial) {
            const mine = ++seq;
            if (mode === 'none') {
                previewEl.textContent = '';
                errorEl.textContent = '';
                accepted = null;
                if (!initial) onChange(null);
                return;
            }
            const spec = specFromDraft();
            try {
                const resp = await fetch('/api/schedules/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ spec: spec, purpose: purpose, count: 5 }),
                });
                const answer = await resp.json().catch(() => ({}));
                if (mine !== seq) return;       // a newer edit is already being previewed
                if (!resp.ok) {
                    previewEl.textContent = '';
                    errorEl.textContent = answer.error || `HTTP ${resp.status}`;
                    return;
                }
                errorEl.textContent = '';
                const runs = (answer.next || []).map((e) => hubTime(e, answer.hub_utc_offset));
                previewEl.textContent = t(purpose === 'window' ? 'schedule.next_openings'
                                                               : 'schedule.next_runs', {
                    zone: offsetLabel(answer.hub_utc_offset || 0),
                    runs: runs.join(' · '),
                });
                const changedSpec = JSON.stringify(answer.spec) !== JSON.stringify(accepted);
                accepted = answer.spec;
                if (changedSpec && !initial) onChange(answer.spec);
            } catch (e) {
                if (mine !== seq) return;
                errorEl.textContent = e.message;
            }
        }

        function changed() {
            clearTimeout(timer);
            timer = setTimeout(() => refreshPreview(false), PREVIEW_DELAY_MS);
        }

        modeSelect.addEventListener('change', () => {
            mode = modeSelect.value;
            renderBody();
            changed();
        });

        renderBody();
        refreshPreview(true);

        return { get: () => accepted };
    }

    window.FleetSchedulePicker = { mount: mount };
})();
