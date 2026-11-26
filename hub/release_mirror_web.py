"""HTTP surface of the release mirror (roadmap #33) -- see release_mirror.py for the design.

Two public routes and one admin route:
  * `/api/agent/dist/repo/<path>` -- a mirrored repository file (a manifest, its .sig, an
    installer, the Linux unit) under its repository path, so `<hub>/api/agent/dist/repo/` stands
    in for `https://raw.githubusercontent.com/<repo>/main/` and nothing else changes.
  * `/api/agent/dist/bin/<sha256>` -- a binary addressed by its hash: an agent build a mirrored
    manifest names, or the pinned PawnIO installer. A client already knows the hash from the
    signed manifest (or the pin), so it never needs a url from the hub.
  * `/api/release-mirror/status` -- the Onboarding page's status card, behind MANAGE_ONBOARDING.

**The two public routes are unauthenticated on purpose.** The files are public on GitHub, every
one that matters is checked by the client against the offline release key or a pinned hash, and
an installer on a PC being onboarded has no credential to present yet. They sit under
`/api/agent/` so the agent listener (hub 1.188.0) serves them through its existing prefix -- an
internet-facing hub can mirror for internet agents without publishing the console. That filter
is never a gate (agent_listener.py says so); here there is deliberately nothing to gate.

**Bytes go out exactly as stored**: `application/octet-stream`, from the file, never decoded.
A charset or line-ending rewrite anywhere between here and the client would fail the signature,
which is correct but would look like tampering.

A 404 is the answer for anything the mirror does not hold -- an unknown path, a hash it does
not list, a mirror that is switched off. Every client treats the mirror as the second source,
so a 404 leaves it where it would have been without the mirror.
"""
from flask import Blueprint, abort, jsonify, send_file

import permissions
import release_mirror


def create_release_mirror_blueprint(mirror_dir, login_required, access, enabled):
    """`enabled()` reads the `fleet.release_mirror` setting at request time, so switching the
    mirror off stops serving at once rather than at the next sync."""
    bp = Blueprint("release_mirror", __name__)
    manage = access.require(permissions.MANAGE_ONBOARDING)

    def _send(path, immutable):
        if not path:
            abort(404)
        resp = send_file(path, mimetype="application/octet-stream", conditional=True,
                         etag=True, max_age=0)
        # A binary is addressed by its hash, so its bytes can never change under one url. A
        # manifest is replaced by every release, so a cache must ask again every time --
        # serving a stale manifest is how a mirrored PC would miss an update for a day.
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if immutable
                                         else "no-cache")
        return resp

    @bp.route("/api/agent/dist/repo/<path:repo_path>", methods=["GET"])
    def dist_repo(repo_path):
        if not enabled():
            abort(404)
        return _send(release_mirror.file_for_path(mirror_dir, repo_path), immutable=False)

    @bp.route("/api/agent/dist/bin/<sha256>", methods=["GET"])
    def dist_bin(sha256):
        if not enabled():
            abort(404)
        return _send(release_mirror.file_for_sha(mirror_dir, sha256), immutable=True)

    @bp.route("/api/release-mirror/status", methods=["GET"])
    @login_required
    @manage
    def mirror_status():
        return jsonify(release_mirror.status(mirror_dir, enabled=enabled()))

    return bp
