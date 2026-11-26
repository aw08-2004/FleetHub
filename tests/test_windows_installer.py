"""The Windows agent installer's signed-manifest check (agent/install/agent-install.ps1).

**The silent failure this file exists to catch is a first install that runs an unverified
binary as LocalSystem while every self-update after it is verified.** install.ps1 used to ask
the GitHub releases API for the newest agent-v* asset and hand it to agent-install.ps1, which
downloaded it straight over the service's exe -- the one agent binary in a PC's life that the
fleet key did not cover. install.sh closed the same gap for Linux (test_linux_installer.py);
this is the Windows half.

The verifier is a hand-written RFC 8032 check compiled with Add-Type, because Windows
PowerShell 5.1 has no Ed25519. Hand-written crypto is exactly what fails quietly -- a verifier
that accepts everything installs fine every time -- so what is pinned here is mostly about
REFUSAL, and about agreeing with a real implementation:

  * the key in agent-install.ps1 equals AgentConfig.UpdatePublicKeyHex in both agents and the
    one install.sh pins, and is not a parameter;
  * the REAL production signature on agent/agent.manifest.json verifies -- the same bytes the
    agents' BouncyCastle verifier accepts, without the private key anywhere near a test;
  * on random keys from the `cryptography` package, every genuine signature verifies and every
    one-bit change to the message, R, S or the key is refused, as is the malleable S + L twin;
  * the whole download path refuses a binary whose sha256 differs from the signed one, a
    manifest signed by any other key and a missing manifest -- and never produces the file;
  * install.ps1 no longer consults the releases API at all.

Run under every PowerShell found (5.1 is what the elevation relaunch uses; install.ps1 may be
run from 7), because the Add-Type call differs between them. The region is loaded on its own,
without running the installer: tests substitute their own key AFTER loading it, the way a test
replaces a constant.
"""
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(ROOT, "agent", "install", "agent-install.ps1")

#: Ed25519's group order. S must be below it; S + L is the same signature in another encoding.
L = 2 ** 252 + 27742317777372353535851937790883648493

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


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def region():
    m = re.search(r"^#region signed-manifest\n(.*?)^#endregion signed-manifest",
                  read("agent", "install", "agent-install.ps1").replace("\r\n", "\n"),
                  re.M | re.S)
    return m.group(1) if m else None


def shells():
    """Every PowerShell on PATH, by edition. shutil.which, not a bare name, for the reason
    test_linux_installer.py gives about bash."""
    found = []
    for exe in ("powershell", "pwsh"):
        path = shutil.which(exe)
        if path:
            found.append(path)
    return found


def run_ps(shell, work, body, key=None):
    """Run BODY in SHELL after dot-sourcing the installer's signed-manifest region."""
    script = os.path.join(work, "harness.ps1")
    with open(script, "w", encoding="utf-8-sig") as fh:
        fh.write("$ErrorActionPreference = 'Stop'\n")
        fh.write(f". '{os.path.join(work, 'region.ps1')}'\n")
        if key:
            fh.write(f'$UpdatePublicKeyHex = "{key}"\n')
        fh.write(body)
    env = dict(os.environ)
    for var in ("no_proxy", "NO_PROXY"):
        env[var] = ",".join(filter(None, ["127.0.0.1", "localhost", env.get(var, "")]))
    return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", script], capture_output=True, text=True, timeout=180, env=env)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory):
    handler = lambda *a, **k: _Quiet(*a, directory=directory, **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def flip(hexstr, byte, bit=0):
    b = bytearray.fromhex(hexstr)
    b[byte] ^= 1 << bit
    return b.hex()


def signature_cases(work):
    """(label, message path, signature hex, key hex, expected) -- expected from `cryptography`."""
    cases = []
    for i in range(12):
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        msg = os.urandom(i * 37)            # includes the empty message
        path = os.path.join(work, f"msg-{i}")
        with open(path, "wb") as fh:
            fh.write(msg)
        sig = key.sign(msg).hex()
        cases.append(("genuine", path, sig, pub, True))
        cases.append(("bit flipped in R", path, flip(sig, i % 32), pub, False))
        cases.append(("bit flipped in S", path, flip(sig, 32 + i % 31), pub, False))
        cases.append(("signed by another key", path, sig,
                      Ed25519PrivateKey.generate().public_key()
                      .public_bytes(Encoding.Raw, PublicFormat.Raw).hex(), False))
        s = int.from_bytes(bytes.fromhex(sig[64:]), "little")
        cases.append(("S + L (malleable twin)", path,
                      sig[:64] + (s + L).to_bytes(32, "little").hex(), pub, False))
        if msg:
            other = os.path.join(work, f"msg-{i}-flipped")
            with open(other, "wb") as fh:
                fh.write(bytes([msg[0] ^ 1]) + msg[1:])
            cases.append(("bit flipped in the message", other, sig, pub, False))
    cases.append(("a key that is not a curve point", cases[0][1], cases[0][2], "ff" * 32, False))
    return cases


def battery(shell, work):
    print(f"\n== {os.path.basename(shell)}: a real production signature verifies ==")
    manifest = os.path.join(ROOT, "agent", "agent.manifest.json")
    sig = manifest + ".sig"
    probe = run_ps(shell, work, "'probe-ok'\n")
    if probe.returncode != 0 or "probe-ok" not in probe.stdout:
        # A shell that cannot load the region makes every refusal below pass for the wrong
        # reason, so prove it works before believing any of them.
        check(f"the region loads in {shell}: {(probe.stderr or '').strip()[:300]}", False)
        return
    tampered = os.path.join(work, "tampered.json")
    with open(manifest, "rb") as fh:
        data = fh.read()
    with open(tampered, "wb") as fh:
        fh.write(data.replace(b'"version":"', b'"version":"9'))
    real = open(sig).read()
    rows = [("the committed Windows manifest verifies under the fleet key", manifest, real, True),
            ("one changed byte is refused", tampered, real, False),
            ("trailing newlines on the .sig are tolerated", manifest,
             real.strip() + "\r\n\n", True),
            ("an empty signature is refused", manifest, "", False),
            ("a non-hex signature is refused", manifest, "zz" * 64, False),
            ("a truncated signature is refused", manifest, real[:100], False),
            ("an odd-length signature is refused", manifest, real.strip()[:-1], False)]
    body = ""
    for i, (_, path, sighex, _) in enumerate(rows):
        sigfile = os.path.join(work, f"row-{i}.sig")
        with open(sigfile, "w", newline="") as fh:
            fh.write(sighex)
        # Read as the installer reads a downloaded .sig: ReadAllText over the file.
        body += (f"'{i}=' + (Test-ManifestSignature ([IO.File]::ReadAllBytes('{path}')) "
                 f"([IO.File]::ReadAllText('{sigfile}')))\n")
    r = run_ps(shell, work, body)
    got = dict(line.split("=", 1) for line in r.stdout.split() if "=" in line)
    for i, (label, _, _, want) in enumerate(rows):
        check(label, got.get(str(i)) == str(want))

    print(f"\n== {os.path.basename(shell)}: agrees with `cryptography` on random keys ==")
    cases = signature_cases(work)
    spec = os.path.join(work, "cases.json")
    with open(spec, "w") as fh:
        json.dump([{"path": p, "sig": s, "key": k} for _, p, s, k, _ in cases], fh)
    r = run_ps(shell, work, (
        f"$cases = Get-Content -Raw '{spec}' | ConvertFrom-Json\n"
        "foreach ($c in $cases) {\n"
        "    $UpdatePublicKeyHex = $c.key\n"
        "    [string](Test-ManifestSignature ([IO.File]::ReadAllBytes($c.path)) $c.sig)\n"
        "}\n"))
    verdicts = r.stdout.split()
    check(f"one verdict per case ({len(cases)})", len(verdicts) == len(cases))
    by_label = {}
    for (label, _, _, _, want), got in zip(cases, verdicts):
        by_label.setdefault(label, []).append(got == str(want))
    for label, results in by_label.items():
        check(f"{label}: {'accepted' if label == 'genuine' else 'refused'} "
              f"({sum(results)}/{len(results)})", all(results))

    print(f"\n== {os.path.basename(shell)}: the whole download path, against a local server ==")
    key = Ed25519PrivateKey.generate()
    pub_hex = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    site = os.path.join(work, "site")
    os.makedirs(site, exist_ok=True)
    binary = b"MZ" + os.urandom(4096)
    with open(os.path.join(site, "FleetHubAgent.exe"), "wb") as fh:
        fh.write(binary)
    server, base = serve(site)

    def publish(name, sha, signer=key):
        body = ('{"sha256":"%s","url":"%s/FleetHubAgent.exe","version":"3.99.0"}'
                % (sha, base)).encode()
        with open(os.path.join(site, name), "wb") as fh:
            fh.write(body)
        with open(os.path.join(site, name + ".sig"), "w") as fh:
            fh.write(signer.sign(body).hex() + "\n")
        return f"{base}/{name}"

    def install(manifest_url, tag):
        dest = os.path.join(work, f"out-{tag}.exe")
        r = run_ps(shell, work,
                   f"try {{ $v = Get-VerifiedAgent '{dest}' '{manifest_url}'; "
                   "'version=' + $v.Version; 'sha256=' + $v.Sha256; "
                   "'manifest=' + $v.ManifestFrom; 'binary=' + $v.BinaryFrom }\n"
                   "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 3 }\n",
                   key=pub_hex)
        return r, dest

    try:
        r, dest = install(publish("good.json", hashlib.sha256(binary).hexdigest()), "good")
        check("a signed manifest whose sha256 matches installs", r.returncode == 0
              and os.path.exists(dest) and open(dest, "rb").read() == binary)
        check("...and hands back the version and where each file came from",
              "version=3.99.0" in r.stdout and f"manifest={base}/good.json" in r.stdout
              and f"binary={base}/FleetHubAgent.exe" in r.stdout)
        check("...and hands back the SIGNED sha256 for the re-check after the copy",
              f"sha256={hashlib.sha256(binary).hexdigest()}" in r.stdout)

        r, dest = install(publish("wrong-sha.json", "0" * 64), "wrong")
        check("a binary that differs from the signed sha256 is refused, and nothing is written",
              r.returncode == 3 and "does not match the signed manifest" in r.stderr
              and not os.path.exists(dest))

        r, dest = install(publish("stranger.json", hashlib.sha256(binary).hexdigest(),
                                  signer=Ed25519PrivateKey.generate()), "stranger")
        check("a manifest signed by any other key is refused, and nothing is written",
              r.returncode == 3 and "NOT signed by the fleet's key" in r.stderr
              and not os.path.exists(dest))

        r, dest = install(f"{base}/missing.json", "missing")
        check("no manifest published is a clear refusal, not an unverified fallback",
              r.returncode == 3 and f"{base}/missing.json" in r.stderr
              and "-AgentExe" in r.stderr and not os.path.exists(dest))

        # The hub's release mirror (roadmap #33): GitHub unreachable, the hub reachable. The
        # mirror is laid out as release_mirror_web serves it -- repo/<path> and bin/<sha256>.
        # "GitHub" here is a port nothing listens on, which is what a blocked site looks like.
        dead = "http://127.0.0.1:9"
        good_sha = hashlib.sha256(binary).hexdigest()

        def mirror(tag, manifest_sha, signer=key, bin_bytes=binary, bin_url=None):
            root = os.path.join(site, tag)
            os.makedirs(os.path.join(root, "repo", "agent"), exist_ok=True)
            os.makedirs(os.path.join(root, "bin"), exist_ok=True)
            body = ('{"sha256":"%s","url":"%s","version":"3.99.0"}'
                    % (manifest_sha, bin_url or f"{dead}/FleetHubAgent.exe")).encode()
            with open(os.path.join(root, "repo", "agent", "agent.manifest.json"), "wb") as fh:
                fh.write(body)
            with open(os.path.join(root, "repo", "agent", "agent.manifest.json.sig"), "w") as fh:
                fh.write(signer.sign(body).hex() + "\n")
            with open(os.path.join(root, "bin", manifest_sha), "wb") as fh:
                fh.write(bin_bytes)
            return f"{base}/{tag}"

        def install_via_mirror(stable_url, mirror_base, tag):
            dest = os.path.join(work, f"out-{tag}.exe")
            r = run_ps(shell, work,
                       f"$StableManifestUrl = '{stable_url}'\n"
                       f"try {{ $v = Get-VerifiedAgent '{dest}' $null '{mirror_base}'; "
                       "'version=' + $v.Version; 'manifest=' + $v.ManifestFrom; "
                       "'binary=' + $v.BinaryFrom }\n"
                       "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 3 }\n",
                       key=pub_hex)
            return r, dest

        r, dest = install_via_mirror(f"{dead}/agent/agent.manifest.json",
                                     mirror("m-good", good_sha), "mirror-good")
        check("GitHub unreachable: the mirror's signed manifest and binary install",
              r.returncode == 0 and open(dest, "rb").read() == binary
              and "/m-good/repo/agent/agent.manifest.json" in r.stdout
              and f"/m-good/bin/{good_sha}" in r.stdout)

        r, dest = install_via_mirror(f"{dead}/agent/agent.manifest.json",
                                     mirror("m-stranger", good_sha,
                                            signer=Ed25519PrivateKey.generate()), "mirror-stranger")
        check("a mirror serving a manifest signed by another key is refused, nothing written",
              r.returncode == 3 and "NOT signed by the fleet's key" in r.stderr
              and not os.path.exists(dest))

        r, dest = install_via_mirror(f"{dead}/agent/agent.manifest.json",
                                     mirror("m-swapped", good_sha, bin_bytes=b"MZ" + os.urandom(64)),
                                     "mirror-swapped")
        check("a mirror serving a different binary under the signed hash is refused",
              r.returncode == 3 and "does not match the signed manifest" in r.stderr
              and not os.path.exists(dest))

        gh = publish("gh-manifest.json", good_sha)
        with open(os.path.join(site, "gh-manifest.json"), "rb") as fh:
            gh_body = fh.read().replace(f"{base}/FleetHubAgent.exe".encode(),
                                        f"{dead}/FleetHubAgent.exe".encode())
        with open(os.path.join(site, "gh-manifest.json"), "wb") as fh:
            fh.write(gh_body)
        with open(os.path.join(site, "gh-manifest.json.sig"), "w") as fh:
            fh.write(key.sign(gh_body).hex() + "\n")
        r, dest = install_via_mirror(gh, mirror("m-binonly", good_sha), "mirror-binonly")
        check("manifest from GitHub, release host blocked: the binary comes from the mirror",
              r.returncode == 0 and open(dest, "rb").read() == binary
              and f"manifest={gh}" in r.stdout and f"/m-binonly/bin/{good_sha}" in r.stdout)
    finally:
        server.shutdown()


def main():
    print("\n== The installer's key is the agents' key ==")
    script = read("agent", "install", "agent-install.ps1")
    ours = re.search(r'^\$UpdatePublicKeyHex = "([0-9a-f]{64})"', script, re.M)
    check("agent-install.ps1 pins a 64-hex-digit key", ours is not None)
    for label, path in (("Linux", ("agent-linux", "src", "FleetHubAgent", "AgentConfig.cs")),
                        ("Windows", ("agent", "src", "TempMonitorAgent", "AgentConfig.cs"))):
        theirs = re.search(r'UpdatePublicKeyHex\s*=\s*"([0-9a-f]{64})"', read(*path))
        check(f"...and it equals the {label} agent's UpdatePublicKeyHex",
              ours and theirs and ours.group(1) == theirs.group(1))
    sh = re.search(r'^UPDATE_PUBLIC_KEY_HEX="([0-9a-f]{64})"',
                   read("agent-linux", "install", "install.sh"), re.M)
    check("...and the one install.sh pins", ours and sh and ours.group(1) == sh.group(1))
    params = re.search(r"^param\((.*?)^\)", script, re.M | re.S)
    check("the key is not a parameter", params and "Key" not in params.group(1))
    stable = re.search(r'StableManifestUrl\s*=\s*\n?\s*"([^"]+)"',
                       read("agent", "src", "TempMonitorAgent", "AgentConfig.cs"))
    check("the manifest it reads is the agent's stable manifest",
          stable and f'$StableManifestUrl = "{stable.group(1)}"' in script)

    print("\n== install.ps1 hands the choice to the verified path ==")
    top = read("install.ps1")
    check("install.ps1 no longer asks the releases API for the agent",
          "api.github.com/repos/$Repo/releases" not in top
          and "Get-LatestAgentAssetUrl" not in top)
    check("-AgentUrl is labelled as not checked",
          "-AgentUrl is NOT checked against the fleet's signing key" in script)
    check("-AgentExe is labelled as not checked",
          "local, not checked against the fleet key" in script)
    stage = script.find("Copy-Item -Path $source -Destination $incoming")
    rehash = script.find("Get-FileHash -Path $incoming")
    stop = script.find("# Stop an existing service before overwriting its exe.")
    check("the copy is staged inside the install directory, not run from %TEMP%",
          '$incoming = "$ExePath.incoming"' in script and stage != -1
          and "Copy-Item -Path $source -Destination $ExePath" not in script)
    check("...re-hashed against the signed sha256 there (TOCTOU on %TEMP%)",
          rehash > stage and "$got -ne $signedSha256" in script[rehash:rehash + 400])
    check("...and BEFORE the running service is stopped, so a refusal leaves it running",
          stage != -1 and rehash != -1 and rehash < stop)
    check("the staged copy becomes the exe by a rename, after the stop",
          script.find("Move-Item -Path $incoming -Destination $ExePath") > stop)
    check("the binary is fetched before the running service is stopped",
          script.index("Get-VerifiedAgent -Destination") < script.index(
              "# Stop an existing service before overwriting its exe."))

    body = region()
    check("the signed-manifest region is present", body is not None)
    found = shells()
    if not found:
        # Not a failure: a Linux cloud session has bash but usually no PowerShell, and the
        # static checks above still ran. Said loudly so a skip is never mistaken for a pass.
        print("  [--] SKIPPED the execution checks: no powershell or pwsh on PATH")
    work = tempfile.mkdtemp()
    try:
        if body is not None:
            with open(os.path.join(work, "region.ps1"), "w", encoding="utf-8-sig") as fh:
                fh.write(body)
            for shell in found:
                battery(shell, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
