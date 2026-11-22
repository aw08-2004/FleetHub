"""The installer scripts' signatures: Authenticode on the two .ps1, Ed25519 on install.sh.

**The silent failure this file exists to catch is an installer edited after it was signed.**
A signature covers exact bytes, so a one-character fix to install.ps1 committed without
re-signing leaves a signature block that no longer matches: Explorer shows "This digital
signature is not valid", and once the downloaders check it (install.ps1 for
agent-install.ps1, the hub's onboarding scripts for both installers) every new PC refuses to
install -- from a change that looked like a typo fix. Nothing about the edit itself says so,
and there is no CI to notice; this test is the only thing that runs before main does.

What is pinned:
  * install.sh.sig is a valid Ed25519 signature by the fleet key over install.sh's exact
    bytes, checked with `cryptography` -- so this half runs on any OS;
  * install.sh and its .sig contain no CR (the pin in .gitattributes, proven on the bytes);
  * each .ps1 ends with exactly one Authenticode signature block, and nothing after it;
  * on Windows, Get-AuthenticodeSignature calls both .ps1 Valid, timestamped, and signed by
    one and the same publisher. Elsewhere that half is skipped and says so.

Every failure names which of two different problems it is, because the fix differs:
  * NOT SIGNED -- no signature at all. Expected exactly once, before the first signing on the
    release PC (stage 1 of the code-signing work, ROADMAP *On beta*). Still a failure rather
    than a skip: a skip would also pass a file whose signature block somebody deleted.
  * STALE -- a signature that no longer matches the bytes. The file was edited after it was
    signed; that is the regression this module exists for.

The fix for either is never to edit the test or the .sig by hand: re-sign with
tools/codesign.ps1 (the .ps1) or tools/sign_script.py (install.sh).
"""
import json
import os
import re
import shutil
import subprocess
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(ROOT, "agent-linux", "install", "install.sh")
PS1_SCRIPTS = [os.path.join(ROOT, "install.ps1"),
               os.path.join(ROOT, "agent", "install", "agent-install.ps1")]

BEGIN = b"# SIG # Begin signature block"
END = b"# SIG # End signature block"

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


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def fleet_key_hex():
    """The key install.sh pins -- test_windows_installer.py already holds it equal to the
    agents' and agent-install.ps1's, so one source is enough here."""
    m = re.search(r'^UPDATE_PUBLIC_KEY_HEX="([0-9a-f]{64})"$',
                  read_bytes(INSTALL_SH).decode("utf-8"), re.M)
    return m.group(1) if m else None


def test_install_sh():
    data = read_bytes(INSTALL_SH)
    sig_path = INSTALL_SH + ".sig"
    check("install.sh has no CR (a CRLF script fails as bash^M)", b"\r" not in data)
    if not os.path.exists(sig_path):
        check("NOT SIGNED: install.sh.sig is missing -- run: python tools/sign_script.py "
              "agent-linux/install/install.sh", False)
        return
    sig_text = read_bytes(sig_path)
    check("install.sh.sig is 128 lowercase hex chars, no newline",
          re.fullmatch(rb"[0-9a-f]{128}", sig_text) is not None)
    key = fleet_key_hex()
    check("install.sh pins a fleet key", key is not None)
    if not key or not re.fullmatch(rb"[0-9a-f]{128}", sig_text):
        return
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(
            bytes.fromhex(sig_text.decode()), data)
        ok = True
    except InvalidSignature:
        ok = False
    check("install.sh.sig verifies against the fleet key over install.sh's exact bytes"
          + ("" if ok else " -- STALE: install.sh was edited after signing; "
             "re-run tools/sign_script.py"), ok)


def test_docs_key():
    """docs/installation.md prints the fleet key for a by-hand openssl check. A copy that
    drifted would tell an operator a genuine install.sh is forged -- or, worse, be the key an
    attacker swapped in a doc nobody re-reads."""
    with open(os.path.join(ROOT, "docs", "installation.md"), encoding="utf-8") as fh:
        doc = fh.read()
    key = fleet_key_hex()
    check("docs/installation.md's openssl snippet prints install.sh's fleet key",
          key is not None and key in doc)
    check("docs/installation.md prints no other 64-hex key",
          set(re.findall(r"\b[0-9a-f]{64}\b", doc)) <= {key})


def test_ps1_blocks():
    for path in PS1_SCRIPTS:
        name = os.path.relpath(path, ROOT).replace("\\", "/")
        data = read_bytes(path)
        if BEGIN not in data:
            check(f"NOT SIGNED: {name} has no Authenticode signature block -- run: "
                  f"powershell -File tools\\codesign.ps1 -Path {name}", False)
            continue
        check(f"{name} carries exactly one Authenticode signature block",
              data.count(BEGIN) == 1 and data.count(END) == 1)
        check(f"{name}: nothing after the signature block",
              data.rstrip(b"\r\n").endswith(END))


def powershell():
    for exe in ("powershell", "pwsh"):
        if shutil.which(exe):
            return exe
    return None


def test_ps1_authenticode():
    shell = powershell()
    if sys.platform != "win32" or not shell:
        print("  [--] SKIPPED Get-AuthenticodeSignature: needs Windows PowerShell")
        return
    paths = ",".join("'" + p.replace("'", "''") + "'" for p in PS1_SCRIPTS)
    script = (f"@({paths}) | ForEach-Object {{ $s = Get-AuthenticodeSignature -LiteralPath $_; "
              "[pscustomobject]@{ path = $_; status = [string]$s.Status; "
              "subject = $(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' }); "
              "stamped = [bool]$s.TimeStamperCertificate } } | ConvertTo-Json -Compress")
    out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=120)
    rows = json.loads(out.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    check("Get-AuthenticodeSignature answered for every script", len(rows) == len(PS1_SCRIPTS))
    for row in rows:
        name = os.path.relpath(row["path"], ROOT).replace("\\", "/")
        if row["status"] == "NotSigned":
            check(f"NOT SIGNED: {name} (Get-AuthenticodeSignature: NotSigned)", False)
            continue
        hint = {"HashMismatch": " -- STALE: edited after signing; re-run tools/codesign.ps1"}
        check(f"{name} is Valid (got {row['status']}){hint.get(row['status'], '')}",
              row["status"] == "Valid")
        check(f"{name} is timestamped (it would stop validating when the cert expires)",
              row["stamped"])
    subjects = {r["subject"] for r in rows if r["subject"]}
    if not subjects:
        return  # nothing signed yet; the NOT SIGNED lines above already say so
    check(f"both installers name one publisher ({' | '.join(sorted(subjects)) or 'none'})",
          len(subjects) == 1)


def main():
    print("== install.sh (Ed25519)")
    test_install_sh()
    test_docs_key()
    print("== .ps1 signature blocks")
    test_ps1_blocks()
    print("== .ps1 Authenticode")
    test_ps1_authenticode()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
