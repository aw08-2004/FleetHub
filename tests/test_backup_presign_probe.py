"""The S3 destination test must fail when PCs' pre-signed upload links would be refused.

The silent failure this file exists to catch: **"Test connection" passes, and then every PC
file backup fails within seconds with "Upload failed: Error while copying content to a
stream"**, while the hub's own database backup keeps working. It happened against MinIO
AIStor with an access key entered in mixed case. MinIO stores an LDAP user's key in
lowercase and matches the key case-insensitively, so every header-signed request the hub
makes -- including the whole old probe -- succeeded. A pre-signed URL carries the key in
`X-Amz-Credential`, inside the canonical query the signature covers, so MinIO's
recomputation with its lowercase copy never matched and every agent upload got 403
SignatureDoesNotMatch.

The fake server below reproduces exactly that behaviour, rather than stubbing the probe's
helper: header-signed requests are accepted whatever the key's case, and a pre-signed GET
is verified by recomputing the signature with the server's own (lowercase) copy of the key
through backups.sigv4_presign. So the test fails if the probe stops pre-signing, stops
requesting the URL, or stops telling SignatureDoesNotMatch apart from other refusals.
"""
import calendar
import io
import json
import os
import sys
import time
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import backups

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


SERVER_KEY = "fleethub-backup"          # how MinIO AIStor stores the LDAP user's key
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
CONFIG = {"endpoint": "https://minio.example.test:9000", "bucket": "backups",
          "region": "us-east-1", "path_style": True, "prefix": "hub-a"}


class Response:
    def __init__(self, status, body=b""):
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")

    def close(self):
        pass


class FakeMinio:
    """Header-signed requests: key matched case-insensitively. Pre-signed: recomputed."""

    def __init__(self, presign_refusal=None):
        self.objects = {}
        self.presigned_requests = 0
        self.presign_refusal = presign_refusal

    def request(self, method, url, data=None, headers=None, stream=False, timeout=None):
        auth = (headers or {}).get("Authorization") or (headers or {}).get("authorization", "")
        presented = auth.split("Credential=", 1)[1].split("/", 1)[0] if auth else ""
        if presented.lower() != SERVER_KEY:
            return Response(403, b"<Error><Code>InvalidAccessKeyId</Code></Error>")
        path = urlsplit(url).path
        if method == "PUT":
            self.objects[path] = data.read() if hasattr(data, "read") else data
            return Response(200)
        if method == "GET":
            if path not in self.objects:
                return Response(404)
            return Response(200, self.objects[path])
        if method == "DELETE":
            self.objects.pop(path, None)
            return Response(204)
        return Response(405)

    def get(self, url, timeout=None):
        self.presigned_requests += 1
        if self.presign_refusal:
            return Response(403, self.presign_refusal)
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        stamp = q["X-Amz-Date"]
        when = calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ"))
        expected = backups.sigv4_presign(
            "GET", f"{parts.scheme}://{parts.netloc}{parts.path}", q["X-Amz-Credential"].split("/")[2],
            SERVER_KEY, SECRET, int(q["X-Amz-Expires"]), now=when)
        if parse_qs(urlsplit(expected).query)["X-Amz-Signature"][0] != q["X-Amz-Signature"]:
            return Response(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
        return Response(200, self.objects.get(parts.path, b""))


def run_probe(server, access_key, client=None):
    """probe_destination against `server`, with storage and audit stubbed out."""
    backups.requests.request = server.request
    backups.requests.get = server.get
    client = client or backups.S3Destination(
        CONFIG, {"access_key_id": access_key, "secret_access_key": SECRET})
    backups.open_client = lambda db, logs, dest: (client, {"name": "minio", "config": CONFIG})
    backups.fleet.audit = lambda *a, **k: None
    try:
        return backups.probe_destination("unused.db", "unused", "dest-1"), None
    except backups.BackupError as e:
        return None, e


class NoPresignClient:
    """A destination without pre-signed URLs, as WebDAV is."""

    def __init__(self):
        self.objects = {}

    def put(self, key, fileobj, size, sha256_hex):
        self.objects[key] = fileobj.read()

    def open(self, key):
        return Response(200, self.objects[key])

    def delete(self, key):
        self.objects.pop(key, None)


def main():
    print("== mixed-case key against a lowercase-storing server ==")
    server = FakeMinio()
    summary, err = run_probe(server, "FleetHub-Backup")
    check("the probe fails instead of reporting success", summary is None)
    check("the failure is PresignRejected, not a generic error",
          isinstance(err, backups.PresignRejected))
    check("the probe actually requested a pre-signed URL", server.presigned_requests == 1)
    check("the message names the access key's case as the likely cause",
          err is not None and "wrong case" in str(err) and "pre-signed" in str(err))
    check("the probe object is still deleted", server.objects == {})

    print("== the same key in the server's case ==")
    server = FakeMinio()
    summary, err = run_probe(server, SERVER_KEY)
    check("the probe passes", err is None and summary and "successfully" in summary)
    check("the pre-signed GET was made and accepted", server.presigned_requests == 1)
    check("the probe object is deleted", server.objects == {})

    print("== a pre-signed refusal for another reason ==")
    server = FakeMinio(presign_refusal=b"<Error><Code>AccessDenied</Code></Error>")
    summary, err = run_probe(server, SERVER_KEY)
    check("the probe fails", summary is None and isinstance(err, backups.BackupError))
    check("it is not blamed on the key's case",
          not isinstance(err, backups.PresignRejected) and "AccessDenied" in str(err))

    print("== a destination with no pre-signed URLs ==")
    server = FakeMinio()
    summary, err = run_probe(server, SERVER_KEY, client=NoPresignClient())
    check("the probe passes without a pre-signed request",
          err is None and server.presigned_requests == 0)

    print("== the console's translation ==")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with io.open(os.path.join(root, "hub", "locales", "en.json"), encoding="utf-8") as f:
        en = json.load(f)
    check("en.json's presign_rejected says what the server's message says",
          en["backups"]["destinations"]["editor"]["presign_rejected"]
          == backups.PRESIGN_REJECTED_MESSAGE)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


def test_backup_presign_probe():
    main()


if __name__ == "__main__":
    sys.exit(main())
