// Sign-in provider editor for the Settings -> Sign-in tab.
//
// Unlike every other settings panel, this one is built entirely here: its values live in
// .env rather than the settings table (they are credentials, and the perimeter itself), so
// there is no schema for settings.js to render.
//
// Three rules the UI follows, each mirroring a rule the server enforces:
//
//   * a secret that is set renders as a placeholder and is sent back unchanged unless the
//     admin actually types a new one. The server treats that placeholder as "leave it
//     alone", so a saved form never blanks a secret nobody looked at.
//   * the Save button refuses a configuration with no provider enabled, before asking the
//     server. Not a substitute for the server check -- it is the one mistake whose
//     consequence is nobody being able to sign in again, so it is worth catching twice.
//   * changing the perimeter asks for confirmation, and says what will happen.
//
// It re-injects on 'settings:rendered' like the other panel scripts, because settings.js
// replaces panel children on load and after every save.
(function () {
    'use strict';

    const PANEL_ID = 'tab-signin';
    const CARD_ID = 'signin-config-card';
    let cached = null;        // the redacted config from GET /api/auth/providers
    let denied = false;       // not a break-glass admin

    function el(tag, props, children) {
        const node = document.createElement(tag);
        if (props) {
            for (const [k, v] of Object.entries(props)) {
                if (k === 'class') node.className = v;
                else if (k === 'text') node.textContent = v;
                else if (k === 'value') node.value = v;
                else node.setAttribute(k, v);
            }
        }
        for (const child of children || []) {
            if (child) node.appendChild(typeof child === 'string'
                ? document.createTextNode(child) : child);
        }
        return node;
    }

    function pill(text, kind) {
        return el('span', { class: 'status-pill status-pill--' + kind }, [
            el('span', { class: 'status-pill__dot' }), text,
        ]);
    }

    function field(id, label, value, help, opts) {
        const options = opts || {};
        const wrap = el('div', { class: 'setting' });
        wrap.appendChild(el('label', { class: 'setting__label', for: id, text: label }));
        const input = el('input', {
            class: 'input', id: id, autocomplete: 'off',
            type: options.password ? 'password' : 'text',
            value: value || '',
        });
        if (options.placeholder) input.placeholder = options.placeholder;
        input.style.width = '100%';
        wrap.appendChild(input);
        if (help) wrap.appendChild(el('p', { class: 'setting__help', text: help }));
        return wrap;
    }

    function checkbox(id, label, isOn, help) {
        const wrap = el('div', { class: 'setting' });
        const input = el('input', { type: 'checkbox', id: id });
        input.checked = !!isOn;
        wrap.appendChild(el('label', { class: 'setting__label', for: id }, [input, ' ', label]));
        if (help) wrap.appendChild(el('p', { class: 'setting__help', text: help }));
        return wrap;
    }

    function checked(id) {
        const node = document.getElementById(id);
        return !!(node && node.checked);
    }

    // Untrimmed: a person chose the AD service password, and spaces at either end of it are
    // part of it. The server keeps it as sent (authconfig.RAW_KEYS).
    function rawVal(id) {
        const node = document.getElementById(id);
        return node ? node.value : '';
    }

    function val(id) {
        const node = document.getElementById(id);
        return node ? node.value.trim() : '';
    }

    async function load() {
        const resp = await fetch('/api/auth/providers');
        if (resp.status === 403) { denied = true; return null; }
        if (!resp.ok) return null;
        return resp.json().catch(() => null);
    }

    async function save(payload) {
        const resp = await fetch('/api/auth/providers', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.error || ('HTTP ' + resp.status));
        return body;
    }

    function deniedCard() {
        const card = el('div', { class: 'card', id: CARD_ID });
        card.appendChild(el('h3', { class: 'perm-subhead',
                                   text: t('settings.signin.providers') }));
        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.denied') }));
        return card;
    }

    function buildCard() {
        const card = el('div', { class: 'card', id: CARD_ID });
        card.appendChild(el('h3', { class: 'perm-subhead',
                                   text: t('settings.signin.providers') }));

        const state = el('div', { class: 'turn-stat' }, [
            el('span', { class: 'turn-stat__label', text: t('settings.signin.enabled_now') }),
            el('span', { class: 'turn-stat__value' }, [
                cached.google_enabled ? pill('Google', 'ok') : null,
                cached.oidc_enabled
                    ? pill(cached.oidc_display_name || 'OIDC', 'ok') : null,
                cached.ldap_enabled
                    ? pill(cached.ldap_display_name || 'Active Directory', 'ok') : null,
                (!cached.google_enabled && !cached.oidc_enabled && !cached.ldap_enabled)
                    ? pill(t('settings.signin.none'), 'danger') : null,
            ]),
        ]);
        card.appendChild(state);

        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.env_note') }));

        // ---- Google
        card.appendChild(el('h3', { class: 'perm-subhead', text: t('settings.signin.google') }));
        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.google_help') }));
        card.appendChild(field('auth-google-id', t('settings.signin.client_id'),
            cached.google_client_id, t('settings.signin.google_id_help')));
        card.appendChild(field('auth-google-secret', t('settings.signin.client_secret'),
            cached.google_client_secret_set ? cached.unchanged_placeholder : '',
            cached.google_client_secret_set ? t('settings.signin.secret_saved')
                                            : t('settings.signin.secret_unsaved'),
            { password: true }));

        // ---- OIDC
        card.appendChild(el('h3', { class: 'perm-subhead', text: t('settings.signin.oidc') }));
        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.oidc_help') }));
        card.appendChild(field('auth-oidc-issuer', t('settings.signin.issuer'),
            cached.oidc_issuer, t('settings.signin.issuer_help')));
        card.appendChild(field('auth-oidc-metadata', t('settings.signin.metadata'),
            cached.oidc_metadata_url, t('settings.signin.metadata_help')));
        card.appendChild(field('auth-oidc-id', t('settings.signin.client_id'),
            cached.oidc_client_id, ''));
        card.appendChild(field('auth-oidc-secret', t('settings.signin.client_secret'),
            cached.oidc_client_secret_set ? cached.unchanged_placeholder : '',
            cached.oidc_client_secret_set ? t('settings.signin.secret_saved')
                                          : t('settings.signin.secret_unsaved'),
            { password: true }));
        card.appendChild(field('auth-oidc-name', t('settings.signin.button_label'),
            cached.oidc_display_name, t('settings.signin.button_label_help')));
        card.appendChild(field('auth-oidc-scopes', t('settings.signin.scopes'),
            cached.oidc_scopes, t('settings.signin.scopes_help')));

        // ---- Active Directory (LDAP). The hub checks the password itself here, so the
        // help text says plainly that OIDC is the better door wherever ADFS or Entra exists.
        card.appendChild(el('h3', { class: 'perm-subhead', text: t('settings.signin.ldap') }));
        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.ldap_help') }));
        card.appendChild(field('auth-ldap-server', t('settings.signin.ldap_server'),
            cached.ldap_server, t('settings.signin.ldap_server_help')));
        card.appendChild(field('auth-ldap-bind-dn', t('settings.signin.ldap_bind_dn'),
            cached.ldap_bind_dn, t('settings.signin.ldap_bind_dn_help')));
        card.appendChild(field('auth-ldap-bind-password', t('settings.signin.ldap_bind_password'),
            cached.ldap_bind_password_set ? cached.unchanged_placeholder : '',
            cached.ldap_bind_password_set ? t('settings.signin.secret_saved')
                                          : t('settings.signin.secret_unsaved'),
            { password: true }));
        card.appendChild(field('auth-ldap-base', t('settings.signin.ldap_user_base'),
            cached.ldap_user_base_dn, t('settings.signin.ldap_user_base_help')));
        card.appendChild(field('auth-ldap-group', t('settings.signin.ldap_group'),
            cached.ldap_login_group_dn, t('settings.signin.ldap_group_help')));
        card.appendChild(field('auth-ldap-name', t('settings.signin.button_label'),
            cached.ldap_display_name, ''));
        card.appendChild(field('auth-ldap-ca', t('settings.signin.ldap_ca_file'),
            cached.ldap_ca_file, t('settings.signin.ldap_ca_file_help')));
        card.appendChild(checkbox('auth-ldap-verify', t('settings.signin.ldap_tls_verify'),
            cached.ldap_tls_verify, t('settings.signin.ldap_tls_verify_help')));
        card.appendChild(checkbox('auth-ldap-insecure', t('settings.signin.ldap_allow_insecure'),
            cached.ldap_allow_insecure, t('settings.signin.ldap_allow_insecure_help')));

        // ---- Save
        const actions = el('div', { class: 'chip-add', style: 'margin-top: var(--space-4);' });
        const button = el('button', { class: 'btn btn--primary', type: 'button',
                                      text: t('settings.signin.save') });
        const note = el('span', { class: 'setting__help' });
        button.addEventListener('click', async () => {
            const payload = {
                google_client_id: val('auth-google-id'),
                google_client_secret: val('auth-google-secret'),
                oidc_client_id: val('auth-oidc-id'),
                oidc_client_secret: val('auth-oidc-secret'),
                oidc_issuer: val('auth-oidc-issuer'),
                oidc_metadata_url: val('auth-oidc-metadata'),
                oidc_display_name: val('auth-oidc-name'),
                oidc_scopes: val('auth-oidc-scopes'),
                ldap_server: val('auth-ldap-server'),
                ldap_bind_dn: val('auth-ldap-bind-dn'),
                ldap_bind_password: rawVal('auth-ldap-bind-password'),
                ldap_user_base_dn: val('auth-ldap-base'),
                ldap_login_group_dn: val('auth-ldap-group'),
                ldap_display_name: val('auth-ldap-name'),
                ldap_ca_file: val('auth-ldap-ca'),
                ldap_tls_verify: checked('auth-ldap-verify'),
                ldap_allow_insecure: checked('auth-ldap-insecure'),
            };
            // Mirrors the server's refusal. Checked here too because this is the one
            // mistake whose consequence is that nobody can sign in to fix it.
            const googleOn = payload.google_client_id && payload.google_client_secret;
            const oidcOn = payload.oidc_client_id && payload.oidc_client_secret
                && (payload.oidc_issuer || payload.oidc_metadata_url);
            const ldapOn = payload.ldap_server && payload.ldap_bind_dn
                && payload.ldap_bind_password && payload.ldap_user_base_dn
                && payload.ldap_login_group_dn;
            if (!googleOn && !oidcOn && !ldapOn) {
                note.className = 'setting__error';
                note.textContent = t('settings.signin.no_provider_left');
                return;
            }
            if (!await confirmDialog({
                title: t('settings.signin.confirm_title'),
                message: t('settings.signin.confirm'),
                confirmLabel: t('settings.signin.confirm_ok'),
            })) return;

            button.disabled = true;
            note.className = 'setting__help';
            note.textContent = t('settings.signin.applying');
            try {
                cached = await save(payload);
                note.textContent = cached.changed && cached.changed.length
                    ? t('settings.signin.saved_changed',
                        { fields: cached.changed.join(', ') })
                    : t('settings.signin.saved_unchanged');
                inject();
                return;
            } catch (e) {
                note.className = 'setting__error';
                note.textContent = e.message;
            } finally {
                button.disabled = false;
            }
        });
        actions.append(button, note);
        card.appendChild(actions);

        if (cached.superusers && cached.superusers.length) {
            card.appendChild(el('p', { class: 'setting__help', text:
                'Break-glass administrators who can change this: '
                + cached.superusers.join(', ') + '. They are set by ALLOWED_EMAILS in .env '
                + 'and can always sign in regardless of permission groups.' }));
        }
        return card;
    }

    function inject() {
        const panel = document.getElementById(PANEL_ID);
        if (!panel) return;
        if (!cached && !denied) return;
        const existing = document.getElementById(CARD_ID);
        if (existing) existing.remove();
        panel.insertBefore(denied ? deniedCard() : buildCard(), panel.firstChild);
    }

    async function refresh() {
        cached = await load();
        inject();
    }

    document.addEventListener('settings:rendered', inject);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', refresh);
    } else {
        refresh();
    }
    window.FleetLive.watch(['settings'], refresh, { minInterval: 5000 });
})();
