"""install.ps1's choice of Python for the hub (Resolve-Python and friends).

**The silent failure this file exists to catch is a hub installed on a 32-bit interpreter.**
Nothing goes wrong at install time. Weeks later, under load, the 2 GB address space fragments
and the remote relay and /api/report throw MemoryError on a machine with gigabytes free, then
the WinSW wrapper crashes into a start-timeout loop -- and nothing in any log says "32-bit".
That is what OPENCLAW looked like. Dropping the bitness check would break no other test and
no install, so what is pinned here is mostly about REFUSAL:

  * a 32-bit Python is skipped, and a 64-bit one listed after it is still found;
  * only-32-bit says "Only 32-bit" and offers to install, rather than "Python not detected";
  * a Python whose bitness probe fails is NOT reported as 32-bit -- a broken install and a
    32-bit one have different fixes, and naming the wrong one sends the operator astray;
  * the real Get-PythonBits agrees with this very interpreter's pointer size;
  * a reinstall over a 32-bit service says so, and over a 64-bit one stays quiet;
  * winget is pinned to x64 and Program Files (x86) is not searched.

The functions are lifted out of install.ps1 by PowerShell's own parser and dot-sourced on
their own, with the console helpers and the bitness probe stubbed -- the installer itself is
never run, and no 32-bit Python is needed on the test box.
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(ROOT, "install.ps1")
#: Where the fake 32-bit interpreter says it lives -- the dedup key, not the route to it.
EXE32 = r"C:\Python312-32\python.exe"

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


def read():
    with open(INSTALLER, encoding="utf-8") as fh:
        return fh.read()


def shells():
    """Every PowerShell on PATH -- 5.1 is what the elevation relaunch uses, and install.ps1 may
    be run from 7, which passes native arguments differently."""
    return [p for p in (shutil.which("powershell"), shutil.which("pwsh")) if p]


#: Loads the named functions from install.ps1, then stubs everything that talks to the console
#: or the operator. Console output goes straight to stdout so it never leaks into a function's
#: return value, which is exactly the bug a Write-Output here would hide.
PRELUDE = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$null)
$names = 'Get-PythonBits','Get-PythonCandidates','Test-PythonCandidate','Resolve-Python',
         'Ensure-Python','Test-ExistingHubBitness'
foreach ($f in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($names -contains $f.Name) { . ([scriptblock]::Create($f.Extent.Text)) }
}
$script:SkippedPython32 = @(); $script:SkippedPythonUnverified = @()
$HubServiceId = 'FleetHub'
function Warn($m) { [Console]::Out.WriteLine("WARN: $m") }
function Step($m) { } ; function Ok($m) { } ; function Say($m) { }
function Prompt-YesNo { $false }
function Die($m) { [Console]::Out.WriteLine("DIE: $m"); exit 0 }
function Install-Python { $null }
"""

#: Fake interpreters are PowerShell functions: `& $Cand.Exe --version` calls them like an exe.
FAKES = r"""
# -c is the sys.executable lookup; anything else is --version. Py32 and Py32Launcher are two
# routes (say, the folder scan and py -3) to the same python.exe.
function Py32  { if ($args -contains '-c') { 'C:\Python312-32\python.exe' } else { 'Python 3.12.7' } }
function Py32Launcher { Py32 @args }
function Py64  { 'Python 3.12.7' }
# Answers --version but fails every -c probe, the way an AV block or a broken stdlib does, so
# its dedup key has to fall back to the label.
function PyBad { if ($args -contains '-c') { [Console]::Error.WriteLine('blocked'); return } 'Python 3.12.7' }
function StoreAlias { '' }
$Bits = @{ Py32 = 32; Py32Launcher = 32; Py64 = 64; PyBad = $null; StoreAlias = $null }
function Get-PythonBits($Exe, $PyArgs) { $Bits[$Exe] }
function Cands([string[]]$exes) { foreach ($e in $exes) { @{ Exe = $e; Args = $null; Label = $e } } }
"""


def run(shell, work, body, fakes=True):
    script = os.path.join(work, "harness.ps1")
    with open(script, "w", encoding="utf-8-sig") as fh:
        fh.write(PRELUDE % INSTALLER.replace("'", "''"))
        if fakes:
            fh.write(FAKES)
        fh.write(body)
    r = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", script], capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


def battery(shell, work):
    tag = os.path.basename(shell)
    print(f"\n== Resolve-Python under {tag} ==")

    out = run(shell, work, r"""
function Get-PythonCandidates { Cands 'Py32','Py64' }
$r = Resolve-Python
"PICKED: $($r.Exe)"; "SKIPPED32: $($script:SkippedPython32 -join ',')"
""")
    check(f"{tag}: a 32-bit Python is skipped and the 64-bit one after it chosen",
          "PICKED: Py64" in out and f"SKIPPED32: {EXE32}" in out)
    check(f"{tag}: ...saying which one it skipped and why",
          "WARN: Skipping Python 3.12.7 at Py32 -- it is 32-bit." in out and "MemoryError" in out)

    out = run(shell, work, r"""
function Get-PythonCandidates { Cands 'Py32','Py32' }
$r = Resolve-Python
"NULL: $($null -eq $r)"
""")
    check(f"{tag}: the same interpreter reached twice is warned about once",
          "NULL: True" in out and out.count("WARN: Skipping") == 1)

    out = run(shell, work, r"""
function Get-PythonCandidates { Cands 'Py32Launcher','Py32' }
$r = Resolve-Python
"NULL: $($null -eq $r)"; "SKIPPED32: $($script:SkippedPython32 -join ',')"
""")
    check(f"{tag}: ...even when reached by two different routes",
          "NULL: True" in out and out.count("WARN: Skipping") == 1
          and f"SKIPPED32: {EXE32}" in out)

    out = run(shell, work, r"""
function Get-PythonCandidates { Cands 'Py32' }
Ensure-Python
""")
    check(f"{tag}: only 32-bit says so, rather than 'Python not detected'",
          "Only 32-bit Python found" in out and "Python not detected" not in out)
    check(f"{tag}: ...and does not settle for it", "DIE: 64-bit Python 3 is required" in out)

    out = run(shell, work, r"""
function Get-PythonCandidates { Cands 'PyBad' }
Ensure-Python
""")
    check(f"{tag}: a Python whose bitness probe fails is skipped",
          "Skipping Python 3.12.7 at PyBad" in out and "DIE:" in out)
    check(f"{tag}: ...and is NOT reported as 32-bit",
          "32-bit" not in out.replace("a 32-bit hub", "") and "confirmed 64-bit" in out)

    out = run(shell, work, r"""
function Get-PythonCandidates { Cands 'PyBad','PyBad' }
$r = Resolve-Python
"UNVERIFIED: $($script:SkippedPythonUnverified -join ',')"
""")
    check(f"{tag}: ...keyed on its label when it won't report its own path, and warned once",
          "UNVERIFIED: PyBad\n" in out.replace("\r", "") and out.count("WARN: Skipping") == 1)

    out = run(shell, work, r"""
function Get-PythonCandidates { Cands 'StoreAlias' }
Ensure-Python
""")
    check(f"{tag}: the Store alias is not Python at all, not a skipped one",
          "Python not detected" in out and "Skipping" not in out)

    out = run(shell, work, f"""
$b = Get-PythonBits '{sys.executable}' $null
"BITS: $b"
$b = Get-PythonBits 'C:\\nope\\python.exe' $null
"MISSING: $($null -eq $b)"
""", fakes=False)
    want = struct.calcsize("P") * 8
    check(f"{tag}: the real probe reports this interpreter's {want} bits",
          f"BITS: {want}" in out)
    check(f"{tag}: ...and $null, not an exception, for one that can't run", "MISSING: True" in out)

    # The real Get-PythonCandidates, with the three install roots pointed at temp folders and
    # PATH lookups stubbed out. The per-user root is named to sort ABOVE the machine-wide one,
    # so a single sort across roots (which this once did) puts it first and fails.
    pf, lad, sysd = (os.path.join(work, n) for n in ("Z_pf", "A_lad", "M_sys"))
    exes = [os.path.join(pf, "Python312", "python.exe"),
            os.path.join(pf, "Python312-32", "python.exe"),
            os.path.join(lad, "Programs", "Python", "Python313", "python.exe"),
            os.path.join(sysd, "Python311", "python.exe")]
    for exe in exes:
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        open(exe, "w").close()
    out = run(shell, work, f"""
function Get-Command {{ $null }}
$env:ProgramFiles = '{pf}'; $env:LOCALAPPDATA = '{lad}'; $env:SystemDrive = '{sysd}'
Get-PythonCandidates | ForEach-Object {{ "CAND: $($_.Exe)" }}
""", fakes=False)
    got = [ln[len("CAND: "):].strip() for ln in out.splitlines() if ln.startswith("CAND: ")]
    check(f"{tag}: candidates keep machine-wide, per-user, drive-root order",
          got == [exes[1], exes[0], exes[2], exes[3]])
    check(f"{tag}: ...and every match in a root, so Python312-32 can't hide Python312",
          exes[0] in got and exes[1] in got)

    # Test-ExistingHubBitness walks from <executable> to the python.exe beside it, or one level
    # up for Scripts\waitress-serve.exe -- the two shapes install.ps1 writes.
    hub = os.path.join(work, "hub")
    os.makedirs(os.path.join(hub, "Scripts"), exist_ok=True)
    open(os.path.join(hub, "python.exe"), "w").close()
    with open(os.path.join(hub, "FleetHub.xml"), "w", encoding="utf-8") as fh:
        fh.write(f"<service><executable>{os.path.join(hub, 'Scripts', 'waitress-serve.exe')}"
                 "</executable></service>")
    for bits, loud in ((32, True), (64, False)):
        out = run(shell, work, f"""
function Get-PythonBits($Exe, $PyArgs) {{ {bits} }}
Test-ExistingHubBitness '{hub}'
"DONE"
""", fakes=False)
        check(f"{tag}: a reinstall over a {bits}-bit service is {'loud' if loud else 'quiet'}",
              "DONE" in out and (("running 32-bit Python" in out) == loud))


def main():
    print("\n== Static: the guards are wired in ==")
    src = read()
    check("winget is pinned to the x64 build",
          "winget install --id Python.Python.3.12 --source winget --scope machine --architecture x64" in src)
    check("the python.org fallback is the amd64 installer",
          '$PythonFallback = "https://www.python.org/ftp/python/' in src
          and "-amd64.exe\"" in src.split("$PythonFallback =", 1)[1].split("\n", 1)[0])
    cands = src.split("function Get-PythonCandidates", 1)[1].split("\nfunction ", 1)[0]
    check("Program Files (x86) is not searched", "ProgramFiles(x86)" not in cands)
    test = src.split("function Test-PythonCandidate", 1)[1].split("\nfunction ", 1)[0]
    check("each candidate goes through the bitness probe", "Get-PythonBits" in test)
    hub = src.split("function Install-Hub", 1)[1].split("\nfunction ", 1)[0]
    check("the hub install checks the existing service's bitness",
          "Test-ExistingHubBitness" in hub)

    found = shells()
    if not found:
        # Not a failure: a Linux cloud session has no PowerShell, and the static checks above
        # still ran. Said loudly so a skip is never mistaken for a pass.
        print("  [--] SKIPPED the execution checks: no powershell or pwsh on PATH")
    for shell in found:
        work = tempfile.mkdtemp()
        try:
            battery(shell, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
