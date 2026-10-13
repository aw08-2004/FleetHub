"""The Windows agent service rename, TempMonitorAgent -> FleetHubAgent (2026-10-12).

**The silent failure this file exists to catch is a PC left with no agent service, or with
two.** The rename is a registration change only -- the binary runs under either name, because
the SCM ignores the name an own-process service hands StartServiceCtrlDispatcher -- so the
fleet is mixed for good: every PC installed before the rename stays TempMonitorAgent until
someone re-runs the installer. Everything that names the service has to know both, and the
installer's migration has to happen in the one order that can fail safely:

  * stop the old service before anything is started, so two agents never share one identity;
  * create the new service and give it its recovery actions (the exit-17 self-update restart
    depends on them) BEFORE deleting the old one, so a failed New-Service puts the old one
    back instead of leaving nothing;
  * uninstall removes both names, or a leftover one restarts an agent with no files every
    minute;
  * the watchdog guard refuses both names on the hub and on the agent;
  * onboard.ps1 restarts whichever service exists, not a hardcoded one.

The exe followed on 2026-10-13 (TempMonitorAgent.exe -> FleetHubAgent.exe) under the same
rule, and the same file catches its silent failure: an install that deletes the old exe before
the service points at the new one, so a failed registration falls back to a service with no
binary -- or a kill-protection list that knows only one of the two names a PC can be running.

Static checks over the scripts, plus a parse of each under every PowerShell found. Running the
migration needs a real Windows PC and the SCM -- that is owed locally (ROADMAP.MD).
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))

import watchdogs  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def section(script, start, end):
    a = script.find(start)
    b = script.find(end, a + 1) if a != -1 else -1
    return script[a:b] if a != -1 and b != -1 else ""


def test_installer():
    print("\n== agent-install.ps1 ==")
    script = read("agent", "install", "agent-install.ps1")
    check("new installs register FleetHubAgent",
          '$ServiceName       = "FleetHubAgent"' in script)
    check("the pre-rename name is known as the legacy one",
          '$LegacyServiceName = "TempMonitorAgent"' in script)

    uninstall = section(script, "if ($Uninstall) {", "\n    exit\n")
    check("uninstall removes both names",
          "foreach ($name in @($ServiceName, $LegacyServiceName))" in uninstall
          and "sc.exe delete $name" in uninstall)

    stop = script.find("# Stop an existing service before overwriting its exe.")
    after_stop = script[stop:stop + 400]
    check("the install stops the service under both names before swapping the exe",
          "Stop-AgentService $ServiceName" in after_stop
          and "Stop-AgentService $LegacyServiceName" in after_stop)
    check("...and waits for Stopped rather than sleeping blind",
          'WaitForStatus("Stopped"' in section(script, "function Stop-AgentService", "\n}\n"))

    register = section(script, 'Step "Registering Windows Service"', 'Step "Starting service"')
    create = register.find("New-Service -Name $ServiceName")
    failure = register.find("sc.exe failure $ServiceName")
    delete = register.find("sc.exe delete $LegacyServiceName")
    check("the new service is created before the old one is deleted",
          create != -1 and delete != -1 and create < delete)
    check("its recovery actions are set before the old one is deleted",
          failure != -1 and failure < delete)
    check("a failed registration restarts the old service instead of leaving none",
          "sc.exe start $LegacyServiceName" in register[:delete])
    fallback = section(register, "} catch {", "sc.exe start $LegacyServiceName")
    check("...after deleting the half-registered new one, so both never start on the next boot",
          "sc.exe delete $ServiceName" in fallback)
    check("the old name is deleted by name in exactly one place, the migration",
          script.count("sc.exe delete $LegacyServiceName") == 1)
    check("the service started at the end is the new name",
          "sc.exe start $ServiceName" in section(script, 'Step "Starting service"', '"@'))


def test_agent_and_hub():
    print("\n== the agent and the hub know both names ==")
    program = read("agent", "src", "TempMonitorAgent", "Program.cs")
    check("the agent names itself FleetHubAgent",
          'o.ServiceName = "FleetHubAgent"' in program)
    for name in ("FleetHubAgent", "TempMonitorAgent", " fleethubagent "):
        check(f"the hub refuses a watchdog on '{name.strip()}'", watchdogs.is_protected(name))
    guard = read("agent", "src", "TempMonitorAgent", "Watchdog", "WatchdogGuard.cs")
    check("the agent's own guard lists both names",
          '"fleethubagent"' in guard and '"tempmonitoragent"' in guard)

    onboard = read("hub", "templates", "onboarding", "onboard.ps1")
    check("onboard.ps1 looks up whichever service exists",
          "$ServiceNames = @('FleetHubAgent', 'TempMonitorAgent')" in onboard
          and "$ServiceName = 'TempMonitorAgent'" not in onboard)
    lookup = onboard.find("$ServiceName = $ServiceNames")
    check("...after the installer ran, not before",
          lookup > onboard.find("-File $installer"))


def test_exe_rename():
    print("\n== the exe followed: TempMonitorAgent.exe -> FleetHubAgent.exe (2026-10-13) ==")
    csproj = read("agent", "src", "TempMonitorAgent", "TempMonitorAgent.csproj")
    check("the build produces FleetHubAgent.exe",
          "<AssemblyName>FleetHubAgent</AssemblyName>" in csproj)
    check("...and the csproj comment carries no '--', which MSBuild refuses to load",
          "--" not in "".join(part.split("-->")[0] for part in csproj.split("<!--")[1:]))

    script = read("agent", "install", "agent-install.ps1")
    check("the installer installs FleetHubAgent.exe", '$ExeName        = "FleetHubAgent.exe"' in script)
    register = script.find('Step "Registering Windows Service"')
    removal = script.find("Remove-Item $LegacyExePath")
    check("the old exe is removed only after the service is registered against the new one",
          register != -1 and removal > register
          and removal > script.find("sc.exe delete $LegacyServiceName"))
    check("...after stopping any helper still running from it",
          -1 < script.find("$_.Path -eq $LegacyExePath") < removal)
    check("...and a leftover is a warning, never a failed install",
          "Die" not in section(script, "if ((Test-Path $LegacyExePath)", "\n}\n"))
    check("the uninstall still removes the whole install directory, both names with it",
          "Remove-Item $InstallDir -Recurse" in section(script, "if ($Uninstall) {", "\n    exit\n"))

    release = read("agent", "release.ps1")
    check("release.ps1 publishes and signs FleetHubAgent.exe",
          'Join-Path $DistDir "FleetHubAgent.exe"' in release
          and '/$Tag/FleetHubAgent.exe"' in release)
    check("install.ps1 offers a local build under the new name",
          "agent\\dist\\FleetHubAgent.exe" in read("install.ps1"))

    # Self-update must keep writing over the RUNNING path, not over a path built from the
    # new name: that is what makes an un-reinstalled PC safe to leave as it is.
    updater = read("agent", "src", "TempMonitorAgent", "Update", "SelfUpdater.cs")
    check("self-update swaps onto Environment.ProcessPath, whatever the exe is called",
          "var currentPath = Environment.ProcessPath;" in updater
          and "File.Move(stagedPath, currentPath)" in updater)

    import processes
    for name in ("FleetHubAgent.exe", "TempMonitorAgent.exe"):
        check(f"the hub will not offer to end {name}", processes.is_protected(name))
    guard = read("agent", "src", "TempMonitorAgent", "Fleet", "Executors", "ProcessExecutors.cs")
    check("...and nor will the agent, under either name",
          '"fleethubagent"' in guard and '"tempmonitoragent"' in guard)


def shells():
    return [s for s in ("powershell", "pwsh") if shutil.which(s)]


def test_parse():
    print("\n== the scripts parse ==")
    found = shells()
    if not found:
        print("  [--] SKIPPED the parse checks: no powershell or pwsh on PATH")
        return
    for shell in found:
        for parts in (("agent", "install", "agent-install.ps1"),
                      ("hub", "templates", "onboarding", "onboard.ps1")):
            path = os.path.join(ROOT, *parts)
            cmd = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{path}', [ref]$null, [ref]$e); if ($e) {{ $e | % {{ $_.ToString() }}; exit 1 }}")
            out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", cmd],
                                 capture_output=True, text=True, timeout=120)
            check(f"{parts[-1]} parses under {shell}", out.returncode == 0)
            if out.returncode:
                print(out.stdout + out.stderr)


def main():
    test_installer()
    test_agent_and_hub()
    test_exe_rename()
    test_parse()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
