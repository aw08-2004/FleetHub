// The machine page's Security posture card (roadmap #25 D): antivirus, firewall, AutoRun,
// screen lock, default and administrator accounts, encryption, Secure Boot and the TPM, each
// judged by the hub against the security.posture_* thresholds and labelled with the CIS
// Controls IG1 safeguard it is evidence for.
//
// **Three results, drawn as three.** Passed, failed and unknown each have their own pill, and
// unknown is grey rather than amber: it means "the machine did not say", which is a thing to
// look into, not a finding. A card that coloured unknowns as failures would be red on every PC
// whose WMI hiccupped once, and a helpdesk learns to ignore a red card in about a week.
//
// **No agent version gate**, for the reason machine-bitlocker.js gives: nothing here asks the
// machine to do anything. `posture: null` already means "no agent has told us", and the card
// is absent in that state rather than empty -- an empty compliance card reads as a finding.
//
// Nothing polls. A posture is re-read hourly on the machine.
(function () {
    'use strict';

    const fold = document.getElementById('card-posture');
    if (!fold || !window.MachineContext || !window.PostureLabels) return;

    const machine = window.MachineContext.current();
    if (!machine) return;

    const bodyEl = document.getElementById('posture-body');
    const summaryEl = document.getElementById('posture-summary');
    const reportedEl = document.getElementById('posture-reported');
    const errorEl = document.getElementById('posture-error');
    const labels = window.PostureLabels;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }


    // A 16px stroke icon, built with createElementNS rather than an innerHTML string so the
    // file keeps its one rule without an exception: nothing here is ever parsed as markup.
    // The paths are fixed shapes, chosen by `kind`, never by anything the machine sent.
    const ICON_PATHS = {
        pass: ['M5 12l5 5L20 7'],
        fail: ['M6 6l12 12', 'M18 6L6 18'],
        unknown: ['M6 12h12'],
    };
    function icon(kind, className) {
        const NS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(NS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('width', '16');
        svg.setAttribute('height', '16');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2.2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('class', className);
        for (const d of ICON_PATHS[kind] || []) {
            const path = document.createElementNS(NS, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        }
        return svg;
    }

    // Failed first, then unknown, then passed -- stable within each, so the agent's own order
    // survives. The tile is a checklist an operator reads top-down for "what is wrong", and a
    // fail at row 8 of 9 was the one row the old table made them hunt for.
    const STATUS_ORDER = { fail: 0, unknown: 1, pass: 2 };

    // A checklist (hub 1.189.0), not a table: one line per check with a tick, a cross or a
    // dash, the detail under it. The four-column table it replaces did not fit a tile, and
    // its Result column was a pill per row restating what a coloured icon says. The status
    // WORD is still there for a screen reader and on hover (the icon is aria-hidden), and the
    // CIS safeguard moved to the row's title -- it is evidence for an auditor, not something
    // a helpdesk reads on every visit.
    function draw(data) {
        const list = el('ul', 'checklist');
        const checks = data.checks.slice()
            .sort((a, b) => (STATUS_ORDER[a.status] ?? 1) - (STATUS_ORDER[b.status] ?? 1));
        checks.forEach((check) => {
            const kind = check.status === 'pass' || check.status === 'fail'
                ? check.status : 'unknown';
            const item = el('li', `checklist__item checklist__item--${kind}`);
            item.title = check.cis
                ? `${labels.statusLabel(check.status)} · CIS ${check.cis}`
                : labels.statusLabel(check.status);
            item.appendChild(icon(kind, 'checklist__icon'));
            const text = el('div', 'checklist__text');
            text.appendChild(el('span', 'checklist__title', labels.checkTitle(check.id)));
            text.appendChild(el('span', 'sr-only', ` (${labels.statusLabel(check.status)})`));
            // Text from the machine (product and account names) arrives inside the detail's
            // parameters and is set with textContent like everything else here.
            const detail = labels.detailText(check);
            if (detail) text.appendChild(el('span', 'checklist__detail', detail));
            item.appendChild(text);
            list.appendChild(item);
        });
        bodyEl.replaceChildren(list);
    }

    function showError(text) {
        fold.hidden = false;
        errorEl.hidden = false;
        errorEl.replaceChildren(el('span', null, text), document.createTextNode(' '));
        const retry = el('button', 'btn btn--ghost', t('posture.retry'));
        retry.type = 'button';
        retry.addEventListener('click', () => load());
        errorEl.appendChild(retry);
    }

    async function load() {
        let response;
        try {
            response = await fetch(`/api/posture/machines/${encodeURIComponent(machine)}`);
        } catch (e) {
            showError(t('posture.unreachable'));
            return;
        }
        if (!response.ok) {
            // A 5xx is ours and worth a retry; a 4xx is a scope or capability refusal, and
            // the card simply is not this operator's to see.
            if (response.status >= 500) showError(t('posture.load_error'));
            else fold.hidden = true;
            return;
        }
        const data = await response.json();
        errorEl.hidden = true;
        fold.hidden = !data.checks;
        if (fold.hidden) return;
        summaryEl.textContent = t('posture.summary', data.counts || {});
        reportedEl.textContent = data.reported_at
            ? t('posture.reported', { when: new Date(data.reported_at * 1000).toLocaleString() })
            : '';
        draw(data);
    }

    load();
    window.FleetLive.watch(['posture'], load, { machine });
}());
