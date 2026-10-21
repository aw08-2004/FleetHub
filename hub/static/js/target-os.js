// The `os` target selector (hub 1.160.0), shared by the three editors that build a target --
// Rules, Watchdogs and Device Groups. One copy, because three would drift: a value added to
// rules.TARGET_OS_VALUES and to two of the three lists would leave one editor unable to show a
// selector the other two saved, and render it as a dropdown stuck on the wrong choice.
//
// **Literal keys in the switch, never built by concatenation.** tests/test_i18n.py can only
// scan calls whose key is a string literal, and a computed key whose translation was never
// written would ship silently and caption an option with its own key.
(function () {
    'use strict';

    // rules.TARGET_OS_VALUES, ordered for a person reading the list rather than as the server
    // declares them: the families a group is usually built from first.
    const CHOICES = ['windows', 'windows_11', 'windows_10', 'windows_server', 'windows_legacy',
                     'linux', 'android'];
    const DEFAULT = 'windows';

    function label(value) {
        switch (value) {
            case 'windows': return t('rules.target_os.windows');
            case 'windows_11': return t('rules.target_os.windows_11');
            case 'windows_10': return t('rules.target_os.windows_10');
            case 'windows_server': return t('rules.target_os.windows_server');
            case 'windows_legacy': return t('rules.target_os.windows_legacy');
            case 'linux': return t('rules.target_os.linux');
            case 'android': return t('rules.target_os.android');
            default: return value;
        }
    }

    // The dropdown for one selector. Writes the default into a selector that has none, so what
    // the editor shows and what it saves cannot differ; `onChange` is the editor's own
    // recount/preview hook.
    function select(selector, onChange) {
        const node = document.createElement('select');
        node.className = 'input';
        for (const value of CHOICES) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = label(value);
            node.appendChild(option);
        }
        if (!selector.os) selector.os = DEFAULT;
        node.value = selector.os;
        node.addEventListener('change', () => {
            selector.os = node.value;
            onChange();
        });
        return node;
    }

    window.TargetOs = { DEFAULT, label, select };
}());
