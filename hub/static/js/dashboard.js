// The Dashboard: the fleet, not the machines in it.
//
// This file used to render one card per machine from /api/machines. That made the front
// page a second, worse Asset Inventory -- the same list with less on it -- so it asks a
// different question now: how many machines, on what, how hot, and what is happening. One
// GET /api/fleet/summary supplies all of it, deliberately as ONE request: the numbers have
// to agree with each other, and eight requests cannot be made to agree on a moment.
//
// Two rules carried over from the version this replaces, and one dropped.
//
//  * EVERY value is written with createElement/textContent, never innerHTML. Machine names
//    reach the hub through /api/report, which is unauthenticated by design, and OS captions
//    come from the same place -- so both are arbitrary attacker-controlled text. This page
//    now draws more of that text than it ever did, not less.
//  * It still does NOT flag high temperatures as a fault. What counts as too hot is an
//    operator-written rule (see rules.py), surfacing in Alerts. The tile here says "over
//    N C" with N on screen, so it reads as the filter it is rather than as a verdict.
//  * NO PER-READING UPDATES: re-fetching a fleet aggregate at 1 Hz is not viable. Instead it
//    is re-read on any change notice (fleet-live.js), every reading included, but at most
//    once per REFRESH_MS -- which matches the summary's own 10-second server cache
//    (app.py), so a faster refetch would only be served the same answer. It used to be a
//    fixed 30-second poll.
(function () {
    'use strict';

    const REFRESH_MS = 10_000;

    const errorEl = document.getElementById('summary-error');
    const generatedEl = document.getElementById('health-generated');
    const healthListEl = document.getElementById('health-list');
    const healthTotalEl = document.getElementById('health-total');
    const osEl = document.getElementById('os-breakdown');
    const tempEl = document.getElementById('tile-temp');
    const cpuEl = document.getElementById('tile-cpu');
    const diskEl = document.getElementById('tile-disk');
    const activityEl = document.getElementById('activity-tiles');
    const alertsEl = document.getElementById('activity-alerts');
    const attentionEl = document.getElementById('attention-lists');
    if (!healthListEl) return;

    // The OS buckets the stacked bar has a colour for (dashboard.css .os-seg--*). The class
    // is built from this list, never from the server's string, so an unexpected bucket gets
    // the "unknown" grey rather than a class name somebody else chose.
    const OS_BUCKETS = new Set(['windows_11', 'windows_10', 'windows_legacy',
                                'windows_server', 'linux', 'android']);

    // A switch of literal keys rather than one key built by concatenating the bucket name
    // onto a prefix. A computed key is invisible to tests/test_i18n.py, which can only scan
    // for literals -- so a bucket whose translation was never written would ship silently
    // and show a raw key. Same reason common.js spells out its status labels one by one.
    function osBucketLabel(bucket) {
        switch (bucket) {
            case 'windows_11': return t('dashboard.os.bucket.windows_11');
            case 'windows_10': return t('dashboard.os.bucket.windows_10');
            case 'windows_legacy': return t('dashboard.os.bucket.windows_legacy');
            case 'windows_server': return t('dashboard.os.bucket.windows_server');
            case 'linux': return t('dashboard.os.bucket.linux');
            case 'android': return t('dashboard.os.bucket.android');
            default: return t('dashboard.os.bucket.unknown');
        }
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    const DASH = '--';

    function num(value) {
        return (value === null || value === undefined) ? DASH : String(value);
    }

    // ---- Bento building blocks (hub 1.189.0) --------------------------------------------
    //
    // The six-tile rows became one hero and five tiles, each a headline number and a few
    // label/value rows under it. The TONE rules are the ones tile() had and are kept word for
    // word in spirit: a value is coloured only when it is itself the news, and a zero or a
    // dash with no tone steps back (tile__value--quiet) so "0 failures" is never as loud as
    // "3 not enrolled".

    /** The value's classes: its tone, or quiet when it is nothing to see. */
    function toneValue(node, value, tone) {
        if (tone) node.classList.add(`tile__value--${tone}`);
        else if (value === 0 || value === null || value === undefined) {
            node.classList.add('tile__value--quiet');
        }
        return node;
    }

    /**
     * One label/value row. A link when there is somewhere to go: a number nobody can drill
     * into is how "1 deployment in flight" sat unexplained for weeks (roadmap #26). `hint`
     * is a second, muted line under the label.
     */
    function kvRow(label, value, { tone = null, hint = null, href = null } = {}) {
        const row = el('div', 'dash-kv__row');
        const dt = el('dt', null, label);
        if (hint) dt.appendChild(el('span', 'dash-kv__hint', hint));
        const dd = el('dd');
        const v = el(href ? 'a' : 'span', 'dash-kv__value', num(value));
        if (href) v.href = href;
        dd.appendChild(toneValue(v, value, tone));
        row.append(dt, dd);
        return row;
    }

    function kvList(...rows) {
        const dl = el('dl', 'dash-kv');
        dl.append(...rows);
        return dl;
    }

    /** A tile's head: its label and its headline number. */
    function tileHead(label, value, tone = null) {
        return [el('span', 'stat-card__label', label),
                toneValue(el('div', 'dash-tile__value', num(value)), value, tone)];
    }

    /** A 0-100 bar. Empty, not absent, on a missing value, so the tile keeps its shape. */
    function meter(pct, tone = null) {
        const track = el('div', 'meter');
        track.setAttribute('aria-hidden', 'true');
        const fill = el('span', tone ? `meter__fill meter__fill--${tone}` : 'meter__fill');
        const n = Number(pct);
        fill.style.width = `${Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0}%`;
        track.appendChild(fill);
        return track;
    }

    // The hero ring: online from twelve o'clock, offline after it. online + offline is the
    // total by construction (app.py counts every row as one or the other), so the two arcs
    // close the ring; "not enrolled" overlaps both and is a row beside it, not a third arc.
    const DONUT_C = 2 * Math.PI * 46;   // r="46" in index.html
    function setDonut(online, offline, total) {
        const on = document.getElementById('donut-online');
        const off = document.getElementById('donut-offline');
        if (!on || !off) return;
        const onLen = total ? DONUT_C * (online || 0) / total : 0;
        const offLen = total ? DONUT_C * (offline || 0) / total : 0;
        on.style.strokeDasharray = `${onLen} ${DONUT_C}`;
        off.style.strokeDasharray = `${offLen} ${DONUT_C}`;
        off.style.strokeDashoffset = `${-onLen}`;
    }

    function renderHealth(summary) {
        const c = summary.counts;
        healthTotalEl.textContent = num(c.total);
        setDonut(c.online, c.offline, c.total);
        healthListEl.replaceChildren(
            kvRow(t('dashboard.health.online'), c.online, { tone: c.online ? 'ok' : null }),
            kvRow(t('dashboard.health.offline'), c.offline,
                  { tone: c.offline ? 'warn' : null }),
            // Not a fault, but the thing that most often explains "why did nothing happen
            // when I clicked that": an unenrolled agent reports telemetry and accepts no
            // commands, so it looks perfectly healthy and does nothing.
            //
            // Its own tone, not Offline's amber: the two shared a colour, so "1 offline" and
            // "3 not enrolled" read as the same kind of problem, and a colour-blind operator
            // had nothing else to tell them apart by. Info-blue says "worth knowing".
            kvRow(t('dashboard.health.never_enrolled'), c.never_enrolled,
                  { tone: c.never_enrolled ? 'info' : null,
                    hint: t('dashboard.health.never_enrolled_hint') }),
            kvRow(t('dashboard.health.open_alerts'), c.open_alerts,
                  { tone: c.open_alerts ? 'warn' : null,
                    href: c.open_alerts ? '/alerts' : null }),
            // With no published release to measure against, nothing can be "behind" -- and
            // a 0 there read as "the whole fleet is current", the one claim the hub had no
            // grounds for. A dash and the reason, instead.
            kvRow(t('dashboard.health.agents_outdated'),
                  c.agent_latest ? c.agents_outdated : null,
                  { tone: c.agent_latest && c.agents_outdated ? 'warn' : null,
                    hint: c.agent_latest
                      ? t('dashboard.health.agent_latest', { version: c.agent_latest })
                      : t('dashboard.health.agent_latest_none') })
        );
        generatedEl.textContent = summary.generated_at
            ? t('dashboard.generated_at', {
                time: new Date(summary.generated_at * 1000).toLocaleTimeString() })
            : '';
    }

    // One stacked bar for the whole fleet, then a legend row per bucket. It was a bar per
    // bucket; a single bar answers "how much of the fleet is still on X" in one glance and
    // takes a tile, not a fold. Lengths are still what is compared -- the reason it was
    // never a pie, and the reason the hero ring stops at two segments.
    function renderOs(summary) {
        const total = summary.counts.total || 0;
        osEl.replaceChildren();
        if (!total) {
            osEl.appendChild(el('p', 'stat-card__meta', t('dashboard.empty')));
            return;
        }
        const bar = el('div', 'os-bar');
        bar.setAttribute('aria-hidden', 'true');
        const legend = el('dl', 'dash-kv');
        for (const entry of summary.os) {
            // Buckets nobody has are dropped rather than shown as zero: a fleet with no
            // Linux does not need a permanent Linux row, and the list stays as long as the
            // fleet is varied.
            if (!entry.count) continue;
            const bucket = OS_BUCKETS.has(entry.bucket) ? entry.bucket : 'unknown';
            const pct = Math.round(100 * entry.count / total);
            const seg = el('span', `os-seg os-seg--${bucket}`);
            seg.style.width = `${100 * entry.count / total}%`;
            bar.appendChild(seg);
            const row = el('div', 'dash-kv__row');
            const dt = el('dt');
            dt.append(el('span', `os-swatch os-seg--${bucket}`),
                      document.createTextNode(osBucketLabel(entry.bucket)));
            const dd = el('dd', null, t('dashboard.os.count', { count: entry.count, pct }));
            row.append(dt, dd);
            legend.appendChild(row);
        }
        osEl.append(bar, legend);
    }

    function renderTelemetry(summary) {
        const s = summary.telemetry;
        const gb = (value) => (value === null || value === undefined || !value)
            ? DASH : t('dashboard.telemetry.gb', { value: Math.round(value) });

        // Temperature: the average as the headline, on a fixed 0-100 °C bar (the machine
        // page's ring uses the same scale), with the peak and the over-threshold count
        // under it. Red at or over the hub's hot threshold -- the same >= the count uses.
        // Uncoloured, 95 °C looked exactly as calm as the average beside it.
        const peakHot = s.peak_cpu_temp !== null && s.threshold_c
            && s.peak_cpu_temp >= s.threshold_c;
        tempEl.replaceChildren(
            ...tileHead(t('dashboard.telemetry.avg_temp'),
                        s.avg_cpu_temp === null ? null : celsius(s.avg_cpu_temp)),
            meter(s.avg_cpu_temp, 'warn'),
            kvList(
                kvRow(t('dashboard.telemetry.peak_temp'),
                      s.peak_cpu_temp === null ? null : celsius(s.peak_cpu_temp),
                      { tone: peakHot ? 'bad' : null }),
                // The threshold is IN the label, not implied by it: this is a filter somebody
                // chose in Settings, not the hub declaring these machines faulty.
                kvRow(t('dashboard.telemetry.over_threshold', { threshold: s.threshold_c }),
                      s.over_threshold, { tone: s.over_threshold ? 'warn' : null })
            )
        );

        // Says how much of the fleet these averages are actually OF. An average over three
        // of two hundred machines is not a fleet average, and a page that showed it without
        // saying so would be quietly lying. It sits in the load tile, under the average.
        cpuEl.replaceChildren(
            ...tileHead(t('dashboard.telemetry.avg_load'),
                        s.avg_cpu_load_pct === null ? null
                            : t('dashboard.telemetry.percent', { value: s.avg_cpu_load_pct })),
            meter(s.avg_cpu_load_pct),
            el('p', 'stat-card__meta', t('dashboard.telemetry.coverage', {
                reporting: s.reporting ?? 0, online: summary.counts.online ?? 0 }))
        );

        // Disk: free space as the headline, the bar as the free FRACTION of the total.
        const freePct = s.disk_total_gb ? 100 * s.disk_free_gb / s.disk_total_gb : null;
        diskEl.replaceChildren(
            ...tileHead(t('dashboard.telemetry.disk_free'),
                        s.disk_free_gb ? gb(s.disk_free_gb) : null),
            el('p', 'stat-card__meta', s.disk_total_gb
                ? t('dashboard.telemetry.of_total', { value: gb(s.disk_total_gb) }) : ''),
            meter(freePct, 'ok'),
            kvList(kvRow(t('dashboard.telemetry.low_disk', { threshold: s.low_disk_free_pct }),
                         s.low_disk_machines, { tone: s.low_disk_machines ? 'warn' : null }))
        );
    }

    function renderActivity(summary) {
        const a = summary.activity;
        activityEl.replaceChildren(
            el('span', 'stat-card__label', t('dashboard.activity.title')),
            kvList(
                kvRow(t('dashboard.activity.deployments_running'), a.deployments_running,
                      { href: a.deployments_running && activityEl.dataset.canDeploy
                          ? '/packages#deployments-pane' : null }),
                kvRow(t('dashboard.activity.deployments_failed'), a.deployments_failed_24h,
                      { tone: a.deployments_failed_24h ? 'bad' : null }),
                kvRow(t('dashboard.activity.backups_ok'), a.backups_ok_24h),
                kvRow(t('dashboard.activity.backups_failed'), a.backups_failed_24h,
                      { tone: a.backups_failed_24h ? 'bad' : null }),
                kvRow(t('dashboard.activity.firmware_jobs'), a.firmware_jobs_active),
                kvRow(t('dashboard.activity.wake_requests'), a.wake_requests_open)
            )
        );

        alertsEl.replaceChildren();
        if (!a.alerts_recent.length) return;
        alertsEl.appendChild(el('div', 'section-title dashboard__subhead',
                                t('dashboard.activity.alerts_title')));
        const list = el('div', 'attention-list');
        for (const alert of a.alerts_recent) {
            const row = el('div', 'attention-list__row');
            if (alert.machine) {
                const link = el('a', 'attention-list__name', alert.machine);
                link.href = '/machine/' + encodeURIComponent(alert.machine);
                row.appendChild(link);
            } else {
                // A fleet-wide alert (a duplicate serial spanning several machines) has no
                // single machine to link to -- but it does have a decision waiting on the
                // Alerts page (which record to keep), so that is where it goes. A plain span
                // here was a dead end on the one row that most needs acting on.
                const link = el('a', 'attention-list__name', t('dashboard.activity.fleet_wide'));
                link.href = '/alerts';
                row.appendChild(link);
            }
            row.appendChild(el('span', 'attention-list__value', alertKindLabel(alert.kind)));
            list.appendChild(row);
        }
        alertsEl.appendChild(list);
    }

    // The alert's KIND in words. It used to print the raw enum ("duplicate_serial") -- an
    // internal identifier, and untranslated in a console that ships German and Spanish.
    // Literal keys, one per kind, for the reason osBucketLabel spells out above.
    function alertKindLabel(kind) {
        switch (kind) {
            case 'duplicate_serial': return t('dashboard.activity.kind.duplicate_serial');
            case 'ad_unmatched': return t('dashboard.activity.kind.ad_unmatched');
            case 'rule': return t('dashboard.activity.kind.rule');
            case 'watchdog': return t('dashboard.activity.kind.watchdog');
            case 'high_temperature': return t('dashboard.activity.kind.high_temperature');
            case 'ticket_new': return t('dashboard.activity.kind.ticket_new');
            case 'ticket_overdue': return t('dashboard.activity.kind.ticket_overdue');
            default: return t('dashboard.activity.kind.other');
        }
    }

    // One decimal, like every other temperature in the console. The ranked list printed the
    // stored float verbatim -- "55.555555 °C" beside a Devices row saying 55.6.
    function celsius(value) {
        const n = Number(value);
        return Number.isFinite(n)
            ? t('dashboard.telemetry.celsius', { value: n.toFixed(1) }) : DASH;
    }

    /** One ranked list: a title and up to `top` rows, each linking to its machine. `hotFor`,
     *  when given, says which rows' values to mark hot (temp--hot). */
    function attentionList(title, rows, valueFor, emptyText, hotFor) {
        const box = el('div', 'attention');
        box.appendChild(el('div', 'section-title dashboard__subhead', title));
        if (!rows.length) {
            box.appendChild(el('p', 'stat-card__meta', emptyText));
            return box;
        }
        const list = el('div', 'attention-list');
        for (const row of rows) {
            const line = el('div', 'attention-list__row');
            const link = el('a', 'attention-list__name', row.machine);
            link.href = '/machine/' + encodeURIComponent(row.machine);
            const value = el('span', 'attention-list__value', valueFor(row));
            if (hotFor && hotFor(row)) value.classList.add('temp--hot');
            line.append(link, value);
            list.appendChild(line);
        }
        box.appendChild(list);
        return box;
    }

    function humanGap(seconds) {
        if (seconds === null || seconds === undefined) return t('dashboard.attention.never');
        const days = Math.floor(seconds / 86400);
        if (days >= 1) return t('dashboard.attention.days', { count: days });
        const hours = Math.floor(seconds / 3600);
        if (hours >= 1) return t('dashboard.attention.hours', { count: hours });
        return t('dashboard.attention.minutes', { count: Math.max(1, Math.floor(seconds / 60)) });
    }

    function renderAttention(summary) {
        const a = summary.attention;
        // The same threshold, and the same >=, as the "over threshold" tile beside it and the
        // machine page -- a list headed "hottest" that coloured nothing made 95 °C look like 40.
        const hotC = Number(summary.telemetry && summary.telemetry.threshold_c);
        const isHot = (r) => Number.isFinite(hotC) && hotC > 0 && Number(r.temp) >= hotC;
        attentionEl.replaceChildren(
            attentionList(t('dashboard.attention.hottest'), a.hottest,
                          (r) => celsius(r.temp),
                          t('dashboard.attention.no_readings'), isHot),
            attentionList(t('dashboard.attention.low_disk'), a.low_disk,
                          (r) => t('dashboard.attention.free_pct', { pct: r.free_pct, gb: r.free_gb }),
                          t('dashboard.attention.no_disks')),
            attentionList(t('dashboard.attention.longest_offline'), a.longest_offline,
                          (r) => humanGap(r.seconds),
                          t('dashboard.attention.all_online'))
        );
    }

    async function refresh() {
        let summary;
        try {
            const response = await fetch('/api/fleet/summary');
            if (!response.ok) throw new Error(t('common.hub_error', { status: response.status }));
            summary = await response.json();
        } catch (e) {
            // The last good numbers stay on screen. A page that blanked itself on one failed
            // poll would be less useful than one showing figures thirty seconds old, and the
            // banner says which it is.
            errorEl.hidden = false;
            errorEl.textContent = e.message;
            return;
        }
        errorEl.hidden = true;
        renderHealth(summary);
        renderOs(summary);
        renderTelemetry(summary);
        renderActivity(summary);
        renderAttention(summary);
    }

    refresh();
    // Every topic: the summary counts machines, alerts, deployments, patches and tickets, so
    // anything changing can move a number here. A hidden tab catches up when shown.
    window.FleetLive.watch('*', refresh, { minInterval: REFRESH_MS, temp: true });
})();
