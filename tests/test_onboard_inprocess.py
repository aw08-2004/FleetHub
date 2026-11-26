"""The Windows onboarding script runs the agent installer in its own process (roadmap #29, #33).

**The silent failure this file exists to catch is an onboarding that dies without a word.**
Until hub 1.193.0, hub/templates/onboarding/onboard.ps1 started a second
`powershell.exe -ExecutionPolicy RemoteSigned -File <installer>`, and Kaspersky Endpoint
Security killed the onboarding process at exactly that step (event 4662, FIND1136,
2026-11-26). Killed, it printed nothing: under Invoke-Command the output stopped after
"Installing the FleetHub agent", with no agent, no red line and the private folder left behind.

So the script now calls the installer with `&`. That rests on PowerShell semantics that are
easy to get wrong and that nothing else exercises, so they are run here, under every
PowerShell found, with the invocation block lifted out of the real template and stub
installers in place of the real one:

  * `exit 1` inside an installer FUNCTION (Die) ends only the installer -- the onboarding
    script carries on to its own failure message, with the code unspent;
  * a finished install reads as success even when the installer's last native command
    returned non-zero, because the installer ends with an explicit `exit 0`;
  * an error thrown inside the installer is reported, not lost;
  * the template starts no second PowerShell and strips no download mark.

Run as `-File`, the way onboard.ps1 itself runs: that is the context where a careless `exit`
would end the whole host.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
        return fh.read().replace("\r\n", "\n")


def shells():
    """Every PowerShell on PATH. shutil.which, not a bare name, for the reason
    test_linux_installer.py gives about bash."""
    return [p for p in (shutil.which("powershell"), shutil.which("pwsh")) if p]


def invocation_block(template):
    """The template's lines from resetting LASTEXITCODE through the exit-code check."""
    m = re.search(r"^[ \t]*\$global:LASTEXITCODE = 0\n.*?^[ \t]*if \(\$LASTEXITCODE -ne 0\)[^\n]*\n",
                  template, re.M | re.S)
    return m.group(0) if m else None


#: Stub installers, each standing in for one way the real one can end.
STUBS = {
    # The real installer's Die: a message, then `exit 1` from inside a function.
    "dies": "param([string]$HubUrl)\nfunction Die($m) { Write-Host \"  [xx] $m\"; exit 1 }\n"
            "Die 'no signed manifest'\n'unreachable'\n",
    # A finished install whose last native command failed, as `sc.exe start` can.
    "finishes": "param([string]$HubUrl)\nWrite-Host \"installed for $HubUrl\"\n"
                "cmd.exe /c exit 1056\nexit 0\n",
    "throws": "param([string]$HubUrl)\n$ErrorActionPreference = 'Stop'\n"
              "throw 'the service could not be registered'\n",
}


def run(shell, work, block, stub):
    installer = os.path.join(work, f"{stub}.ps1")
    with open(installer, "w", encoding="utf-8-sig") as fh:
        fh.write(STUBS[stub])
    harness = os.path.join(work, f"harness-{stub}.ps1")
    with open(harness, "w", encoding="utf-8-sig") as fh:
        fh.write("$ErrorActionPreference = 'Stop'\n"
                 "function Fail($msg) { throw [System.Exception]::new($msg) }\n"
                 f"$installer = '{installer}'\n$HubUrl = 'https://hub.example'\n"
                 "try {\n" + block + "    'RESULT: installed'\n"
                 "} catch { 'RESULT: failed: ' + $_.Exception.Message }\n"
                 "'RESULT: the onboarding script is still running'\n")
    return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
                           "-File", harness], capture_output=True, text=True, timeout=120)


def main():
    template = read("hub", "templates", "onboarding", "onboard.ps1")
    installer = read("agent", "install", "agent-install.ps1")

    print("\n== The template starts no second PowerShell ==")
    code = "\n".join(l for l in template.splitlines() if not l.lstrip().startswith("#"))
    check("no powershell.exe is started to run the installer",
          not re.search(r"powershell(\.exe)?\s+-", code, re.I))
    check("the installer is called in this process, with the hub address",
          "& $installer -HubUrl $HubUrl" in code)
    check("no download mark is stripped from the installer", "Unblock-File" not in code)
    last = [l.strip() for l in installer.splitlines() if l.strip() and not l.strip().startswith("#")]
    check("agent-install.ps1 ends with an explicit exit 0", last and last[-1] == "exit 0")

    block = invocation_block(template)
    check("the invocation block is found in the template", block is not None)
    found = shells()
    if not found or block is None:
        print("  (no PowerShell on PATH: the behaviour checks are skipped)")
    work = tempfile.mkdtemp(prefix="fh-onboard-")
    try:
        for shell in found:
            label = os.path.basename(shell)
            print(f"\n== In-process exit semantics under {label} ==")
            r = run(shell, work, block, "dies")
            check(f"{label}: the installer's Die is reported with its exit code",
                  "RESULT: failed: The agent installer failed (exit code 1)" in r.stdout
                  and "NOT used" in r.stdout)
            check(f"{label}: ...and its own message reaches the output",
                  "[xx] no signed manifest" in r.stdout and "unreachable" not in r.stdout)
            check(f"{label}: ...and the onboarding script carries on to say so",
                  "RESULT: the onboarding script is still running" in r.stdout)

            r = run(shell, work, block, "finishes")
            check(f"{label}: a finished install is success despite a failed last native command",
                  "RESULT: installed" in r.stdout and "installed for https://hub.example" in r.stdout)

            r = run(shell, work, block, "throws")
            check(f"{label}: an error thrown in the installer is reported with its message",
                  "RESULT: failed: The agent installer stopped: the service could not be registered"
                  in r.stdout and "RESULT: the onboarding script is still running" in r.stdout)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
