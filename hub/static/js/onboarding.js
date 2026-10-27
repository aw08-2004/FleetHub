// The public onboarding wizard (roadmap #29) -- what somebody at an unenrolled PC sees at
// /onboarding. Three steps: enter the code, run the installer, watch the PC appear.
//
// Loaded without common.js on purpose: that script assumes a signed-in console (the CSRF
// interceptor, the topbar, toasts), and this page has none of it. So `el` is defined here.
//
// **This page never handles a credential.** It calls /api/onboarding/check, which consumes
// nothing, and /api/onboarding/status, which says how far the PC has got. The token is
// fetched by the install script, on the PC, after the agent is installed -- see
// onboarding_web.py. Keep it that way: a token in this page would be a token in the
// browser's memory, history and extensions, on the one machine nobody has vetted yet.
//
// Status words go through a literal branch, never a computed key, because
// tests/test_i18n.py only scans literal t() calls.
(function () {
'use strict';

const POLL_MS = 4000;

const steps = [1, 2, 3].map((n) => document.getElementById(`step-${n}`));
const dots = [1, 2, 3].map((n) => document.getElementById(`step-dot-${n}`));
const form = document.getElementById('code-form');
const input = document.getElementById('code-input');
const codeError = document.getElementById('code-error');
const submit = document.getElementById('code-submit');
const installFor = document.getElementById('install-for');
const downloadCmd = document.getElementById('download-cmd');
const psCommand = document.getElementById('ps-command');
const copyStatus = document.getElementById('copy-status');
const linuxCommand = document.getElementById('linux-command');
const copyLinuxStatus = document.getElementById('copy-linux-status');
const osChoices = Array.from(document.querySelectorAll('input[name="os"]'));
const installPanels = {
    windows: document.getElementById('install-windows'),
    linux: document.getElementById('install-linux'),
};
const waitingLine = document.getElementById('waiting-line');
const doneLine = document.getElementById('done-line');
const fatal = document.getElementById('fatal-error');

let code = null;
let pollTimer = null;

function show(step) {
    steps.forEach((section, i) => { section.hidden = i !== step - 1; });
    dots.forEach((dot, i) => {
        dot.classList.toggle('onboarding-steps__item--active', i === step - 1);
        dot.classList.toggle('onboarding-steps__item--done', i < step - 1);
    });
}

// Which installer step 2 shows (hub 1.169.0). A GUESS from the browser, and only a guess: a
// Linux server usually has no browser, so the person reading this page is often on a Windows
// laptop setting up the box beside it -- which is why the switch exists and the guess only
// picks where it starts. An Android user agent says "Linux" too and is not a Linux machine.
function guessOs() {
    const ua = navigator.userAgent || '';
    return /Linux/.test(ua) && !/Android/.test(ua) ? 'linux' : 'windows';
}

function chooseOs(os) {
    osChoices.forEach((radio) => { radio.checked = radio.value === os; });
    installPanels.windows.hidden = os !== 'windows';
    installPanels.linux.hidden = os !== 'linux';
}

async function copyFrom(textarea, statusEl) {
    try {
        await navigator.clipboard.writeText(textarea.value);
        statusEl.textContent = t('onboarding.wizard.copied');
    } catch {
        // Refused outside a secure context; the text is selectable, so say so.
        textarea.select();
        statusEl.textContent = t('onboarding.wizard.copy_failed');
    }
}

function showError(target, text) {
    target.textContent = text;
    target.hidden = !text;
}

async function checkCode(raw) {
    showError(codeError, '');
    submit.disabled = true;
    try {
        const resp = await fetch('/api/onboarding/check', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: raw }),
        });
        if (resp.status === 429) {
            showError(codeError, t('onboarding.wizard.too_many'));
            return;
        }
        if (!resp.ok) {
            showError(codeError, t('onboarding.wizard.code_invalid'));
            return;
        }
        const body = await resp.json();
        code = body.code;
        installFor.textContent = t('onboarding.wizard.install_for', { label: body.label });
        downloadCmd.href = body.cmd_url;
        downloadCmd.setAttribute('download', `FleetHub-Onboard-${body.code}.cmd`);
        // Built by the hub (onboarding_web.run_command), the very command the .cmd launcher
        // runs, so the two cannot drift. It used to be assembled here, and drifted: it saved
        // the script into the user's TEMP, where a non-elevated process could swap it before
        // it ran elevated.
        psCommand.value = body.command;
        // The Linux one-liner, from the hub for the same reason (onboarding_web.linux_command).
        linuxCommand.value = body.linux_command;
        chooseOs(guessOs());
        try { history.replaceState(null, '', `/onboarding?code=${encodeURIComponent(code)}`); }
        catch { /* cosmetic only */ }
        show(2);
        startPolling();
    } catch {
        // fetch rejects only when the hub could not be reached at all.
        showError(codeError, t('onboarding.wizard.network_error'));
    } finally {
        submit.disabled = false;
    }
}

function startPolling() {
    stopPolling();
    pollTimer = setInterval(poll, POLL_MS);
}

function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
}

async function poll() {
    if (!code) return;
    let body;
    try {
        const resp = await fetch(`/api/onboarding/status/${encodeURIComponent(code)}`);
        if (resp.status === 429) return;          // slow down quietly; the next tick retries
        if (!resp.ok) {
            stopPolling();
            showError(fatal, t('onboarding.wizard.code_invalid'));
            return;
        }
        body = await resp.json();
    } catch {
        return;                                   // a dropped poll is not worth a message
    }
    if (body.state === 'waiting') {
        waitingLine.textContent = t('onboarding.wizard.waiting');
    } else if (body.state === 'enrolled') {
        waitingLine.textContent = t('onboarding.wizard.enrolled', { machine: body.machine });
    } else if (body.state === 'online') {
        stopPolling();
        doneLine.textContent = t('onboarding.wizard.online', { machine: body.machine });
        show(3);
    } else if (body.state === 'expired') {
        stopPolling();
        showError(fatal, t('onboarding.wizard.expired'));
    } else if (body.state === 'revoked') {
        stopPolling();
        showError(fatal, t('onboarding.wizard.revoked'));
    }
}

form.addEventListener('submit', (e) => {
    e.preventDefault();
    const raw = input.value.trim();
    if (raw) void checkCode(raw);
});

input.addEventListener('input', () => {
    // Upper-case as typed, so what is on screen is what the hub compares.
    const pos = input.selectionStart;
    input.value = input.value.toUpperCase();
    try { input.setSelectionRange(pos, pos); } catch { /* not a text input */ }
});

document.getElementById('copy-command').addEventListener('click', () => {
    void copyFrom(psCommand, copyStatus);
});
document.getElementById('copy-linux').addEventListener('click', () => {
    void copyFrom(linuxCommand, copyLinuxStatus);
});
osChoices.forEach((radio) => {
    radio.addEventListener('change', () => { if (radio.checked) chooseOs(radio.value); });
});

const preset = new URLSearchParams(location.search).get('code');
if (preset) {
    input.value = preset.toUpperCase();
    void checkCode(preset);
} else {
    input.focus();
}

})();
