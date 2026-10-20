// Tickets page (roadmap #31): the queue of tickets people opened from their own PCs.
//
// House rules, the same ones watchdogs.js follows: everything is built with textContent /
// createElement, never innerHTML -- a subject, a reporter name and a label are arbitrary text
// typed on a PC this hub does not control -- and the status vocabulary comes from the API, not
// a copy here.
//
// **Live, but only as a nudge.** The socket's `ticket_new` / `ticket_updated` carry ids, never
// text (tickets_web._emit), so a new ticket makes this page re-read the list through the gated
// route rather than trusting what the socket said.
(function () {
    'use strict';

    var body = document.getElementById('tickets-body');
    var empty = document.getElementById('tickets-empty');
    var count = document.getElementById('tickets-count');
    var errorBox = document.getElementById('tickets-error');
    var statusSelect = document.getElementById('tickets-status');
    var search = document.getElementById('tickets-search');

    var rows = [];
    var vocabularyLoaded = false;

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function opt(value, label) {
        var o = document.createElement('option');
        o.value = value;
        o.textContent = label;
        return o;
    }

    function epoch(value) {
        return value ? new Date(value * 1000).toLocaleString() : '--';
    }

    // Literal keys, one per value, so tests/test_i18n.py's scan sees every one of them.
    var URGENCY_TEXT = {
        low: t('tickets.urgency_low'),
        normal: t('tickets.urgency_normal'),
        high: t('tickets.urgency_high'),
        critical: t('tickets.urgency_critical')
    };
    var STATUS_TEXT = {
        'new': t('tickets.status_new'),
        open: t('tickets.status_open'),
        waiting: t('tickets.status_waiting'),
        resolved: t('tickets.status_resolved'),
        closed: t('tickets.status_closed')
    };
    var URGENCY_PILL = { low: 'muted', normal: 'muted', high: 'warn', critical: 'danger' };

    function urgencyPill(urgency) {
        var pill = el('span', 'status-pill status-pill--' + (URGENCY_PILL[urgency] || 'muted'));
        pill.appendChild(el('span', 'status-pill__dot'));
        pill.appendChild(document.createTextNode(URGENCY_TEXT[urgency] || urgency));
        return pill;
    }

    var SOURCE_TEXT = {
        auto: t('tickets.label_source_auto'),
        ai: t('tickets.label_source_ai'),
        operator: t('tickets.label_source_operator')
    };

    function labelBadge(label) {
        var badge = el('span', 'badge', label.label);
        badge.title = SOURCE_TEXT[label.source] || label.source;
        return badge;
    }

    function fillStatuses(statuses) {
        if (vocabularyLoaded) return;
        vocabularyLoaded = true;
        statusSelect.appendChild(opt('open', t('tickets.filter_open')));
        statusSelect.appendChild(opt('', t('tickets.filter_all')));
        statuses.forEach(function (s) { statusSelect.appendChild(opt(s, STATUS_TEXT[s] || s)); });
    }

    function matches(row, needle) {
        if (!needle) return true;
        var hay = [row.subject, row.machine, row.reporter_name, row.assignee]
            .concat((row.labels || []).map(function (l) { return l.label; }))
            .join(' ').toLowerCase();
        return hay.includes(needle);
    }

    function render() {
        var needle = (search.value || '').trim().toLowerCase();
        var shown = rows.filter(function (r) { return matches(r, needle); });
        body.textContent = '';
        var now = Date.now() / 1000;
        shown.forEach(function (row) {
            var tr = document.createElement('tr');
            var link = el('a', null, '#' + row.id);
            link.href = '/tickets/' + row.id;
            var idCell = el('td');
            idCell.appendChild(link);
            tr.appendChild(idCell);
            var urgencyCell = el('td');
            urgencyCell.appendChild(urgencyPill(row.urgency));
            tr.appendChild(urgencyCell);
            var subjectCell = el('td');
            var subjectLink = el('a', null, row.subject);
            subjectLink.href = '/tickets/' + row.id;
            subjectCell.appendChild(subjectLink);
            if (row.attachment_count) {
                subjectCell.appendChild(document.createTextNode(' '));
                subjectCell.appendChild(el('span', 'badge',
                    t('tickets.screenshot_count', { count: row.attachment_count })));
            }
            tr.appendChild(subjectCell);
            var machineCell = el('td');
            var machineLink = el('a', null, row.machine);
            machineLink.href = '/machine/' + encodeURIComponent(row.machine);
            machineCell.appendChild(machineLink);
            tr.appendChild(machineCell);
            tr.appendChild(el('td', null, row.reporter_name || '--'));
            tr.appendChild(el('td', null, STATUS_TEXT[row.status] || row.status));
            tr.appendChild(el('td', 'stat-card__meta', row.assignee || '--'));
            var labelCell = el('td');
            (row.labels || []).slice(0, 4).forEach(function (l) {
                labelCell.appendChild(labelBadge(l));
                labelCell.appendChild(document.createTextNode(' '));
            });
            tr.appendChild(labelCell);
            var dueCell = el('td', 'stat-card__meta', row.due_at ? epoch(row.due_at) : '--');
            var done = row.status === 'resolved' || row.status === 'closed';
            if (row.due_at && row.due_at <= now && !done) {
                dueCell.className = 'status-pill--danger';
                dueCell.title = t('tickets.overdue');
            }
            tr.appendChild(dueCell);
            tr.appendChild(el('td', 'stat-card__meta', epoch(row.created_at)));
            body.appendChild(tr);
        });
        empty.hidden = shown.length > 0;
        count.textContent = t('tickets.count', { count: shown.length });
    }

    async function load() {
        var status = statusSelect.value;
        // The first load has no select to read yet and asks for what it will show selected.
        var query = '?open=1';
        if (vocabularyLoaded && status !== 'open') {
            query = status ? '?status=' + encodeURIComponent(status) : '';
        }
        var resp = await fetch('/api/tickets' + query);
        var data = await resp.json();
        if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
        fillStatuses(data.statuses || []);
        rows = data.tickets || [];
        errorBox.hidden = true;
        render();
    }

    function showError(e) {
        errorBox.textContent = t('tickets.load_failed', { error: e.message });
        errorBox.hidden = false;
    }

    function reload() {
        load().catch(showError);
    }

    statusSelect.addEventListener('change', reload);
    search.addEventListener('input', render);

    reload();
    // The tickets blueprint's own ticket_new / ticket_updated and the generic `tickets`
    // change notice both land here (fleet-live.js folds the first two into the third).
    window.FleetLive.watch(['tickets'], load);
}());
