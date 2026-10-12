// Onboarding codes admin page (roadmap #29).
//
// One list, one creator dialog, one dialog that shows the finished code -- the same shape as
// invites.js, for the same reasons: a code is created once and is then only revoked, so
// there is nothing to auto-save, and the code itself can be shown exactly once because only
// its hash is stored.
//
//  * Everything is built with textContent / createElement. Labels and machine names are
//    operator- and PC-supplied strings.
//  * Status labels go through a literal branch, never a computed key -- tests/test_i18n.py
//    only scans literal t() calls (see the longer note in invites.js).
//  * "Revoke" on a redeemed code also revokes the agent it minted, and the confirmation
//    says so: that is the button an operator reaches for when a code was spent by a machine
//    they did not expect.
(function () {
'use strict';

const host = document.getElementById('codes-host');
const modal = document.getElementById('code-modal');
const labelInput = document.getElementById('code-label');
const ttlSelect = document.getElementById('code-ttl');
const errorEl = document.getElementById('code-error');
const statusEl = document.getElementById('code-status');
const saveBtn = document.getElementById('code-save');
const createdModal = document.getElementById('created-modal');
const createdCode = document.getElementById('created-code');
const createdLink = document.getElementById('created-link');
const createdStatus = document.getElementById('created-status');
const unsignedCard = document.getElementById('unsigned-card');
const unsignedList = document.getElementById('unsigned-list');

async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch { /* empty body is fine */ }
    if (!resp.ok) throw new Error(body?.error || `HTTP ${resp.status}`);
    return body;
}

function statusLabel(code) {
    if (code.status === 'active') return t('onboarding.admin.status.active');
    if (code.status === 'expired') return t('onboarding.admin.status.expired');
    if (code.status === 'revoked') return t('onboarding.admin.status.revoked');
    if (code.status === 'redeemed') {
        return code.online ? t('onboarding.admin.status.online')
                           : t('onboarding.admin.status.redeemed');
    }
    return code.status;
}

function renderCodes(codes) {
    host.replaceChildren();
    if (!codes.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('onboarding.admin.empty')));
        empty.appendChild(el('p', 'stat-card__meta', t('onboarding.admin.empty_hint')));
        host.appendChild(empty);
        return;
    }
    const card = el('div', 'card');
    const table = el('table', 'data-table');
    const head = el('thead');
    const headRow = el('tr');
    [t('onboarding.admin.col.label'), t('onboarding.admin.col.machine'),
     t('onboarding.admin.col.expires'), t('onboarding.admin.col.status'), ''].forEach((text) => {
        headRow.appendChild(el('th', null, text));
    });
    head.appendChild(headRow);
    table.appendChild(head);
    const body = el('tbody');
    codes.forEach((code) => body.appendChild(renderRow(code)));
    table.appendChild(body);
    card.appendChild(table);
    host.appendChild(card);
}

function renderRow(code) {
    const tr = el('tr');

    const labelCell = el('td');
    labelCell.appendChild(el('div', null, code.label));
    labelCell.appendChild(el('div', 'stat-card__meta',
        t('onboarding.admin.created_by', { email: code.created_by, when: fmtTime(code.created_at) })));
    tr.appendChild(labelCell);

    // Which PC spent it and from where. The row an operator reads to spot a code that leaked.
    const machineCell = el('td');
    if (code.redeemed_machine) {
        machineCell.appendChild(el('div', null, code.redeemed_machine));
        machineCell.appendChild(el('div', 'stat-card__meta',
            t('onboarding.admin.redeemed_from', { ip: code.redeemed_ip || '—',
                                                  when: fmtTime(code.redeemed_at) })));
    } else {
        machineCell.appendChild(el('span', 'stat-card__meta', '—'));
    }
    tr.appendChild(machineCell);

    tr.appendChild(el('td', 'stat-card__meta', fmtTime(code.expires_at)));

    const statusCell = el('td');
    const live = code.status === 'active' || (code.status === 'redeemed' && code.online);
    statusCell.appendChild(el('span', live ? 'cap-badge' : 'stat-card__meta', statusLabel(code)));
    tr.appendChild(statusCell);

    const actions = el('td');
    if (code.status === 'active' || code.status === 'redeemed') {
        const revoke = el('button', 'btn', t('onboarding.admin.revoke'));
        revoke.type = 'button';
        revoke.addEventListener('click', () => revokeCode(code, revoke));
        actions.appendChild(revoke);
    }
    tr.appendChild(actions);
    return tr;
}

function renderUnsigned(rows) {
    unsignedList.replaceChildren();
    unsignedCard.hidden = !rows.length;
    rows.forEach((row) => {
        const li = el('li');
        li.appendChild(el('span', null, row.machine));
        li.appendChild(el('span', 'stat-card__meta',
            t('onboarding.admin.unsigned_last', { when: fmtTime(row.last_report) })));
        unsignedList.appendChild(li);
    });
}

async function revokeCode(code, btn) {
    const message = code.redeemed_machine
        ? t('onboarding.admin.confirm_revoke_machine', { label: code.label, machine: code.redeemed_machine })
        : t('onboarding.admin.confirm_revoke', { label: code.label });
    if (!await confirmDialog({
        title: t('onboarding.admin.confirm_revoke_title'),
        message,
        confirmLabel: t('onboarding.admin.revoke'),
        danger: true,
    })) return;
    btn.disabled = true;
    try {
        await api(`/api/onboarding/codes/${encodeURIComponent(code.code_id)}/revoke`,
                  { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        await load();
    } catch (e) {
        btn.disabled = false;
        toast(e.message, { kind: 'error' });
    }
}

async function load() {
    try {
        const body = await api('/api/onboarding/codes');
        renderCodes(body.codes || []);
        renderUnsigned(body.unsigned_reporters || []);
    } catch (e) {
        host.replaceChildren();
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('onboarding.admin.load_failed', { error: e.message })));
        host.appendChild(empty);
    }
}

function openEditor() {
    labelInput.value = '';
    ttlSelect.value = '86400';
    errorEl.textContent = '';
    statusEl.textContent = '';
    modal.showModal();
    labelInput.focus();
}

async function createCode() {
    saveBtn.disabled = true;
    errorEl.textContent = '';
    statusEl.textContent = t('common.saving');
    try {
        const created = await api('/api/onboarding/codes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label: labelInput.value.trim(),
                                   ttl_seconds: Number(ttlSelect.value) }),
        });
        modal.close();
        createdCode.textContent = created.code;
        createdLink.value = created.link || '';
        createdStatus.textContent = '';
        createdModal.showModal();
        createdLink.select();
        await load();
    } catch (e) {
        errorEl.textContent = e.message;
        statusEl.textContent = '';
    } finally {
        saveBtn.disabled = false;
    }
}

async function copyLink() {
    try {
        await navigator.clipboard.writeText(createdLink.value);
        createdStatus.textContent = t('onboarding.admin.copied');
    } catch {
        // Refused outside a secure context (plain http on a LAN); the link is selectable.
        createdStatus.textContent = t('onboarding.admin.copy_failed');
        createdLink.select();
    }
}

document.getElementById('new-code').addEventListener('click', openEditor);
document.getElementById('code-cancel').addEventListener('click', () => modal.close());
saveBtn.addEventListener('click', createCode);
labelInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); void createCode(); }
});
document.getElementById('created-copy').addEventListener('click', copyLink);
document.getElementById('created-close').addEventListener('click', () => createdModal.close());
modal.addEventListener('click', (e) => { if (e.target === modal) modal.close(); });

void load();

})();
