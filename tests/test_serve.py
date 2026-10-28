"""Pins the hub's own listener (serve.py + tls.py, hub 1.170.0): HTTPS on 29443 by default,
with the thread pool and body cap waitress used to give it.

The silent failures this exists to catch:
  * a hub that was asked for TLS and serves plain HTTP anyway -- nothing errors, the proxy
    simply keeps working over http:// and the origin hop is in clear text;
  * a regenerated certificate on every start, or worse an operator's own certificate
    overwritten by a self-signed one, which shows up only as a proxy that suddenly refuses
    the origin;
  * the pool shrinking to cheroot's default of 10 threads, which starves the console the
    moment a few tabs and held agent polls are open (install.ps1's service block has the
    arithmetic);
  * the 1 GB request-body ceiling quietly disappearing, because cheroot's default is
    unlimited and waitress's was the only cap the hub ever had.

Runs a trivial WSGI app, not the hub: what is under test is the server around it.
"""
import http.client
import os
import shutil
import ssl
import sys
import tempfile
import threading

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "hub"))

import serve
import tls

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def _hello(environ, start_response):
    body = environ["wsgi.input"].read() if environ.get("CONTENT_LENGTH") else b""
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"hello:" + str(len(body)).encode() + b":" + environ["wsgi.url_scheme"].encode()]


def _running(cfg):
    """Start a server for _hello on an ephemeral port; returns (server, port)."""
    server = serve.build_server(_hello, dict(cfg, host="127.0.0.1", port=0))
    server.prepare()
    port = server.bind_addr[1]
    threading.Thread(target=server.serve, daemon=True).start()
    return server, port


def _request(port, use_tls, method="GET", body=None):
    if use_tls:
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=10,
                                           context=ssl._create_unverified_context())
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, "/", body=body)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_config_defaults():
    print("\n-- server_config: defaults and overrides --")
    cfg = serve.server_config({}, "/srv/hub")
    check("default port is 29443", cfg["port"] == 29443)
    check("TLS is on by default", cfg["tls"] is True)
    check("certificate defaults under STATE_ROOT/tls",
          cfg["cert"] == os.path.join("/srv/hub", "tls", "hub.crt")
          and cfg["key"] == os.path.join("/srv/hub", "tls", "hub.key"))
    cfg = serve.server_config({"FLEETHUB_PORT": "3001", "FLEETHUB_TLS": "0",
                               "FLEETHUB_TLS_CERT": "/c.pem", "FLEETHUB_TLS_KEY": "/k.pem",
                               "HUB_URL": "https://fleet.example.com"}, "/srv/hub")
    check("FLEETHUB_PORT is honoured", cfg["port"] == 3001)
    check("FLEETHUB_TLS=0 turns TLS off (the Docker legacy default)", cfg["tls"] is False)
    check("operator certificate paths are honoured",
          cfg["cert"] == "/c.pem" and cfg["key"] == "/k.pem")
    check("the HUB_URL host is offered to the certificate",
          "fleet.example.com" in cfg["hostnames"])
    check("an unparsable port falls back to 29443, not a crash",
          serve.server_config({"FLEETHUB_PORT": "x"}, "/s")["port"] == 29443)


def test_certificate(work):
    print("\n-- tls.ensure_certificate: generate once, never overwrite --")
    cert, key = os.path.join(work, "tls", "hub.crt"), os.path.join(work, "tls", "hub.key")
    _, _, created = tls.ensure_certificate(cert, key, ["hubbox", "10.0.0.5"])
    check("a missing pair is generated", created and os.path.isfile(cert) and os.path.isfile(key))
    summary = tls.certificate_summary(cert) or {}
    sans = summary.get("sans") or []
    check("SANs cover the machine name, localhost and loopback",
          "hubbox" in sans and "localhost" in sans and "127.0.0.1" in sans)
    check("an IP name becomes an IP SAN", "10.0.0.5" in sans)
    check("it is reported as self-signed", summary.get("self_signed") is True)
    with open(cert, "rb") as fh:
        before = fh.read()
    _, _, created = tls.ensure_certificate(cert, key, ["other-name"])
    with open(cert, "rb") as fh:
        check("an existing pair is reused, not regenerated", not created and fh.read() == before)

    # An operator's pair, however it looks, is left exactly as it is.
    op_cert, op_key = os.path.join(work, "op.crt"), os.path.join(work, "op.key")
    for path, data in ((op_cert, b"OPERATOR CERT"), (op_key, b"OPERATOR KEY")):
        with open(path, "wb") as fh:
            fh.write(data)
    tls.ensure_certificate(op_cert, op_key, ["hubbox"])
    with open(op_cert, "rb") as fh:
        check("an operator certificate is never overwritten", fh.read() == b"OPERATOR CERT")

    # A key without a certificate is the half-written state; it must be replaced, not kept.
    os.remove(cert)
    _, _, created = tls.ensure_certificate(cert, key, ["hubbox"])
    check("a key with no certificate is treated as missing", created and os.path.isfile(cert))
    return cert, key


def test_server(cert, key):
    print("\n-- build_server: pool, cap, and the scheme actually served --")
    cfg = {"tls": True, "cert": cert, "key": key}
    server, port = _running(cfg)
    try:
        check("the pool is 128 threads, not cheroot's 10",
              server.requests.min == serve.THREADS == 128)
        check("the body cap is waitress's 1 GB, not unlimited",
              server.max_request_body_size == 1024 ** 3)
        check("the socket timeout is 120 s, not cheroot's 10", server.timeout == 120)
        status, body = _request(port, True)
        check("TLS on: an HTTPS request is served over https",
              status == 200 and body.endswith(b":https"))
        try:
            _request(port, False)
            plain_refused = False
        except Exception:
            plain_refused = True
        if not plain_refused:
            status, _ = _request(port, False)
            plain_refused = status >= 400
        check("TLS on: plain HTTP is not served", plain_refused)

        # The cap itself: shrunk on the live server so the test does not send a gigabyte.
        server.max_request_body_size = 1024
        status, _ = _request(port, True, "POST", b"x" * 4096)
        check("a body over the cap is refused with 413", status == 413)
        status, body = _request(port, True, "POST", b"x" * 512)
        check("...and one under it is read whole", status == 200 and body.startswith(b"hello:512:"))
    finally:
        server.stop()

    server, port = _running({"tls": False, "cert": cert, "key": key})
    try:
        status, body = _request(port, False)
        check("TLS off: plain HTTP is served", status == 200 and body.endswith(b":http"))
    finally:
        server.stop()


def main():
    work = tempfile.mkdtemp(prefix="hub-serve-test-")
    try:
        test_config_defaults()
        cert, key = test_certificate(work)
        test_server(cert, key)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
