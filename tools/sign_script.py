#!/usr/bin/env python3
"""Sign an installer script with the fleet's Ed25519 key: writes <script>.sig beside it.

Exists for agent-linux/install/install.sh, the one installer Authenticode cannot cover (there
is no Authenticode on Linux). Before this, the hub's onboard.sh downloaded install.sh from
main and piped it into a root bash with **nothing checked at all** -- install.sh verified the
agent binary against the signed manifest, but nothing verified install.sh, so the one script
that decides what "verified" means was the unverified step.

The .sig is the same format as every manifest's: lowercase hex of a detached Ed25519 signature
over the file's EXACT bytes, so the openssl verifier install.sh already uses for manifests
checks it unchanged.

**Re-sign on every edit to the script.** A stale .sig is a refused install, and
tests/test_script_signatures.py fails on one so it is caught before main is.

Rejected: a `--sign-file` option in sign_release.py. That file is the release trust root and
is kept out of routine edits; this imports its key handling instead of copying it, so there is
still one place that reads the private key.

Usage:
    python tools/sign_script.py agent-linux/install/install.sh [--key PATH]
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import sign_release  # noqa: E402  (needs ROOT on the path first)


def sign_file(key_path, script):
    Ed25519PrivateKey, _ = sign_release._ed25519()
    if not os.path.exists(key_path):
        sys.exit(f"No signing key at {key_path}. Run: python sign_release.py --genkey")
    with open(script, "rb") as f:
        data = f.read()
    # A CRLF in a shell script is a broken install (`bash^M`), and signing one would bless it.
    if script.endswith(".sh") and b"\r" in data:
        sys.exit(f"{script} contains CR characters; it must be LF-only before it is signed.")
    with open(key_path, "r", encoding="utf-8") as f:
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))
    with open(script + ".sig", "w", encoding="utf-8", newline="") as f:
        f.write(priv.sign(data).hex())
    print(f"Signed  : {script} ({len(data)} bytes)")
    print(f"Wrote   : {script}.sig -- commit the two together.")


def main():
    ap = argparse.ArgumentParser(description="Ed25519-sign an installer script with the fleet key.")
    ap.add_argument("script")
    ap.add_argument("--key", default=sign_release.DEFAULT_KEY_PATH)
    args = ap.parse_args()
    sign_file(args.key, args.script)


if __name__ == "__main__":
    main()
