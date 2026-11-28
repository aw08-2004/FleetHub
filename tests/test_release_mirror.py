"""The hub's agent release mirror (roadmap #33): release_mirror.py and release_mirror_web.py.

**The silent failure this file exists to catch is a mirror that looks healthy and serves
nothing a PC will accept** -- or, worse, serves something it should not have kept. Nothing on
the hub errors in either case: the PC behind the firewall simply fails its install, or never
updates, and a failed update check is silent by design. So every rule the module docstring
states is asserted here against a fake GitHub:
  * a manifest is kept only if the release key signed it, and its binary only if the hash
    matches the SIGNED value;
  * a transient failure keeps the last good copy, and only a 404 drops a file;
  * PawnIO is kept only under the pinned hash, and that pin equals the installer's;
  * the routes serve exactly the stored bytes, resolve only what the index names, and 404
    everything else -- including the whole mirror when it is switched off.
"""
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))
import capabilities
import channels
import clientrelease
import fleet
import permissions
import release_mirror
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from flask import Flask, session
from permissions_web import create_access
from release_mirror_web import create_release_mirror_blueprint

PASS = 0
FAIL = 0

SUPERUSERS = {"root@x.com"}
CURRENT_USER = "root@x.com"

WIN = capabilities.PLATFORM_WINDOWS
LINUX = capabilities.PLATFORM_LINUX


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


class FakeGitHub:
    """url -> bytes, or an exception class to raise. Counts requests per url."""

    def __init__(self):
        self.files = {}
        self.fail = {}
        self.hits = {}

    def __call__(self, url, dest, max_bytes):
        self.hits[url] = self.hits.get(url, 0) + 1
        if url in self.fail:
            raise self.fail[url](url)
        if url not in self.files:
            raise release_mirror.NotFound(url)
        data = self.files[url]
        if len(data) > max_bytes:
            raise release_mirror.FetchError(f"{url}: too large")
        dest.write(data)


class Release:
    """A test release key and a way to publish signed manifests on the fake GitHub."""

    def __init__(self, gh):
        self.gh = gh
        self.key = Ed25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def verify(self, manifest, sig_hex):
        return clientrelease.verify_signature(manifest, sig_hex, public_key_hex=self.pub)

    def publish(self, repo_path, version, binary, signer=None, sha=None):
        bin_url = f"https://github.com/x/releases/download/v{version}/{os.path.basename(repo_path)}.bin"
        body = json.dumps({"sha256": sha or hashlib.sha256(binary).hexdigest(),
                           "url": bin_url, "version": version}).encode()
        url = release_mirror.RAW_BASE + repo_path
        self.gh.files[url] = body
        self.gh.files[url + ".sig"] = ((signer or self.key).sign(body).hex() + "\n").encode()
        self.gh.files[bin_url] = binary
        return body


def blob_names(mirror_dir):
    return set(os.listdir(release_mirror.blob_dir(mirror_dir)))


def test_pin_and_layout():
    print("\n== Pins and paths agree with the installers ==")
    ps1 = open(os.path.join(ROOT, "agent", "install", "agent-install.ps1"), encoding="utf-8").read()
    m = re.search(r'^\$PawnIoSha256 = "([0-9a-f]{64})"', ps1, re.MULTILINE)
    check("agent-install.ps1 pins PawnIO by sha256", m is not None)
    check("...the same hash the mirror keeps it under",
          m is not None and m.group(1) == release_mirror.PAWNIO_SHA256)
    url = re.search(r'^\$PawnIoUrl\s*=\s*"([^"]+)"', ps1, re.MULTILINE)
    check("...and the same PawnIO url", url is not None and url.group(1) == release_mirror.PAWNIO_URL)
    # The third copy: the agent installs PawnIO itself since 3.55.0, and a pin that drifted from
    # the mirror's would make every such agent refuse the hub's copy without anyone noticing.
    cs = open(os.path.join(ROOT, "agent", "src", "TempMonitorAgent", "Telemetry", "PawnIoInstaller.cs"),
              encoding="utf-8").read()
    cs_sha = re.search(r'const string Sha256 = "([0-9a-f]{64})"', cs)
    cs_url = re.search(r'const string Url = "([^"]+)"', cs)
    check("the agent's PawnIoInstaller pins the same hash",
          cs_sha is not None and cs_sha.group(1) == release_mirror.PAWNIO_SHA256)
    check("...and the same url", cs_url is not None and cs_url.group(1) == release_mirror.PAWNIO_URL)
    check("the mirrored repo paths are where GitHub has them",
          all(os.path.isfile(os.path.join(ROOT, *p.split("/")))
              for p in release_mirror.INSTALLER_FILES))
    for platform in release_mirror.PLATFORMS:
        stable = channels.agent_manifest_path(channels.STABLE, platform)
        check(f"the {platform} stable manifest path exists in the repo",
              os.path.isfile(os.path.join(ROOT, *stable.split("/"))))
        check(f"...and is the url the hub's version watcher reads",
              release_mirror.RAW_BASE + stable == channels.agent_manifest_url(channels.STABLE, platform))
    check("Android is not mirrored (its agent does not update from these manifests)",
          capabilities.PLATFORM_ANDROID not in release_mirror.PLATFORMS)
    wanted = release_mirror.wanted_manifests({WIN: "3.50.0", LINUX: "", "junk": "1.2.3"})
    check("a pinned platform adds its version's manifest",
          channels.pinned_manifest_path(WIN, "3.50.0") in wanted)
    check("...an automatic one adds none, and a malformed pin is ignored",
          not any("/manifests/" in p for p in wanted if p.startswith("agent-linux"))
          and len(release_mirror.wanted_manifests({WIN: "3.50"})) == 4)

    print("\n== The hub's verifier accepts a real production manifest ==")
    with open(os.path.join(ROOT, "agent", "agent.manifest.json"), "rb") as fh:
        body = fh.read()
    with open(os.path.join(ROOT, "agent", "agent.manifest.json.sig"), "r") as fh:
        sig = fh.read()
    check("the committed Windows manifest verifies under the release key",
          clientrelease.verify_signature(body, sig))
    check("...and one changed byte does not", not clientrelease.verify_signature(body + b" ", sig))


def test_sync(work):
    print("\n== A sync against a fake GitHub ==")
    gh = FakeGitHub()
    rel = Release(gh)
    mirror = os.path.join(work, "mirror")
    win_bin = b"MZ" + os.urandom(2048)
    lin_bin = b"\x7fELF" + os.urandom(2048)
    pawnio = b"PAWNIO" + os.urandom(512)
    pin = hashlib.sha256(pawnio).hexdigest()
    stable_win = channels.agent_manifest_path(channels.STABLE, WIN)
    beta_win = channels.agent_manifest_path(channels.BETA, WIN)
    stable_lin = channels.agent_manifest_path(channels.STABLE, LINUX)
    win_body = rel.publish(stable_win, "3.50.0", win_bin)
    rel.publish(stable_lin, "0.5.0", lin_bin)
    for path in release_mirror.INSTALLER_FILES:
        gh.files[release_mirror.RAW_BASE + path] = f"# {path}\r\nline two\r\n".encode()
    gh.files[release_mirror.PAWNIO_URL] = pawnio

    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T1", pawnio_sha256=pin)
    check("a clean sync records no errors", result["errors"] == [])
    check("...and says when", result["synced_at"] == "T1" and result["attempted_at"] == "T1")
    check("...holding both stable manifests, by version",
          result["manifests"].get(stable_win) == "3.50.0"
          and result["manifests"].get(stable_lin) == "0.5.0")
    check("...no beta, because none is published (a 404 is not an error)",
          beta_win not in result["manifests"])
    check("...every installer, and PawnIO",
          result["installers"] == sorted(release_mirror.INSTALLER_FILES) and result["pawnio"])
    served = release_mirror.file_for_path(mirror, stable_win)
    check("the manifest is stored as the EXACT bytes GitHub sent",
          served and open(served, "rb").read() == win_body)
    inst = release_mirror.file_for_path(mirror, release_mirror.INSTALLER_FILES[0])
    check("...and so is an installer, CRLF and all",
          inst and open(inst, "rb").read().count(b"\r\n") == 2)
    bin_path = release_mirror.file_for_sha(mirror, hashlib.sha256(win_bin).hexdigest())
    check("the binary is served by its signed sha256",
          bin_path and open(bin_path, "rb").read() == win_bin)
    check("...PawnIO by its pinned one",
          release_mirror.file_for_sha(mirror, pin) is not None)
    check("a manifest is not reachable as a binary by its own hash",
          release_mirror.file_for_sha(mirror, hashlib.sha256(win_body).hexdigest()) is None)
    check("a path the index does not name resolves to nothing",
          release_mirror.file_for_path(mirror, "agent/../index.json") is None
          and release_mirror.file_for_path(mirror, "../index.json") is None
          and release_mirror.file_for_path(mirror, "hub/app.py") is None)
    check("a malformed hash resolves to nothing", release_mirror.file_for_sha(mirror, "../x") is None)
    # `$` matches before a trailing newline; the pattern is anchored with \Z so this fails.
    check("...including a real hash with a newline on the end",
          release_mirror.file_for_sha(mirror, hashlib.sha256(win_bin).hexdigest() + "\n") is None)

    print("\n== The second sync downloads nothing it already has ==")
    bin_url = json.loads(win_body)["url"]
    before = gh.hits[bin_url]
    release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T2", pawnio_sha256=pin)
    check("the unchanged binary is not fetched again", gh.hits[bin_url] == before)
    check("...nor PawnIO", gh.hits[release_mirror.PAWNIO_URL] == 1)

    print("\n== Refusals keep the last good copy ==")
    rel.publish(stable_win, "3.51.0", win_bin, signer=Ed25519PrivateKey.generate())
    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T3", pawnio_sha256=pin)
    check("a manifest signed by another key is not kept",
          result["manifests"].get(stable_win) == "3.50.0")
    check("...and says so", any("NOT signed" in e for e in result["errors"]))
    check("...and a partial round keeps the last full sync time", result["synced_at"] == "T2"
          and result["attempted_at"] == "T3")

    new_bin = b"MZ" + os.urandom(2048)
    rel.publish(stable_win, "3.52.0", new_bin, sha=hashlib.sha256(b"something else").hexdigest())
    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T4", pawnio_sha256=pin)
    check("a binary that does not match its signed sha256 is not kept, nor its manifest",
          result["manifests"].get(stable_win) == "3.50.0"
          and any("not kept" in e for e in result["errors"]))
    check("...and no partial download is left behind",
          not any(n.startswith(".in-") for n in blob_names(mirror)))

    rel.publish(stable_win, "3.53.0", new_bin)
    gh.fail[release_mirror.RAW_BASE + stable_win] = release_mirror.FetchError
    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T5", pawnio_sha256=pin)
    check("a transient GitHub error keeps the previous manifest AND its binary",
          result["manifests"].get(stable_win) == "3.50.0"
          and release_mirror.file_for_sha(mirror, hashlib.sha256(win_bin).hexdigest()))
    del gh.fail[release_mirror.RAW_BASE + stable_win]

    gh.fail[release_mirror.RAW_BASE + release_mirror.INSTALLER_FILES[0]] = \
        release_mirror.FetchError
    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T6", pawnio_sha256=pin)
    check("...the same for an installer", release_mirror.INSTALLER_FILES[0] in result["installers"])
    gh.fail.clear()

    print("\n== A new release replaces the old, and the old binary goes ==")
    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T7", pawnio_sha256=pin)
    check("the new manifest is served", result["manifests"].get(stable_win) == "3.53.0"
          and result["errors"] == [] and result["synced_at"] == "T7")
    check("the old binary is deleted from disk",
          hashlib.sha256(win_bin).hexdigest() not in blob_names(mirror))
    check("the new one is served",
          release_mirror.file_for_sha(mirror, hashlib.sha256(new_bin).hexdigest()) is not None)

    print("\n== Beta, pins and revocations ==")
    beta_bin = b"MZ" + os.urandom(1024)
    rel.publish(beta_win, "3.54.0", beta_bin)
    pinned = channels.pinned_manifest_path(WIN, "3.50.0")
    rel.publish(pinned, "3.50.0", win_bin)
    result = release_mirror.sync(mirror, {WIN: "3.50.0"}, rel.verify, fetch=gh, now="T8", pawnio_sha256=pin)
    check("a published beta is mirrored", result["manifests"].get(beta_win) == "3.54.0")
    check("a pinned version's manifest and binary are mirrored",
          result["manifests"].get(pinned) == "3.50.0"
          and release_mirror.file_for_sha(mirror, hashlib.sha256(win_bin).hexdigest()))
    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T9", pawnio_sha256=pin)
    check("an unpinned platform drops the pin's files",
          pinned not in result["manifests"]
          and hashlib.sha256(win_bin).hexdigest() not in blob_names(mirror))
    del gh.files[release_mirror.RAW_BASE + beta_win]
    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T10", pawnio_sha256=pin)
    check("a manifest deleted on GitHub (a 404) is dropped -- that is how a release is revoked",
          beta_win not in result["manifests"] and result["errors"] == [])

    print("\n== PawnIO is kept only under its pin ==")
    gh.files[release_mirror.PAWNIO_URL] = b"a different driver"
    other = os.path.join(work, "mirror-2")
    result = release_mirror.sync(other, {}, rel.verify, fetch=gh, now="T1", pawnio_sha256=pin)
    check("upstream bytes that do not match the pin are not kept",
          not result["pawnio"] and any("PawnIO" in e for e in result["errors"]))
    result = release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T11", pawnio_sha256=pin)
    check("...while a mirror that already holds the pinned copy keeps serving it",
          result["pawnio"])

    print("\n== Switching it off ==")
    release_mirror.clear(mirror)
    result = release_mirror.status(mirror, enabled=False)
    check("clear() empties the index and the disk",
          result["manifests"] == {} and not result["pawnio"] and blob_names(mirror) == set())
    check("...and reports itself off", result["enabled"] is False)

    with open(os.path.join(mirror, release_mirror.INDEX_NAME), "w") as fh:
        fh.write("{not json")
    check("a corrupt index reads as an empty mirror, not a crash",
          release_mirror.load_index(mirror)["files"] == {})
    return gh, rel


def fake_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        return view(*a, **k)
    return wrapped


def test_routes(work, db):
    global CURRENT_USER
    print("\n== The routes ==")
    gh = FakeGitHub()
    rel = Release(gh)
    mirror = os.path.join(work, "mirror-web")
    win_bin = b"MZ" + os.urandom(4096)
    stable_win = channels.agent_manifest_path(channels.STABLE, WIN)
    body = rel.publish(stable_win, "3.50.0", win_bin)
    release_mirror.sync(mirror, {}, rel.verify, fetch=gh, now="T1")

    state = {"enabled": True}
    app = Flask(__name__)
    app.secret_key = "test"
    access = create_access(db, SUPERUSERS)
    app.register_blueprint(create_release_mirror_blueprint(
        mirror, fake_login_required, access, enabled=lambda: state["enabled"]))

    @app.before_request
    def _seed_session():
        session["user"] = {"email": CURRENT_USER}

    c = app.test_client()
    r = c.get(f"/api/agent/dist/repo/{stable_win}")
    check("a mirrored manifest is served, unauthenticated", r.status_code == 200)
    check("...as the exact stored bytes, octet-stream",
          r.data == body and r.mimetype == "application/octet-stream")
    check("...and must be revalidated by any cache", r.headers.get("Cache-Control") == "no-cache")
    sig = c.get(f"/api/agent/dist/repo/{stable_win}.sig")
    check("its signature is beside it and verifies",
          sig.status_code == 200 and rel.verify(r.data, sig.data.decode()))
    sha = hashlib.sha256(win_bin).hexdigest()
    b = c.get(f"/api/agent/dist/bin/{sha}")
    check("the binary is served by its hash, cacheable forever",
          b.status_code == 200 and b.data == win_bin
          and "immutable" in (b.headers.get("Cache-Control") or ""))
    check("an unknown path is a 404", c.get("/api/agent/dist/repo/hub/app.py").status_code == 404)
    check("...so is a traversal", c.get("/api/agent/dist/repo/../index.json").status_code == 404
          and c.get("/api/agent/dist/repo/agent/%2e%2e/index.json").status_code == 404)
    check("...an unknown hash", c.get("/api/agent/dist/bin/" + "0" * 64).status_code == 404)
    check("...and a manifest asked for by hash",
          c.get(f"/api/agent/dist/bin/{hashlib.sha256(body).hexdigest()}").status_code == 404)

    state["enabled"] = False
    check("switched off, everything 404s at once",
          c.get(f"/api/agent/dist/repo/{stable_win}").status_code == 404
          and c.get(f"/api/agent/dist/bin/{sha}").status_code == 404)
    state["enabled"] = True

    print("\n== The status card needs manage_onboarding ==")
    CURRENT_USER = "viewer@x.com"
    check("a viewer is refused", c.get("/api/release-mirror/status").status_code == 403)
    CURRENT_USER = "tech@x.com"
    s = c.get("/api/release-mirror/status")
    body = s.get_json() or {}
    check("a technician with the capability reads it",
          s.status_code == 200 and body.get("manifests", {}).get(stable_win) == "3.50.0"
          and body.get("enabled") is True and body.get("bytes", 0) > len(win_bin))

    print("\n== The agent listener serves the public routes ==")
    import agent_listener
    check("both dist routes are on the agent listener",
          agent_listener.is_agent_path(f"/api/agent/dist/repo/{stable_win}")
          and agent_listener.is_agent_path(f"/api/agent/dist/bin/{sha}"))
    check("...and the status route is not", not agent_listener.is_agent_path("/api/release-mirror/status"))


def main():
    work = tempfile.mkdtemp(prefix="fleethub-mirror-")
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        permissions.init_permissions_db(db)
        fleet.init_fleet_db(db)
        permissions.invalidate()
        permissions.create_group(db, name="Viewers", capabilities=[permissions.VIEW],
                                 members=["viewer@x.com"], scope_mode=permissions.SCOPE_ALL)
        permissions.create_group(db, name="Helpdesk",
                                 capabilities=[permissions.VIEW, permissions.MANAGE_ONBOARDING],
                                 members=["tech@x.com"], scope_mode=permissions.SCOPE_ALL)
        test_pin_and_layout()
        test_sync(work)
        test_routes(work, db)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            os.unlink(db)
        except OSError:
            pass
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
