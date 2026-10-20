// PC tree page: AD OUs and the hub's own folders, browsable the way ADUC browses a domain.
//
// The left pane is the tree -- "All PCs", "Not in a folder", the hub folders, then the AD OUs
// -- and the right pane lists the PCs under whatever is selected. PCs are filed by ticking
// rows and choosing a folder, or by dragging rows onto a folder in the tree.
//
// Everything comes from GET /api/pc-tree, which the server has already narrowed to the
// caller's scope: an OU holding none of their PCs is not in it at all. Built with
// createElement/textContent throughout -- folder names are operator text and PC names and OU
// strings are whatever a host or AD reported.
(function () {
    'use strict';

    const root = document.getElementById('pc-tree-root');
    if (!root) return;

    const CAN_MANAGE = root.dataset.canManage === '1';
    const nav = document.getElementById('pc-tree-nav');
    const body = document.getElementById('pc-machines-body');
    const titleEl = document.getElementById('pc-node-title');
    const pathEl = document.getElementById('pc-node-path');
    const actionsEl = document.getElementById('pc-node-actions');
    const filterInput = document.getElementById('pc-filter');
    const includeSub = document.getElementById('pc-include-sub');
    const moveTarget = document.getElementById('pc-move-target');
    const selectAll = document.getElementById('pc-select-all');
    const dialog = document.getElementById('pc-folder-dialog');

    let data = { folders: [], filed: {}, ous: [], machines: [] };
    // What is open on the right: {kind: 'all'|'unfiled'|'folder'|'ou', id?, dn?}
    let selected = { kind: 'all' };
    const collapsed = new Set();
    const ticked = new Set();
    let editingFolder = null;

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

    // ---------------------------------------------------------------- folder helpers

    function folderById(id) {
        return data.folders.find((f) => f.id === id) || null;
    }

    function childFolders(parentId) {
        return data.folders.filter((f) => (f.parent_id == null ? null : f.parent_id) === parentId);
    }

    function folderPath(id) {
        const names = [];
        const seen = new Set();
        let current = folderById(id);
        while (current && !seen.has(current.id)) {
            seen.add(current.id);
            names.unshift(current.name);
            current = current.parent_id == null ? null : folderById(current.parent_id);
        }
        return names.join(' / ');
    }

    function folderSubtree(id) {
        const found = new Set([id]);
        let frontier = [id];
        while (frontier.length) {
            const next = [];
            frontier.forEach((fid) => childFolders(fid).forEach((c) => {
                if (!found.has(c.id)) { found.add(c.id); next.push(c.id); }
            }));
            frontier = next;
        }
        return found;
    }

    // PCs under a folder, counted over the subtree -- what a permission group or a rule
    // scoped to this folder would reach (pc_tree.machines_in_folders).
    function folderTotal(id) {
        const ids = folderSubtree(id);
        return data.folders.filter((f) => ids.has(f.id)).reduce((n, f) => n + (f.count || 0), 0);
    }

    // ---------------------------------------------------------------- the tree

    function isSelected(kind, key) {
        if (selected.kind !== kind) return false;
        if (kind === 'folder') return selected.id === key;
        if (kind === 'ou') return selected.dn === key;
        return true;
    }

    function treeItem(label, count, kind, key, onSelect) {
        const row = el('div', 'perm-tree__row');
        const item = el('button', 'perm-tree__item', label);
        item.type = 'button';
        if (isSelected(kind, key)) item.classList.add('perm-tree__item--active');
        item.addEventListener('click', onSelect);
        row.appendChild(item);
        if (count !== null && count !== undefined) {
            row.appendChild(el('span', 'pc-tree__count', String(count)));
        }
        return { row, item };
    }

    function toggleFor(key, hasChildren, label) {
        const toggle = el('button', hasChildren
            ? `perm-tree__toggle ${collapsed.has(key) ? 'is-closed' : 'is-open'}`
            : 'perm-tree__toggle');
        toggle.type = 'button';
        if (!hasChildren) {
            toggle.disabled = true;
            toggle.setAttribute('aria-hidden', 'true');
            return toggle;
        }
        toggle.setAttribute('aria-label', collapsed.has(key)
            ? t('permissions.tree.expand', { group: label })
            : t('permissions.tree.collapse', { group: label }));
        toggle.addEventListener('click', () => {
            if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
            renderTree();
        });
        return toggle;
    }

    // Drop target: a folder (or "Not in a folder", id null) that accepts dragged PC rows.
    function makeDropTarget(item, folderId) {
        if (!CAN_MANAGE) return;
        item.addEventListener('dragover', (e) => {
            if (!e.dataTransfer.types.includes('application/x-fleethub-machines')) return;
            e.preventDefault();
            item.classList.add('perm-tree__item--drop');
        });
        item.addEventListener('dragleave', () => item.classList.remove('perm-tree__item--drop'));
        item.addEventListener('drop', (e) => {
            item.classList.remove('perm-tree__item--drop');
            const raw = e.dataTransfer.getData('application/x-fleethub-machines');
            if (!raw) return;
            e.preventDefault();
            let machines = [];
            try { machines = JSON.parse(raw); } catch (err) { return; }
            fileMachines(machines, folderId);
        });
    }

    function folderBranch(parentId, seen) {
        const list = el('ul', 'perm-tree__list');
        childFolders(parentId).forEach((folder) => {
            if (seen.has(folder.id)) return;   // a hand-made cycle must not hang the page
            const next = new Set(seen).add(folder.id);
            const key = `f:${folder.id}`;
            const kids = childFolders(folder.id);
            const li = el('li');
            const { row, item } = treeItem(folder.name, folderTotal(folder.id), 'folder', folder.id,
                () => select({ kind: 'folder', id: folder.id }));
            row.prepend(toggleFor(key, kids.length > 0, folder.name));
            makeDropTarget(item, folder.id);
            li.appendChild(row);
            if (kids.length && !collapsed.has(key)) li.appendChild(folderBranch(folder.id, next));
            list.appendChild(li);
        });
        return list;
    }

    function ouBranch(nodes) {
        const list = el('ul', 'perm-tree__list');
        nodes.forEach((node) => {
            const key = `o:${node.dn.toLowerCase()}`;
            const li = el('li');
            const { row } = treeItem(node.name, node.count, 'ou', node.dn,
                () => select({ kind: 'ou', dn: node.dn }));
            row.prepend(toggleFor(key, node.children.length > 0, node.name));
            li.appendChild(row);
            if (node.children.length && !collapsed.has(key)) li.appendChild(ouBranch(node.children));
            list.appendChild(li);
        });
        return list;
    }

    function renderTree() {
        nav.replaceChildren();
        const unfiledCount = data.machines.filter((m) => data.filed[m] == null).length;
        nav.appendChild(treeItem(t('pc_tree.all'), data.machines.length, 'all', null,
            () => select({ kind: 'all' })).row);
        const unfiled = treeItem(t('pc_tree.unfiled'), unfiledCount, 'unfiled', null,
            () => select({ kind: 'unfiled' }));
        makeDropTarget(unfiled.item, null);
        nav.appendChild(unfiled.row);

        nav.appendChild(el('div', 'pc-tree__section', t('pc_tree.folders')));
        if (data.folders.length) {
            nav.appendChild(folderBranch(null, new Set()));
        } else {
            nav.appendChild(el('p', 'stat-card__meta', t('pc_tree.no_folders')));
        }

        nav.appendChild(el('div', 'pc-tree__section', t('pc_tree.ad')));
        if (data.ous.length) {
            nav.appendChild(ouBranch(data.ous));
        } else {
            nav.appendChild(el('p', 'stat-card__meta', t('pc_tree.ad_empty')));
        }
    }

    // ---------------------------------------------------------------- the right pane

    function findOu(nodes, dn) {
        for (const node of nodes) {
            if (node.dn === dn) return node;
            const found = findOu(node.children, dn);
            if (found) return found;
        }
        return null;
    }

    function ouMachines(node, deep) {
        const out = node.machines.slice();
        if (deep) node.children.forEach((child) => out.push(...ouMachines(child, true)));
        return out;
    }

    // machine -> its OU, for the OU column.
    function ouIndex() {
        const index = {};
        const walk = (nodes) => nodes.forEach((node) => {
            node.machines.forEach((m) => { index[m] = node.dn; });
            walk(node.children);
        });
        walk(data.ous);
        return index;
    }

    function machinesForSelection() {
        const deep = includeSub.checked;
        if (selected.kind === 'unfiled') return data.machines.filter((m) => data.filed[m] == null);
        if (selected.kind === 'folder') {
            const ids = deep ? folderSubtree(selected.id) : new Set([selected.id]);
            return data.machines.filter((m) => ids.has(data.filed[m]));
        }
        if (selected.kind === 'ou') {
            const node = findOu(data.ous, selected.dn);
            return node ? ouMachines(node, deep) : [];
        }
        return data.machines.slice();
    }

    function renderHeader() {
        actionsEl.replaceChildren();
        pathEl.textContent = '';
        if (selected.kind === 'folder') {
            const folder = folderById(selected.id);
            const path = folderPath(selected.id);
            titleEl.textContent = folder ? folder.name : '';
            pathEl.textContent = path.includes(' / ') ? path : '';
            if (CAN_MANAGE && folder) {
                const sub = el('button', 'btn btn--primary', t('pc_tree.new_subfolder'));
                sub.type = 'button';
                sub.addEventListener('click', () => openFolderDialog(null, folder.id));
                const edit = el('button', 'btn', t('pc_tree.edit_folder'));
                edit.type = 'button';
                edit.addEventListener('click', () => openFolderDialog(folder, folder.parent_id));
                const remove = el('button', 'btn', t('pc_tree.delete_folder'));
                remove.type = 'button';
                remove.addEventListener('click', () => deleteFolder(folder));
                actionsEl.append(sub, edit, remove);
            }
        } else if (selected.kind === 'ou') {
            const node = findOu(data.ous, selected.dn);
            titleEl.textContent = node ? node.name : '';
            pathEl.textContent = selected.dn;
        } else if (selected.kind === 'unfiled') {
            titleEl.textContent = t('pc_tree.unfiled');
        } else {
            titleEl.textContent = t('pc_tree.all');
        }
    }

    function renderMachines() {
        const q = filterInput.value.trim().toLowerCase();
        const ous = ouIndex();
        const rows = machinesForSelection()
            .filter((m) => !q || m.toLowerCase().includes(q))
            .sort((a, b) => a.localeCompare(b));
        // Drop ticks for PCs no longer shown, so "Move" never acts on a row the operator
        // cannot see.
        Array.from(ticked).forEach((m) => { if (!rows.includes(m)) ticked.delete(m); });
        body.replaceChildren();
        if (!rows.length) {
            const tr = el('tr');
            const td = el('td', 'stat-card__meta', t('pc_tree.empty_node'));
            td.colSpan = CAN_MANAGE ? 4 : 3;
            tr.appendChild(td);
            body.appendChild(tr);
        }
        rows.forEach((machine) => {
            const tr = el('tr', 'pc-row');
            if (CAN_MANAGE) {
                const td = el('td');
                const box = document.createElement('input');
                box.type = 'checkbox';
                box.checked = ticked.has(machine);
                box.setAttribute('aria-label', t('pc_tree.select_machine', { machine }));
                box.addEventListener('change', () => {
                    if (box.checked) ticked.add(machine); else ticked.delete(machine);
                    syncSelectAll(rows);
                });
                td.appendChild(box);
                tr.appendChild(td);
                tr.draggable = true;
                tr.addEventListener('dragstart', (e) => {
                    // Dragging a ticked row carries every ticked row; an unticked one carries
                    // itself -- the same rule as a file manager.
                    const moving = ticked.has(machine) ? Array.from(ticked) : [machine];
                    e.dataTransfer.setData('application/x-fleethub-machines', JSON.stringify(moving));
                    e.dataTransfer.effectAllowed = 'move';
                });
            }
            const nameCell = el('td');
            const link = el('a', null, machine);
            link.href = `/machine/${encodeURIComponent(machine)}`;
            nameCell.appendChild(link);
            tr.appendChild(nameCell);
            const folderId = data.filed[machine];
            tr.appendChild(el('td', folderId == null ? 'stat-card__meta' : null,
                folderId == null ? '—' : folderPath(folderId)));
            tr.appendChild(el('td', 'stat-card__meta', ous[machine] || '—'));
            body.appendChild(tr);
        });
        syncSelectAll(rows);
    }

    function syncSelectAll(rows) {
        if (!selectAll) return;
        selectAll.checked = rows.length > 0 && rows.every((m) => ticked.has(m));
    }

    function fillMoveTarget() {
        if (!moveTarget) return;
        const previous = moveTarget.value;
        moveTarget.replaceChildren();
        const placeholder = el('option', null, t('pc_tree.move_to'));
        placeholder.value = '';
        moveTarget.appendChild(placeholder);
        const none = el('option', null, t('pc_tree.move_none'));
        none.value = 'none';
        moveTarget.appendChild(none);
        data.folders
            .map((f) => ({ id: f.id, label: folderPath(f.id) }))
            .sort((a, b) => a.label.localeCompare(b.label))
            .forEach(({ id, label }) => {
                const option = el('option', null, label);
                option.value = String(id);
                moveTarget.appendChild(option);
            });
        moveTarget.value = Array.from(moveTarget.options).some((o) => o.value === previous) ? previous : '';
    }

    function select(next) {
        selected = next;
        ticked.clear();
        render();
    }

    function render() {
        // A folder deleted, or an OU emptied, under the operator's feet: fall back to the top.
        if (selected.kind === 'folder' && !folderById(selected.id)) selected = { kind: 'all' };
        if (selected.kind === 'ou' && !findOu(data.ous, selected.dn)) selected = { kind: 'all' };
        renderTree();
        renderHeader();
        renderMachines();
        fillMoveTarget();
    }

    async function load() {
        try {
            data = await api('/api/pc-tree');
            render();
        } catch (e) {
            body.replaceChildren();
            const tr = el('tr');
            const td = el('td', 'stat-card__meta', t('pc_tree.load_failed', { error: e.message }));
            td.colSpan = 4;
            tr.appendChild(td);
            body.appendChild(tr);
        }
    }

    // ---------------------------------------------------------------- writes

    async function fileMachines(machines, folderId) {
        if (!machines.length) {
            toast(t('pc_tree.nothing_selected'), { kind: 'error' });
            return;
        }
        try {
            const result = await api('/api/pc-tree/file', json('POST', { machines, folder_id: folderId }));
            toast(tPlural('pc_tree.moved', (result.moved || []).length));
            ticked.clear();
            await load();
        } catch (e) {
            toast(t('pc_tree.action_failed', { error: e.message }), { kind: 'error' });
        }
    }

    async function deleteFolder(folder) {
        if (!await confirmDialog({
            title: t('pc_tree.confirm_delete_title'),
            message: t('pc_tree.confirm_delete', { folder: folder.name }),
            confirmLabel: t('common.delete'),
            danger: true,
        })) return;
        try {
            await api(`/api/pc-tree/folders/${folder.id}`, { method: 'DELETE' });
            selected = folder.parent_id == null ? { kind: 'all' } : { kind: 'folder', id: folder.parent_id };
            await load();
        } catch (e) {
            toast(t('pc_tree.action_failed', { error: e.message }), { kind: 'error' });
        }
    }

    function fillParentSelect(select, excluded, current) {
        select.replaceChildren();
        const top = el('option', null, t('pc_tree.dialog.parent_none'));
        top.value = '';
        select.appendChild(top);
        data.folders
            .filter((f) => !excluded.has(f.id))
            .map((f) => ({ id: f.id, label: folderPath(f.id) }))
            .sort((a, b) => a.label.localeCompare(b.label))
            .forEach(({ id, label }) => {
                const option = el('option', null, label);
                option.value = String(id);
                select.appendChild(option);
            });
        select.value = current == null ? '' : String(current);
    }

    function openFolderDialog(folder, parentId) {
        if (!dialog) return;
        editingFolder = folder;
        document.getElementById('pc-folder-title').textContent = folder
            ? t('pc_tree.dialog.edit_title', { folder: folder.name })
            : t('pc_tree.dialog.new_title');
        document.getElementById('pc-folder-name').value = folder ? folder.name : '';
        document.getElementById('pc-folder-description').value = (folder && folder.description) || '';
        document.getElementById('pc-folder-error').textContent = '';
        // A folder cannot move into itself or anything beneath it; not offering those is
        // kinder than the server's refusal.
        const excluded = folder ? folderSubtree(folder.id) : new Set();
        fillParentSelect(document.getElementById('pc-folder-parent'), excluded, parentId);
        dialog.showModal();
        document.getElementById('pc-folder-name').focus();
    }

    async function saveFolder() {
        const errorEl = document.getElementById('pc-folder-error');
        const parentRaw = document.getElementById('pc-folder-parent').value;
        const payload = {
            name: document.getElementById('pc-folder-name').value.trim(),
            description: document.getElementById('pc-folder-description').value.trim(),
            parent_id: parentRaw ? Number(parentRaw) : null,
        };
        try {
            let folder;
            if (editingFolder) {
                folder = await api(`/api/pc-tree/folders/${editingFolder.id}`, json('PUT', payload));
            } else {
                folder = await api('/api/pc-tree/folders', json('POST', payload));
            }
            dialog.close();
            selected = { kind: 'folder', id: folder.id };
            await load();
        } catch (e) {
            errorEl.textContent = e.message;
        }
    }

    // ---------------------------------------------------------------- wiring

    filterInput.addEventListener('input', renderMachines);
    includeSub.addEventListener('change', renderMachines);
    if (selectAll) {
        selectAll.addEventListener('change', () => {
            const rows = Array.from(body.querySelectorAll('tr.pc-row a')).map((a) => a.textContent);
            rows.forEach((m) => { if (selectAll.checked) ticked.add(m); else ticked.delete(m); });
            renderMachines();
        });
    }
    if (CAN_MANAGE) {
        document.getElementById('pc-folder-new').addEventListener('click', () => openFolderDialog(null, null));
        document.getElementById('pc-folder-cancel').addEventListener('click', () => dialog.close());
        document.getElementById('pc-folder-save').addEventListener('click', saveFolder);
        document.getElementById('pc-folder-name').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); saveFolder(); }
        });
        document.getElementById('pc-move').addEventListener('click', () => {
            const target = moveTarget.value;
            if (!target) return;
            fileMachines(Array.from(ticked), target === 'none' ? null : Number(target));
        });
        dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
    }

    load();
    // A PC enrolling or a directory sync moving one changes the tree too, not only a write here.
    window.FleetLive.watch(['pc_tree', 'machines', 'permissions'], load);
})();
