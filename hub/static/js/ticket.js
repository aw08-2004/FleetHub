// One ticket (roadmap #31): what the person wrote, their screenshots, the machine as it was when
// they wrote it, the labels and the thread -- and the button that hands it to the assistant.
//
// textContent / createElement only, never innerHTML: every string here -- the subject, the body,
// a program name in the facts -- came from a PC this hub does not control.
//
// AI triage used to be a second AI surface on this page: its own provider call, its own
// summary box, its own "draft script" button. It is now the console assistant (roadmap #26),
// opened in a new conversation with this ticket sent to it, so a follow-up question, a check
// on the machine or a drafted script all happen in one place, behind the assistant's own
// confirm tiers. Rejected: keeping both -- two AI answers to one ticket that disagree, and
// two places to look.
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

    /** The facts as [label, value] pairs. One list for the table AND the assistant's message,
     *  so what the operator sees and what the model is told cannot drift apart. */
    function factLines() {
        var f = ticket.facts || {};
        if (f.error) return [[t('tickets.fact_error'), f.error]];
        return [
            [t('tickets.fact_captured'), epoch(f.captured_at)],
            [t('tickets.fact_os'), [f.os?.label, f.os?.build].filter(Boolean).join(' · ')],
            [t('tickets.fact_hardware'), [f.manufacturer, f.model].filter(Boolean).join(' ')],
            [t('tickets.fact_serial'), f.serial_number],
            [t('tickets.fact_agent'), f.agent_version],
            [t('tickets.fact_uptime'), duration(f.uptime_seconds)],
            [t('tickets.fact_disks'), (f.disks || []).map(function (d) {
                return t('tickets.disk_value', { name: d.name, free: d.free_gb, total: d.total_gb, pct: d.free_pct });
            }).join(', ')],
            [t('tickets.fact_alerts'), (f.open_alerts || []).map(function (a) {
                return a.text || a.kind;
            }).join('; ')],
            [t('tickets.fact_patches'), (f.patches || []).map(function (p) {
                return (p.kb ? p.kb + ' ' : '') + (p.title || '') + ' (' + p.status + ', ' + epoch(p.resolved_at || p.attempted_at) + ')';
            }).join('; ')],
            [t('tickets.fact_installed'), (f.installed || []).map(function (p) {
                return p.name + (p.version ? ' ' + p.version : '') + ' (' + p.install_date + ')';
            }).join('; ')]
        ];
    }

    function renderFacts() {
        var tbody = document.getElementById('ticket-facts');
        tbody.textContent = '';
        factLines().forEach(function (line) { factRow(tbody, line[0], line[1]); });
    }

    function renderAi() {
        var card = document.getElementById('ticket-ai');
        card.hidden = !(card.dataset.assistant && ticket.can_manage);
    }

    /** What the assistant is sent. **The same line the old inline triage held: never who
     *  reported it, never the screenshots** -- the ticket text, its state and the facts. The
     *  assistant can read the ticket route itself if it needs more, through the same gates
     *  as the operator. */
    function buildDraft() {
        var lines = [
            t('tickets.assistant_draft_intro', {
                id: ticket.id, subject: ticket.subject, machine: ticket.machine,
                link: '/tickets/' + ticket.id
            }),
            t('tickets.assistant_draft_state', {
                urgency: URGENCY_TEXT[ticket.urgency] || ticket.urgency,
                status: STATUS_TEXT[ticket.status] || ticket.status
            }),
            '',
            t('tickets.description') + ':',
            ticket.body || t('tickets.no_description'),
            '',
            t('tickets.facts') + ':'
        ];
        factLines().forEach(function (line) {
            var value = line[1];
            if (value !== null && value !== undefined && value !== '') lines.push('- ' + line[0] + ': ' + value);
        });
        lines.push('', t('tickets.assistant_draft_ask'));
        return lines.join('\n');
    }

    function askAssistant() {
        var text = buildDraft();
        var shell = null;
        try { shell = window.parent !== window ? window.parent.FleetShell : null; } catch (e) { /* ok */ }
        if (shell && shell.askAssistant) {
            shell.askAssistant(text);
            return;
        }
        // Opened as a bare page, outside the shell: assistant.js reads the same one-shot key.
        try { sessionStorage.setItem('tempmonitor:assistant:draft', text); } catch (e) { /* ok */ }
        location.href = '/assistant';
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
            renderAi();
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
    document.getElementById('ticket-ask-assistant').addEventListener('click', function () {
        if (ticket) askAssistant();
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
