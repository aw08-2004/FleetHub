"""The hub's server process on new installs: cheroot serving `wsgi:application`, over HTTPS
on 29443 by default.

WHY NOT WAITRESS ANY MORE. Waitress has no TLS support at all -- its own documentation sends
TLS to a front proxy -- and since 1.170.0 the hub encrypts the proxy's hop to it as well
(see tls.py for what that protects). cheroot is the pure-Python WSGI server with a built-in
ssl adapter, a fixed thread pool like waitress's, and Windows support, so the hub's
threading model survives the swap unchanged: one process, many threads, never workers.

WHY 29443 AND NOT 3001. IANA assigns 3001 to origo-native (OrigoDB). 29443 sits inside
29170-29998, the largest unassigned block in the registered range with no "unauthorized
use" note against it (checked against the IANA CSV on 2026-10-28), and clear of the Windows
ephemeral range (49152+) and of the TURN relay's 3478/3479 and 49160-49250.

**Existing installs are not moved.** A self-update runs only `pip install -r
requirements.txt` and never rewrites the WinSW service XML, so a hub installed before
1.170.0 keeps launching `waitress-serve --port=3001` -- which is why waitress stays in
requirements.txt. install.ps1 -MigrateHubServer is the deliberate, operator-chosen move,
because it also needs the tunnel/proxy origin changed at the same moment.

Rejected:
  * A bundled Caddy/nginx in front of a loopback-only waitress. A second service to install,
    update and supervise on Windows and a sidecar in Docker, for what one ssl adapter does
    in-process.
  * socketio.run / werkzeug with ssl_context. werkzeug's server is a development server,
    and the hub already learned that a thread pool sized for held requests is not optional
    (install.ps1's service block explains the 128).

Configuration, all read AFTER the app import so the hub's .env has been loaded:
  FLEETHUB_PORT      default 29443
  FLEETHUB_TLS       default on; "0" serves plain HTTP (the Docker image's legacy default,
                     so an existing compose file mapping 3001 keeps working on pull)
  FLEETHUB_TLS_CERT  default <STATE_ROOT>/tls/hub.crt  -- an operator's PEM goes here
  FLEETHUB_TLS_KEY   default <STATE_ROOT>/tls/hub.key
"""
import os
import sys
from urllib.parse import urlparse

DEFAULT_PORT = 29443
# Every open console tab (Socket.IO polling) and every held agent long-poll pins one worker
# thread for its whole wait, so the pool is sized by concurrency, not request rate. Same
# number waitress ran with; install.ps1's service block has the full arithmetic.
THREADS = 128
# waitress's default max_request_body_size, which was -- without anyone choosing it -- the
# only hub-wide cap on a request body (the hub sets no MAX_CONTENT_LENGTH). cheroot's
# default is unlimited, so dropping this would quietly lift the ceiling on every upload
# route that streams request.stream without its own cap.
MAX_BODY_BYTES = 1024 * 1024 * 1024
# Seconds a connection may sit idle mid-request. cheroot's default of 10 would cut off a
# slow agent upload on a poor link; 120 matches waitress's channel_timeout. Held long-polls
# are unaffected -- they wait server-side, not on the socket.
SOCKET_TIMEOUT = 120


def _truthy(value, default):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def server_config(environ, state_root):
    """Pure: the environment and the state dir in, the server's settings out. Kept apart
    from main() so the tests can check every default without binding a socket or importing
    the whole hub."""
    try:
        port = int(environ.get("FLEETHUB_PORT") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    tls_dir = os.path.join(state_root, "tls")
    hub_url_host = urlparse(environ.get("HUB_URL") or "").hostname
    return {
        "host": "0.0.0.0",
        "port": port,
        "tls": _truthy(environ.get("FLEETHUB_TLS"), True),
        "cert": environ.get("FLEETHUB_TLS_CERT") or os.path.join(tls_dir, "hub.crt"),
        "key": environ.get("FLEETHUB_TLS_KEY") or os.path.join(tls_dir, "hub.key"),
        # The names the self-signed certificate covers, beyond localhost/127.0.0.1 which
        # tls.py always adds: this machine's name (what a proxy on another box dials) and
        # the public HUB_URL host (what an operator browsing the origin directly types).
        "hostnames": [n for n in (_machine_name(), hub_url_host) if n],
    }


def key_dir_to_protect(cfg, state_root):
    """The directory whose ACL serve.py may tighten for the private key, or None.

    **Only the hub's own `<STATE_ROOT>/tls`.** protect_directory replaces the inherited ACL
    with SYSTEM + Administrators + the hub's account, which is right for a folder the hub
    created and wrong for one an operator pointed FLEETHUB_TLS_KEY into: a shared C:\\certs\\
    that IIS or a backup agent also reads would silently lose those readers, and nothing in
    the hub's log would explain the other service's failure. An operator's key keeps the
    permissions the operator gave it.
    """
    own = os.path.normcase(os.path.abspath(os.path.join(state_root, "tls")))
    key_dir = os.path.normcase(os.path.dirname(os.path.abspath(cfg["key"])))
    return key_dir if key_dir == own else None


def _machine_name():
    import socket

    try:
        return socket.gethostname()
    except OSError:
        return None


def build_server(application, cfg):
    """A cheroot server for `application` with the hub's pool, timeout and body cap, and
    the ssl adapter attached when cfg["tls"]. Not started."""
    from cheroot import wsgi

    server = wsgi.Server(
        (cfg["host"], cfg["port"]),
        application,
        numthreads=THREADS,
        max=THREADS,
        # The accept backlog. cheroot's 5 is a desktop default; a hub restart has the whole
        # fleet's heartbeats plus every open tab reconnecting inside the same few seconds.
        request_queue_size=128,
        timeout=SOCKET_TIMEOUT,
        server_name="FleetHub",
    )
    server.max_request_body_size = MAX_BODY_BYTES
    if cfg["tls"]:
        from cheroot.ssl.builtin import BuiltinSSLAdapter

        # ssl.create_default_context, which the adapter uses, already refuses anything
        # below TLS 1.2 on every Python the hub supports.
        server.ssl_adapter = BuiltinSSLAdapter(cfg["cert"], cfg["key"])
    return server


def main():
    from wsgi import application  # runs the self-heal and loads .env, as under waitress
    import app as hub_app
    import envfile
    import tls

    cfg = server_config(os.environ, hub_app.STATE_ROOT)
    if cfg["tls"]:
        _, _, created = tls.ensure_certificate(cfg["cert"], cfg["key"], cfg["hostnames"])
        # The private key sits under STATE_ROOT, beneath C:\Program Files on Windows, whose
        # inherited ACL grants every local user read -- the same exposure envfile.protect
        # closes for .env. Fails soft and is silent once correct. Only OUR tls folder, though:
        # see key_dir_to_protect for why an operator's key directory is left alone.
        own_dir = key_dir_to_protect(cfg, hub_app.STATE_ROOT)
        if own_dir:
            note = envfile.protect_directory(own_dir)
            if note:
                print(f"[serve] {note}", flush=True)
        else:
            print(f"[serve] TLS key {cfg['key']} is outside the hub's own tls folder; its "
                  "permissions are left as the operator set them.", flush=True)
        summary = tls.certificate_summary(cfg["cert"]) or {}
        print(f"[serve] TLS certificate {'generated' if created else 'loaded'}: "
              f"{cfg['cert']} (sans={summary.get('sans')}, expires {summary.get('not_after')}, "
              f"self-signed={summary.get('self_signed')})", flush=True)
    scheme = "https" if cfg["tls"] else "http"
    print(f"[serve] FleetHub hub {hub_app.HUB_VERSION} on {scheme}://{cfg['host']}:{cfg['port']}/ "
          f"({THREADS} threads)", flush=True)
    server = build_server(application, cfg)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
