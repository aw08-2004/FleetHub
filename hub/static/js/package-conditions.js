// The condition builder behind a package's If steps and a payload's "only download on some
// machines" (roadmap #5, hub/package_conditions.py).
//
// Its own file because two places in the package editor need the same builder, and the
// editor (packages.js) is long enough already. It exposes one object, window.PackageConditions,
// and keeps the house rules of the page it serves:
//
//  * Built with createElement / textContent, never innerHTML -- a condition's values are
//    model names, registry paths and PowerShell an operator typed.
//  * The vocabulary (variables, their kinds and operators, the probe kinds, the labels) is
//    whatever GET /api/packages sent under `conditions`. A hardcoded variable list here would
//    silently stop offering the one added server-side next month.
//
// A condition is held as a DRAFT while it is edited -- every group a plain
// { mode: 'all'|'any'|'none', items: [...] } -- and converted to and from the server's AST
// (rules.py's { op, nodes } / { var, cmp, value } / { probe, ... }) only on open and on save.
// The draft is what makes "None of these" a first-class choice: the AST spells it
// not(or(...)), which nobody wants to build by nesting a Not around an Any by hand.
//
// Field edits write into the draft WITHOUT re-rendering, exactly as the rest of the editor
// does; only structural changes (add, remove, change a clause's variable) redraw. Re-rendering
// on every keystroke would move focus out of the box being typed in.
(function () {
    'use strict';

    // Literal keys per value, so tests/test_i18n.py's scan can see every one of them.
    const MODE_LABELS = {
        all: () => t('packages.cond.mode_all'),
        any: () => t('packages.cond.mode_any'),
        none: () => t('packages.cond.mode_none'),
    };
    const GROUP_LABELS = {
        hw: () => t('packages.cond.group.hw'),
        os: () => t('packages.cond.group.os'),
        bios: () => t('packages.cond.group.bios'),
        sys: () => t('packages.cond.group.sys'),
        ad: () => t('packages.cond.group.ad'),
        step: () => t('packages.cond.group.step'),
        field: () => t('packages.cond.group.field'),
    };
    const RESULT_GLYPHS = { true: '✓', false: '✕', unknown: '?' };

    let vocab = { variables: [], operators: {}, probes: [], max_depth: 4 };
    let byName = new Map();
    let seq = 0;

    function setVocab(conditions) {
        vocab = conditions || vocab;
        byName = new Map((vocab.variables || []).map((v) => [v.name, v]));
    }

    function node(tag, className, text) {
        const n = document.createElement(tag);
        if (className) n.className = className;
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }

    function option(value, label) {
        const o = document.createElement('option');
        o.value = value;
        o.textContent = label;
        return o;
    }

    function input(value, onInput, placeholder) {
        const box = node('input', 'input');
        box.type = 'text';
        box.value = value === undefined || value === null ? '' : String(value);
        box.autocomplete = 'off';
        box.spellcheck = false;
        if (placeholder) box.placeholder = placeholder;
        box.addEventListener('input', () => onInput(box.value));
        return box;
    }

    // ---------------------------------------------------------------- AST <-> draft

    function blankClause(allowRuntime) {
        const first = (vocab.variables || []).find((v) => allowRuntime || v.source !== 'runtime');
        const name = first ? first.name : 'hw.model';
        return { var: name, cmp: (operatorsFor(name)[0] || '=='), value: '' };
    }

    function blank(allowRuntime) {
        return { mode: 'all', items: [blankClause(allowRuntime)] };
    }

    /** Server AST -> draft group. Anything that is not a group becomes a one-item group. */
    function fromAst(ast) {
        if (!ast) return null;
        if (ast.op === 'and' || ast.op === 'or') {
            return { mode: ast.op === 'and' ? 'all' : 'any', items: (ast.nodes || []).map(fromItem) };
        }
        if (ast.op === 'not') {
            const inner = (ast.nodes || [])[0];
            // not(or(a, b)) is "none of a, b"; not(anything else) is "none of [it]".
            if (inner && inner.op === 'or') {
                return { mode: 'none', items: (inner.nodes || []).map(fromItem) };
            }
            return { mode: 'none', items: inner ? [fromItem(inner)] : [] };
        }
        return { mode: 'all', items: [fromItem(ast)] };
    }

    function fromItem(ast) {
        if (ast && (ast.op === 'and' || ast.op === 'or' || ast.op === 'not')) return fromAst(ast);
        const leaf = { ...ast };
        if (Array.isArray(leaf.value)) leaf.value = leaf.value.join(', ');
        return leaf;
    }

    /** Draft group -> server AST. Values are sent as typed; the server coerces them. */
    function toAst(group) {
        const nodes = (group.items || []).map((item) => (item.items ? toAst(item) : toLeaf(item)));
        if (group.mode === 'none') return { op: 'not', nodes: [{ op: 'or', nodes }] };
        return { op: group.mode === 'any' ? 'or' : 'and', nodes };
    }

    function toLeaf(item) {
        if (item.probe) {
            const out = { probe: item.probe };
            ['path', 'root', 'key', 'name', 'min_version', 'script', 'timeout_seconds']
                .forEach((field) => {
                    const value = typeof item[field] === 'string' ? item[field].trim() : item[field];
                    if (value !== undefined && value !== null && value !== '') out[field] = value;
                });
            if (item.probe === 'registry_value' && item.has_equals) out.equals = item.equals || '';
            return out;
        }
        const out = { var: item.var, cmp: item.cmp };
        if (item.cmp !== 'is_known' && item.cmp !== 'is_unknown') out.value = item.value;
        return out;
    }

    // ---------------------------------------------------------------- vocabulary helpers

    function operatorsFor(name) {
        const entry = byName.get(name);
        return entry ? entry.operators : ['=='];
    }

    function operatorLabel(op) {
        return (vocab.operators || {})[op] || op;
    }

    function variableLabel(name) {
        const entry = byName.get(name);
        return entry ? entry.label : name;
    }

    function probeText(kind) {
        const found = (vocab.probes || []).find((p) => p.name === kind);
        return found ? [found.label, found.description] : [kind, ''];
    }

    // ---------------------------------------------------------------- rendering

    /**
     * Draw `group` into `host`. `opts`:
     *   allowProbes   -- offer the machine checks (off for a payload's download condition)
     *   allowRuntime  -- offer step.last_exit_code (off for a payload's download condition)
     */
    function render(host, group, opts) {
        host.replaceChildren(renderGroup(group, null, 0, opts || {}, () => render(host, group, opts)));
    }

    function renderGroup(group, parent, depth, opts, redraw) {
        const box = node('div', depth ? 'pkg-cond pkg-cond--nested' : 'pkg-cond');

        const head = node('div', 'pkg-cond__head');
        const mode = node('select', 'input pkg-cond__mode');
        ['all', 'any', 'none'].forEach((m) => mode.appendChild(option(m, MODE_LABELS[m]())));
        mode.value = group.mode;
        mode.addEventListener('change', () => { group.mode = mode.value; });
        head.appendChild(mode);

        const tools = node('span', 'pkg-card__actions');
        const addClause = node('button', 'btn pkg-card__btn', t('packages.cond.add_clause'));
        addClause.type = 'button';
        addClause.addEventListener('click', () => {
            group.items.push(blankClause(opts.allowRuntime));
            redraw();
        });
        tools.appendChild(addClause);
        if (depth + 1 < (vocab.max_condition_depth || 4)) {
            const addGroup = node('button', 'btn pkg-card__btn', t('packages.cond.add_group'));
            addGroup.type = 'button';
            addGroup.addEventListener('click', () => {
                group.items.push({ mode: 'any', items: [blankClause(opts.allowRuntime)] });
                redraw();
            });
            tools.appendChild(addGroup);
        }
        if (parent) {
            const remove = node('button', 'btn pkg-card__btn', t('common.delete'));
            remove.type = 'button';
            remove.addEventListener('click', () => {
                parent.items.splice(parent.items.indexOf(group), 1);
                redraw();
            });
            tools.appendChild(remove);
        }
        head.appendChild(tools);
        box.appendChild(head);

        if (!group.items.length) {
            box.appendChild(node('p', 'setting__default', t('packages.cond.empty_group')));
        }
        group.items.forEach((item) => {
            box.appendChild(item.items
                ? renderGroup(item, group, depth + 1, opts, redraw)
                : renderClause(item, group, opts, redraw));
        });
        return box;
    }

    function subjectSelect(item, opts) {
        // One dropdown for "what is being tested": every variable, grouped, then the checks
        // that run on the machine. Picking one or the other is the only thing that changes a
        // clause's shape, so it is the one control that redraws the clause.
        const select = node('select', 'input pkg-cond__subject');
        const groups = new Map();
        (vocab.variables || []).forEach((v) => {
            if (v.source === 'runtime' && !opts.allowRuntime) return;
            if (!groups.has(v.group)) groups.set(v.group, []);
            groups.get(v.group).push(v);
        });
        groups.forEach((vars, group) => {
            const og = document.createElement('optgroup');
            og.label = GROUP_LABELS[group] ? GROUP_LABELS[group]() : group;
            vars.forEach((v) => og.appendChild(option(`var:${v.name}`, v.label)));
            select.appendChild(og);
        });
        if (opts.allowProbes) {
            const og = document.createElement('optgroup');
            og.label = t('packages.cond.group.checks');
            (vocab.probes || []).forEach((p) => og.appendChild(option(`probe:${p.name}`, p.label)));
            select.appendChild(og);
        }
        const current = item.probe ? `probe:${item.probe}` : `var:${item.var}`;
        // A stored clause naming a variable this hub no longer lists (a deleted custom
        // field) stays selectable rather than being rewritten to whatever sorts first.
        if (!Array.from(select.options).some((o) => o.value === current)) {
            select.appendChild(option(current, item.probe || item.var));
        }
        select.value = current;
        return select;
    }

    function renderClause(item, group, opts, redraw) {
        const row = node('div', 'pkg-cond__clause');
        const line = node('div', 'pkg-cond__line');
        const subject = subjectSelect(item, opts);
        subject.addEventListener('change', () => {
            const [kind, name] = subject.value.split(/:(.+)/);
            Object.keys(item).forEach((k) => delete item[k]);
            if (kind === 'probe') {
                item.probe = name;
                if (name === 'registry_value') item.root = 'HKLM';
            } else {
                item.var = name;
                item.cmp = operatorsFor(name)[0];
                item.value = '';
            }
            redraw();
        });
        line.appendChild(subject);

        if (item.probe) {
            row.appendChild(line);
            row.appendChild(node('p', 'setting__default', probeText(item.probe)[1]));
            probeFields(item).forEach((field) => row.appendChild(field));
        } else {
            const operator = node('select', 'input pkg-cond__op');
            operatorsFor(item.var).forEach((op) => operator.appendChild(option(op, operatorLabel(op))));
            if (!operatorsFor(item.var).includes(item.cmp)) item.cmp = operatorsFor(item.var)[0];
            operator.value = item.cmp;
            line.appendChild(operator);

            const valueHost = node('span', 'pkg-cond__value');
            const drawValue = () => valueHost.replaceChildren(...valueControl(item));
            operator.addEventListener('change', () => { item.cmp = operator.value; drawValue(); });
            drawValue();
            line.appendChild(valueHost);
            row.appendChild(line);
        }

        const remove = node('button', 'btn btn--ghost pkg-cond__remove', '×');
        remove.type = 'button';
        remove.title = t('common.delete');
        remove.setAttribute('aria-label', t('common.delete'));
        remove.addEventListener('click', () => {
            group.items.splice(group.items.indexOf(item), 1);
            redraw();
        });
        line.appendChild(remove);
        return row;
    }

    function valueControl(item) {
        if (item.cmp === 'is_known' || item.cmp === 'is_unknown') return [];
        const entry = byName.get(item.var) || {};
        const list = item.cmp === 'in' || item.cmp === 'not_in';
        if (entry.kind === 'bool') {
            const select = node('select', 'input');
            select.appendChild(option('true', t('packages.cond.yes')));
            select.appendChild(option('false', t('packages.cond.no')));
            if (String(item.value) !== 'false') item.value = 'true';
            select.value = String(item.value);
            select.addEventListener('change', () => { item.value = select.value; });
            return [select];
        }
        if (entry.choices && entry.choices.length && !list) {
            const select = node('select', 'input');
            entry.choices.forEach((c) => select.appendChild(option(c, c)));
            if (!entry.choices.includes(String(item.value))) item.value = entry.choices[0];
            select.value = String(item.value);
            select.addEventListener('change', () => { item.value = select.value; });
            return [select];
        }
        const box = input(item.value, (v) => { item.value = v; },
            list ? t('packages.cond.list_placeholder') : placeholderFor(entry));
        return [box];
    }

    function placeholderFor(entry) {
        // Examples in the operator's own terms, which is most of what makes "model contains"
        // obvious: the value is the text Windows reports, not a catalog id.
        const examples = {
            'hw.manufacturer': 'Dell Inc.',
            'hw.model': 'Latitude 5440',
            'hw.model_name': 'ThinkPad X1 Carbon Gen 11',
            'hw.sku': '0C0F',
            'hw.memory_gb': '16',
            'os.version': '10.0.22631',
            'os.build': '22631',
            'os.display_version': '23H2',
            'sys.agent_version': '3.54.0',
            'step.last_exit_code': '3010',
        };
        return examples[entry.name] || '';
    }

    function labelled(labelText, control) {
        const wrap = node('div', 'pkg-cond__field');
        const id = `pkg-cond-${++seq}`;
        control.id = id;
        const label = node('label', 'setting__label', labelText);
        label.htmlFor = id;
        wrap.appendChild(label);
        wrap.appendChild(control);
        return wrap;
    }

    function probeFields(item) {
        if (item.probe === 'file_exists') {
            return [labelled(t('packages.editor.path'),
                input(item.path, (v) => { item.path = v; }, 'C:\\Program Files\\Vendor\\app.exe'))];
        }
        if (item.probe === 'registry_value') {
            const root = node('select', 'input');
            ['HKLM', 'HKCU', 'HKCR', 'HKU'].forEach((r) => root.appendChild(option(r, r)));
            root.value = item.root || 'HKLM';
            item.root = root.value;
            root.addEventListener('change', () => { item.root = root.value; });
            const equals = input(item.equals, (v) => { item.equals = v; });
            equals.disabled = !item.has_equals && item.equals === undefined;
            if (item.equals !== undefined) item.has_equals = true;
            const exact = node('label', 'checkbox');
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.checked = !!item.has_equals;
            box.addEventListener('change', () => {
                item.has_equals = box.checked;
                equals.disabled = !box.checked;
            });
            exact.appendChild(box);
            exact.appendChild(document.createTextNode(' ' + t('packages.editor.require_exact')));
            return [
                labelled(t('packages.editor.root'), root),
                labelled(t('packages.editor.key'),
                    input(item.key, (v) => { item.key = v; }, 'SOFTWARE\\Vendor\\App')),
                labelled(t('packages.editor.value_name'),
                    input(item.name, (v) => { item.name = v; }, 'Version')),
                exact,
                equals,
            ];
        }
        if (item.probe === 'installed_version') {
            return [
                labelled(t('packages.editor.product_name'),
                    input(item.name, (v) => { item.name = v; }, 'Dell Command | Update')),
                labelled(t('packages.editor.min_version'),
                    input(item.min_version, (v) => { item.min_version = v; }, '5.0')),
            ];
        }
        // powershell
        const script = node('textarea', 'input');
        script.rows = 4;
        script.value = item.script || '';
        script.spellcheck = false;
        script.placeholder = "exit ([int](-not (Get-PnpDevice -FriendlyName '*Thunderbolt*' -ErrorAction SilentlyContinue)))";
        script.addEventListener('input', () => { item.script = script.value; });
        return [
            labelled(t('packages.editor.script'), script),
            node('p', 'setting__default', t('packages.cond.powershell_hint')),
            labelled(t('packages.cond.probe_timeout'),
                input(item.timeout_seconds, (v) => { item.timeout_seconds = v; }, '60')),
        ];
    }

    // ---------------------------------------------------------------- preview

    /** "On PC-0412 ... the Then branch runs", with each clause's actual value under it. */
    function renderPreview(host, doc) {
        host.replaceChildren();
        const key = {
            true: () => t('packages.cond.preview.true', { machine: doc.machine }),
            false: () => t('packages.cond.preview.false', { machine: doc.machine }),
            unknown: () => t('packages.cond.preview.unknown', { machine: doc.machine }),
        }[doc.result] || (() => doc.result);
        host.appendChild(node('p', 'pkg-cond__verdict', `${RESULT_GLYPHS[doc.result] || ''} ${key()}`));
        const list = node('ul', 'pkg-cond__explain');
        const walk = (n) => {
            if (n.nodes) { n.nodes.forEach(walk); return; }
            let text;
            if (n.probe) {
                text = `${probeText(n.probe)[0]} — ${t('packages.cond.preview.on_machine')}`;
            } else {
                const actual = n.known ? String(n.actual) : t('packages.cond.preview.no_value');
                const wanted = n.value === undefined ? '' : ` ${Array.isArray(n.value) ? n.value.join(', ') : n.value}`;
                text = `${variableLabel(n.var)} ${operatorLabel(n.cmp)}${wanted} — ${actual}`;
            }
            list.appendChild(node('li', null, `${RESULT_GLYPHS[n.result] || ''} ${text}`));
        };
        walk(doc.explain || {});
        host.appendChild(list);
        host.appendChild(node('p', 'setting__default', t('packages.cond.preview.caveat')));
    }

    window.PackageConditions = { setVocab, blank, fromAst, toAst, render, renderPreview };
}());
