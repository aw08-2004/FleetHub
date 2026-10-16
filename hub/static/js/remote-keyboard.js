// The on-screen keyboard for the remote viewer on a phone (roadmap #2, issue #138).
//
// remote.js takes keys from keydown/keyup on the focused <video>. A desktop has a keyboard
// attached to that, a phone does not: focusing a <video> never brings the system keyboard up,
// so a phone could watch the PC and click on it and never type a single character into it --
// while every test and every desktop browser stayed green.
//
// So a small keyboard button floats over the picture (on the left edge, the owner's choice on
// the issue) and can be dragged anywhere, because wherever it sits it covers some of the
// desktop. A tap focuses a hidden <input>, which is what makes iOS and Android open their own
// keyboard, and what the operator types into it is translated into the {t:'k'} messages the
// agent already understands. A strip of the keys a phone keyboard lacks -- Esc, Tab, Ctrl,
// Alt, Win, the arrows, Del -- sits above it while it is open.
//
// **Typed text goes to the agent as a CHARACTER, not as a key position**: code '' and key 'ä'
// lands in InputInjector's KEYEVENTF_UNICODE fallback, which every agent since remote control
// shipped has had. That is why this is a hub-only change, and it is also the only correct
// choice: sending code 'KeyZ' would type whatever the PC's layout has in that position, so a
// German phone's z would arrive as y on a US PC, and @, umlauts and € would not arrive at all.
// The exception is a letter or digit typed while Ctrl, Alt or Win is held: a Unicode
// injection does not combine with modifiers, so Ctrl then c goes out as KeyC to make Ctrl+C.
//
// Ctrl, Alt and Win are one-shot sticky keys: tap Ctrl, then c. Holding a strip button while
// typing on the system keyboard is not something two thumbs can do.
//
// Rejected: a keyboard drawn in the page. It would be a worse copy of the one the phone has,
// and lose dictation, IME, swipe typing and every accented character the user's own keyboard
// already offers. Also rejected: reading keydown alone. Android keyboards report almost every
// key as 'Unidentified' (keyCode 229) and only the input events say what was typed.
//
// Exposed as window.RemoteKeyboard.create(...), a factory like RemoteTouch. The translation
// is pure and exported on its own (RemoteKeyboard.keys) so it can be checked without a
// browser -- see tests/test_remote_keyboard.py.
(function () {
    'use strict';

    // A press that moves further than this is a drag of the button, not a tap on it.
    const DRAG_SLOP_PX = 8;
    const DEFAULT_POS = { x: 0, y: 0.5 };   // fractions of the free space: left edge, mid-height

    // The strip's keys, by data-kbd-key. `code` is what MapCodeToVk in InputInjector.cs knows.
    const NAMED = {
        Escape: { code: 'Escape', key: 'Escape' },
        Tab: { code: 'Tab', key: 'Tab' },
        Enter: { code: 'Enter', key: 'Enter' },
        Backspace: { code: 'Backspace', key: 'Backspace' },
        Delete: { code: 'Delete', key: 'Delete' },
        ArrowLeft: { code: 'ArrowLeft', key: 'ArrowLeft' },
        ArrowUp: { code: 'ArrowUp', key: 'ArrowUp' },
        ArrowRight: { code: 'ArrowRight', key: 'ArrowRight' },
        ArrowDown: { code: 'ArrowDown', key: 'ArrowDown' },
    };
    const MODIFIERS = {
        Control: { code: 'ControlLeft', key: 'Control' },
        Alt: { code: 'AltLeft', key: 'Alt' },
        Meta: { code: 'MetaLeft', key: 'Meta' },
    };

    // ---- Translation (pure) ----------------------------------------------------------------

    // The unshifted punctuation keys, by the character a US layout prints on them, for a
    // shortcut typed with a sticky modifier held: Ctrl+- zooms out, Ctrl+/ comments a line.
    // Every code here is one MapCodeToVk in InputInjector.cs knows. A SHIFTED character
    // (Ctrl++ is Ctrl+Shift+=) is not here: that needs a Shift the strip does not offer,
    // and it still goes out as a plain character.
    const PUNCT_CODES = {
        '-': 'Minus', '=': 'Equal', '[': 'BracketLeft', ']': 'BracketRight',
        '\\': 'Backslash', ';': 'Semicolon', "'": 'Quote', '`': 'Backquote',
        ',': 'Comma', '.': 'Period', '/': 'Slash', ' ': 'Space',
    };

    /** The key messages (down then up) for one typed character, or [] for one the agent
     *  cannot inject. `withModifier` asks for the key position instead of the character. */
    function charMessages(ch, withModifier) {
        if (ch === '\n' || ch === '\r') return press(NAMED.Enter);
        if (ch === '\t') return press(NAMED.Tab);
        if (withModifier) {
            if (/^[a-zA-Z]$/.test(ch)) return press({ code: 'Key' + ch.toUpperCase(), key: ch });
            if (/^\d$/.test(ch)) return press({ code: 'Digit' + ch, key: ch });
            if (PUNCT_CODES[ch]) return press({ code: PUNCT_CODES[ch], key: ch });
        }
        // The agent injects key[0] only, so a character outside the BMP (an emoji) would
        // arrive as half a surrogate pair -- garbage. Dropped instead.
        if (ch.length !== 1) return [];
        return press({ code: '', key: ch });
    }

    function press(k) {
        return [
            { t: 'k', code: k.code, key: k.key, down: true },
            { t: 'k', code: k.code, key: k.key, down: false },
        ];
    }

    /** Every message for a run of typed text. Array.from splits by code point. */
    function textMessages(text, withModifier) {
        const out = [];
        for (const ch of Array.from(text || '')) out.push(...charMessages(ch, withModifier));
        return out;
    }

    /** The messages for a beforeinput event's inputType, or null when it is not one we turn
     *  into keys (the caller then lets the input event's fallback deal with it). */
    function inputTypeMessages(inputType, data, withModifier) {
        switch (inputType) {
            case 'insertText':
            // A replacement is typed as new text: the box is always empty, so there is no
            // word on the PC side we know how to select and overwrite. An autocorrect would
            // therefore land AFTER the original word, not over it -- autocorrect="off" on
            // the box is what keeps that rare. Owed on an iPhone (ROADMAP.MD, Owed locally).
            case 'insertReplacementText':
            case 'insertFromPaste':
            case 'insertCompositionText':
                return textMessages(data, withModifier);
            case 'insertLineBreak':
            case 'insertParagraph':
                return press(NAMED.Enter);
            case 'deleteContentBackward':
            case 'deleteWordBackward':
                return press(NAMED.Backspace);
            case 'deleteContentForward':
            case 'deleteWordForward':
                return press(NAMED.Delete);
            default:
                return null;
        }
    }

    // ---- Controller --------------------------------------------------------------------------

    /**
     * opts: {
     *   stage, button, sink, strip   -- the elements (the partial's data-remote="kbd*")
     *   send(msg)                    -- remote.js's sendInput
     *   release(msg)                 -- remote.js's sendControl, which View-only does not stop
     *   storageKey                   -- where the button's position is remembered
     * }
     */
    function create(opts) {
        const { stage, button, sink, strip, send } = opts;
        const release = opts.release || send;
        const held = new Set();          // 'Control' | 'Alt' | 'Meta'
        let pos = loadPos();
        let drag = null;
        let enabled = true;
        let shown = false;
        let composing = false;

        // ---- position ----
        function loadPos() {
            try {
                const saved = JSON.parse(window.localStorage.getItem(opts.storageKey) || 'null');
                if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
                    return { x: clamp01(saved.x), y: clamp01(saved.y) };
                }
            } catch (e) { /* private mode, blocked storage, junk -- the default will do */ }
            return { ...DEFAULT_POS };
        }

        function savePos() {
            try { window.localStorage.setItem(opts.storageKey, JSON.stringify(pos)); } catch (e) { /* ok */ }
        }

        // Stored as fractions of the space the button can move in, so it stays on the same
        // side after the stage changes size -- fullscreen, a rotated phone.
        function place() {
            const free = freeSpace();
            button.style.left = `${Math.round(pos.x * free.w)}px`;
            button.style.top = `${Math.round(pos.y * free.h)}px`;
        }

        function freeSpace() {
            return {
                w: Math.max(0, stage.clientWidth - button.offsetWidth),
                h: Math.max(0, stage.clientHeight - button.offsetHeight),
            };
        }

        // ---- modifiers ----
        // A tap on Ctrl/Alt/Win only LATCHES it here; nothing reaches the PC until the next
        // key, which goes out wrapped in the modifier's down and up. Pressing it on the PC at
        // tap time was the first version, and a latch then let go unused -- tapped twice, or
        // the keyboard closed -- sent a lone Win down-up, which opens the Start menu. It also
        // left a window in which a modifier could be stranded down on the PC.
        function setHeld(name, on) {
            if (on === held.has(name)) return;
            if (on) held.add(name); else held.delete(name);
            const el = strip.querySelector(`[data-kbd-mod="${name}"]`);
            if (el) el.setAttribute('aria-pressed', on ? 'true' : 'false');
        }

        function releaseAll() {
            for (const name of Array.from(held)) setHeld(name, false);
        }

        const modMessage = (name, down) => {
            const m = MODIFIERS[name];
            return { t: 'k', code: m.code, key: m.key, down };
        };

        /** Send a run of key messages inside any latched modifiers, then unlatch: one-shot.
         *  The ups go through `release`, not `send`: should View-only be ticked in between,
         *  sendInput drops everything, and a modifier pressed on the PC must still be let go
         *  of -- releasing a key the viewer itself pressed is not input View-only stops. */
        function emit(messages) {
            if (!messages.length) return;
            const mods = Array.from(held);
            for (const name of mods) send(modMessage(name, true));
            for (const m of messages) send(m);
            // Let go innermost first, as fingers would. An index loop, not toReversed(): that
            // is Safari 16+, and an older iPhone is exactly who this module is for.
            for (let i = mods.length - 1; i >= 0; i--) release(modMessage(mods[i], false));
            releaseAll();
        }

        // ---- the hidden input ----
        function onBeforeInput(e) {
            if (composing) return;   // Android composition: the input event settles it
            // A paste, and Safari's autocorrect replacement, carry their text in dataTransfer
            // with data null. Read from data alone, both were swallowed without a trace.
            let data = e.data;
            if (data == null && e.dataTransfer) {
                try { data = e.dataTransfer.getData('text/plain'); } catch (err) { data = null; }
            }
            const messages = inputTypeMessages(e.inputType, data, held.size > 0);
            if (messages === null) return;
            e.preventDefault();
            emit(messages);
            sink.value = '';
        }

        // Fallback for whatever beforeinput did not swallow: a keyboard that does not fire it,
        // or one that composes (GBoard). Everything left in the box was typed; the box is
        // emptied after every event, so there is nothing to diff against but ''.
        function onInput() {
            if (composing) return;
            const text = sink.value;
            sink.value = '';
            if (text) emit(textMessages(text, held.size > 0));
        }

        // An empty box has nothing for Backspace to delete, so no input event fires at all --
        // and Enter on a single-line input fires none either. keydown is the only witness.
        function onKeyDown(e) {
            const named = NAMED[e.key];
            if (named && (e.key === 'Enter' || e.key === 'Backspace' || !sink.value)) {
                e.preventDefault();
                emit(press(named));
            }
        }

        function onFocus() {
            strip.hidden = false;
            button.setAttribute('aria-expanded', 'true');
            liftStrip();
        }

        function onBlur() {
            strip.hidden = true;
            button.setAttribute('aria-expanded', 'false');
            composing = false;
            sink.value = '';
            // A latch outliving the keyboard would turn the operator's first keystroke next
            // time into a shortcut they never saw being armed.
            releaseAll();
        }

        // ---- the button: tap to toggle, drag to move ----
        function onButtonDown(e) {
            if (!enabled) return;
            e.preventDefault();
            e.stopPropagation();
            try { button.setPointerCapture(e.pointerId); } catch (err) { /* gone */ }
            drag = {
                id: e.pointerId, x: e.clientX, y: e.clientY,
                left: button.offsetLeft, top: button.offsetTop, moved: false,
            };
        }

        function onButtonMove(e) {
            if (!drag || e.pointerId !== drag.id) return;
            e.preventDefault();
            e.stopPropagation();
            const dx = e.clientX - drag.x;
            const dy = e.clientY - drag.y;
            if (!drag.moved && Math.hypot(dx, dy) <= DRAG_SLOP_PX) return;
            drag.moved = true;
            const free = freeSpace();
            pos = {
                x: free.w ? clamp01((drag.left + dx) / free.w) : 0,
                y: free.h ? clamp01((drag.top + dy) / free.h) : 0,
            };
            place();
        }

        function onButtonUp(e) {
            if (!drag || e.pointerId !== drag.id) return;
            e.preventDefault();
            e.stopPropagation();
            const d = drag;
            drag = null;
            if (d.moved) { savePos(); return; }
            if (e.type === 'pointercancel') return;
            // focus() synchronously inside the gesture: iOS opens its keyboard for nothing
            // else, and a setTimeout or a promise in between is already too late.
            if (document.activeElement === sink) sink.blur(); else sink.focus();
        }

        // ---- the strip ----
        // pointerdown + preventDefault, not click: a click moves focus to the button first,
        // which blurs the box and closes the phone's keyboard under the operator's thumb.
        function onStripDown(e) {
            const el = e.target.closest ? e.target.closest('[data-kbd-key],[data-kbd-mod]') : null;
            if (!el) return;
            e.preventDefault();
            e.stopPropagation();
            const mod = el.dataset.kbdMod;
            if (mod) { setHeld(mod, !held.has(mod)); return; }
            const named = NAMED[el.dataset.kbdKey];
            if (named) emit(press(named));
        }

        // ---- the strip above the keyboard ----
        // The phone's keyboard covers the bottom of the LAYOUT viewport without resizing it,
        // so a strip pinned to the bottom of a full-height stage would sit underneath it. The
        // visual viewport is what is still visible.
        const vv = window.visualViewport || null;
        function liftStrip() {
            if (!vv || strip.hidden) { strip.style.bottom = ''; return; }
            const rect = stage.getBoundingClientRect();
            const visibleBottom = vv.offsetTop + vv.height;
            const covered = Math.max(0, rect.bottom - visibleBottom);
            strip.style.bottom = covered ? `${Math.round(covered)}px` : '';
        }

        const onCompStart = () => { composing = true; };
        const onCompEnd = () => { composing = false; onInput(); };
        const onResize = () => place();

        button.addEventListener('pointerdown', onButtonDown);
        button.addEventListener('pointermove', onButtonMove);
        button.addEventListener('pointerup', onButtonUp);
        button.addEventListener('pointercancel', onButtonUp);
        // The pointer path does the work; a click (or Enter on a focused button with a
        // keyboard) would toggle it a second time.
        button.addEventListener('click', (e) => e.preventDefault());
        sink.addEventListener('beforeinput', onBeforeInput);
        sink.addEventListener('input', onInput);
        sink.addEventListener('keydown', onKeyDown);
        sink.addEventListener('compositionstart', onCompStart);
        sink.addEventListener('compositionend', onCompEnd);
        sink.addEventListener('focus', onFocus);
        sink.addEventListener('blur', onBlur);
        strip.addEventListener('pointerdown', onStripDown);
        // remote-touch.js cancels every touchmove on the stage so a pinch cannot zoom the
        // page -- and the strip sits on the stage, so on a phone narrower than its ten keys
        // it could not be swiped sideways at all. Kept from bubbling that far, the strip's
        // own touch-action: pan-x scrolls it.
        strip.addEventListener('touchmove', (e) => e.stopPropagation(), { passive: true });
        if (vv) {
            vv.addEventListener('resize', liftStrip);
            vv.addEventListener('scroll', liftStrip);
        }
        const resize = window.ResizeObserver ? new ResizeObserver(onResize) : null;
        if (resize) resize.observe(stage);

        function refreshVisibility() {
            button.hidden = !(shown && enabled);
            if (!button.hidden) place();
            else if (document.activeElement === sink) sink.blur();
        }

        return {
            /** Show the button: a touch has happened, or the device is a phone or tablet. */
            show() { shown = true; refreshVisibility(); },
            /** View-only hides it -- remote.js would drop every key it sent anyway. */
            setEnabled(on) { enabled = !!on; refreshVisibility(); },
            dispose() {
                if (document.activeElement === sink) sink.blur();
                releaseAll();
                if (resize) resize.disconnect();
                if (vv) {
                    vv.removeEventListener('resize', liftStrip);
                    vv.removeEventListener('scroll', liftStrip);
                }
            },
        };
    }

    function clamp01(n) { return Math.min(1, Math.max(0, Number(n) || 0)); }

    window.RemoteKeyboard = {
        create,
        keys: { charMessages, textMessages, inputTypeMessages, PUNCT_CODES },
    };
})();
