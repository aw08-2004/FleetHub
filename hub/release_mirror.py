"""The hub's copy of the agent release files, for PCs that cannot reach GitHub (roadmap #33).

WHY THIS EXISTS. Every agent install and every agent self-update reads GitHub: the signed
manifest from raw.githubusercontent.com, the binary from a GitHub release, and the installer
itself from `main`. A site that blocks the internet by default, and allows it per user, can
reach the hub but not GitHub. Found on 2026-11-26: an onboarding run on such a PC reached the
hub, then died at "Installing the FleetHub agent" with *Unable to connect to the remote
server*. Worse, an agent that does get installed there runs as SYSTEM, which no per-user
allowance covers, so it never updates -- and a failed update check is silent by design.

The fix is this mirror: the hub downloads the files the fleet needs and serves them, and
installers (and, from the next agent MINOR, the agents) try GitHub first and the hub second.

**THE HUB IS A MIRROR, NOT A TRUST ROOT.** Nothing here changes what a PC accepts. A manifest
is installed only if its Ed25519 signature verifies against the key compiled into the agent and
the installers, and a binary only if its SHA-256 equals the SIGNED value. So the source of the
bytes is not a security property: a compromised hub that serves altered files gets them refused
exactly as a compromised GitHub would. That is also why the paths here mirror the repository's
own layout (`agent/agent.manifest.json`, ...) and binaries are addressed by their sha256: a
client swaps one base url for another and changes nothing else, and the hub never names a
download location of its own.

The silent failures this module is written against:
  * **a mirror that holds files the agents would refuse.** It would look healthy and fail at
    the second source too. So the hub applies the agents' own checks before it keeps anything:
    signature first, then the binary's hash against the signed value. An unverifiable file is
    never stored, and the last good copy is kept.
  * **a transient GitHub error emptying the mirror.** A fetch that fails (DNS, timeout, 5xx)
    carries the previous copy of that file over. Only an authoritative 404 -- a beta that was
    never published, a pin that was revoked by deleting its manifest -- drops a file.
  * **bytes rewritten on the way through.** A signature covers exact bytes, and a rewritten line
    ending reads as tampering (.gitattributes pins these `-text` for the same reason). Every file
    is stored and served as the bytes GitHub sent, opened in binary mode, never decoded.

**Two unsigned kinds of file, and why mirroring them adds no trust.** The installers and the
Linux systemd unit are not signed; today a PC fetches them from `main` over TLS. During
onboarding the hub already serves the onboarding script that runs them elevated, so a hub that
could forge the installer could already forge the script that starts it. PawnIO's installer is
not ours at all, so it is pinned by hash (PAWNIO_SHA256) here and in agent-install.ps1, and
the mirror keeps only bytes that match the pin -- the hub can serve that file or nothing.

Storage is content-addressed: `blobs/<sha256>` holds each file once, and `index.json` maps a
repository path to the blob that holds it. The index is replaced atomically, so a request never
sees half a sync, and a blob nothing names is deleted after the index that dropped it is written.

Rejected:
  * Mirroring the whole release history. A PC can only ever install what a channel manifest or
    the platform's pin names, so that is all the hub keeps -- a few hundred MB, not gigabytes.
  * Re-signing on the hub. The offline key must stay offline; a mirror that copies signed
    files needs no key.
  * A per-client rate limit on the download routes. Behind one office NAT every PC shares an
    address, and a new release sends them all within fifteen minutes. The files are the same
    public bytes GitHub serves; bandwidth abuse is the reverse proxy's to limit.
  * Android. Its agent does not self-update from these manifests.

Flask-free, like every model half: the HTTP fetch is injectable, so tests/test_release_mirror.py
runs a whole sync against a fake GitHub.
"""
import hashlib
import json
import os
import re
import tempfile
import time

import capabilities
import channels

#: The prefix every mirrored repository path is fetched from. `main` because channel
#: manifests are only ever read from main (channels.py says why).
RAW_BASE = f"https://raw.githubusercontent.com/{channels.REPO}/main/"

#: Platforms whose agents install and update from these manifests. Android is not one of them.
PLATFORMS = (capabilities.PLATFORM_WINDOWS, capabilities.PLATFORM_LINUX)

#: The unsigned files an install needs from `main`, kept as GitHub serves them. See the module
#: docstring for why serving them from the hub adds no trust during onboarding.
INSTALLER_FILES = (
    "agent/install/agent-install.ps1",
    "agent-linux/install/install.sh",
    "agent-linux/packaging/fleethub-agent.service",
)

#: The PawnIO driver installer that agent-install.ps1 runs for sensor access. Not ours, served
#: from the master branch of another repository, and unverified until this pin existed.
#: **Must equal $PawnIoSha256 in agent/install/agent-install.ps1**;
#: tests/test_release_mirror.py fails if the two drift. When LibreHardwareMonitor ships a new
#: PawnIO, both stop accepting it until someone checks the new file and moves both pins -- a
#: PC then installs without sensors rather than running an unchecked driver installer.
PAWNIO_URL = ("https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/"
              "refs/heads/master/LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe")
PAWNIO_SHA256 = "a3a46226c5e2824f4cdd42be0eecbabfc672c86f7889710f5ab1e6ad385b47a0"

#: Size ceilings, so a misbehaving upstream cannot fill the hub's disk. A manifest is a few
#: hundred bytes; the self-contained Windows agent is about 80 MB today.
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_BINARY_BYTES = 512 * 1024 * 1024

INDEX_NAME = "index.json"
BLOB_DIR = "blobs"

# \A and \Z, not ^ and $: `$` also matches before a trailing newline, so "<64 hex>\n" would
# pass -- and the hash pattern guards a value a route takes from the request path.
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_VERSION = re.compile(r"\A\d+\.\d+\.\d+\Z")


class FetchError(Exception):
    """The file could not be fetched for a reason that says nothing about whether it exists:
    DNS, connect, TLS, timeout, a 5xx. The previous copy is kept."""


class NotFound(Exception):
    """GitHub answered 404: the file does not exist. The mirror drops it."""


# ------------------------------------------------------------------ paths

def blob_dir(mirror_dir):
    return os.path.join(mirror_dir, BLOB_DIR)


def _blob_path(mirror_dir, sha):
    """The blob file for `sha`, which must resolve INSIDE the blob directory or this raises
    ValueError. Every caller has already checked `sha` against _SHA256, so this cannot fire
    today; it is the containment check itself rather than a reason to trust that, because a
    route hands a request value to file_for_sha and the regex is one edit from being wrong."""
    base = os.path.realpath(blob_dir(mirror_dir))
    path = os.path.realpath(os.path.join(base, str(sha)))
    if not path.startswith(base + os.sep):
        raise ValueError(f"blob path outside the mirror: {sha!r}")
    return path


def channel_manifest_paths(platform):
    """Repository paths of both channels' manifests for one platform."""
    return [channels.agent_manifest_path(c, platform) for c in channels.CHANNELS]


def wanted_manifests(pins):
    """Every manifest the fleet can install from: each platform's two channels, plus the
    pinned version's manifest where the platform is pinned. `pins` maps a platform to its
    pinned version, or to None/"" when it updates automatically."""
    paths = []
    for platform in PLATFORMS:
        paths.extend(channel_manifest_paths(platform))
        pin = str((pins or {}).get(platform) or "").strip()
        if _VERSION.match(pin):
            paths.append(channels.pinned_manifest_path(platform, pin))
    return paths


# ------------------------------------------------------------------ index

def _empty_index():
    return {"files": {}, "binaries": {}, "synced_at": None, "attempted_at": None,
            "errors": []}


def load_index(mirror_dir):
    """The current index, or an empty one. Never raises: a corrupt index reads as an empty
    mirror, which the next sync rebuilds -- a 404 from the mirror sends a client back to
    nothing worse than it had before the mirror existed."""
    try:
        with open(os.path.join(mirror_dir, INDEX_NAME), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return _empty_index()
    if not isinstance(data, dict):
        return _empty_index()
    index = _empty_index()
    for key in ("files", "binaries"):
        if isinstance(data.get(key), dict):
            index[key] = {str(k): str(v) for k, v in data[key].items()}
    for key in ("synced_at", "attempted_at"):
        index[key] = data.get(key)
    if isinstance(data.get("errors"), list):
        index["errors"] = [str(e) for e in data["errors"]]
    return index


def _write_index(mirror_dir, index):
    """Atomically: written beside the old one, then renamed over it."""
    fd, tmp = tempfile.mkstemp(dir=mirror_dir, prefix=".index-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(index, sort_keys=True, indent=1).encode("utf-8"))
        os.replace(tmp, os.path.join(mirror_dir, INDEX_NAME))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ lookups for the routes

def file_for_path(mirror_dir, repo_path):
    """The blob that holds `repo_path`, or None. Only paths in the index resolve, so a request
    path is never joined onto the filesystem -- there is nothing to traverse."""
    sha = load_index(mirror_dir)["files"].get(str(repo_path or ""))
    return _existing_blob(mirror_dir, sha)


def file_for_sha(mirror_dir, sha):
    """The blob for a binary addressed by its sha256, or None. Only hashes the index lists as
    binaries (an agent build or the pinned PawnIO) resolve: a manifest or an installer is
    reached by its path, never by its hash, so the two namespaces cannot be confused."""
    sha = str(sha or "").lower()
    if not _SHA256.match(sha):
        return None
    index = load_index(mirror_dir)
    if sha not in set(index["binaries"].values()):
        return None
    return _existing_blob(mirror_dir, sha)


def _existing_blob(mirror_dir, sha):
    if not sha or not _SHA256.match(sha):
        return None
    try:
        path = _blob_path(mirror_dir, sha)
    except ValueError:
        return None
    return path if os.path.isfile(path) else None


def status(mirror_dir, enabled=True):
    """What the console shows: when the mirror last synced, what it holds, and what failed."""
    index = load_index(mirror_dir)
    shas = set(index["files"].values()) | set(index["binaries"].values())
    size = 0
    for sha in shas:
        try:
            size += os.path.getsize(_blob_path(mirror_dir, sha))
        except OSError:
            pass
    versions = {}
    for path, sha in index["files"].items():
        if path.endswith(".json"):
            blob = _existing_blob(mirror_dir, sha)
            try:
                with open(blob, "rb") as f:
                    versions[path] = json.loads(f.read().decode("utf-8")).get("version")
            except (OSError, ValueError, TypeError, AttributeError):
                versions[path] = None
    return {
        "enabled": bool(enabled),
        "synced_at": index["synced_at"],
        "attempted_at": index["attempted_at"],
        "errors": list(index["errors"]),
        "manifests": dict(sorted(versions.items())),
        "installers": sorted(p for p in index["files"] if p in INSTALLER_FILES),
        "pawnio": PAWNIO_SHA256 in index["binaries"].values(),
        "bytes": size,
    }


# ------------------------------------------------------------------ fetching

def http_fetch(url, dest, max_bytes):
    """Download `url` into the open binary file `dest`, at most `max_bytes`. The production
    fetcher; tests pass their own. Raises NotFound on 404 and FetchError on anything else
    that went wrong."""
    import requests
    try:
        with requests.get(url, stream=True, timeout=(15, 120),
                          headers={"User-Agent": "FleetHub-Hub-Mirror"}) as resp:
            if resp.status_code == 404:
                raise NotFound(url)
            if resp.status_code != 200:
                raise FetchError(f"{url}: HTTP {resp.status_code}")
            total = 0
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(f"{url}: larger than {max_bytes} bytes")
                dest.write(chunk)
    except (NotFound, FetchError):
        raise
    except Exception as e:  # requests' whole family: DNS, connect, TLS, read timeout
        raise FetchError(f"{url}: {e}") from e


def _fetch_bytes(fetch, url, max_bytes):
    """Small files, in memory."""
    import io
    buf = io.BytesIO()
    fetch(url, buf, max_bytes)
    return buf.getvalue()


def _store_bytes(mirror_dir, data):
    """Store `data` as a blob; returns its sha256."""
    sha = hashlib.sha256(data).hexdigest()
    path = _blob_path(mirror_dir, sha)
    if not os.path.isfile(path):
        fd, tmp = tempfile.mkstemp(dir=blob_dir(mirror_dir), prefix=".in-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return sha


def _store_download(mirror_dir, fetch, url, want_sha, max_bytes):
    """Download `url` and keep it as blob `want_sha` only if its hash IS `want_sha`. A blob
    already held is not downloaded again -- content addressing makes "already have it" exact.
    Raises FetchError (or NotFound) when it cannot, naming the mismatch."""
    if _existing_blob(mirror_dir, want_sha):
        return
    fd, tmp = tempfile.mkstemp(dir=blob_dir(mirror_dir), prefix=".in-")
    try:
        with os.fdopen(fd, "wb") as f:
            fetch(url, f, max_bytes)
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        got = h.hexdigest()
        if got != want_sha:
            raise FetchError(f"{url}: sha256 {got}, expected {want_sha} -- not kept")
        os.replace(tmp, _blob_path(mirror_dir, want_sha))
        tmp = None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ------------------------------------------------------------------ the sync

class _Refused(Exception):
    """A file GitHub served that the mirror will not keep. The previous copy is kept."""


def _signed_target(manifest, sig, verify_signature, url):
    """(sha256, binary url) from a manifest the release key signed, or raises _Refused. The
    agents' own order: signature over the exact bytes FIRST, parse only after."""
    try:
        sig_text = sig.decode("ascii")
    except UnicodeDecodeError:
        sig_text = ""
    if not verify_signature(manifest, sig_text):
        raise _Refused(f"{url}: NOT signed by the fleet's release key -- not kept")
    try:
        parsed = json.loads(manifest.decode("utf-8"))
        want = str(parsed.get("sha256") or "").strip().lower()
        bin_url = str(parsed.get("url") or "").strip()
    except (ValueError, AttributeError):   # UnicodeDecodeError is a ValueError
        want, bin_url = "", ""
    if not _SHA256.match(want) or not bin_url.startswith("https://"):
        raise _Refused(f"{url}: signed, but missing its sha256 or url -- a release-signing bug")
    return want, bin_url


class _Round:
    """One sync round: the new index being built, the old one it may carry files over from,
    and the errors that round met. A class only so each kind of file is one short method."""

    def __init__(self, mirror_dir, fetch, verify_signature):
        self.mirror_dir = mirror_dir
        self.fetch = fetch
        self.verify_signature = verify_signature
        self.old = load_index(mirror_dir)
        self.files, self.binaries, self.errors = {}, {}, []

    def carry(self, path):
        """Keep the previous copy of `path` (and its .sig and binary) after a failed fetch."""
        for p in (path, path + ".sig"):
            if p in self.old["files"]:
                self.files[p] = self.old["files"][p]
        if path in self.old["binaries"]:
            self.binaries[path] = self.old["binaries"][path]

    def manifest(self, path):
        url = RAW_BASE + path
        try:
            manifest = _fetch_bytes(self.fetch, url, MAX_MANIFEST_BYTES)
            sig = _fetch_bytes(self.fetch, url + ".sig", MAX_MANIFEST_BYTES)
        except NotFound:
            return   # normal and permanent for most fleets (no beta published): not an error
        except FetchError as e:
            self.errors.append(str(e))
            self.carry(path)
            return
        try:
            want, bin_url = _signed_target(manifest, sig, self.verify_signature, url)
            _store_download(self.mirror_dir, self.fetch, bin_url, want, MAX_BINARY_BYTES)
        except (_Refused, FetchError, NotFound) as e:
            self.errors.append(str(e))
            self.carry(path)
            return
        self.files[path] = _store_bytes(self.mirror_dir, manifest)
        self.files[path + ".sig"] = _store_bytes(self.mirror_dir, sig)
        self.binaries[path] = want

    def installer(self, path):
        url = RAW_BASE + path
        try:
            self.files[path] = _store_bytes(self.mirror_dir,
                                            _fetch_bytes(self.fetch, url, MAX_TEXT_BYTES))
        except NotFound:
            return
        except FetchError as e:
            self.errors.append(str(e))
            if path in self.old["files"]:
                self.files[path] = self.old["files"][path]

    def pawnio(self):
        try:
            _store_download(self.mirror_dir, self.fetch, PAWNIO_URL, PAWNIO_SHA256,
                            MAX_BINARY_BYTES)
            self.binaries["pawnio"] = PAWNIO_SHA256
        except (FetchError, NotFound) as e:
            self.errors.append(f"PawnIO: {e}")


def sync(mirror_dir, pins, verify_signature, fetch=http_fetch, now=None):
    """Bring the mirror up to date with GitHub. Returns the new status.

    `verify_signature(manifest_bytes, sig_hex) -> bool` is the release-key check -- passed in
    so this module stays free of the key and tests can supply their own. Never raises for a
    fetch problem: each one is recorded in the index's `errors` and that file's previous copy
    is kept. Errors are reported, not hidden, because a mirror that quietly stopped syncing
    is the failure the Onboarding page's status card exists to show.
    """
    os.makedirs(blob_dir(mirror_dir), exist_ok=True)
    stamp = now if now is not None else int(time.time())
    r = _Round(mirror_dir, fetch, verify_signature)
    for path in wanted_manifests(pins):
        r.manifest(path)
    for path in INSTALLER_FILES:
        r.installer(path)
    r.pawnio()
    index = {"files": r.files, "binaries": r.binaries, "attempted_at": stamp,
             "errors": r.errors,
             # "Synced" means every file was checked this round; a partial round keeps the
             # previous time, so the card can say how stale the oldest copy might be.
             "synced_at": stamp if not r.errors else r.old["synced_at"]}
    _write_index(mirror_dir, index)
    _collect_garbage(mirror_dir, index)
    return status(mirror_dir)


def _collect_garbage(mirror_dir, index):
    """Delete blobs the index no longer names, and abandoned partial downloads. A blob open
    for a download in progress cannot be deleted on Windows; it goes on the next round."""
    keep = set(index["files"].values()) | set(index["binaries"].values())
    try:
        names = os.listdir(blob_dir(mirror_dir))
    except OSError:
        return
    for name in names:
        if name in keep:
            continue
        try:
            os.unlink(os.path.join(blob_dir(mirror_dir), name))
        except OSError:
            pass


def clear(mirror_dir):
    """Empty the mirror, for a hub that has switched it off: its disk comes back, and the
    routes answer 404, which sends a client to GitHub alone, as before the mirror existed."""
    index = _empty_index()
    if os.path.isdir(mirror_dir):
        os.makedirs(blob_dir(mirror_dir), exist_ok=True)
        _write_index(mirror_dir, index)
        _collect_garbage(mirror_dir, index)
