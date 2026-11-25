// The machine page's Drive encryption card (roadmap #19): what protects this PC's volumes,
// and whether anybody still holds the key.
//
// **The card answers "do we have the key" before it answers "what is the key".** That is the
// question worth asking while the machine still works, and it is the one an operator can act
// on -- a volume protected by a TPM alone, with no recovery password escrowed anywhere, is a
// reimage waiting for a firmware update. The password itself is one deliberate click behind
// it, and reading it writes a security-level row into the audit log.
//
// **Three states look alike and are not**, so each gets its own wording: a machine that has
// never reported (the card is absent), a machine that reports it has no BitLocker at all
// (`unsupported`, a permanent and correct answer for Home editions and for every Linux and
// Android device), and a machine whose provider failed. Collapsing the first into the second
// would write off a fleet the day before the agent release lands.
//
// **There is no agent version gate here**, unlike the Files and Processes tabs, and that is
// not an oversight: nothing on this card asks a machine to do anything. It renders stored
// facts and a hub-side reveal, so `support === null` -- "no agent has told us" -- already says
// everything a version number would, and says it correctly for a machine that is simply
// offline. See tests/test_bitlocker.py for the hub half of that distinction.
//
// Nothing here polls. Encryption state changes on the day somebody turns BitLocker on.
(function () {
    'use strict';

    const fold = document.getElementById('card-bitlocker');
    if (!fold || !window.MachineContext) return;

    const machine = window.MachineContext.current();
    if (!machine) return;

    const bodyEl = document.getElementById('bitlocker-body');
    const summaryEl = document.getElementById('bitlocker-summary');
    const noteEl = document.getElementById('bitlocker-note');
    const errorEl = document.getElementById('bitlocker-error');
    const keyEl = document.getElementById('bitlocker-key');
    const keyLabelEl = document.getElementById('bitlocker-key-label');
    const keyValueEl = document.getElementById('bitlocker-key-value');

    let inventory = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    // Literal keys in a switch rather than one built by concatenation: tests/test_i18n.py can
    // only scan for literals, so a computed key whose translation was never written would ship
    // silently and caption the card with itself.
    function protectionLabel(state) {
        if (state === 'on') return t('bitlocker.protection.on');
        if (state === 'off') return t('bitlocker.protection.off');
        return t('bitlocker.protection.unknown');
    }

    function conversionLabel(state) {
        if (state === 'fully_encrypted') return t('bitlocker.conversion.fully_encrypted');
        if (state === 'fully_decrypted') return t('bitlocker.conversion.fully_decrypted');
        if (state === 'encrypting') return t('bitlocker.conversion.encrypting');
        if (state === 'decrypting') return t('bitlocker.conversion.decrypting');
        if (state === 'encryption_paused') return t('bitlocker.conversion.encryption_paused');
        if (state === 'decryption_paused') return t('bitlocker.conversion.decryption_paused');
        return '';
    }

    function volumeName(volume) {
        return volume.mount || volume.device_id;
    }

    // A 16px stroke icon, built with createElementNS rather than an innerHTML string so the
    // file keeps its one rule without an exception: nothing here is ever parsed as markup.
    // The paths are fixed shapes, chosen by `kind`, never by anything the machine sent.
    const ICON_PATHS = {
        lock: ['M7 11V7a5 5 0 0 1 10 0v4', 'M5 11h14v10H5z'],
        unlock: ['M7 11V7a5 5 0 0 1 9.9-1', 'M5 11h14v10H5z'],
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

    // What the hub holds for one volume, as a sentence rather than a count. "1 of 2 escrowed"
    // is a number nobody can act on; "no recovery key is escrowed" is the finding.
    function escrowCell(volume) {
        const recovery = (volume.protectors || [])
            .filter((p) => p.kind === 'recovery_password');
        const held = recovery.filter((p) => p.escrowed);
        const cell = el('div', 'volume-row__escrow');

        if (recovery.length === 0) {
            // The case the whole feature exists for. A volume with a TPM protector and no
            // recovery password has nothing anybody can type at a recovery screen -- not here,
            // not in AD, not on a printout.
            cell.appendChild(el('span', null, t('bitlocker.escrow.no_recovery_protector')));
            return cell;
        }
        if (held.length === 0) {
            cell.appendChild(el('span', null, inventory.escrow_enabled
                ? t('bitlocker.escrow.pending')
                : t('bitlocker.escrow.disabled')));
            return cell;
        }

        held.forEach((protector) => {
            const row = el('div');
            row.appendChild(el('span', null, t('bitlocker.escrow.held', {
                when: new Date(protector.escrowed_at * 1000).toLocaleDateString(),
            })));
            if (inventory.can_read_keys) {
                row.appendChild(document.createTextNode(' '));
                const button = el('button', 'btn btn--ghost', t('bitlocker.reveal'));
                button.type = 'button';
                button.addEventListener('click', () => reveal(volume, protector));
                row.appendChild(button);
            }
            if (protector.read_count) {
                // tPlural, not t(): read_before is a {one, other} pair, and t() on the
                // parent key printed the raw key name on every machine whose key was read.
                row.appendChild(el('div', 'stat-card__meta', tPlural('bitlocker.escrow.read_before',
                    protector.read_count, {
                        when: new Date(protector.last_read_at * 1000).toLocaleString(),
                    })));
            }
            cell.appendChild(row);
        });
        return cell;
    }

    function draw() {
        bodyEl.replaceChildren();
        const volumes = inventory.volumes || [];

        if (inventory.support === 'unsupported') {
            noteEl.hidden = false;
            noteEl.textContent = t('bitlocker.unsupported');
            return;
        }
        if (inventory.support === 'error') {
            noteEl.hidden = false;
            noteEl.textContent = t('bitlocker.read_failed', {
                error: inventory.error || '',
            });
            return;
        }

        // One row per volume (hub 1.189.0) rather than a four-column table, which did not
        // fit a tile: a closed lock for protection on, an open one for off, a dash for a state
        // the machine did not report -- with the protection WORD beside it, because a lock
        // icon alone is a colour-and-shape test. The escrow line and its Reveal button are
        // unchanged.
        const list = el('ul', 'volume-list');
        volumes.forEach((volume) => {
            const kind = volume.protection === 'on' ? 'lock'
                : volume.protection === 'off' ? 'unlock' : 'unknown';
            const row = el('li', `volume-row volume-row--${kind}`);
            row.appendChild(icon(kind, 'volume-row__icon'));
            const text = el('div', 'volume-row__text');
            const head = el('div', 'volume-row__head');
            head.appendChild(el('span', 'volume-row__name', volumeName(volume)));
            head.appendChild(el('span', 'volume-row__state', protectionLabel(volume.protection)));
            text.appendChild(head);
            // The second line is where a suspended volume stops looking encrypted: protection
            // reads `off` while conversion still reads `fully_encrypted`, which is exactly the
            // state Windows leaves a machine in after a firmware update.
            const facts = [conversionLabel(volume.conversion), volume.method || '']
                .filter(Boolean).join(' · ');
            if (facts) text.appendChild(el('div', 'stat-card__meta', facts));
            text.appendChild(escrowCell(volume));
            row.appendChild(text);
            list.appendChild(row);
        });
        bodyEl.appendChild(list);

        // Keys the hub holds for protectors no volume claims any more. Shown rather than
        // hidden: "we still have a key for a disk that is gone" is a fact somebody should be
        // able to see, and a key nobody can see is a key nobody knows to clean up.
        if ((inventory.escrowed || []).length) {
            bodyEl.appendChild(el('p', 'stat-card__meta', t('bitlocker.orphaned', {
                count: inventory.escrowed.length,
            })));
        }
    }

    async function reveal(volume, protector) {
        errorEl.hidden = true;
        let response;
        try {
            response = await fetch(
                `/api/bitlocker/${encodeURIComponent(machine)}/reveal`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ protector_id: protector.id }),
                });
        } catch (e) {
            errorEl.hidden = false;
            errorEl.textContent = t('bitlocker.unreachable');
            return;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            errorEl.hidden = false;
            errorEl.textContent = data.error || t('bitlocker.reveal_failed');
            return;
        }
        keyEl.hidden = false;
        keyLabelEl.textContent = t('bitlocker.key_for', { volume: volumeName(volume) });
        keyValueEl.textContent = data.recovery_password;
        // Reload so the "read before" line under the button reflects what just happened --
        // this operator has now taken custody of that key, and the card should say so to the
        // next person who opens it without them having to find the audit log.
        load();
    }

    function render(data) {
        // `support: null` is "no agent has told us", which is every Windows PC until the agent
        // half ships and every machine that has not heartbeated since. The card is absent
        // rather than empty; an empty encryption card reads as a finding.
        fold.hidden = data.support === null;
        if (fold.hidden) return;

        inventory = data;
        noteEl.hidden = true;
        const encrypted = (data.volumes || [])
            .filter((v) => v.protection === 'on').length;
        summaryEl.textContent = t('bitlocker.summary', {
            encrypted: encrypted, total: (data.volumes || []).length,
        });
        draw();
    }

    async function load() {
        let response;
        try {
            response = await fetch(`/api/bitlocker/${encodeURIComponent(machine)}`);
        } catch (e) {
            // Transport failure — show error with retry instead of silently vanishing.
            fold.hidden = false;
            errorEl.hidden = false;
            errorEl.replaceChildren(
                el('span', null, t('bitlocker.unreachable')),
                document.createTextNode(' '),
            );
            const retry = el('button', 'btn btn--ghost', t('bitlocker.retry'));
            retry.type = 'button';
            retry.addEventListener('click', () => load());
            errorEl.appendChild(retry);
            return;
        }
        if (!response.ok) {
            if (response.status >= 500) {
                // Server error — show error with retry.
                fold.hidden = false;
                errorEl.hidden = false;
                errorEl.replaceChildren(
                    el('span', null, t('bitlocker.load_error')),
                    document.createTextNode(' '),
                );
                const retry = el('button', 'btn btn--ghost', t('bitlocker.retry'));
                retry.type = 'button';
                retry.addEventListener('click', () => load());
                errorEl.appendChild(retry);
            } else {
                // Access denial (4xx) — hide card.
                fold.hidden = true;
            }
            return;
        }
        errorEl.hidden = true;
        render(await response.json());
    }

    load();
    window.FleetLive.watch(['bitlocker'], load, { machine });
}());
