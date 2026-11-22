"""The sign-in provider configuration -- Google OAuth, generic OIDC and Active Directory
over LDAP -- as editable data.

Historically these were `.env`-only and read once at import, so changing an issuer meant
shell access on the hub host plus a service restart. This module makes them editable from
the console by the break-glass admins, and is the flask-free half of that: read, validate,
and write. `auth_web.py` is the HTTP surface; `app.configure_oauth()` is what re-registers
the Authlib clients so a change takes effect without a restart.

**Why break-glass only, and not `manage_settings`.** This is the perimeter itself. Someone
who can point the hub at an OIDC issuer can point it at one THEY control, assert any email
they like, and sign in as anybody -- so this must not be delegable to the capability that
otherwise means "tune the hub". `ALLOWED_EMAILS` members already hold every capability
over every machine, including code execution as SYSTEM, so for them it is not an
escalation; for everyone else it would be.

**The lockout question, which is the whole risk here.** Every guard below exists because
the thing being edited is the way back in, and a hub with no working provider cannot be
fixed from the console that just broke. Hence: a config leaving zero providers enabled is
refused (`validate`); a partially-filled provider is refused rather than silently ignored;
and the caller is expected to keep the OLD configuration live if re-registration fails.
What this module deliberately does NOT try to prevent is a *valid but wrong* provider --
correct-looking credentials pointing at the wrong tenant. Nothing here can tell that from
a correct one, which is why auth_web.py makes the operator confirm and audits the change.

**Why the Active Directory sign-in has its own LDAP_* keys here, and does not reuse the
`directory.*` settings the computer sync already has.** Those settings are edited by anyone
holding `manage_settings`. The sync only READS computer objects, so a wrong server there
costs a wrong OU column; a sign-in that trusted the same server would let that capability
point the hub at a directory it controls and sign in as a break-glass address. The DC that
decides who you are belongs on this page, behind ALLOWED_EMAILS, like the OIDC issuer.
Both may name the same DC and the same service account -- they are configured twice on
purpose, not by accident.
"""
import os
import re

import envfile

# ---------------------------------------------------------------- the fields
# Each entry: (env var, secret?). `secret` fields are never read back to the console --
# the API reports whether one is set, never what it is, the same rule the TURN secret and
# the AD bind password already follow.
GOOGLE_FIELDS = (
    ("GOOGLE_CLIENT_ID", False),
    ("GOOGLE_CLIENT_SECRET", True),
)
OIDC_FIELDS = (
    ("OIDC_CLIENT_ID", False),
    ("OIDC_CLIENT_SECRET", True),
    ("OIDC_ISSUER", False),
    ("OIDC_METADATA_URL", False),
    ("OIDC_DISPLAY_NAME", False),
    ("OIDC_SCOPES", False),
)
# Active Directory over LDAP (roadmap #4). The service account finds the user and checks the
# login group BEFORE the user's own password is tried -- see ldapauth.py for why that order
# is the point of the design.
LDAP_FIELDS = (
    ("LDAP_SERVER", False),
    ("LDAP_BIND_DN", False),
    ("LDAP_BIND_PASSWORD", True),
    ("LDAP_USER_BASE_DN", False),
    ("LDAP_LOGIN_GROUP_DN", False),
    ("LDAP_DISPLAY_NAME", False),
    ("LDAP_CA_FILE", False),
    ("LDAP_TLS_VERIFY", False),
    ("LDAP_ALLOW_INSECURE", False),
)
ALL_FIELDS = GOOGLE_FIELDS + OIDC_FIELDS + LDAP_FIELDS
FIELD_NAMES = tuple(name for name, _ in ALL_FIELDS)
SECRET_FIELDS = frozenset(name for name, secret in ALL_FIELDS if secret)

DEFAULT_OIDC_DISPLAY_NAME = "SSO"
DEFAULT_OIDC_SCOPES = "openid email profile"
DEFAULT_LDAP_DISPLAY_NAME = "Active Directory"

# The config keys that hold secrets, i.e. the ones the UNCHANGED placeholder applies to.
SECRET_KEYS = ("google_client_secret", "oidc_client_secret", "ldap_bind_password")
# Kept exactly as typed -- see load().
RAW_KEYS = ("ldap_bind_password",)
# The two LDAP switches, which are booleans in the config and "1"/absent in .env.
BOOL_KEYS = ("ldap_tls_verify", "ldap_allow_insecure")
# Every part an Active Directory sign-in needs. Any of them set without the rest is a
# half-filled provider, refused for the same reason a Google id without a secret is.
LDAP_REQUIRED = (
    ("ldap_server", "domain controller"),
    ("ldap_bind_dn", "service account"),
    ("ldap_bind_password", "service account password"),
    ("ldap_user_base_dn", "user search base"),
    ("ldap_login_group_dn", "login group"),
)

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_SCOPES = "openid email profile"

# A value the console sends back to mean "leave the stored secret alone". The editor has to
# render SOMETHING in a password field for a secret that is set, and if that placeholder
# were saved verbatim the client secret would become the literal string below -- breaking
# sign-in in a way whose cause is invisible in every log.
UNCHANGED = "••••••••"


class AuthConfigError(ValueError):
    """A rejected configuration. The message is written for an operator to read in the
    console -- these are all mistakes a human can fix, not internal errors."""


def _clean(value):
    return str(value or "").strip()


_FALSE_WORDS = ("0", "false", "no", "off")
_TRUE_WORDS = ("1", "true", "yes", "on")


def _flag(value, default):
    """A boolean from .env text or from the console's JSON. Anything unrecognised keeps the
    default rather than flipping it, so a typo in LDAP_TLS_VERIFY cannot switch certificate
    checking off."""
    if isinstance(value, bool):
        return value
    text = _clean(value).lower()
    if text in _FALSE_WORDS:
        return False
    if text in _TRUE_WORDS:
        return True
    return default


def load(environ):
    """Read the current provider configuration out of an environment mapping."""
    get = environ.get
    issuer = _clean(get("OIDC_ISSUER")).rstrip("/")
    metadata_url = _clean(get("OIDC_METADATA_URL"))
    # Either form works: an admin is as likely to have the issuer on hand as the full
    # discovery URL, and deriving one from the other here means the rest of the hub only
    # ever deals with a metadata URL.
    if issuer and not metadata_url:
        metadata_url = issuer + "/.well-known/openid-configuration"
    return {
        "google_client_id": _clean(get("GOOGLE_CLIENT_ID")),
        "google_client_secret": _clean(get("GOOGLE_CLIENT_SECRET")),
        "oidc_client_id": _clean(get("OIDC_CLIENT_ID")),
        "oidc_client_secret": _clean(get("OIDC_CLIENT_SECRET")),
        "oidc_issuer": issuer,
        "oidc_metadata_url": metadata_url,
        "oidc_display_name": _clean(get("OIDC_DISPLAY_NAME")) or DEFAULT_OIDC_DISPLAY_NAME,
        "oidc_scopes": _clean(get("OIDC_SCOPES")) or DEFAULT_OIDC_SCOPES,
        "ldap_server": _clean(get("LDAP_SERVER")).rstrip("/"),
        "ldap_bind_dn": _clean(get("LDAP_BIND_DN")),
        # NOT stripped: unlike a generated client secret, a person chose this password, and
        # a leading or trailing space in it is part of it. envfile quotes it on write so it
        # comes back from .env exactly as typed.
        "ldap_bind_password": str(get("LDAP_BIND_PASSWORD") or ""),
        "ldap_user_base_dn": _clean(get("LDAP_USER_BASE_DN")),
        "ldap_login_group_dn": _clean(get("LDAP_LOGIN_GROUP_DN")),
        "ldap_display_name": _clean(get("LDAP_DISPLAY_NAME")) or DEFAULT_LDAP_DISPLAY_NAME,
        "ldap_ca_file": _clean(get("LDAP_CA_FILE")),
        "ldap_tls_verify": _flag(get("LDAP_TLS_VERIFY"), True),
        "ldap_allow_insecure": _flag(get("LDAP_ALLOW_INSECURE"), False),
    }


def load_current():
    """The live configuration, from os.environ -- which `save()` keeps in step with .env.
    One accessor so callers cannot accidentally read a stale snapshot taken at import."""
    import os
    return load(os.environ)


def google_enabled(config):
    return bool(config.get("google_client_id") and config.get("google_client_secret"))


def oidc_enabled(config):
    return bool(config.get("oidc_client_id") and config.get("oidc_client_secret")
                and config.get("oidc_metadata_url"))


def ldap_configured(config):
    """Every required LDAP key is filled in -- which is NOT the same as usable. See
    ldap_enabled."""
    return all(config.get(key) for key, _ in LDAP_REQUIRED)


def ldap_problem(config):
    """Why a fully-filled LDAP config must not be used, or None.

    **The same checks as a console save, applied to whatever was loaded.** `validate` runs
    only when Settings -> Sign-in is saved, but the first provider on a Docker volume is
    written into .env BY HAND (docs/installation.md), and so is any later edit on the host.
    Before this, a hand-written `LDAP_SERVER=ldap://dc1` -- or a bare `dc1` with no scheme --
    booted, showed the form and sent every operator's password in cleartext, while the docs
    said plain LDAP was refused. Found in review before release.
    """
    if not ldap_configured(config):
        return None
    try:
        _validate_ldap(config)
    except AuthConfigError as e:
        return str(e)
    return None


def ldap_enabled(config):
    """Configured AND passing the save-time checks. Everything that decides whether the form
    is shown or a bind is attempted asks this, so an invalid hand-written config is off --
    loudly, at boot (app.py) -- rather than half-on."""
    return ldap_configured(config) and ldap_problem(config) is None


def any_enabled(config):
    return google_enabled(config) or oidc_enabled(config) or ldap_enabled(config)


# ---------------------------------------------------------------- validation
_HTTPS_URL = re.compile(r"^https://[^\s/@]+(/\S*)?$", re.IGNORECASE)


def _check_url(value, label):
    """Issuer/discovery URLs must be https. Not pedantry: the discovery document names the
    token endpoint and the signing keys, so anything that can rewrite it in flight chooses
    who this hub believes you are. An http:// issuer is a downgrade of the entire login."""
    if not _HTTPS_URL.match(value):
        if value.lower().startswith("http://"):
            raise AuthConfigError(
                f"{label} must use https. Over plain http, anything on the network path "
                f"can rewrite the discovery document and choose who this hub believes "
                f"you are.")
        raise AuthConfigError(f"{label} does not look like a URL: {value!r}. "
                              f"Expected something like https://login.example.com.")


def validate(config, *, allow_no_provider=False):
    """Check a fully-resolved config, raising AuthConfigError with an operator-readable
    message. Returns the config unchanged so it can be used inline.

    `allow_no_provider` exists only for tests; the console never passes it. Refusing a
    zero-provider config is the single most important guard here -- it is the difference
    between "you made a mistake" and "nobody can sign in to this hub again without shell
    access on the host".
    """
    # A half-filled provider is refused rather than treated as "off". Silently ignoring a
    # client id whose secret is missing is how someone spends an afternoon wondering why
    # the button they configured never appeared.
    if config.get("google_client_id") and not config.get("google_client_secret"):
        raise AuthConfigError("Google is missing its client secret.")
    if config.get("google_client_secret") and not config.get("google_client_id"):
        raise AuthConfigError("Google is missing its client ID.")

    oidc_parts = [bool(config.get("oidc_client_id")),
                  bool(config.get("oidc_client_secret")),
                  bool(config.get("oidc_metadata_url"))]
    if any(oidc_parts) and not all(oidc_parts):
        missing = []
        if not config.get("oidc_client_id"):
            missing.append("client ID")
        if not config.get("oidc_client_secret"):
            missing.append("client secret")
        if not config.get("oidc_metadata_url"):
            missing.append("issuer URL")
        raise AuthConfigError("The OIDC provider is missing its "
                              + " and ".join(missing) + ".")

    if config.get("oidc_issuer"):
        _check_url(config["oidc_issuer"], "The OIDC issuer")
    if config.get("oidc_metadata_url"):
        _check_url(config["oidc_metadata_url"], "The OIDC discovery URL")

    scopes = (config.get("oidc_scopes") or "").split()
    if oidc_enabled(config) and "openid" not in scopes:
        # Without `openid` the provider need not return an ID token at all, and the whole
        # sign-in silently degrades to a plain OAuth grant with no verified identity.
        raise AuthConfigError(
            "The OIDC scopes must include 'openid' -- without it the provider is not "
            "obliged to return an ID token, and there is no verified identity to sign in.")

    _validate_ldap(config)

    if not allow_no_provider and not any_enabled(config):
        raise AuthConfigError(
            "That would leave no way to sign in to this hub. Configure Google, an OIDC "
            "provider or Active Directory before saving -- a hub with no provider cannot be "
            "fixed from this page, only by editing .env on the server.")
    return config


_LDAP_URL = re.compile(r"^(ldaps?)://[a-z0-9.\-]+(:\d{1,5})?$", re.IGNORECASE)


def _has_dc(dn):
    return any(part.strip().lower().startswith("dc=") for part in dn.split(","))


def _validate_ldap(config):
    """The Active Directory half of `validate`. Every refusal here is about the one thing
    that makes this provider different from the other two: the hub handles the user's
    password itself, so how it reaches the DC is part of the perimeter.

    One helper per thing an admin can get wrong, in the order they bite. Split for that
    reason, not only to satisfy SonarCloud's complexity gate (which flagged the single
    function): each refusal now reads on its own, next to the mistake it names."""
    _check_ldap_complete(config)
    if not ldap_configured(config):
        return
    _check_ldap_server(config)
    _check_ldap_names(config)
    _check_ldap_ca_file(config)


def _check_ldap_complete(config):
    parts = [(label, bool(config.get(key))) for key, label in LDAP_REQUIRED]
    present = [ok for _, ok in parts]
    if any(present) and not all(present):
        missing = [label for label, ok in parts if not ok]
        raise AuthConfigError("Active Directory sign-in is missing its "
                              + " and ".join(missing) + ".")


def _check_ldap_server(config):
    server = config["ldap_server"]
    match = _LDAP_URL.match(server)
    port = int(match.group(2)[1:]) if match and match.group(2) else None
    if not match or (port is not None and not 1 <= port <= 65535):
        raise AuthConfigError(
            f"The domain controller does not look like an LDAP address: {server!r}. "
            f"Expected something like ldaps://dc1.corp.local or ldaps://dc1.corp.local:636.")
    if match.group(1).lower() == "ldap" and not config.get("ldap_allow_insecure"):
        # A simple bind over plain LDAP sends the password in cleartext -- and on this path it
        # is not one service account's password but every operator's, on every sign-in.
        raise AuthConfigError(
            "Active Directory sign-in over plain ldap:// would send every operator's "
            "password across the network in cleartext. Use ldaps:// (port 636), or allow "
            "insecure LDAP explicitly if this is an isolated lab.")


def _check_ldap_names(config):
    if not _has_dc(config["ldap_user_base_dn"]):
        raise AuthConfigError(
            "The user search base must be a distinguished name such as "
            "OU=Staff,DC=corp,DC=local or DC=corp,DC=local.")
    group = config["ldap_login_group_dn"]
    if not group.lower().startswith("cn=") or not _has_dc(group):
        # The commonest mistake is pasting the group's NAME. A bare name would never match
        # the memberOf test, so every sign-in would be refused with nothing to say why.
        raise AuthConfigError(
            "The login group must be the group's full distinguished name, such as "
            "CN=FleetHub Operators,OU=Groups,DC=corp,DC=local -- not just its name. In "
            "Active Directory Users and Computers it is the group's distinguishedName "
            "attribute (Attribute Editor tab).")
    if not _has_dc(config["ldap_bind_dn"]) and "@" not in config["ldap_bind_dn"]:
        raise AuthConfigError(
            "The service account must be a distinguished name "
            "(CN=svc-fleethub,OU=Service,DC=corp,DC=local) or a user principal name "
            "(svc-fleethub@corp.local).")


def _check_ldap_ca_file(config):
    ca_file = config.get("ldap_ca_file")
    if ca_file and not os.path.isfile(ca_file):
        raise AuthConfigError(
            f"The CA certificate file {ca_file!r} does not exist on the hub. In Docker it "
            f"must be under /state, e.g. /state/ad-ca.pem.")


# ---------------------------------------------------------------- applying
def merge(current, submitted):
    """Fold a console submission onto the current config.

    Two rules, both about not destroying what you cannot see:
      * a key the caller did not send is LEFT ALONE, so a form that renders one provider
        cannot blank the other;
      * a secret sent back as the UNCHANGED placeholder keeps its stored value, which is
        what lets the editor show "a secret is set" without ever reading it out.
    """
    merged = dict(current)
    for key, value in (submitted or {}).items():
        if key in merged:
            _merge_field(merged, key, value)
    _fill_derived(merged, submitted or {})
    return merged


def _merge_field(merged, key, value):
    if key in BOOL_KEYS:
        merged[key] = _flag(value, merged[key])
        return
    text = str(value or "") if key in RAW_KEYS else _clean(value)
    if key in SECRET_KEYS and text.strip() == UNCHANGED:
        return
    merged[key] = text


def _fill_derived(merged, submitted):
    """Re-derive what follows from other fields, and put back defaults a blank field left.

    The discovery URL is re-derived so a caller that changed only the issuer gets a matching
    one, rather than keeping one pointing at the previous tenant."""
    issuer = merged.get("oidc_issuer", "").rstrip("/")
    merged["oidc_issuer"] = issuer
    if issuer and not _clean(submitted.get("oidc_metadata_url")):
        merged["oidc_metadata_url"] = issuer + "/.well-known/openid-configuration"
    merged["ldap_server"] = merged.get("ldap_server", "").rstrip("/")
    for key, default in (("oidc_display_name", DEFAULT_OIDC_DISPLAY_NAME),
                         ("oidc_scopes", DEFAULT_OIDC_SCOPES),
                         ("ldap_display_name", DEFAULT_LDAP_DISPLAY_NAME)):
        if not merged.get(key):
            merged[key] = default

# .env key -> config key, for every plain text field. A table rather than one line per key,
# so adding a field is one row here and to_env cannot drift into special-casing one of them.
_TEXT_ENV = (
    ("GOOGLE_CLIENT_ID", "google_client_id"),
    ("GOOGLE_CLIENT_SECRET", "google_client_secret"),
    ("OIDC_CLIENT_ID", "oidc_client_id"),
    ("OIDC_CLIENT_SECRET", "oidc_client_secret"),
    ("OIDC_ISSUER", "oidc_issuer"),
    ("OIDC_METADATA_URL", "oidc_metadata_url"),
    ("OIDC_DISPLAY_NAME", "oidc_display_name"),
    ("OIDC_SCOPES", "oidc_scopes"),
    ("LDAP_SERVER", "ldap_server"),
    ("LDAP_BIND_DN", "ldap_bind_dn"),
    ("LDAP_BIND_PASSWORD", "ldap_bind_password"),
    ("LDAP_USER_BASE_DN", "ldap_user_base_dn"),
    ("LDAP_LOGIN_GROUP_DN", "ldap_login_group_dn"),
    ("LDAP_DISPLAY_NAME", "ldap_display_name"),
    ("LDAP_CA_FILE", "ldap_ca_file"),
)


def to_env(config):
    """The .env representation of a config. Empty values become None, i.e. the key is
    REMOVED rather than written blank -- so turning a provider off leaves a file that
    honestly says it is not configured."""
    env = {name: config.get(key) or None for name, key in _TEXT_ENV}
    # Written only when they differ from the default, so a .env that never touched them
    # stays free of them -- and an absent LDAP_TLS_VERIFY keeps meaning "verify".
    env["LDAP_TLS_VERIFY"] = None if config.get("ldap_tls_verify", True) else "0"
    env["LDAP_ALLOW_INSECURE"] = "1" if config.get("ldap_allow_insecure") else None
    return env

def save(env_path, config):
    """Write a validated config to .env AND to os.environ, and report which keys moved.

    Both, always: the file is what survives a restart and os.environ is what the running
    process reads, so writing one without the other gives either a change that evaporates
    on restart or one that does nothing until it happens.
    """
    updates = to_env(config)
    changed = envfile.set_vars(env_path, updates)
    envfile.apply_to_environ(updates)
    return changed


def redacted(config):
    """The console-safe view: every field except the secrets, plus a flag per secret
    saying whether one is set. The secrets themselves never leave the server."""
    return {
        "google_client_id": config.get("google_client_id", ""),
        "google_client_secret_set": bool(config.get("google_client_secret")),
        "google_enabled": google_enabled(config),
        "oidc_client_id": config.get("oidc_client_id", ""),
        "oidc_client_secret_set": bool(config.get("oidc_client_secret")),
        "oidc_issuer": config.get("oidc_issuer", ""),
        "oidc_metadata_url": config.get("oidc_metadata_url", ""),
        "oidc_display_name": config.get("oidc_display_name", ""),
        "oidc_scopes": config.get("oidc_scopes", ""),
        "oidc_enabled": oidc_enabled(config),
        "ldap_server": config.get("ldap_server", ""),
        "ldap_bind_dn": config.get("ldap_bind_dn", ""),
        "ldap_bind_password_set": bool(config.get("ldap_bind_password")),
        "ldap_user_base_dn": config.get("ldap_user_base_dn", ""),
        "ldap_login_group_dn": config.get("ldap_login_group_dn", ""),
        "ldap_display_name": config.get("ldap_display_name", ""),
        "ldap_ca_file": config.get("ldap_ca_file", ""),
        "ldap_tls_verify": bool(config.get("ldap_tls_verify", True)),
        "ldap_allow_insecure": bool(config.get("ldap_allow_insecure")),
        "ldap_enabled": ldap_enabled(config),
        "unchanged_placeholder": UNCHANGED,
    }


def describe_changes(before, after):
    """Which fields changed, for the audit trail -- names only, never values.

    A client secret's old and new values in an audit row would put the credential in the
    database, and then in the hub-database backup, which is the exact thing keeping it out
    of the settings table was for.
    """
    changed = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed
