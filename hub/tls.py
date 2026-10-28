"""The hub's own TLS certificate: generate a self-signed one on first start, or use the
operator's.

WHY THIS EXISTS. Since 1.170.0 a new install serves HTTPS itself on 29443 (serve.py),
instead of plain HTTP on 3001 behind a TLS-terminating tunnel or proxy. The proxy still
sits in front; what changed is that its hop to the hub is encrypted too, so the session
cookie and every agent's enrollment secret no longer cross the LAN in clear text between
nginx/cloudflared and the hub when the two are on different machines.

The silent failure this module prevents is a hub that *believes* it serves TLS but cannot:
a missing key file makes cheroot's BuiltinSSLAdapter raise at bind time, and a service
that crash-loops on boot looks, from the console, exactly like a hub that is down. So the
pair is created when absent, and **an existing pair is never touched** -- it is either the
one we made last time (reuse keeps the fingerprint stable for a proxy that pins it) or one
an operator dropped in on purpose (FLEETHUB_TLS_CERT / FLEETHUB_TLS_KEY), and overwriting
the latter would silently swap a CA-issued certificate for a self-signed one.

Flask-free and process-free: paths and names in, files and a summary out.

Rejected: generating the certificate in install.ps1 with New-SelfSignedCertificate. It
would cover only the Windows service -- the Docker image and a dev run would each need
their own path -- and Windows exports the key as PFX, which cheroot's ssl adapter cannot
load without a conversion step that itself needs openssl on the box.
"""
import datetime
import ipaddress
import os

# 825 days: the longest validity Apple's and Chrome's rules ever accepted for a TLS server
# certificate. Nothing in the hub's path enforces it for a self-signed one today, but a
# proxy configured to *trust* this certificate (instead of skipping verification) is
# exactly the kind of client that may, and a renewal every ~2 years is cheap.
VALIDITY_DAYS = 825


def _san_entries(hostnames):
    """SubjectAlternativeName entries for the given names. IP literals become IPAddress
    entries -- a proxy dialling https://127.0.0.1:29443 matches only an IP SAN, never a
    DNS one spelled the same way."""
    from cryptography import x509

    out, seen = [], set()
    for name in hostnames:
        name = (name or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            out.append(x509.DNSName(name))
    return out


def ensure_certificate(cert_path, key_path, hostnames):
    """Return (cert_path, key_path), generating a self-signed EC P-256 pair if EITHER file
    is missing. Returns a third element, True, when it generated one -- the caller logs it,
    because a regenerated certificate changes the fingerprint a proxy may have pinned.

    **Both files present means hands off**, whatever is in them. A pair that does not match
    fails loudly at bind time, which is the right place for an operator's mistake to show.

    The key is written before the certificate. A crash between the two leaves a key with no
    certificate, which the next start treats as "missing" and replaces -- never a
    certificate whose key is gone, which would look present and fail to load.
    """
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return cert_path, key_path, False

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    names = list(hostnames) + ["localhost", "127.0.0.1"]
    sans = _san_entries(names)
    common = next((n.strip() for n in names if n and n.strip()), "localhost")

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common[:64]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FleetHub hub (self-signed)"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Back-dated an hour so a proxy whose clock runs slightly behind does not reject a
        # certificate that is "not yet valid" in the first minutes after install.
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )

    for path in (cert_path, key_path):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass  # Windows: the directory ACL (envfile.protect_directory) does this job
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path, True


def certificate_summary(cert_path):
    """Subject, SANs, expiry and whether it is self-signed, as a dict -- for the startup log
    line, so an operator reading the service log can tell which certificate is live without
    opening the file. None if the file cannot be read or parsed."""
    try:
        from cryptography import x509

        with open(cert_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
    except Exception:
        return None
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)]
        names += [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    return {
        "subject": cert.subject.rfc4514_string(),
        "sans": names,
        "not_after": cert.not_valid_after_utc.isoformat(),
        "self_signed": cert.issuer == cert.subject,
    }
