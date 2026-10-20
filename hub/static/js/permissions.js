// Permission Groups admin page.
//
// The whole page is one list + one <dialog> editor. Two rules it sticks to:
//
//  * Everything is built with textContent / createElement, never innerHTML. Group
//    names, member emails and machine hostnames are operator- and agent-supplied
//    strings that get re-rendered on every load; this page is where an XSS would be
//    worth the most, since its audience is by definition the people who can grant
//    capabilities.
//  * The capability vocabulary comes from the server (GET
//    /api/permissions/capabilities), not a copy here. A hardcoded list would silently
//    stop offering a new capability, which reads as "the feature doesn't work" rather
//    than "the UI is stale".
//
// Groups nest (permissions.py), so the list is laid out the way ADUC lays out a domain: the
// tree on the left, and on the right the selected group with the groups directly beneath it.
// What a group INHERITS is always drawn beside what it grants itself, never merged into it --
// an admin reading a row has to be able to tell "this group grants X" from "a parent does",
// because only the first one goes away when they edit this row.

const groupsHost = document.getElementById('groups-host');
const treeHost = document.getElementById('group-tree');
const detailHost = document.getElementById('group-detail');
const parentSelect = document.getElementById('group-parent');
const inheritedScopeEl = document.getElementById('inherited-scope');
const folderPicker = document.getElementById('folder-picker');
const folderChips = document.getElementById('folder-chips');
const folderSelect = document.getElementById('folder-select');
const folderResolvedEl = document.getElementById('folder-resolved');
const modal = document.getElementById('group-modal');
const modalTitle = document.getElementById('group-modal-title');
const nameInput = document.getElementById('group-name');
const descriptionInput = document.getElementById('group-description');
const capabilityList = document.getElementById('capability-list');
const machinePicker = document.getElementById('machine-picker');
const machineChips = document.getElementById('machine-chips');
const machineInput = document.getElementById('machine-input');
const memberChips = document.getElementById('member-chips');
const memberInput = document.getElementById('member-input');
const ouPicker = document.getElementById('ou-picker');
const ouChips = document.getElementById('ou-chips');
const ouInput = document.getElementById('ou-input');
const ouResolvedEl = document.getElementById('ou-resolved');
const dirGroupChips = document.getElementById('dirgroup-chips');
const dirGroupInput = document.getElementById('dirgroup-input');
const dirGroupSelfEl = document.getElementById('dirgroup-self');
const errorEl = document.getElementById('group-error');
const groupStatusEl = document.getElementById('group-status');

let capabilities = [];        // [{name, label, description}]
let editingId = null;         // null while creating
let draftMachines = [];
let draftMembers = [];
let draftDirGroups = [];
let draftOus = [];
let machineDirectory = [];    // full /api/machines rows, for the machine picker's search
let myDirGroups = [];         // this session's own mapped directory tokens, as a hint
let fleetOus = [];            // OUs the fleet is actually in, for the OU picker
let allGroups = [];           // the last /api/permissions/groups answer, for tree + editor
let selectedId = null;        // the group open on the right; null is "All groups"
const collapsed = new Set();  // tree nodes the admin folded away
let pcFolders = [];           // /api/pc-tree folders, for the folder scope picker
let draftFolders = [];
let creatingUnder = null;     // the parent a "New subgroup" editor was opened beneath

async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
    if (!resp.ok) {
        throw new Error((body && body.error) || `HTTP ${resp.status}`);
    }
    return body;
}


// ---------------------------------------------------------------- the group list

function capabilityLabel(name) {
    const found = capabilities.find((c) => c.name === name);
    return found ? found.label : name;
}

function groupById(id) {
    return allGroups.find((g) => g.id === id) || null;
}

function childrenOf(id) {
    // A parent the list does not contain counts as top level -- the same reading the server
    // gives a dangling parent (permissions._ancestor_ids).
    return allGroups.filter((g) => (g.parent_id && groupById(g.parent_id) ? g.parent_id : null) === id);
}

// "Hospital / Ward 3 / Admins" -- the name an admin needs wherever a group is picked out of
// a flat list, since two subgroups may share a name under different parents.
function groupPath(group) {
    const names = (group.ancestors || []).slice().reverse()
        .map((id) => (groupById(id) || {}).name).filter(Boolean);
    names.push(group.name);
    return names.join(' / ');
}

function descendantIds(id) {
    const found = new Set();
    const walk = (parent) => childrenOf(parent).forEach((child) => {
        if (!found.has(child.id)) { found.add(child.id); walk(child.id); }
    });
    walk(id);
    return found;
}

function folderLabel(id) {
    const byId = new Map(pcFolders.map((f) => [f.id, f]));
    const names = [];
    let current = byId.get(id);
    const seen = new Set();
    while (current && !seen.has(current.id)) {
        seen.add(current.id);
        names.unshift(current.name);
        current = current.parent_id == null ? null : byId.get(current.parent_id);
    }
    return names.length ? names.join(' / ') : t('permissions.editor.folder_missing', { id });
}

// ---------------------------------------------------------------- the tree (left pane)

function selectGroup(id) {
    selectedId = id;
    renderAll();
}

function renderTree() {
    treeHost.replaceChildren();
    const all = el('button', 'perm-tree__item', t('permissions.tree.all'));
    all.type = 'button';
    if (selectedId === null) all.classList.add('perm-tree__item--active');
    all.addEventListener('click', () => selectGroup(null));
    treeHost.appendChild(all);
    treeHost.appendChild(renderBranch(null, new Set()));
}

function renderBranch(parentId, seen) {
    const list = el('ul', 'perm-tree__list');
    childrenOf(parentId).forEach((group) => {
        if (seen.has(group.id)) return;   // a hand-made cycle must not hang the page
        const next = new Set(seen).add(group.id);
        const li = el('li');
        const row = el('div', 'perm-tree__row');
        const kids = childrenOf(group.id);
        const toggle = el('button', kids.length
            ? `perm-tree__toggle ${collapsed.has(group.id) ? 'is-closed' : 'is-open'}`
            : 'perm-tree__toggle');
        toggle.type = 'button';
        if (kids.length) {
            toggle.setAttribute('aria-label', collapsed.has(group.id)
                ? t('permissions.tree.expand', { group: group.name })
                : t('permissions.tree.collapse', { group: group.name }));
            toggle.addEventListener('click', () => {
                if (collapsed.has(group.id)) collapsed.delete(group.id); else collapsed.add(group.id);
                renderTree();
            });
        } else {
            toggle.disabled = true;
            toggle.setAttribute('aria-hidden', 'true');
        }
        const item = el('button', 'perm-tree__item', group.name);
        item.type = 'button';
        if (group.id === selectedId) item.classList.add('perm-tree__item--active');
        item.addEventListener('click', () => selectGroup(group.id));
        row.append(toggle, item);
        li.appendChild(row);
        if (kids.length && !collapsed.has(group.id)) li.appendChild(renderBranch(group.id, next));
        list.appendChild(li);
    });
    return list;
}

// ---------------------------------------------------------------- the selected group

function renderDetail() {
    detailHost.replaceChildren();
    const group = selectedId ? groupById(selectedId) : null;
    if (!group) return;
    const card = el('div', 'card perm-detail');
    if ((group.ancestors || []).length) {
        card.appendChild(el('div', 'stat-card__meta', groupPath(group)));
    }
    card.appendChild(el('h2', 'section-title', group.name));
    if (group.description) card.appendChild(el('p', 'stat-card__meta', group.description));
    const actions = el('div', 'perm-row-actions');
    actions.style.justifyContent = 'flex-start';
    const edit = el('button', 'btn', t('common.edit'));
    edit.type = 'button';
    edit.addEventListener('click', () => openEditor(group));
    const sub = el('button', 'btn btn--primary', t('permissions.new_subgroup'));
    sub.type = 'button';
    sub.addEventListener('click', () => openEditor(null, group.id));
    const remove = el('button', 'btn', t('common.delete'));
    remove.type = 'button';
    remove.addEventListener('click', () => deleteGroup(group, remove));
    actions.append(edit, sub, remove);
    card.appendChild(actions);
    // The selected group's own row, so its capabilities, scope and members read exactly as
    // they do in the list -- one rendering, not a second one that can drift from it.
    const table = el('table', 'data-table');
    table.appendChild(tableHead());
    const body = el('tbody');
    body.appendChild(renderGroupRow(group, { actions: false }));
    table.appendChild(body);
    card.appendChild(table);
    detailHost.appendChild(card);
}

function renderAll() {
    if (selectedId && !groupById(selectedId)) selectedId = null;
    renderTree();
    renderDetail();
    renderGroups(childrenOf(selectedId));
}

function tableHead() {
    const head = el('thead');
    const headRow = el('tr');
    [t('permissions.col.group'), t('permissions.col.capabilities'),
     t('permissions.col.machines'), t('permissions.col.members'), ''].forEach((label) => {
        headRow.appendChild(el('th', null, label));
    });
    head.appendChild(headRow);
    return head;
}

// ---------------------------------------------------------------- the group list (right pane)

function renderGroups(groups) {
    groupsHost.replaceChildren();
    if (!allGroups.length) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('permissions.empty')));
        empty.appendChild(el('p', 'stat-card__meta', t('permissions.empty_hint')));
        groupsHost.appendChild(empty);
        return;
    }
    const selected = selectedId ? groupById(selectedId) : null;
    groupsHost.appendChild(el('h3', 'perm-subhead', selected
        ? t('permissions.tree.subgroups', { group: selected.name })
        : t('permissions.tree.top_level')));
    if (!groups.length) {
        groupsHost.appendChild(el('p', 'stat-card__meta', t('permissions.tree.no_subgroups')));
        return;
    }

    const card = el('div', 'card');
    const table = el('table', 'data-table');
    table.appendChild(tableHead());

    const body = el('tbody');
    groups.forEach((group) => body.appendChild(renderGroupRow(group)));
    table.appendChild(body);
    card.appendChild(table);
    groupsHost.appendChild(card);
}

// The nearest ancestor that grants `capability` -- named on an inherited badge so an admin
// knows which row to edit if the grant is wrong.
function grantingAncestor(group, capability) {
    for (const id of group.ancestors || []) {
        const ancestor = groupById(id);
        if (ancestor && ancestor.capabilities.includes(capability)) return ancestor;
    }
    return null;
}

function renderGroupRow(group, options) {
    const showActions = !options || options.actions !== false;
    const tr = el('tr');

    const nameCell = el('td');
    // The name drills in, the way double-clicking a container does in ADUC.
    const open = el('button', 'perm-link', group.name);
    open.type = 'button';
    open.addEventListener('click', () => selectGroup(group.id));
    nameCell.appendChild(open);
    if (group.description) {
        nameCell.appendChild(el('div', 'stat-card__meta', group.description));
    }
    tr.appendChild(nameCell);

    const capsCell = el('td');
    const inherited = group.inherited_capabilities || [];
    if (!group.capabilities.length && !inherited.length) {
        capsCell.appendChild(el('span', 'stat-card__meta', t('permissions.none')));
    }
    group.capabilities.forEach((name) => {
        // The one capability worth calling out visually: it lets its holder grant
        // themselves everything else on this very page.
        const isAdmin = name === 'manage_permission_groups';
        capsCell.appendChild(
            el('span', isAdmin ? 'cap-badge cap-badge--admin' : 'cap-badge',
               capabilityLabel(name)));
    });
    inherited.forEach((name) => {
        const badge = el('span', 'cap-badge cap-badge--inherited', capabilityLabel(name));
        const from = grantingAncestor(group, name);
        if (from) badge.title = t('permissions.editor.inherited_capability', { group: from.name });
        capsCell.appendChild(badge);
    });
    tr.appendChild(capsCell);

    const machinesCell = el('td');
    if (group.scope_mode === 'all') {
        machinesCell.appendChild(
            el('span', 'cap-badge cap-badge--admin', t('permissions.every_machine')));
    } else if (!group.machines.length) {
        machinesCell.appendChild(el('span', 'stat-card__meta', t('permissions.none')));
    } else {
        machinesCell.appendChild(el('span', null, String(group.machines.length)));
        machinesCell.appendChild(el('div', 'stat-card__meta', group.machines.join(', ')));
    }
    if (group.scope_mode === 'ad_ou') {
        // Name the RULE as well as its current result: the count above is what the OUs
        // resolve to today, and an operator reading the row needs to know it will move on
        // its own when someone re-files a PC in AD.
        const ous = group.ous || [];
        machinesCell.appendChild(el('div', 'stat-card__meta',
            ous.length ? t('permissions.from_ou', { ous: ous.join(', ') })
                       : t('permissions.from_ou_empty')));
    }
    if (group.scope_mode === 'folder') {
        const folders = group.folders || [];
        machinesCell.appendChild(el('div', 'stat-card__meta',
            folders.length ? t('permissions.from_folder', { folders: folders.map(folderLabel).join(', ') })
                           : t('permissions.from_folder_empty')));
    }
    // Inherited scope last, and in its own line: it is real access this group's members hold,
    // and the one part of the row an edit HERE cannot take away.
    if (group.inherited_all) {
        machinesCell.appendChild(el('div', 'cap-badge cap-badge--inherited',
            t('permissions.inherited_every_machine')));
    } else if ((group.inherited_machines || []).length) {
        machinesCell.appendChild(el('div', 'stat-card__meta',
            tPlural('permissions.inherited_machines', group.inherited_machines.length)));
    }
    tr.appendChild(machinesCell);

    const membersCell = el('td');
    const dirGroups = group.directory_groups || [];
    if (!group.members.length && !dirGroups.length) {
        membersCell.appendChild(el('span', 'stat-card__meta', t('permissions.none')));
    }
    if (group.members.length) {
        membersCell.appendChild(el('div', 'stat-card__meta', group.members.join(', ')));
    }
    if (dirGroups.length) {
        // Named separately from the emails: these are not people, and a group whose only
        // membership is a directory mapping would otherwise read as "nobody has this"
        // when it may in fact be the widest grant on the page.
        membersCell.appendChild(el('div', 'stat-card__meta',
            tPlural('permissions.directory_group_list', dirGroups.length,
                    { groups: dirGroups.join(', ') })));
    }
    tr.appendChild(membersCell);

    if (!showActions) {
        tr.appendChild(el('td'));
        return tr;
    }
    const actions = el('td');
    const wrap = el('div', 'perm-row-actions');
    const edit = el('button', 'btn', t('common.edit'));
    edit.type = 'button';
    edit.addEventListener('click', () => openEditor(group));
    const remove = el('button', 'btn', t('common.delete'));
    remove.type = 'button';
    remove.addEventListener('click', () => deleteGroup(group, remove));
    wrap.append(edit, remove);
    actions.appendChild(wrap);
    tr.appendChild(actions);

    return tr;
}

async function deleteGroup(group, btn) {
    // Two whole sentences rather than one built from clauses: which counts the warning
    // has to name depends on whether the group grants access through a directory mapping
    // as well as by name, and a sentence spliced together around that reads as English
    // word order in every other language.
    const dirCount = (group.directory_groups || []).length;
    const warning = dirCount
        ? tPlural('permissions.confirm_delete_directory', dirCount, {
            group: group.name,
            members: tPlural('permissions.member_count', group.members.length),
          })
        : tPlural('permissions.confirm_delete', group.members.length, { group: group.name });
    if (!await confirmDialog({
        title: t('permissions.confirm_delete_title'),
        message: warning,
        confirmLabel: t('common.delete'),
        danger: true,
    })) return;
    btn.disabled = true;
    try {
        await api(`/api/permissions/groups/${encodeURIComponent(group.id)}`,
                  { method: 'DELETE' });
        await loadGroups();
    } catch (e) {
        btn.disabled = false;
        toast(t('permissions.delete_failed', { group: group.name, error: e.message }),
              { kind: 'error' });
    }
}

async function loadGroups() {
    try {
        allGroups = await api('/api/permissions/groups');
        renderAll();
    } catch (e) {
        groupsHost.replaceChildren();
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, t('permissions.load_failed', { error: e.message })));
        groupsHost.appendChild(empty);
    }
}

// ---------------------------------------------------------------- the editor

function renderChips(host, values, onRemove) {
    host.replaceChildren();
    values.forEach((value) => {
        const chip = el('span', 'chip');
        chip.appendChild(el('span', 'chip__name', value));
        const x = el('button', 'chip__remove', '×');
        x.type = 'button';
        x.setAttribute('aria-label', t('permissions.editor.remove', { value }));
        x.addEventListener('click', () => onRemove(value));
        chip.appendChild(x);
        host.appendChild(chip);
    });
}

function renderMachineChips() {
    renderChips(machineChips, draftMachines, (value) => {
        draftMachines = draftMachines.filter((m) => m !== value);
        renderMachineChips();
        autoSaveGroup();
    });
}

function renderMemberChips() {
    renderChips(memberChips, draftMembers, (value) => {
        draftMembers = draftMembers.filter((m) => m !== value);
        renderMemberChips();
        autoSaveGroup();
    });
}

function renderOuChips() {
    renderChips(ouChips, draftOus, (value) => {
        draftOus = draftOus.filter((o) => o !== value);
        renderOuChips();
        autoSaveGroup();
    });
}

// An OU scope is a RULE, not a list, so the editor has to answer "and which machines is
// that right now?" -- otherwise an admin picks an OU and has no way to tell whether they
// picked the right one until somebody complains about their access.
function renderOuResolved(group) {
    ouResolvedEl.replaceChildren();
    if (scopeMode() !== 'ad_ou') return;
    if (!draftOus.length) {
        ouResolvedEl.appendChild(el('span', null, t('permissions.editor.ou_none_selected')));
        return;
    }
    const machines = (group && group.machines) || [];
    if (!machines.length) {
        ouResolvedEl.appendChild(el('span', null, t('permissions.editor.ou_no_machines')));
        return;
    }
    ouResolvedEl.appendChild(el('span', null,
        tPlural('permissions.editor.ou_in_scope', machines.length, {
            machines: machines.slice(0, 12).join(', ') + (machines.length > 12 ? '…' : ''),
        })));
}

function renderDirGroupChips() {
    renderChips(dirGroupChips, draftDirGroups, (value) => {
        draftDirGroups = draftDirGroups.filter((d) => d !== value);
        renderDirGroupChips();
        autoSaveGroup();
    });
}

// There is no directory to search — the hub never queries one, it only hears what an
// issuer asserts at sign-in — so instead of a picker, offer the admin the tokens their
// OWN session carried. That answers the question this field actually raises ("which
// claim does my tenant send, and in what form?") with real data from this provider,
// rather than making them read Entra's docs and guess.
function renderDirGroupHint() {
    dirGroupSelfEl.replaceChildren();
    if (!myDirGroups.length) return;
    dirGroupSelfEl.appendChild(el('span', null, t('permissions.editor.directory_self') + ' '));
    myDirGroups.forEach((token, i) => {
        if (i) dirGroupSelfEl.appendChild(el('span', null, ', '));
        const btn = el('button', 'dirgroup-token', token);
        btn.type = 'button';
        btn.title = t('permissions.editor.directory_add_token');
        btn.addEventListener('click', () => {
            addValue(draftDirGroups, token, renderDirGroupChips);
        });
        dirGroupSelfEl.appendChild(btn);
    });
}

// What the parent currently chosen in the editor would hand this group: every ancestor's own
// grants, nearest first. Worked out here from the loaded list rather than taken from the saved
// group, because the admin may have just picked a different parent and must see what THAT one
// brings before it is saved.
function draftAncestors() {
    const chain = [];
    const seen = new Set(editingId ? [editingId] : []);
    let current = groupById(parentSelect.value || null);
    while (current && !seen.has(current.id)) {
        seen.add(current.id);
        chain.push(current);
        current = current.parent_id ? groupById(current.parent_id) : null;
    }
    return chain;
}

function renderCapabilities(selected) {
    capabilityList.replaceChildren();
    const ancestors = draftAncestors();
    capabilities.forEach((capability) => {
        const row = el('label', 'perm-capability');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.value = capability.name;
        const from = ancestors.find((a) => a.capabilities.includes(capability.name));
        const text = el('span');
        text.appendChild(el('span', 'perm-capability__label', capability.label));
        if (from) {
            // Ticked and locked: the grant is real for this group's members, and unticking it
            // here could not take it away -- only an edit to the parent can.
            box.checked = true;
            box.disabled = true;
            text.appendChild(el('span', 'perm-capability__inherited',
                t('permissions.editor.inherited_capability', { group: from.name })));
        } else {
            box.checked = selected.includes(capability.name);
        }
        text.appendChild(el('span', 'perm-capability__help', capability.description));
        row.append(box, text);
        capabilityList.appendChild(row);
    });
}

function selectedCapabilities() {
    // :not(:disabled) -- an inherited box is ticked for display only, and saving it as this
    // group's own grant would keep it after the group is moved out from under that parent.
    return Array.from(capabilityList.querySelectorAll('input:checked:not(:disabled)'))
        .map((b) => b.value);
}

// Remember the own ticks across a parent change, then redraw with the new inheritance.
function rerenderCapabilities() {
    renderCapabilities(selectedCapabilities());
}

function renderInheritedScope() {
    const ancestors = draftAncestors();
    inheritedScopeEl.replaceChildren();
    inheritedScopeEl.hidden = !ancestors.length;
    if (!ancestors.length) return;
    if (ancestors.some((a) => a.scope_mode === 'all')) {
        inheritedScopeEl.textContent = t('permissions.editor.inherited_scope_all');
        return;
    }
    const machines = Array.from(new Set(ancestors.flatMap((a) => a.machines || []))).sort();
    if (!machines.length) {
        inheritedScopeEl.hidden = true;
        return;
    }
    inheritedScopeEl.textContent = tPlural('permissions.editor.inherited_scope_machines',
        machines.length, {
            machines: machines.slice(0, 12).join(', ') + (machines.length > 12 ? '…' : ''),
        });
}

function fillParentSelect(currentParent) {
    // Every group except this one and the groups beneath it -- picking one of those would be a
    // cycle, which the server refuses; not offering it is kinder than a refusal.
    const excluded = editingId ? descendantIds(editingId) : new Set();
    if (editingId) excluded.add(editingId);
    while (parentSelect.options.length > 1) parentSelect.remove(1);
    allGroups
        .filter((g) => !excluded.has(g.id))
        .map((g) => ({ id: g.id, label: groupPath(g) }))
        .sort((a, b) => a.label.localeCompare(b.label))
        .forEach(({ id, label }) => {
            const option = document.createElement('option');
            option.value = id;
            option.textContent = label;
            parentSelect.appendChild(option);
        });
    parentSelect.value = currentParent || '';
}

function renderFolderChips() {
    folderChips.replaceChildren();
    draftFolders.forEach((id) => {
        const chip = el('span', 'chip');
        chip.appendChild(el('span', 'chip__name', folderLabel(id)));
        const x = el('button', 'chip__remove', '×');
        x.type = 'button';
        x.setAttribute('aria-label', t('permissions.editor.remove', { value: folderLabel(id) }));
        x.addEventListener('click', () => {
            draftFolders = draftFolders.filter((f) => f !== id);
            renderFolderChips();
            autoSaveGroup();
        });
        chip.appendChild(x);
        folderChips.appendChild(chip);
    });
}

function fillFolderSelect() {
    while (folderSelect.options.length > 1) folderSelect.remove(1);
    folderSelect.options[0].textContent = pcFolders.length
        ? t('permissions.editor.folder_choose') : t('permissions.editor.folder_none_defined');
    pcFolders
        .map((f) => ({ id: f.id, label: folderLabel(f.id) }))
        .sort((a, b) => a.label.localeCompare(b.label))
        .forEach(({ id, label }) => {
            const option = document.createElement('option');
            option.value = String(id);
            option.textContent = label;
            folderSelect.appendChild(option);
        });
    folderSelect.value = '';
}

// The folder twin of renderOuResolved: a folder scope is a rule too, so show what it
// resolves to now.
function renderFolderResolved(group) {
    folderResolvedEl.replaceChildren();
    if (scopeMode() !== 'folder') return;
    if (!draftFolders.length) {
        folderResolvedEl.appendChild(el('span', null, t('permissions.editor.folder_none_selected')));
        return;
    }
    const machines = (group && group.scope_mode === 'folder' && group.machines) || [];
    if (!machines.length) return;
    folderResolvedEl.appendChild(el('span', null,
        tPlural('permissions.editor.ou_in_scope', machines.length, {
            machines: machines.slice(0, 12).join(', ') + (machines.length > 12 ? '…' : ''),
        })));
}

function scopeMode() {
    const checked = modal.querySelector('input[name="scope-mode"]:checked');
    return checked ? checked.value : 'list';
}

function syncScopeMode() {
    // Show exactly the picker the selected mode uses. A list left visible under "every
    // machine" -- or under an OU rule -- is a list that silently has no effect, which is
    // the kind of thing an admin reads as a grant that is in force.
    const mode = scopeMode();
    machinePicker.hidden = mode !== 'list';
    ouPicker.hidden = mode !== 'ad_ou';
    folderPicker.hidden = mode !== 'folder';
}

// `parentId` is set when the editor opens from "New subgroup", so the new group is created
// beneath the one on screen rather than at the top level.
function openEditor(group, parentId) {
    editingId = group ? group.id : null;
    creatingUnder = group ? null : (parentId || null);
    modalTitle.textContent = group
        ? t('permissions.editor.edit_title', { group: group.name })
        : t('permissions.editor.new_title');
    nameInput.value = group ? group.name : '';
    descriptionInput.value = (group && group.description) || '';
    draftMachines = group ? group.machines.slice() : [];
    draftMembers = group ? group.members.slice() : [];
    draftDirGroups = group ? (group.directory_groups || []).slice() : [];
    draftOus = group ? (group.ous || []).slice() : [];
    draftFolders = group ? (group.folders || []).slice() : [];
    const mode = group ? group.scope_mode : 'list';
    modal.querySelectorAll('input[name="scope-mode"]').forEach((radio) => {
        radio.checked = radio.value === mode;
    });
    fillParentSelect(group ? group.parent_id : creatingUnder);
    fillFolderSelect();
    renderCapabilities(group ? group.capabilities : []);
    renderInheritedScope();
    renderMachineChips();
    renderMemberChips();
    renderOuChips();
    renderFolderChips();
    renderDirGroupChips();
    renderDirGroupHint();
    syncScopeMode();
    renderOuResolved(group);
    renderFolderResolved(group);
    errorEl.textContent = '';
    // Reset the auto-save machinery for the new editing session.
    groupSaving = false;
    groupPending = false;
    if (groupDebounce) { clearTimeout(groupDebounce); groupDebounce = null; }
    setGroupStatus(group ? '' : t('permissions.editor.name_required'), '');
    modal.showModal();
    nameInput.focus();
}

// ---------------------------------------------------------------- auto-save
//
// The editor saves as you go instead of on a Save button. Two guards make that safe on a
// page that grants sign-in access: (1) saves are serialised -- one request at a time, with
// a `pending` flag so an edit made mid-request is written straight after, never dropped;
// (2) every save sends the WHOLE group (name, capabilities, scope, machines, members), so
// there is no window where a half-applied group is live. A brand-new group cannot be
// created without a name, so nothing is written until one is typed; from the first
// successful create we hold its id and switch to updating it in place.

let groupSaving = false;
let groupPending = false;
let groupDebounce = null;

function setGroupStatus(text, cls) {
    if (!groupStatusEl) return;
    groupStatusEl.textContent = text;
    groupStatusEl.className = cls ? `autosave ${cls}` : 'autosave';
}

// Text fields debounce so a name is not POSTed letter by letter; discrete changes
// (a capability ticked, a chip added, the scope mode switched) save at once.
function autoSaveGroupDebounced() {
    if (groupDebounce) clearTimeout(groupDebounce);
    groupDebounce = setTimeout(autoSaveGroup, 500);
}

function autoSaveGroup() {
    if (groupDebounce) { clearTimeout(groupDebounce); groupDebounce = null; }
    if (groupSaving) { groupPending = true; return; }
    flushGroup();
}

async function flushGroup() {
    const name = nameInput.value.trim();
    // A group cannot exist without a name; don't create one until there is something to
    // call it. Editing an existing group with the name cleared is a real (rejected) edit,
    // so only short-circuit while still creating.
    if (!editingId && !name) {
        setGroupStatus(t('permissions.editor.name_required'), '');
        return;
    }
    const payload = {
        name,
        description: descriptionInput.value.trim(),
        capabilities: selectedCapabilities(),
        scope_mode: scopeMode(),
        // Always send machines, even in "all" mode: the server ignores the list for an
        // "all" group, and keeping it means switching back to "list" doesn't silently
        // discard what was there.
        machines: draftMachines,
        members: draftMembers,
        directory_groups: draftDirGroups,
        // Always sent, like machines: the server ignores OUs for a non-ad_ou group, and
        // keeping them means switching modes back and forth doesn't discard what was set.
        ous: draftOus,
        folders: draftFolders,
        // Always sent, and "" means top level -- the server reads a present key as the
        // parent to set (permissions_web.update_permission_group).
        parent_id: parentSelect.value || '',
    };
    groupSaving = true;
    groupPending = false;
    errorEl.textContent = '';
    setGroupStatus(t('common.saving'), '');
    try {
        let group;
        if (editingId) {
            group = await api(`/api/permissions/groups/${encodeURIComponent(editingId)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
        } else {
            group = await api('/api/permissions/groups', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            // From here on this is an existing group: hold its id and update in place, so a
            // second change doesn't create a duplicate.
            editingId = group.id;
            modalTitle.textContent = t('permissions.editor.edit_title', { group: group.name });
        }
        setGroupStatus(t('common.saved'), 'autosave--saved');
        // The saved group carries its freshly re-derived machine list, so an OU edit
        // shows its effect immediately rather than at the next time the editor is opened.
        renderOuResolved(group);
        renderFolderResolved(group);
        await loadGroups();      // refresh the list behind the modal
    } catch (e) {
        // Validation failure (e.g. a duplicate name) writes nothing; show it and leave the
        // editor open. We do NOT auto-retry -- only a genuine new edit (groupPending) does.
        errorEl.textContent = e.message;
        setGroupStatus('', '');
    } finally {
        groupSaving = false;
        if (groupPending) flushGroup();
    }
}

function addFrom(input, list, render, normalize) {
    addValue(list, normalize(input.value), render);
    input.value = '';
    input.focus();
}

// Add one already-normalized value to a draft list (machines or members), re-render its
// chips and save. Shared by the Add button, the Enter key, and an autocomplete pick.
function addValue(list, value, render) {
    if (!value) return;
    if (!list.includes(value)) list.push(value);
    render();
    autoSaveGroup();
}

// ---------------------------------------------------------------- pickers
//
// Both fields are search-as-you-type comboboxes (autocomplete.js). The machine picker
// filters the already-loaded fleet client-side across name + the three identifiers; the
// member picker queries the Registered Users directory server-side. Both still accept
// free text on Enter/Add -- a machine can be scoped before it enrolls, and a member added
// by an email that has never signed in -- so the dropdown assists without gating.

function machineSublabel(row) {
    // Show whichever identifiers the row has, so a search hit on serial/asset/service is
    // visible in the suggestion rather than looking like a bare name match.
    return [
        row.asset_tag && t('permissions.editor.asset', { value: row.asset_tag }),
        row.serial_number && t('permissions.editor.serial', { value: row.serial_number }),
        row.service_tag && t('permissions.editor.service', { value: row.service_tag }),
    ].filter(Boolean).join('  ·  ');
}

function machineMatches(query) {
    const q = query.toLowerCase();
    const fields = ['machine', 'asset_tag', 'serial_number', 'service_tag'];
    return machineDirectory
        .filter((row) => !draftMachines.includes(row.machine))
        .filter((row) => fields.some((f) => row[f] && String(row[f]).toLowerCase().includes(q)))
        .slice(0, 20)
        .map((row) => ({ value: row.machine, label: row.machine, sublabel: machineSublabel(row) }));
}

function attachPickers() {
    attachAutocomplete(machineInput, {
        minChars: 0,
        emptyText: t('permissions.editor.machine_empty'),
        source: (query) => machineMatches(query),
        onSelect: (item) => {
            addValue(draftMachines, item.value, renderMachineChips);
            machineInput.value = '';
            machineInput.focus();
        },
    });
    attachAutocomplete(memberInput, {
        minChars: 1,
        emptyText: t('permissions.editor.member_empty'),
        source: async (query) => {
            const body = await api(`/api/permissions/directory?q=${encodeURIComponent(query)}`);
            return (body.users || []).map((u) => ({
                value: u.email,
                label: u.full_name || u.email,
                sublabel: u.username ? `${u.username}  ·  ${u.email}` : u.email,
            }));
        },
        onSelect: (item) => {
            addValue(draftMembers, String(item.value).toLowerCase(), renderMemberChips);
            memberInput.value = '';
            memberInput.focus();
        },
    });
    // The OUs the fleet is actually in, filtered client-side -- a short list that only
    // changes on a directory sync, so there is nothing to query per keystroke. Free text
    // still works, so an OU that has no machines in it yet can be scoped ahead of time.
    attachAutocomplete(ouInput, {
        minChars: 0,
        emptyText: t('permissions.editor.ou_empty'),
        source: (query) => {
            const q = query.toLowerCase();
            return fleetOus
                .filter((ou) => !draftOus.includes(ou))
                .filter((ou) => ou.toLowerCase().includes(q))
                .slice(0, 20)
                .map((ou) => ({ value: ou, label: ou }));
        },
        onSelect: (item) => {
            addValue(draftOus, item.value, renderOuChips);
            ouInput.value = '';
            ouInput.focus();
        },
    });
}

// ---------------------------------------------------------------- wiring

document.getElementById('new-group').addEventListener('click', () => openEditor(null));
document.getElementById('group-cancel').addEventListener('click', () => modal.close());

// Auto-save wiring. Text fields debounce; everything else saves on change. Capabilities
// are caught by delegation on their container, so a checkbox added by a future capability
// is covered without extra wiring.
nameInput.addEventListener('input', autoSaveGroupDebounced);
descriptionInput.addEventListener('input', autoSaveGroupDebounced);
capabilityList.addEventListener('change', autoSaveGroup);
modal.querySelectorAll('input[name="scope-mode"]').forEach((radio) => {
    radio.addEventListener('change', () => {
        syncScopeMode();
        renderOuResolved(null);
        renderFolderResolved(null);
        autoSaveGroup();
    });
});
parentSelect.addEventListener('change', () => {
    rerenderCapabilities();
    renderInheritedScope();
    autoSaveGroup();
});
folderSelect.addEventListener('change', () => {
    const id = Number(folderSelect.value);
    folderSelect.value = '';
    if (!id || draftFolders.includes(id)) return;
    draftFolders.push(id);
    renderFolderChips();
    autoSaveGroup();
});

document.getElementById('machine-add').addEventListener('click',
    () => addFrom(machineInput, draftMachines, renderMachineChips, (v) => v.trim()));
document.getElementById('member-add').addEventListener('click',
    () => addFrom(memberInput, draftMembers, renderMemberChips, (v) => v.trim().toLowerCase()));
// OUs keep the case the admin typed -- they are displayed back, and comparison is
// casefolded server-side (directory.ou_contains), so folding here would only make the
// chip disagree with what their AD tools show.
document.getElementById('ou-add').addEventListener('click',
    () => addFrom(ouInput, draftOus, renderOuChips, (v) => v.trim()));
// Lowercased to match the server's normalisation, so the chip shown is the token that
// will actually be compared against a claim rather than whatever case was pasted.
document.getElementById('dirgroup-add').addEventListener('click',
    () => addFrom(dirGroupInput, draftDirGroups, renderDirGroupChips,
                  (v) => v.trim().toLowerCase()));

// Enter inside the chip inputs adds the entry rather than submitting the dialog --
// which would otherwise close the editor and discard everything typed so far.
[[machineInput, () => document.getElementById('machine-add').click()],
 [memberInput, () => document.getElementById('member-add').click()],
 [dirGroupInput, () => document.getElementById('dirgroup-add').click()],
 [ouInput, () => document.getElementById('ou-add').click()]].forEach(([input, add]) => {
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            add();
        }
    });
});

// Clicking the backdrop (the dialog element itself, which covers the viewport) closes
// the editor -- same convention as the favorites dialog.
modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.close();
});

async function init() {
    try {
        const doc = await api('/api/permissions/capabilities');
        capabilities = doc.capabilities || [];
    } catch (e) {
        errorEl.textContent = t('permissions.capabilities_load_failed', { error: e.message });
    }
    // Suggestions only -- a hostname that hasn't reported yet can still be typed in,
    // so a machine can be scoped before it enrolls. /api/machines is itself scope
    // filtered, so a non-superuser admin is only ever offered machines they can see.
    try {
        machineDirectory = await api('/api/machines');
    } catch (e) { /* suggestions are optional; typing still works */ }
    // This session's own directory-group tokens, offered as a starting point in the
    // editor. Optional in the same way the machine list is: without it the field is
    // simply typed into by hand.
    try {
        const me = await api('/api/permissions/me');
        myDirGroups = me.directory_groups_seen || [];
    } catch (e) { /* the hint is optional */ }
    // The OUs the fleet sits in, for the OU scope picker. Absent on a hub with no AD
    // configured, which is the ordinary case -- the field then just takes typed input.
    try {
        fleetOus = (await api('/api/directory/ous')).ous || [];
    } catch (e) { /* no AD, or no permission to read it; typing still works */ }
    // The hub's PC folders, for the folder scope picker. Optional like the OU list: without
    // them the folder mode simply has nothing to offer yet.
    await loadFolders();
    attachPickers();
    await loadGroups();
    // The group editor is a <dialog>, so FleetLive holds this off while it is open.
    window.FleetLive.watch(['permissions', 'users', 'device_groups'], loadGroups);
    window.FleetLive.watch(['pc_tree'], async () => { await loadFolders(); renderAll(); });
}

async function loadFolders() {
    try {
        pcFolders = (await api('/api/pc-tree')).folders || [];
    } catch (e) { pcFolders = []; }
}

init();
