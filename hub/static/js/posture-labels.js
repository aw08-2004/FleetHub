// Words for the security posture checks (roadmap #25 D), shared by the machine page's card,
// the device sheet and the Reports page. One copy, because three would drift, and a printed
// sheet that words a check differently from the screen it was printed from reads as two
// different findings.
//
// **Literal keys in switches, never built by concatenation.** tests/test_i18n.py can only
// scan calls whose key is a string literal, and a computed key whose translation was never written would
// ship silently and caption a row with its own key. The hub sends a check id, a status and a
// detail code (hub/posture.py); every one of them is mapped here by name.
//
// Detail parameters that are lists of codes (the firewall profiles) are translated here too;
// everything else in a parameter is text from the machine -- a product name, an account name
// -- and is passed through untouched for the caller to render with textContent.
(function () {
    'use strict';

    function checkTitle(id) {
        switch (id) {
            case 'antivirus': return t('posture.check.antivirus');
            case 'signatures': return t('posture.check.signatures');
            case 'autorun': return t('posture.check.autorun');
            case 'firewall': return t('posture.check.firewall');
            case 'session_lock': return t('posture.check.session_lock');
            case 'default_accounts': return t('posture.check.default_accounts');
            case 'admin_accounts': return t('posture.check.admin_accounts');
            case 'encryption': return t('posture.check.encryption');
            case 'secure_boot': return t('posture.check.secure_boot');
            case 'tpm': return t('posture.check.tpm');
            case 'os_patch': return t('posture.check.os_patch');
            default: return id;
        }
    }

    function statusLabel(status) {
        if (status === 'pass') return t('posture.status.pass');
        if (status === 'fail') return t('posture.status.fail');
        return t('posture.status.unknown');
    }

    function profileName(code) {
        if (code === 'domain') return t('posture.profile.domain');
        if (code === 'private') return t('posture.profile.private');
        if (code === 'public') return t('posture.profile.public');
        return code;
    }

    // A table rather than a switch: one row per detail code in hub/posture.py, each a
    // literal t() call so tests/test_i18n.py still sees every key. A code this page is
    // older than falls back to "not reported" rather than printing its own id.
    const DETAIL_TEXT = {
        read_failed: (p) => t('posture.detail.read_failed', p),
        av_on: (p) => t('posture.detail.av_on', p),
        av_off: (p) => t('posture.detail.av_off', p),
        av_none: (p) => t('posture.detail.av_none', p),
        sig_current: (p) => t('posture.detail.sig_current', p),
        sig_stale: (p) => t('posture.detail.sig_stale', p),
        sig_stale_product: (p) => t('posture.detail.sig_stale_product', p),
        autorun_off: (p) => t('posture.detail.autorun_off', p),
        autorun_on: (p) => t('posture.detail.autorun_on', p),
        fw_on: (p) => t('posture.detail.fw_on', p),
        fw_off: (p) => t('posture.detail.fw_off', p),
        lock_ok: (p) => t('posture.detail.lock_ok', p),
        lock_too_long: (p) => t('posture.detail.lock_too_long', p),
        lock_missing: (p) => t('posture.detail.lock_missing', p),
        lock_no_users: (p) => t('posture.detail.lock_no_users', p),
        defaults_disabled: (p) => t('posture.detail.defaults_disabled', p),
        defaults_enabled: (p) => t('posture.detail.defaults_enabled', p),
        admins_ok: (p) => t('posture.detail.admins_ok', p),
        admins_extra: (p) => t('posture.detail.admins_extra', p),
        enc_all: (p) => t('posture.detail.enc_all', p),
        enc_unprotected: (p) => t('posture.detail.enc_unprotected', p),
        enc_unsupported: (p) => t('posture.detail.enc_unsupported', p),
        enc_not_reported: (p) => t('posture.detail.enc_not_reported', p),
        enc_device: (p) => t('posture.detail.enc_device', p),
        enc_device_off: (p) => t('posture.detail.enc_device_off', p),
        enc_device_unsupported: (p) => t('posture.detail.enc_device_unsupported', p),
        patch_current: (p) => t('posture.detail.patch_current', p),
        patch_stale: (p) => t('posture.detail.patch_stale', p),
        secure_boot_on: (p) => t('posture.detail.secure_boot_on', p),
        secure_boot_off: (p) => t('posture.detail.secure_boot_off', p),
        secure_boot_legacy: (p) => t('posture.detail.secure_boot_legacy', p),
        tpm_ready: (p) => t('posture.detail.tpm_ready', p),
        tpm_missing: (p) => t('posture.detail.tpm_missing', p),
        tpm_not_ready: (p) => t('posture.detail.tpm_not_ready', p),
    };

    function detailText(check) {
        const p = Object.assign({}, check.params || {});
        if (Array.isArray(p.profiles)) p.profiles = p.profiles.map(profileName).join(', ');
        const text = Object.hasOwn(DETAIL_TEXT, check.detail) ? DETAIL_TEXT[check.detail] : null;
        return text ? text(p) : t('posture.detail.not_reported', p);
    }

    window.PostureLabels = { checkTitle, statusLabel, detailText };
}());
