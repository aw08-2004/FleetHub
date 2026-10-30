// Group Policy page (roadmap #32), laid out like Group Policy Management.
//
// Left: "Group Policy Objects", "Every PC", the hub folders and the AD OUs (from /api/pc-tree,
// already narrowed to the caller's scope), and "Resultant set of policy". Right: whatever is
// selected -- the policy list, one node's linked policies, or one PC's resultant set.
//
// The precedence rules live in ONE place, gpo.py; this page never works out what wins. The
// resultant-set view asks the server, so what an operator reads here is what the heartbeat
// sends. Built with createElement/textContent throughout: policy names and registry values are
// operator text, and PC and OU names are whatever a host or AD reported.
(function () {
    'use strict';

    const root = document.getElementById('gpo-root');
    if (!root) return;

    const CAN_MANAGE = root.dataset.canManage === '1';
    const CAN_RUN = root.dataset.canRun === '1';
    const nav = document.getElementById('gpo-tree');
    const pane = document.getElementById('gpo-pane');
    const dialog = document.getElementById('gpo-dialog');

    let data = { gpos: [], links: [], blocked: [], agent_settings: [], registry_types: [], scripts: [] };
    let tree = { folders: [], ous: [], machines: [] };
    // {view: 'objects'|'node'|'rsop', kind?, key?, label?, machine?}
    let selected = { view: 'objects' };
    const collapsed = new Set();
    let editing = null;      // the policy open in the editor, or null while creating
    let draft = null;

    async function api(path, options) {
        const resp = await fetch(path, options);
        let payload = null;
        try { payload = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((payload && payload.error) || `HTTP ${resp.status}`);
        return payload;
    }

    function json(method, payload) {
        // Content-Type: application/json is load-bearing -- it is what makes a cross-origin
        // POST preflight and fail (see fleet_web.py's module docstring).
        return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(label, className, onClick) {
        const b = el('button', className || 'btn', label);
        b.type = 'button';
        b.addEventListener('click', onClick);
        return b;
    }

    function option(value, label) {
        const o = el('option', null, label);
        o.value = value;
        return o;
    }

    function fail(e) {
        toast(t('gpo.action_failed', { error: e.message }), { kind: 'error' });
    }

    function table(headers) {
        const t1 = el('table', 'data-table');
        const head = el('thead');
        const row = el('tr');
        headers.forEach((h) => row.appendChild(el('th', null, h)));
        head.appendChild(row);
        t1.appendChild(head);
        const body = el('tbody');
        t1.appendChild(body);
        // Wrapped, so a long registry path scrolls inside its card instead of pushing the
        // row's buttons off the page.
        const wrap = el('div', 'gpo-table-wrap');
        wrap.appendChild(t1);
        return { table: wrap, body };
    }

    // ---------------------------------------------------------------- helpers

    function policyById(id) { return data.gpos.find((p) => p.id === id) || null; }
    function folderById(id) { return tree.folders.find((f) => f.id === id) || null; }

    function folderPath(id) {
        const names = [];
        const seen = new Set();
        let current = folderById(id);
        while (current && !seen.has(current.id)) {
            seen.add(current.id);
            names.unshift(current.name);
            current = current.parent_id == null ? null : folderById(current.parent_id);
        }
        return names.length ? names.join(' / ') : `#${id}`;
    }

    // The comparison form of an OU, the way gpo.node_key computes it -- so a link stored for
    // "OU=Cajas,..." is found from the tree node whatever case AD reported it in.
    function normalizeDn(dn) {
        return String(dn || '').split(/(?<!\\),/).map((p) => p.trim().toLowerCase())
            .filter(Boolean).join(',');
    }

    function nodeKey(kind, key) {
        if (kind === 'root') return '';
        if (kind === 'ou') return normalizeDn(key);
        return String(key);
    }

    // An OU the way the tree shows it -- "FILADELFIA / Usuarios / Cajas" -- rather than the full
    // distinguished name, which is too long for a table and harder to read than the path the
    // admin just clicked through. The DN is still on the node's own page.
    function ouPath(dn) {
        const parts = String(dn || '').split(/(?<!\\),/).map((p) => p.trim())
            .filter((p) => p && !/^dc=/i.test(p))
            .map((p) => p.replace(/^[a-z]+=/i, '').replace(/\\,/g, ','));
        return parts.length ? parts.toReversed().join(' / ') : String(dn || '');
    }

    function nodeLabel(kind, key, label) {
        if (kind === 'root') return t('gpo.every_pc');
        if (kind === 'folder') return folderPath(Number(key));
        return ouPath(label || key);
    }

    function linksAt(kind, key) {
        const k = nodeKey(kind, key);
        return data.links.filter((l) => l.node_kind === kind && l.node_key === k)
            .sort((a, b) => a.link_order - b.link_order);
    }

    function isBlocked(kind, key) {
        const k = nodeKey(kind, key);
        return data.blocked.some((b) => b.node_kind === kind && b.node_key === k);
    }

    function summary(policy) {
        const s = policy.settings || {};
        return [
            tPlural('gpo.count.agent', Object.keys(s.agent_settings || {}).length),
            tPlural('gpo.count.registry', (s.registry || []).length),
            tPlural('gpo.count.admx', (s.admx || []).length),
            tPlural('gpo.count.scripts', (s.scripts || []).length),
        ].join(' · ');
    }

    // ---------------------------------------------------------------- the tree

    function treeItem(label, isActive, onSelect, count) {
        const row = el('div', 'perm-tree__row');
        const item = el('button', 'perm-tree__item', label);
        item.type = 'button';
        if (isActive) item.classList.add('perm-tree__item--active');
        item.addEventListener('click', onSelect);
        row.appendChild(item);
        if (count) row.appendChild(el('span', 'pc-tree__count', String(count)));
        return row;
    }

    function toggleClass(key, hasChildren) {
        if (!hasChildren) return 'perm-tree__toggle';
        return collapsed.has(key) ? 'perm-tree__toggle is-closed' : 'perm-tree__toggle is-open';
    }

    function toggle(key, hasChildren, label) {
        const b = el('button', toggleClass(key, hasChildren));
        b.type = 'button';
        if (!hasChildren) {
            b.disabled = true;
            b.setAttribute('aria-hidden', 'true');
            return b;
        }
        b.setAttribute('aria-label', collapsed.has(key)
            ? t('permissions.tree.expand', { group: label })
            : t('permissions.tree.collapse', { group: label }));
        b.addEventListener('click', () => {
            if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
            renderTree();
        });
        return b;
    }

    function isNode(kind, key) {
        return selected.view === 'node' && selected.kind === kind
            && nodeKey(kind, selected.key) === nodeKey(kind, key);
    }

    function folderBranch(parentId, seen) {
        const list = el('ul', 'perm-tree__list');
        tree.folders.filter((f) => (f.parent_id == null ? null : f.parent_id) === parentId)
            .forEach((folder) => {
                if (seen.has(folder.id)) return;
                const kids = tree.folders.some((f) => f.parent_id === folder.id);
                const key = `f:${folder.id}`;
                const li = el('li');
                const row = treeItem(folder.name, isNode('folder', folder.id),
                    () => select({ view: 'node', kind: 'folder', key: folder.id }),
                    linksAt('folder', folder.id).length);
                row.prepend(toggle(key, kids, folder.name));
                li.appendChild(row);
                if (kids && !collapsed.has(key)) {
                    li.appendChild(folderBranch(folder.id, new Set(seen).add(folder.id)));
                }
                list.appendChild(li);
            });
        return list;
    }

    function ouBranch(nodes) {
        const list = el('ul', 'perm-tree__list');
        nodes.forEach((node) => {
            const key = `o:${node.dn.toLowerCase()}`;
            const li = el('li');
            const row = treeItem(node.name, isNode('ou', node.dn),
                () => select({ view: 'node', kind: 'ou', key: node.dn, label: node.dn }),
                linksAt('ou', node.dn).length);
            row.prepend(toggle(key, node.children.length > 0, node.name));
            li.appendChild(row);
            if (node.children.length && !collapsed.has(key)) li.appendChild(ouBranch(node.children));
            list.appendChild(li);
        });
        return list;
    }

    function renderTree() {
        nav.replaceChildren();
        nav.appendChild(treeItem(t('gpo.objects'), selected.view === 'objects',
            () => select({ view: 'objects' }), data.gpos.length));
        nav.appendChild(treeItem(t('gpo.rsop.title'), selected.view === 'rsop',
            () => select({ view: 'rsop', machine: selected.machine || '' })));
        nav.appendChild(el('div', 'pc-tree__section', t('gpo.links_section')));
        nav.appendChild(treeItem(t('gpo.every_pc'), isNode('root', ''),
            () => select({ view: 'node', kind: 'root', key: '' }), linksAt('root', '').length));
        nav.appendChild(el('div', 'pc-tree__section', t('pc_tree.folders')));
        if (tree.folders.length) nav.appendChild(folderBranch(null, new Set()));
        else nav.appendChild(el('p', 'stat-card__meta', t('pc_tree.no_folders')));
        nav.appendChild(el('div', 'pc-tree__section', t('pc_tree.ad')));
        if (tree.ous.length) nav.appendChild(ouBranch(tree.ous));
        else nav.appendChild(el('p', 'stat-card__meta', t('pc_tree.ad_empty')));
    }

    // ---------------------------------------------------------------- the policy list

    function renderObjects() {
        const card = el('div', 'card');
        card.appendChild(el('h2', 'section-title', t('gpo.objects')));
        if (!data.gpos.length) {
            card.appendChild(el('p', 'stat-card__meta', t('gpo.empty')));
            pane.appendChild(card);
            return;
        }
        const { table: tbl, body } = table([t('gpo.col.name'), t('gpo.col.status'),
            t('gpo.col.settings'), t('gpo.col.links'), '']);
        data.gpos.forEach((policy) => {
            const tr = el('tr');
            const name = el('td');
            name.appendChild(el('div', null, policy.name));
            if (policy.description) name.appendChild(el('div', 'stat-card__meta', policy.description));
            tr.appendChild(name);
            tr.appendChild(el('td', policy.enabled ? null : 'stat-card__meta',
                policy.enabled ? t('gpo.status_enabled') : t('gpo.status_disabled')));
            tr.appendChild(el('td', 'stat-card__meta', summary(policy)));
            const links = data.links.filter((l) => l.gpo_id === policy.id);
            tr.appendChild(el('td', 'stat-card__meta', links.length
                ? links.map((l) => nodeLabel(l.node_kind, l.node_key, l.node_label)).join(', ')
                : t('gpo.not_linked')));
            const actions = el('td');
            const wrap = el('div', 'perm-row-actions');
            if (CAN_MANAGE) {
                wrap.appendChild(button(t('common.edit'), 'btn', () => openEditor(policy)));
                wrap.appendChild(button(t('common.delete'), 'btn', () => removePolicy(policy)));
            }
            actions.appendChild(wrap);
            tr.appendChild(actions);
            body.appendChild(tr);
        });
        card.appendChild(tbl);
        pane.appendChild(card);
    }

    async function removePolicy(policy) {
        if (!await confirmDialog({
            title: t('gpo.confirm_delete_title'),
            message: t('gpo.confirm_delete', { name: policy.name }),
            confirmLabel: t('common.delete'),
            danger: true,
        })) return;
        try {
            await api(`/api/gpo/${policy.id}`, { method: 'DELETE' });
            await load();
        } catch (e) { fail(e); }
    }

    // ---------------------------------------------------------------- one node

    // Which half of the tree a node is in, shown above its name.
    function sectionLabel(kind) {
        if (kind === 'ou') return t('pc_tree.ad');
        if (kind === 'folder') return t('pc_tree.folders');
        return '';
    }

    function renderNode() {
        const { kind, key } = selected;
        const card = el('div', 'card');
        card.appendChild(el('div', 'stat-card__meta', sectionLabel(kind)));
        card.appendChild(el('h2', 'section-title', nodeLabel(kind, key, selected.label)));
        if (kind === 'ou') card.appendChild(el('div', 'stat-card__meta gpo-mono', selected.label || key));

        if (kind !== 'root') {
            const section = el('div', 'gpo-section');
            const label = el('label', 'checkbox');
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.checked = isBlocked(kind, key);
            box.disabled = !CAN_MANAGE;
            box.addEventListener('change', async () => {
                try {
                    await api('/api/gpo/nodes', json('PUT', {
                        node_kind: kind, node_key: String(key), block_inheritance: box.checked }));
                    await load();
                } catch (e) { box.checked = !box.checked; fail(e); }
            });
            label.append(box, document.createTextNode(' ' + t('gpo.node.block')));
            section.appendChild(label);
            section.appendChild(el('p', 'setting__help', t('gpo.node.block_help')));
            card.appendChild(section);
        }

        const section = el('div', 'gpo-section');
        section.appendChild(el('h3', null, t('gpo.node.linked')));
        const links = linksAt(kind, key);
        if (!links.length) {
            section.appendChild(el('p', 'stat-card__meta', t('gpo.node.none')));
        } else {
            const { table: tbl, body } = table([t('gpo.link.order'), t('gpo.col.name'),
                t('gpo.link.enforced'), t('gpo.link.enabled'), '']);
            links.forEach((link, index) => {
                const policy = policyById(link.gpo_id);
                const tr = el('tr');
                tr.appendChild(el('td', null, String(link.link_order)));
                const name = el('td');
                name.appendChild(el('div', null, policy ? policy.name : `#${link.gpo_id}`));
                if (policy && !policy.enabled) {
                    name.appendChild(el('div', 'stat-card__meta', t('gpo.status_disabled')));
                }
                if (link.scoped) name.appendChild(el('div', 'stat-card__meta', t('gpo.link.scoped')));
                tr.appendChild(name);
                tr.appendChild(checkboxCell(link.enforced, (v) => editLink(link, { enforced: v })));
                tr.appendChild(checkboxCell(link.enabled, (v) => editLink(link, { enabled: v })));
                const actions = el('td');
                const wrap = el('div', 'gpo-link-actions');
                if (CAN_MANAGE) {
                    const up = button('↑', 'btn btn--ghost', () => editLink(link, { move: -1 }));
                    up.setAttribute('aria-label', t('gpo.link.up'));
                    up.disabled = index === 0;
                    const down = button('↓', 'btn btn--ghost', () => editLink(link, { move: 1 }));
                    down.setAttribute('aria-label', t('gpo.link.down'));
                    down.disabled = index === links.length - 1;
                    wrap.append(up, down, button(t('gpo.link.remove'), 'btn', () => unlink(link)));
                }
                actions.appendChild(wrap);
                tr.appendChild(actions);
                body.appendChild(tr);
            });
            section.appendChild(tbl);
        }
        if (CAN_MANAGE) {
            const linked = new Set(links.map((l) => l.gpo_id));
            const available = data.gpos.filter((p) => !linked.has(p.id));
            if (available.length) {
                const bar = el('div', 'toolbar');
                bar.style.marginTop = 'var(--space-3)';
                const pick = el('select', 'input');
                pick.appendChild(option('', t('gpo.node.link_select')));
                available.forEach((p) => pick.appendChild(option(String(p.id), p.name)));
                bar.append(pick, button(t('gpo.node.link'), 'btn btn--primary', async () => {
                    if (!pick.value) return;
                    try {
                        await api(`/api/gpo/${pick.value}/links`, json('POST', {
                            node_kind: kind, node_key: String(key),
                            node_label: kind === 'ou' ? selected.label : '' }));
                        await load();
                    } catch (e) { fail(e); }
                }));
                section.appendChild(bar);
            }
        }
        card.appendChild(section);
        pane.appendChild(card);
    }

    function checkboxCell(checked, onChange) {
        const td = el('td');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = checked;
        box.disabled = !CAN_MANAGE;
        box.addEventListener('change', () => onChange(box.checked));
        td.appendChild(box);
        return td;
    }

    async function editLink(link, change) {
        try {
            await api(`/api/gpo/links/${link.id}`, json('PUT', change));
            await load();
        } catch (e) { fail(e); await load(); }
    }

    async function unlink(link) {
        try {
            await api(`/api/gpo/links/${link.id}`, { method: 'DELETE' });
            await load();
        } catch (e) { fail(e); }
    }

    // ---------------------------------------------------------------- resultant set

    function runLabel(status) {
        // Literal keys, so tests/test_i18n.py sees every one.
        switch (status) {
            case 'queued': return t('gpo.run.queued');
            case 'done': return t('gpo.run.done');
            case 'failed': return t('gpo.run.failed');
            case 'skipped': return t('gpo.run.skipped');
            case 'retry': return t('gpo.run.retry');
            default: return status;
        }
    }

    function displayValue(value) {
        if (Array.isArray(value)) return value.join(', ');
        if (value === true) return t('gpo.editor.bool_yes');
        if (value === false) return t('gpo.editor.bool_no');
        return String(value);
    }

    function settingLabel(key) {
        const found = data.agent_settings.find((s) => s.key === key);
        return found ? found.label : key;
    }

    function renderRsop() {
        const card = el('div', 'card');
        card.appendChild(el('h2', 'section-title', t('gpo.rsop.title')));
        const bar = el('div', 'toolbar');
        const input = el('input', 'input');
        input.type = 'search';
        input.placeholder = t('gpo.rsop.placeholder');
        input.setAttribute('aria-label', t('gpo.rsop.pick'));
        input.value = selected.machine || '';
        const list = el('datalist');
        list.id = 'gpo-machines';
        tree.machines.forEach((m) => list.appendChild(option(m, m)));
        input.setAttribute('list', 'gpo-machines');
        const show = () => { selected.machine = input.value.trim(); render(); };
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); show(); } });
        bar.append(input, list, button(t('gpo.rsop.show'), 'btn btn--primary', show));
        card.appendChild(bar);
        const host = el('div');
        host.style.marginTop = 'var(--space-4)';
        card.appendChild(host);
        pane.appendChild(card);
        // loadRsop reports its own failure into `host`, so nothing is left to catch here.
        if (selected.machine) void loadRsop(selected.machine, host);
    }

    async function loadRsop(machine, host) {
        let result;
        try {
            result = await api(`/api/gpo/rsop/${encodeURIComponent(machine)}`);
        } catch (e) {
            host.appendChild(el('p', 'setting__error', e.message));
            return;
        }
        if (!result.links.length) {
            host.appendChild(el('p', 'stat-card__meta', t('gpo.rsop.none')));
        }
        if (!result.agent_supports_registry && result.registry.length) {
            host.appendChild(el('p', 'setting__help', t('gpo.rsop.agent_too_old')));
        }
        // Where the values land (phase 2): an administrator at the PC who opens gpedit.msc
        // sees them only when the agent writes the local GPO, and should know which to expect.
        if (result.agent_supports_registry && result.registry.length) {
            host.appendChild(el('p', 'setting__help', result.agent_writes_local_gpo
                ? t('gpo.rsop.local_gpo') : t('gpo.rsop.direct_registry')));
        }
        // User Configuration (phase 3) is the local GPO's User half: Windows applies it to
        // everyone who signs in, at sign-in, so "Applied" here never means "the signed-in user
        // has it right now".
        if (result.registry.some((entry) => entry.class === 'user')) {
            host.appendChild(el('p', 'setting__help', t('gpo.rsop.user_note')));
        }

        if (result.links.length) {
            const section = el('div', 'gpo-section');
            section.appendChild(el('h3', null, t('gpo.rsop.links')));
            const { table: tbl, body } = table([t('gpo.col.name'), t('gpo.rsop.col.node'),
                t('gpo.link.enforced')]);
            result.links.forEach((link) => {
                const tr = el('tr');
                tr.appendChild(el('td', null, link.gpo));
                tr.appendChild(el('td', 'stat-card__meta',
                    nodeLabel(link.node_kind, link.node_key, link.node_label)));
                tr.appendChild(el('td', null, link.enforced ? '✓' : ''));
                body.appendChild(tr);
            });
            section.appendChild(tbl);
            host.appendChild(section);
        }

        const agentKeys = Object.keys(result.agent_settings);
        if (agentKeys.length) {
            const section = el('div', 'gpo-section');
            section.appendChild(el('h3', null, t('gpo.editor.agent_settings')));
            const { table: tbl, body } = table([t('gpo.rsop.col.setting'), t('gpo.rsop.col.value'),
                t('gpo.rsop.col.from')]);
            agentKeys.forEach((key) => {
                const entry = result.agent_settings[key];
                const tr = el('tr');
                tr.appendChild(el('td', null, settingLabel(key)));
                tr.appendChild(el('td', null, displayValue(entry.value)));
                tr.appendChild(el('td', 'stat-card__meta', entry.gpo));
                body.appendChild(tr);
            });
            section.appendChild(tbl);
            host.appendChild(section);
        }

        if ((result.admx || []).length) {
            const section = el('div', 'gpo-section');
            section.appendChild(el('h3', null, t('gpo.admx.heading')));
            const { table: tbl, body } = table([t('gpo.rsop.col.setting'), t('gpo.admx.col_state'),
                t('gpo.rsop.col.from')]);
            result.admx.forEach((entry) => {
                const tr = el('tr');
                const name = el('td', null, entry.name);
                name.appendChild(el('div', 'stat-card__meta', halfLabel(entry.class)));
                tr.appendChild(name);
                const state = el('td', null, admxSummary(entry));
                if ((entry.deletes || []).length && !result.agent_writes_local_gpo) {
                    state.title = t('gpo.admx.deletes_old_agent');
                }
                tr.appendChild(state);
                tr.appendChild(el('td', 'stat-card__meta', entry.gpo));
                body.appendChild(tr);
            });
            section.appendChild(tbl);
            host.appendChild(section);
        }

        if (result.registry.length) {
            const section = el('div', 'gpo-section');
            section.appendChild(el('h3', null, t('gpo.editor.registry')));
            const { table: tbl, body } = table([t('gpo.rsop.col.setting'), t('gpo.rsop.col.value'),
                t('gpo.rsop.col.from'), t('gpo.rsop.col.state')]);
            result.registry.forEach((entry) => {
                const tr = el('tr');
                const setting = el('td', 'gpo-mono', `${hive(entry.class)}\\${entry.path}\\${entry.name}`);
                if (entry.admx_name) setting.appendChild(el('div', 'stat-card__meta', entry.admx_name));
                tr.appendChild(setting);
                const deleted = entry.type === 'delete';
                tr.appendChild(el('td', 'gpo-mono', deleted ? t('gpo.rsop.deleted') : displayValue(entry.value)));
                tr.appendChild(el('td', 'stat-card__meta', entry.gpo));
                // A delete never reaches an agent that cannot write the local GPO, so it would
                // otherwise sit at "Waiting for the PC" for ever.
                // A user row likewise never reaches an agent before 3.49.
                let pending = t('gpo.rsop.pending');
                if (entry.class === 'user' && !result.agent_applies_user) pending = t('gpo.rsop.user_needs_agent');
                else if (deleted && !result.agent_writes_local_gpo) pending = t('gpo.rsop.delete_needs_agent');
                const state = el('td', entry.state ? `gpo-state--${entry.state}` : 'stat-card__meta',
                    entry.state ? (result.state_labels[entry.state] || entry.state) : pending);
                if (entry.state_detail) state.title = entry.state_detail;
                tr.appendChild(state);
                body.appendChild(tr);
            });
            section.appendChild(tbl);
            host.appendChild(section);
        }

        if (result.scripts.length || result.script_runs.length) {
            const section = el('div', 'gpo-section');
            section.appendChild(el('h3', null, t('gpo.editor.scripts')));
            const { table: tbl, body } = table([t('gpo.rsop.col.script'), t('gpo.rsop.col.from'),
                t('gpo.rsop.col.run')]);
            result.scripts.forEach((entry) => {
                const runs = result.script_runs.filter((r) => r.script_name === entry.name);
                const latest = runs[0];
                const tr = el('tr');
                tr.appendChild(el('td', null, entry.name));
                tr.appendChild(el('td', 'stat-card__meta', entry.gpo));
                const status = el('td', null, latest ? runLabel(latest.status) : t('gpo.rsop.pending'));
                if (latest && latest.detail) status.title = latest.detail;
                tr.appendChild(status);
                body.appendChild(tr);
            });
            section.appendChild(tbl);
            host.appendChild(section);
        }
    }

    // ---------------------------------------------------------------- the editor

    function agentSetting(key) { return data.agent_settings.find((s) => s.key === key); }

    // One editor control per agent-setting type. Each reports its starting value through
    // onChange straight away, so a setting just added to the draft holds a real value rather
    // than null -- which the server would read as "not configured" and drop.
    function boolInput(value, onChange) {
        const s = el('select', 'input gpo-row__value');
        s.append(option('true', t('gpo.editor.bool_yes')), option('false', t('gpo.editor.bool_no')));
        s.value = value === false ? 'false' : 'true';
        s.addEventListener('change', () => onChange(s.value === 'true'));
        onChange(s.value === 'true');
        return s;
    }

    function choiceInput(schema, value, onChange) {
        const s = el('select', 'input gpo-row__value');
        const labels = schema.choice_labels || {};
        schema.choices.forEach((c) => s.appendChild(option(c, labels[c] || c)));
        s.value = String(value ?? schema.choices[0]);
        s.addEventListener('change', () => onChange(s.value));
        onChange(s.value);
        return s;
    }

    function asText(initial) {
        if (Array.isArray(initial)) return initial.join(', ');
        return initial == null ? '' : String(initial);
    }

    function textInput(schema, value, onChange) {
        const input = el('input', 'input gpo-row__value');
        if (schema.type === 'int' || schema.type === 'float') {
            input.type = 'number';
            if (schema.min != null) input.min = schema.min;
            if (schema.max != null) input.max = schema.max;
        }
        input.value = asText(value ?? schema.default);
        const read = () => (schema.type === 'str_list'
            ? input.value.split(',').map((v) => v.trim()).filter(Boolean)
            : input.value);
        input.addEventListener('input', () => onChange(read()));
        onChange(read());
        return input;
    }

    function valueInput(schema, value, onChange) {
        if (schema.type === 'bool') return boolInput(value, onChange);
        if (schema.choices?.length) return choiceInput(schema, value, onChange);
        return textInput(schema, value, onChange);
    }

    function renderAgentRows() {
        const host = document.getElementById('gpo-agent-rows');
        host.replaceChildren();
        Object.keys(draft.agent_settings).forEach((key) => {
            const schema = agentSetting(key) || { key, type: 'str', label: key };
            const row = el('div', 'gpo-row');
            const label = el('span', 'gpo-row__label', schema.label);
            if (schema.unit) label.textContent = `${schema.label} (${schema.unit})`;
            row.appendChild(label);
            row.appendChild(valueInput(schema, draft.agent_settings[key],
                (v) => { draft.agent_settings[key] = v; }));
            row.appendChild(button(t('gpo.editor.remove'), 'btn btn--ghost', () => {
                delete draft.agent_settings[key];
                renderAgentRows();
            }));
            host.appendChild(row);
        });
        const add = document.getElementById('gpo-agent-add');
        add.replaceChildren(option('', t('gpo.editor.add_setting')));
        data.agent_settings.filter((s) => !(s.key in draft.agent_settings))
            .forEach((s) => add.appendChild(option(s.key, s.label)));
        add.hidden = add.options.length <= 1;
    }

    function renderRegistryRows() {
        const host = document.getElementById('gpo-registry-rows');
        host.replaceChildren();
        draft.registry.forEach((entry, index) => {
            const row = el('div', 'gpo-row');
            // HKLM is Computer Configuration, HKCU User Configuration (phase 3) -- the same
            // SOFTWARE\Policies tree under either, as the server and the agent both insist.
            const half = el('select', 'input gpo-row__class');
            half.setAttribute('aria-label', t('gpo.editor.hive'));
            half.append(option('machine', hive('machine')), option('user', hive('user')));
            half.value = entry.class === 'user' ? 'user' : 'machine';
            half.addEventListener('change', () => { entry.class = half.value; });
            const path = el('input', 'input gpo-row__path');
            path.placeholder = t('gpo.editor.path_placeholder');
            path.setAttribute('aria-label', t('gpo.editor.path'));
            path.value = entry.path || '';
            path.addEventListener('input', () => {
                entry.path = path.value;
                // A path pasted out of regedit names its hive; let it move the picker rather
                // than be refused at save as contradicting it (gpo._registry_target).
                const named = pastedHive(path.value);
                if (named && named !== entry.class) {
                    entry.class = named;
                    half.value = named;
                }
            });
            const name = el('input', 'input gpo-row__name');
            name.placeholder = t('gpo.editor.value_name');
            name.setAttribute('aria-label', t('gpo.editor.value_name'));
            name.value = entry.name || '';
            name.addEventListener('input', () => { entry.name = name.value; });
            const type = el('select', 'input gpo-row__type');
            type.setAttribute('aria-label', t('gpo.editor.type'));
            data.registry_types.forEach((rt) => type.appendChild(option(rt.name, rt.label)));
            type.value = entry.type || 'dword';
            type.addEventListener('change', () => {
                entry.type = type.value;
                if (type.value === 'multi_string' && !Array.isArray(entry.value)) {
                    entry.value = entry.value ? [String(entry.value)] : [];
                } else if (type.value !== 'multi_string' && Array.isArray(entry.value)) {
                    entry.value = entry.value.join('');
                }
                renderRegistryRows();
            });
            let value;
            if (entry.type === 'multi_string') {
                value = el('textarea', 'input gpo-row__value');
                value.placeholder = t('gpo.editor.multi_help');
                value.value = (entry.value || []).join('\n');
                value.addEventListener('input', () => { entry.value = value.value.split('\n'); });
            } else {
                value = el('input', 'input gpo-row__value');
                if (entry.type === 'dword') value.inputMode = 'numeric';
                value.value = entry.value == null ? '' : String(entry.value);
                value.addEventListener('input', () => { entry.value = value.value; });
            }
            value.setAttribute('aria-label', t('gpo.editor.value'));
            row.append(half, path, name, type, value, button(t('gpo.editor.remove'), 'btn btn--ghost', () => {
                draft.registry.splice(index, 1);
                renderRegistryRows();
            }));
            host.appendChild(row);
        });
    }

    function renderScriptRows() {
        const host = document.getElementById('gpo-script-rows');
        const locked = !CAN_RUN;
        document.getElementById('gpo-scripts-locked').hidden = !locked;
        host.replaceChildren();
        draft.scripts.forEach((entry, index) => {
            const meta = data.scripts.find((s) => s.name === entry.name);
            const row = el('div', 'gpo-row');
            row.appendChild(el('span', 'gpo-row__label', meta && meta.label ? `${meta.label} (${entry.name})` : entry.name));
            ((meta && meta.inputs) || []).forEach((item) => {
                const input = el('input', 'input gpo-row__value');
                input.placeholder = item.label || item.name;
                input.setAttribute('aria-label', item.label || item.name);
                input.value = (entry.inputs || {})[item.name] || '';
                input.disabled = locked;
                input.addEventListener('input', () => {
                    entry.inputs = entry.inputs || {};
                    entry.inputs[item.name] = input.value;
                });
                row.appendChild(input);
            });
            const remove = button(t('gpo.editor.remove'), 'btn btn--ghost', () => {
                draft.scripts.splice(index, 1);
                renderScriptRows();
            });
            remove.disabled = locked;
            row.appendChild(remove);
            host.appendChild(row);
        });
        const add = document.getElementById('gpo-script-add');
        add.replaceChildren(option('', t('gpo.editor.add_script')));
        data.scripts.filter((s) => s.enabled && !draft.scripts.some((d) => d.name === s.name))
            .forEach((s) => add.appendChild(option(s.name, s.label || s.name)));
        add.hidden = locked || add.options.length <= 1;
    }

    // ---------------------------------------------------------------- Administrative Templates
    //
    // gpedit's tree, read from the template sets the PCs reported (/api/gpo/admx). The browser
    // only collects a setting's state and option values; what they WRITE is worked out on the
    // server (admx.compile_policy) when the policy is saved, so this page never decides what a
    // template means. The draft entry is {set, ref, name, state, options}.

    const admxDialog = document.getElementById('gpo-admx-dialog');
    let admxSets = [];
    let admxSet = null;        // the hash being browsed
    let admxTree = null;       // {categories, policies} for admxSet
    let admxHalf = 'machine';  // 'machine' | 'user'
    let admxCategory = null;
    let admxEditing = null;    // {detail, entry} while one setting is open

    function admxSummary(entry) {
        return entry.state === 'enabled' ? t('gpo.admx.state_enabled') : t('gpo.admx.state_disabled');
    }

    function halfLabel(cls) {
        return cls === 'user' ? t('gpo.admx.user') : t('gpo.admx.computer');
    }

    function hive(cls) {
        return cls === 'user' ? 'HKCU' : 'HKLM';
    }

    // The half a pasted path's hive prefix names, or null -- the same prefixes the server
    // strips (gpo._HIVE_PREFIXES).
    function pastedHive(text) {
        const upper = text.trim().replace(/\//g, '\\').toUpperCase();
        if (/^(HKEY_CURRENT_USER|HKCU:?)\\/.test(upper)) return 'user';
        if (/^(HKEY_LOCAL_MACHINE|HKLM:?)\\/.test(upper)) return 'machine';
        return null;
    }

    // The half a template is set in: its own when it lives in one, the half being browsed
    // when gpedit lists it under both -- there it is two settings, one per half.
    function halfFor(policy) {
        return policy.class === 'both' ? admxHalf : policy.class;
    }

    function sameSetting(entry, ref, cls) {
        return entry.ref === ref && (entry.class || 'machine') === cls;
    }

    function renderAdmxRows() {
        const host = document.getElementById('gpo-admx-rows');
        host.replaceChildren();
        draft.admx.forEach((entry, index) => {
            const row = el('div', 'gpo-row');
            row.appendChild(el('span', 'gpo-row__label', entry.name || entry.ref));
            row.appendChild(el('span', 'stat-card__meta', `${halfLabel(entry.class)} · ${admxSummary(entry)}`));
            row.appendChild(button(t('gpo.admx.edit'), 'btn btn--ghost',
                () => openAdmxSetting(entry.set, entry.ref, entry.class || 'machine')));
            row.appendChild(button(t('gpo.editor.remove'), 'btn btn--ghost', () => {
                draft.admx.splice(index, 1);
                renderAdmxRows();
            }));
            host.appendChild(row);
        });
    }

    function inHalf(policy) {
        return policy.class === 'both' || policy.class === admxHalf;
    }

    function admxChildren(parent) {
        return admxTree.categories.filter((c) => (c.parent || null) === parent);
    }

    // A category is shown in a half only when something of that half sits beneath it, as in
    // gpedit, where Computer and User Configuration list different trees.
    function categoryHasPolicies(ref, memo) {
        if (memo.has(ref)) return memo.get(ref);
        memo.set(ref, false);
        const found = admxTree.policies.some((p) => p.category === ref && inHalf(p))
            || admxChildren(ref).some((c) => categoryHasPolicies(c.ref, memo));
        memo.set(ref, found);
        return found;
    }

    function selectAdmx(half, category) {
        admxHalf = half;
        admxCategory = category;
        renderAdmxTree();
        renderAdmxList();
    }

    // The categories beneath `parent` that hold something of the open half, depth-first.
    function appendAdmxCategories(nav2, parent, depth, memo) {
        admxChildren(parent).filter((c) => categoryHasPolicies(c.ref, memo)).forEach((c) => {
            const item = button(c.name, 'gpo-admx-node', () => selectAdmx(admxHalf, c.ref));
            item.style.paddingLeft = `calc(var(--space-3) * ${depth + 1})`;
            if (admxCategory === c.ref) item.classList.add('is-selected');
            nav2.appendChild(item);
            appendAdmxCategories(nav2, c.ref, depth + 1, memo);
        });
    }

    function renderAdmxTree() {
        const nav2 = document.getElementById('gpo-admx-tree');
        nav2.replaceChildren();
        if (!admxTree) return;
        const memo = new Map();
        ['machine', 'user'].forEach((half) => {
            const label = half === 'machine' ? t('gpo.admx.computer') : t('gpo.admx.user');
            const head = button(label, 'gpo-admx-node gpo-admx-node--half', () => selectAdmx(half, null));
            if (admxHalf === half) head.classList.add('is-selected');
            nav2.appendChild(head);
            if (admxHalf === half) appendAdmxCategories(nav2, null, 1, memo);
        });
    }

    function admxRowState(policy) {
        if (policy.unsupported) return t(`gpo.admx.unsupported.${policy.unsupported}`);
        const configured = draft.admx.find((d) => sameSetting(d, policy.ref, halfFor(policy)));
        return configured ? admxSummary(configured) : t('gpo.admx.state_not_configured');
    }

    function renderAdmxList() {
        const host = document.getElementById('gpo-admx-list');
        host.replaceChildren();
        if (!admxTree) return;
        const query = document.getElementById('gpo-admx-search').value.trim().toLowerCase();
        const shown = admxTree.policies.filter((p) => inHalf(p)
            && (query ? p.name.toLowerCase().includes(query) : p.category === admxCategory));
        if (!shown.length) {
            host.appendChild(el('p', 'stat-card__meta', query ? t('gpo.admx.no_match') : t('gpo.admx.pick_category')));
            return;
        }
        shown.slice(0, 500).forEach((p) => {
            const row = el('div', 'gpo-admx-policy');
            const name = button(p.name, 'gpo-admx-policy__name', () => openAdmxSetting(admxSet, p.ref, halfFor(p)));
            row.appendChild(name);
            row.appendChild(el('span', 'stat-card__meta', admxRowState(p)));
            if (p.unsupported) row.classList.add('is-unavailable');
            host.appendChild(row);
        });
    }

    async function loadAdmxSet(digest) {
        admxSet = digest;
        admxTree = null;
        admxCategory = null;
        renderAdmxTree();
        renderAdmxList();
        if (!digest) return;
        try {
            admxTree = await api(`/api/gpo/admx/${encodeURIComponent(digest)}/tree`);
        } catch (e) {
            document.getElementById('gpo-admx-list').replaceChildren(el('p', 'setting__error', e.message));
            return;
        }
        renderAdmxTree();
        renderAdmxList();
    }

    async function openAdmxBrowser() {
        try {
            admxSets = (await api('/api/gpo/admx/sets')).sets;
        } catch (e) {
            fail(e);
            return;
        }
        const picker = document.getElementById('gpo-admx-set');
        picker.replaceChildren();
        admxSets.forEach((set) => picker.appendChild(option(set.hash,
            t('gpo.admx.set_label', { os: set.os || set.hash.slice(0, 12), count: set.machines }))));
        document.getElementById('gpo-admx-empty').hidden = admxSets.length > 0;
        document.getElementById('gpo-admx-search').value = '';
        showAdmxBrowse();
        admxDialog.showModal();
        // Most-reported first (admx.list_sets), so the default is the build most of the
        // caller's PCs run -- the templates that match what those PCs will read.
        await loadAdmxSet(admxSets.length ? admxSets[0].hash : null);
    }

    function showAdmxBrowse() {
        admxEditing = null;
        document.getElementById('gpo-admx-browse').hidden = false;
        document.getElementById('gpo-admx-setting').hidden = true;
    }

    function optionField(item, element, options) {
        const wrap = el('label', 'gpo-admx-field');
        // A list box's caption is often the presentation's text line just above it, with the
        // box's own label empty -- gpedit shows nothing there, and neither does this.
        if (item.label) wrap.appendChild(el('span', 'setting__label', item.label));
        else wrap.setAttribute('aria-label', element.id);
        const current = options[element.id];
        let input;
        if (item.kind === 'checkbox') {
            input = el('input');
            input.type = 'checkbox';
            input.checked = current === undefined ? !!item.default : !!current;
            input.addEventListener('change', () => { options[element.id] = input.checked; });
            options[element.id] = input.checked;
            wrap.classList.add('checkbox');
            wrap.prepend(input);
            return wrap;
        }
        if (item.kind === 'dropdown') {
            input = el('select', 'input');
            element.items.forEach((label, index) => input.appendChild(option(String(index), label)));
            input.value = String(current ?? item.default ?? 0);
            input.addEventListener('change', () => { options[element.id] = Number(input.value); });
            options[element.id] = Number(input.value);
        } else if (item.kind === 'listbox' || item.kind === 'multitext') {
            input = el('textarea', 'input');
            if (element.explicit) input.placeholder = t('gpo.admx.list_explicit_help');
            const lines = Array.isArray(current) ? current.map((v) => (Array.isArray(v) ? `${v[0]}=${v[1]}` : v)) : [];
            input.value = lines.join('\n');
            const read = () => {
                const raw = input.value.split('\n').map((v) => v.trim()).filter(Boolean);
                options[element.id] = element.explicit
                    ? raw.map((line) => { const at = line.indexOf('='); return at < 0 ? [line, ''] : [line.slice(0, at), line.slice(at + 1)]; })
                    : raw;
            };
            input.addEventListener('input', read);
            read();
        } else {
            input = el('input', 'input');
            if (element.type === 'decimal') {
                input.type = 'number';
                input.min = element.min;
                input.max = element.max;
            } else if (element.max_length) {
                input.maxLength = element.max_length;
            }
            input.value = String(current ?? item.default ?? '');
            input.addEventListener('input', () => { options[element.id] = input.value; });
            options[element.id] = input.value;
        }
        wrap.appendChild(input);
        return wrap;
    }

    function renderAdmxOptions(detail, options) {
        const host = document.getElementById('gpo-admx-options');
        host.replaceChildren();
        const byId = new Map(detail.elements.map((e) => [e.id, e]));
        detail.presentation.forEach((item) => {
            if (item.kind === 'text') {
                host.appendChild(el('p', 'setting__help', item.label));
                return;
            }
            const element = byId.get(item.ref);
            if (element) host.appendChild(optionField(item, element, options));
        });
    }

    async function openAdmxSetting(digest, ref, cls) {
        let detail;
        try {
            detail = await api(`/api/gpo/admx/${encodeURIComponent(digest)}/policy?ref=${encodeURIComponent(ref)}`);
        } catch (e) {
            fail(e);
            return;
        }
        if (!admxDialog.open) admxDialog.showModal();
        const existing = draft.admx.find((d) => sameSetting(d, ref, cls));
        const entry = existing
            ? { ...existing, options: structuredClone(existing.options || {}) }
            : { set: digest, ref, class: cls, name: detail.name, state: '', options: {} };
        admxEditing = { detail, entry };
        document.getElementById('gpo-admx-browse').hidden = true;
        document.getElementById('gpo-admx-setting').hidden = false;
        document.getElementById('gpo-admx-setting-title').textContent = `${detail.name} (${halfLabel(cls)})`;
        document.getElementById('gpo-admx-explain').textContent = detail.explain;
        let why = '';
        if (detail.unsupported) why = t(`gpo.admx.unsupported.${detail.unsupported}`);
        const blocked = why !== '';
        const reason = document.getElementById('gpo-admx-reason');
        reason.hidden = !blocked;
        reason.textContent = why;
        document.querySelectorAll('input[name="gpo-admx-state"]').forEach((radio) => {
            radio.checked = radio.value === (entry.state || 'not_configured');
            radio.disabled = !!blocked;
        });
        document.getElementById('gpo-admx-ok').disabled = !!blocked;
        renderAdmxOptions(detail, entry.options);
        syncAdmxOptions();
    }

    function admxState() {
        const picked = document.querySelector('input[name="gpo-admx-state"]:checked');
        return picked ? picked.value : 'not_configured';
    }

    // Options mean something only when Enabled, exactly as in gpedit, where they grey out.
    function syncAdmxOptions() {
        const enabled = admxState() === 'enabled';
        document.querySelectorAll('#gpo-admx-options input, #gpo-admx-options select, #gpo-admx-options textarea')
            .forEach((input) => { input.disabled = !enabled; });
    }

    function applyAdmxSetting() {
        if (!admxEditing) return;
        const { entry } = admxEditing;
        const state = admxState();
        const cls = entry.class || 'machine';
        const index = draft.admx.findIndex((d) => sameSetting(d, entry.ref, cls));
        if (index >= 0) draft.admx.splice(index, 1);
        // Not configured is the absence of the setting, as in gpedit -- never a stored state.
        if (state !== 'not_configured') {
            draft.admx.push({ set: entry.set, ref: entry.ref, class: cls, name: entry.name, state,
                options: state === 'enabled' ? entry.options : {} });
        }
        renderAdmxRows();
        if (admxTree) {
            showAdmxBrowse();
            renderAdmxList();
        } else {
            admxDialog.close();
        }
    }

    function openEditor(policy) {
        if (!dialog) return;
        editing = policy;
        const s = (policy && policy.settings) || {};
        draft = {
            agent_settings: { ...(s.agent_settings || {}) },
            registry: JSON.parse(JSON.stringify(s.registry || [])),
            admx: (s.admx || []).map((a) => ({ set: a.set, ref: a.ref, class: a.class || 'machine',
                name: a.name, state: a.state, options: structuredClone(a.options || {}) })),
            scripts: JSON.parse(JSON.stringify(s.scripts || [])),
        };
        document.getElementById('gpo-dialog-title').textContent = policy
            ? t('gpo.editor.edit_title', { name: policy.name }) : t('gpo.editor.new_title');
        document.getElementById('gpo-name').value = policy ? policy.name : '';
        document.getElementById('gpo-description').value = (policy && policy.description) || '';
        document.getElementById('gpo-enabled').checked = policy ? policy.enabled : true;
        document.getElementById('gpo-error').textContent = '';
        renderAgentRows();
        renderRegistryRows();
        renderAdmxRows();
        renderScriptRows();
        dialog.showModal();
        document.getElementById('gpo-name').focus();
    }

    async function savePolicy() {
        const payload = {
            name: document.getElementById('gpo-name').value.trim(),
            description: document.getElementById('gpo-description').value.trim(),
            enabled: document.getElementById('gpo-enabled').checked,
            settings: {
                agent_settings: draft.agent_settings,
                registry: draft.registry.map((e) => ({ class: e.class === 'user' ? 'user' : 'machine',
                    path: e.path, name: e.name, type: e.type || 'dword',
                    value: e.type === 'multi_string' ? (e.value || []).filter((v) => v !== '') : e.value })),
                admx: draft.admx.map((a) => ({ set: a.set, ref: a.ref, class: a.class || 'machine',
                    state: a.state, options: a.options })),
                scripts: draft.scripts,
            },
        };
        try {
            if (editing) await api(`/api/gpo/${editing.id}`, json('PUT', payload));
            else await api('/api/gpo', json('POST', payload));
            dialog.close();
            await load();
        } catch (e) {
            document.getElementById('gpo-error').textContent = e.message;
        }
    }

    // ---------------------------------------------------------------- wiring

    function select(next) {
        selected = next;
        render();
    }

    function render() {
        renderTree();
        pane.replaceChildren();
        if (selected.view === 'node') renderNode();
        else if (selected.view === 'rsop') renderRsop();
        else renderObjects();
    }

    async function load() {
        try {
            const [gpoData, treeData] = await Promise.all([api('/api/gpo'), api('/api/pc-tree')]);
            data = gpoData;
            tree = treeData;
            render();
        } catch (e) {
            pane.replaceChildren(el('p', 'setting__error', t('gpo.load_failed', { error: e.message })));
        }
    }

    if (CAN_MANAGE && dialog) {
        document.getElementById('gpo-new').addEventListener('click', () => openEditor(null));
        document.getElementById('gpo-cancel').addEventListener('click', () => dialog.close());
        document.getElementById('gpo-save').addEventListener('click', savePolicy);
        document.getElementById('gpo-admx-add').addEventListener('click', () => { void openAdmxBrowser(); });
        document.getElementById('gpo-admx-close').addEventListener('click', () => admxDialog.close());
        document.getElementById('gpo-admx-back').addEventListener('click', () => {
            if (admxTree) showAdmxBrowse();
            else admxDialog.close();
        });
        document.getElementById('gpo-admx-ok').addEventListener('click', applyAdmxSetting);
        document.getElementById('gpo-admx-set').addEventListener('change', (e) => { void loadAdmxSet(e.target.value); });
        document.getElementById('gpo-admx-search').addEventListener('input', renderAdmxList);
        document.querySelectorAll('input[name="gpo-admx-state"]').forEach((radio) => {
            radio.addEventListener('change', syncAdmxOptions);
        });
        document.getElementById('gpo-registry-add').addEventListener('click', () => {
            draft.registry.push({ class: 'machine', path: 'SOFTWARE\\Policies\\', name: '', type: 'dword', value: '' });
            renderRegistryRows();
        });
        document.getElementById('gpo-agent-add').addEventListener('change', (e) => {
            const key = e.target.value;
            if (!key) return;
            draft.agent_settings[key] = null;
            renderAgentRows();
        });
        document.getElementById('gpo-script-add').addEventListener('change', (e) => {
            const name = e.target.value;
            if (!name) return;
            draft.scripts.push({ name, inputs: {} });
            renderScriptRows();
        });
    }

    // load() reports its own failure into the pane.
    void load();
    // The editor is a <dialog>, which FleetLive waits on. `gpo` carries agents' reported state
    // and the script scheduler, so an open resultant-set view follows the PC.
    window.FleetLive.watch(['gpo', 'pc_tree', 'machines'], load);
})();
