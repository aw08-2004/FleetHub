// One ticket (roadmap #31): what the person wrote, their screenshots, the machine as it was when
// they wrote it, the labels, the AI's suggestion and the thread.
//
// textContent / createElement only, never innerHTML: every string here -- the subject, the body,
// a program name in the facts, a model's suggested script -- came from a PC or a provider this
// hub does not control. The script in particular is SHOWN, never run: drafting puts it in the
// library switched off, which is the whole of the AI's reach.
(function () {
    'use strict';

    var root = document.getElementById('ticket-root');
    var ticketId = root.dataset.ticketId;
    var base = '/api/tickets/' + encodeURIComponent(ticketId);

    var errorBox = document.getElementById('ticket-error');
    var statusSelect = document.getElementById('ticket-status');
    var ticket = null;

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function epoch(value) {
        return value ? new Date(value * 1000).toLocaleString() : '--';
    }

    async function api(path, options) {
        var resp = await fetch(path, options);
        var data = null;
        try { data = await resp.json(); } catch (e) { /* an empty body is fine */ }
        if (!resp.ok) throw new Error(data?.error || ('HTTP ' + resp.status));
        return data;
    }

    function json(method, payload) {
        // Content-Type: application/json is what makes a cross-origin POST preflight and fail.
        return { method: method, headers: { 'Content-Type': 'application/json' },
                 body: JSON.stringify(payload) };
    }

    function showError(e) {
        errorBox.textContent = e.message || String(e);
        errorBox.hidden = false;
    }

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
    var SOURCE_TEXT = {
        auto: t('tickets.label_source_auto'),
        ai: t('tickets.label_source_ai'),
        operator: t('tickets.label_source_operator')
    };
    var URGENCY_PILL = { low: 'muted', normal: 'muted', high: 'warn', critical: 'danger' };

    function metaItem(label, value) {
        var span = el('span', 'stat-card__meta');
        span.appendChild(el('strong', null, label + ': '));
        if (value instanceof Node) span.appendChild(value);
        else span.appendChild(document.createTextNode(value || '--'));
        return span;
    }

    function renderHeader() {
        document.getElementById('ticket-subject').textContent = '#' + ticket.id + ' ' + ticket.subject;
        var meta = document.getElementById('ticket-meta');
        meta.textContent = '';
        var pill = el('span', 'status-pill status-pill--' + (URGENCY_PILL[ticket.urgency] || 'muted'));
        pill.appendChild(el('span', 'status-pill__dot'));
        pill.appendChild(document.createTextNode(URGENCY_TEXT[ticket.urgency] || ticket.urgency));
        meta.appendChild(pill);
        meta.appendChild(metaItem(t('tickets.status'), STATUS_TEXT[ticket.status] || ticket.status));
        var machineLink = el('a', null, ticket.machine);
        machineLink.href = '/machine/' + encodeURIComponent(ticket.machine);
        meta.appendChild(metaItem(t('tickets.machine'), machineLink));
        var reporter = ticket.reporter_name || '--';
        if (ticket.reporter_kind === 'local') reporter += ' (' + t('tickets.reporter_local') + ')';
        meta.appendChild(metaItem(t('tickets.reporter'), reporter));
        meta.appendChild(metaItem(t('tickets.opened'), epoch(ticket.created_at)));
        var due = metaItem(t('tickets.due'), ticket.due_at ? epoch(ticket.due_at) : '--');
        var done = ticket.status === 'resolved' || ticket.status === 'closed';
        if (ticket.due_at && ticket.due_at <= Date.now() / 1000 && !done) {
            due.className = 'status-pill--danger';
            due.title = t('tickets.overdue');
        }
        meta.appendChild(due);
        meta.appendChild(metaItem(t('tickets.assignee'), ticket.assignee || '--'));
    }

    function renderBody() {
        document.getElementById('ticket-body').textContent = ticket.body || t('tickets.no_description');
        var shots = document.getElementById('ticket-shots');
        shots.textContent = '';
        (ticket.attachments || []).forEach(function (a) {
            var url = base + '/attachments/' + a.id;
            var link = el('a');
            link.href = url;
            link.target = '_blank';
            link.rel = 'noopener';
            var img = el('img');
            img.src = url;
            img.alt = t('tickets.screenshot_alt', { id: a.id });
            img.style.maxWidth = '320px';
            img.style.maxHeight = '200px';
            img.style.border = '1px solid var(--card-border)';
            img.style.borderRadius = 'var(--radius-sm)';
            link.appendChild(img);
            shots.appendChild(link);
        });
        if (!(ticket.attachments || []).length) {
            shots.appendChild(el('span', 'stat-card__meta', t('tickets.no_screenshots')));
        }
    }

    function renderLabels() {
        var box = document.getElementById('ticket-labels');
        box.textContent = '';
        (ticket.labels || []).forEach(function (l) {
            var badge = el('span', 'badge', l.label);
            badge.title = SOURCE_TEXT[l.source] || l.source;
            // Only a model's guess is marked on the chip itself; where the rest came from is
            // in the tooltip. Spelling out every source inline made each chip a sentence.
            if (l.source === 'ai') badge.appendChild(el('span', 'stat-card__meta', t('tickets.label_short_ai')));
            if (ticket.can_manage) {
                var remove = el('button', 'btn btn--icon', '×');
                remove.type = 'button';
                remove.setAttribute('aria-label', t('tickets.label_remove', { label: l.label }));
                remove.addEventListener('click', function () {
                    api(base + '/labels', json('DELETE', { label: l.label })).then(load).catch(showError);
                });
                badge.appendChild(remove);
            }
            box.appendChild(badge);
        });
        if (!(ticket.labels || []).length) box.appendChild(el('span', 'stat-card__meta', t('tickets.no_labels')));
        document.getElementById('ticket-label-form').hidden = !ticket.can_manage;
    }

    function factRow(tbody, label, value) {
        var tr = el('tr');
        var header = el('th', null, label);
        header.scope = 'row';
        tr.appendChild(header);
        tr.appendChild(el('td', null, value === null || value === undefined || value === '' ? '--' : String(value)));
        tbody.appendChild(tr);
    }

    function duration(seconds) {
        if (typeof seconds !== 'number') return null;
        var days = Math.floor(seconds / 86400);
        var hours = Math.floor((seconds % 86400) / 3600);
        return t('tickets.uptime_value', { days: days, hours: hours });
    }

    function renderFacts() {
        var f = ticket.facts || {};
        var tbody = document.getElementById('ticket-facts');
        tbody.textContent = '';
        if (f.error) {
            factRow(tbody, t('tickets.fact_error'), f.error);
            return;
        }
        factRow(tbody, t('tickets.fact_captured'), epoch(f.captured_at));
        factRow(tbody, t('tickets.fact_os'), [f.os?.label, f.os?.build].filter(Boolean).join(' · '));
        factRow(tbody, t('tickets.fact_hardware'), [f.manufacturer, f.model].filter(Boolean).join(' '));
        factRow(tbody, t('tickets.fact_serial'), f.serial_number);
        factRow(tbody, t('tickets.fact_agent'), f.agent_version);
        factRow(tbody, t('tickets.fact_uptime'), duration(f.uptime_seconds));
        factRow(tbody, t('tickets.fact_disks'), (f.disks || []).map(function (d) {
            return t('tickets.disk_value', { name: d.name, free: d.free_gb, total: d.total_gb, pct: d.free_pct });
        }).join(', '));
        factRow(tbody, t('tickets.fact_alerts'), (f.open_alerts || []).map(function (a) {
            return a.text || a.kind;
        }).join('; '));
        factRow(tbody, t('tickets.fact_patches'), (f.patches || []).map(function (p) {
            return (p.kb ? p.kb + ' ' : '') + (p.title || '') + ' (' + p.status + ', ' + epoch(p.resolved_at || p.attempted_at) + ')';
        }).join('; '));
        factRow(tbody, t('tickets.fact_installed'), (f.installed || []).map(function (p) {
            return p.name + (p.version ? ' ' + p.version : '') + ' (' + p.install_date + ')';
        }).join('; '));
    }

    function renderTriage() {
        var triage = ticket.triage;
        var askButton = document.getElementById('ticket-triage');
        var draftButton = document.getElementById('ticket-draft');
        askButton.hidden = !ticket.can_manage;
        askButton.textContent = triage ? t('tickets.ai_ask_again') : t('tickets.ai_ask');
        var result = document.getElementById('ticket-triage-result');
        result.hidden = !triage;
        draftButton.hidden = !(triage?.script && ticket.can_draft);
        if (!triage) return;
        document.getElementById('ticket-triage-summary').textContent = triage.summary;
        document.getElementById('ticket-triage-cause').textContent =
            triage.likely_cause ? t('tickets.ai_cause', { cause: triage.likely_cause }) : '';
        var steps = document.getElementById('ticket-triage-steps');
        steps.textContent = '';
        (triage.steps || []).forEach(function (s) { steps.appendChild(el('li', null, s)); });
        var script = document.getElementById('ticket-triage-script');
        script.hidden = !triage.script;
        script.textContent = triage.script ? triage.script.body : '';
        var meta = t('tickets.ai_meta', { model: triage.model || triage.provider || '--',
                                          when: epoch(triage.created_at) });
        if (triage.script_name) meta += ' ' + t('tickets.ai_drafted', { name: triage.script_name });
        document.getElementById('ticket-triage-meta').textContent = meta;
    }

    function renderThread() {
        var box = document.getElementById('ticket-messages');
        box.textContent = '';
        (ticket.messages || []).forEach(function (m) {
            var item = el('div');
            item.style.borderTop = '1px solid var(--card-border)';
            item.style.padding = 'var(--space-3) 0';
            var who = m.author_kind === 'reporter' ? (ticket.reporter_name || t('tickets.reporter')) : m.author;
            var head = el('p', 'stat-card__meta', who + ' · ' + epoch(m.created_at));
            if (m.internal) head.appendChild(el('span', 'badge', t('tickets.internal_badge')));
            item.appendChild(head);
            var text = el('p', null, m.body);
            text.style.whiteSpace = 'pre-wrap';
            item.appendChild(text);
            box.appendChild(item);
        });
        document.getElementById('ticket-messages-empty').hidden = (ticket.messages || []).length > 0;
        document.getElementById('ticket-reply-form').hidden = !ticket.can_manage;
    }

    function renderManage() {
        document.getElementById('ticket-manage').hidden = !ticket.can_manage;
        if (!ticket.can_manage) return;
        if (!statusSelect.options.length) {
            (ticket.statuses || []).forEach(function (s) {
                var o = el('option', null, STATUS_TEXT[s] || s);
                o.value = s;
                statusSelect.appendChild(o);
            });
        }
        statusSelect.value = ticket.status;
    }

    async function load() {
        try {
            ticket = await api(base);
            errorBox.hidden = true;
            renderHeader();
            renderBody();
            renderManage();
            renderLabels();
            renderFacts();
            renderTriage();
            renderThread();
        } catch (e) {
            showError(e);
        }
    }

    statusSelect.addEventListener('change', function () {
        api(base + '/status', json('POST', { status: statusSelect.value })).then(load).catch(showError);
    });
    document.getElementById('ticket-assign-me').addEventListener('click', function () {
        api(base + '/assign', json('POST', { assignee: 'me' })).then(load).catch(showError);
    });
    document.getElementById('ticket-unassign').addEventListener('click', function () {
        api(base + '/assign', json('POST', { assignee: '' })).then(load).catch(showError);
    });
    document.getElementById('ticket-label-form').addEventListener('submit', function (ev) {
        ev.preventDefault();
        var input = document.getElementById('ticket-label-input');
        var label = input.value.trim();
        if (!label) return;
        api(base + '/labels', json('POST', { label: label })).then(function () {
            input.value = '';
            return load();
        }).catch(showError);
    });
    document.getElementById('ticket-reply-form').addEventListener('submit', function (ev) {
        ev.preventDefault();
        var text = document.getElementById('ticket-reply');
        var internal = document.getElementById('ticket-internal');
        if (!text.value.trim()) return;
        api(base + '/messages', json('POST', { body: text.value, internal: internal.checked }))
            .then(function () {
                text.value = '';
                internal.checked = false;
                return load();
            }).catch(showError);
    });
    document.getElementById('ticket-triage').addEventListener('click', function (ev) {
        var status = document.getElementById('ticket-ai-status');
        var button = ev.currentTarget;
        button.disabled = true;
        status.textContent = t('tickets.ai_working');
        api(base + '/triage', json('POST', {})).then(function () {
            status.textContent = '';
            return load();
        }).catch(function (e) {
            status.textContent = t('tickets.ai_failed', { error: e.message });
        }).finally(function () { button.disabled = false; });
    });
    document.getElementById('ticket-draft').addEventListener('click', function () {
        var status = document.getElementById('ticket-ai-status');
        api(base + '/script', json('POST', {})).then(function (script) {
            status.textContent = t('tickets.ai_drafted', { name: script.name });
            return load();
        }).catch(function (e) {
            status.textContent = t('tickets.ai_failed', { error: e.message });
        });
    });

    load().catch(showError);
    // Another operator's reply, a status or assignee change, the AI triage landing: all
    // `tickets` notices. The tickets blueprint's own events carry the id, so a notice about a
    // different ticket is skipped; a generic one (no id) re-reads, which is cheap. The reply
    // box is static markup, so a re-read never eats a half-typed answer.
    window.FleetLive.watch(['tickets'], load, {
        filter: function (change) { return !change.id || String(change.id) === String(ticketId); },
    });
}());
