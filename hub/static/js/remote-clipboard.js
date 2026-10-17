// Text clipboard sync for the remote viewer (issue #148, roadmap #2). The PC half is the agent's
// ClipboardBridge.cs; this is the browser half, split out of remote.js the way the touch and
// keyboard controllers are, so it can be driven under node by tests/test_remote_clipboard.py.
//
// WHY IT EXISTS. Remote control carried video down and input up, and nothing else -- so text
// copied on the PC could not be pasted on the operator's own machine, nor the other way. Copy
// and paste BETWEEN PROGRAMS ON THE PC always worked: Windows does that, and we only deliver
// the keys. The report read the gap as a bug ("Copy Data does nothing"), which is how a missing
// feature looks from the operator's chair.
//
// TWO DIRECTIONS, DIFFERENT RULES, because the browser treats them differently:
//   * PC -> here: the agent pushes {t:'clip', s} whenever someone copies on the PC. Chromium
//     lets a focused page write the clipboard straight away; Safari and Firefox want a click,
//     so when the write is refused the "Copy from PC" button lights up and the click does it.
//   * here -> PC: Ctrl+V on the picture first reads this computer's clipboard and sends it,
//     THEN lets the V through -- so the paste on the PC is of what the operator copied here.
//     The read can be refused (or wait on a permission prompt), so it is bounded by
//     READ_TIMEOUT_MS and the keys go through regardless: a paste of the PC's own clipboard
//     beats a keystroke that never arrives. "Paste to PC" does the same without a keystroke.
//
// ALWAYS CHUNKED. The hub relay refuses an upload batch holding any message over
// MAX_UP_MESSAGE_BYTES (hub/remote_relay.py) -- the whole batch, keys and all -- and it measures
// the message as Python's json.dumps writes it, which escapes every non-ASCII character to six
// bytes. A paste sent whole would work on WebRTC and vanish on the relay with no error on either
// side. So every paste is split by that measure, and the agent's ClipboardAssembler joins it.
//
// Pasting writes into the user's session, so it goes through the viewer's View-only check like
// any keystroke. Reading what the PC copied does not, and works in View only.
(function () {
    'use strict';

    /** ClipboardBridge.MaxChars: the most text either direction carries, in UTF-16 units. */
    const MAX_CHARS = 65536;
    /** One chunk's encoded size, kept under remote_relay.MAX_UP_MESSAGE_BYTES (4096) with
     *  room for the envelope; the test reads both numbers rather than trusting this comment. */
    const CHUNK_BYTES = 3800;
    /** Room for {"t":"clip","id":...,"i":...,"n":...,"s":""} around the text. */
    const ENVELOPE_BYTES = 120;
    const READ_TIMEOUT_MS = 1500;

    /** What one UTF-16 code unit costs once Python's json.dumps (ensure_ascii) has escaped it.
     *  An overestimate on WebRTC, which sends UTF-8 -- the relay is the stricter path. */
    function unitCost(code) {
        if (code === 0x22 || code === 0x5c) return 2;                      // \" \\
        if (code === 0x08 || code === 0x09 || code === 0x0a
                || code === 0x0c || code === 0x0d) return 2;                // \b \t \n \f \r
        if (code < 0x20 || code > 0x7e) return 6;                           // \u00XX, non-ASCII
        return 1;
    }

    /** Cut to at most `max` code units without splitting a surrogate pair. */
    function truncate(text, max) {
        if (text.length <= max) return text;
        // A code point above U+FFFF starting at the last unit kept is a pair the cut would split.
        const cut = text.codePointAt(max - 1) > 0xffff ? max - 1 : max;
        return text.slice(0, cut);
    }

    /** Split `text` into {t:'clip'} messages, each under CHUNK_BYTES as the relay counts them.
     *  Iterates by code point, so an emoji is never split across two chunks. */
    function chunks(text, id) {
        const budget = CHUNK_BYTES - ENVELOPE_BYTES;
        const parts = [];
        let current = '';
        let cost = 0;
        for (const ch of text) {
            // Outside the BMP, json.dumps writes the two halves of the pair as two escapes.
            const point = ch.codePointAt(0);
            const c = point > 0xffff ? 12 : unitCost(point);
            if (cost + c > budget && current) {
                parts.push(current);
                current = '';
                cost = 0;
            }
            current += ch;
            cost += c;
        }
        if (current || !parts.length) parts.push(current);
        return parts.map((s, i) => ({ t: 'clip', id, i, n: parts.length, s }));
    }

    /**
     * opts.send       (msg) => void -- straight onto the control channel (sendControl); the
     *                 View-only check is opts.viewOnly, made here, because a paste must not
     *                 wait behind the keystrokes it is holding
     * opts.viewOnly   () => bool
     * opts.hint       (text) => void
     * opts.copyButton / opts.pasteButton   the partial's two buttons
     * opts.clipboard  navigator.clipboard, injectable for tests
     */
    function create(opts) {
        const clip = opts.clipboard !== undefined ? opts.clipboard : navigator.clipboard;
        let capable = false;
        let fromPc = null;        // the newest text copied on the PC, until it is on our clipboard
        let lastSynced = null;    // the text both sides last agreed on, so a repeat is not resent
        let nextId = Date.now();

        function render() {
            opts.copyButton.hidden = !capable;
            opts.pasteButton.hidden = !capable;
            opts.copyButton.disabled = fromPc === null;
            opts.copyButton.classList.toggle('btn--primary', fromPc !== null);
            opts.copyButton.classList.toggle('btn--ghost', fromPc === null);
            opts.pasteButton.disabled = opts.viewOnly();
        }

        function setCapable(on) {
            capable = !!on;
            if (!capable) { fromPc = null; lastSynced = null; }
            render();
        }

        async function write(text) {
            if (!clip?.writeText) return false;
            try {
                await clip.writeText(text);
                return true;
            } catch (e) {
                // A refusal is the expected answer in Safari and Firefox without a click, not an
                // error: the caller lights up Copy from PC instead.
                return false;
            }
        }

        /** {t:'clip', s, trunc} from the agent: someone copied on the PC. */
        async function received(msg) {
            if (!capable || typeof msg.s !== 'string' || !msg.s) return;
            const note = msg.trunc ? ' ' + t('machine.remote.clip_truncated') : '';
            lastSynced = msg.s;
            if (await write(msg.s)) {
                fromPc = null;
                opts.hint(t('machine.remote.clip_copied') + note);
            } else {
                fromPc = msg.s;
                opts.hint(t('machine.remote.clip_copy_ready') + note);
            }
            render();
        }

        /** The "Copy from PC" click -- a user gesture, which is what Safari and Firefox want. */
        async function copyClicked() {
            if (fromPc === null) return;
            if (await write(fromPc)) {
                fromPc = null;
                opts.hint(t('machine.remote.clip_copied'));
            } else {
                opts.hint(t('machine.remote.clip_write_denied'));
            }
            render();
        }

        /** Read this computer's clipboard and send it to the PC. Resolves once it is sent, or
         *  once there is nothing to send; never rejects. `quiet` is the Ctrl+V path, which says
         *  nothing when there was nothing new -- that is every paste after the first. */
        async function paste(quiet) {
            if (!capable || opts.viewOnly()) return false;
            let text;
            try { text = clip?.readText ? await clip.readText() : null; }
            catch (e) { text = null; }
            if (text === null) {
                opts.hint(t('machine.remote.clip_read_denied'));
                return false;
            }
            if (!text || (quiet && text === lastSynced)) return false;
            const sent = truncate(text, MAX_CHARS);
            const id = nextId++;
            chunks(sent, id).forEach((message) => opts.send(message));
            lastSynced = text;
            opts.hint(t('machine.remote.clip_sent')
                + (sent.length < text.length ? ' ' + t('machine.remote.clip_truncated') : ''));
            return true;
        }

        /** The Ctrl+V path: resolves when the paste is on its way or READ_TIMEOUT_MS has passed,
         *  whichever is first, so the caller can release the keys it held back. A read still
         *  waiting on a permission prompt goes on and sends when it can -- late, it still
         *  leaves the PC holding the right text for the next Ctrl+V. */
        function pasteBeforeKeys() {
            return Promise.race([
                paste(true),
                new Promise((resolve) => setTimeout(resolve, READ_TIMEOUT_MS)),
            ]);
        }

        opts.copyButton.addEventListener('click', copyClicked);
        opts.pasteButton.addEventListener('click', () => paste(false));
        render();

        return {
            setCapable,
            isCapable: () => capable,
            received,
            pasteBeforeKeys,
            /** The View-only box changed: paste is input, so its button follows the box. */
            refresh: render,
        };
    }

    window.RemoteClipboard = {
        create, chunks, truncate, unitCost, MAX_CHARS, CHUNK_BYTES, ENVELOPE_BYTES,
    };
})();
