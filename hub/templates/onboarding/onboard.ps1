# FleetHub onboarding script (roadmap #29). Rendered by the hub for ONE onboarding code;
# see hub/onboarding_web.py. Saved to a file and run with -File, either by the .cmd launcher
# the onboarding page offers (which elevates first) or by the command the page shows for an
# elevated PowerShell. Never run straight from memory: that is the dropper pattern antivirus
# blocks (Kaspersky did, on the first version of the launcher). No comment here names the
# commands that pattern uses, because scanners read comments too.
#
# What it does, in this order, and why the order:
#   1. Installs the agent with agent-install.ps1 and NO enrollment secret. The installer
#      checks the binary against the signed manifest, exactly as a technician's install does.
#   2. Only then spends the code. A failed install must not burn a one-time code.
#   3. Writes the credential the hub returned to agent.json, readable only by SYSTEM and
#      Administrators, and restarts the service so the agent loads it.
# The token goes from the hub straight into that file. It is never printed.
#
# Wrapped in a script block and stopped with throw, never `exit`, so a failure always
# reaches the red line at the bottom instead of closing the window before it can be read.
& {
    $ErrorActionPreference = 'Stop'
    $HubUrl       = {{ hub_url }}
    $Code         = {{ code }}
    $InstallerUrl = {{ installer_url }}
    # FleetHubAgent since 2026-10-12, and agent-install.ps1 migrates a TempMonitorAgent
    # registration, so after step 1 it should always be the new name. Looked up after the
    # install rather than assumed anyway: the installer comes from main and this script from
    # whatever hub rendered it, and the two need not be the same commit. Guessing wrong here
    # is the worst place to -- the code is already spent and the credential already written.
    $ServiceNames = @('FleetHubAgent', 'TempMonitorAgent')
    $StateDir     = Join-Path $env:ProgramData 'FleetHub\Agent'
    $IdentityPath = Join-Path $StateDir 'agent.json'

    function Show-Info($msg) { Write-Host "  $msg" }
    function Show-Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
    function Fail($msg) { throw [System.Exception]::new($msg) }
    # A fresh folder only SYSTEM and Administrators can write to, ACL set in the same call that
    # creates it. The installer runs elevated from here, so it must not sit in the user's TEMP,
    # where any non-elevated process of that user could swap it first -- a random file name
    # does not help against a process watching the folder. Same reasoning as
    # onboarding_web.run_command, which put this script in such a folder.
    function New-PrivateDirectory {
        $dir = Join-Path $env:ProgramData ('FleetHub-Onboard-' + [guid]::NewGuid().ToString('N'))
        $sec = New-Object System.Security.AccessControl.DirectorySecurity
        $sec.SetAccessRuleProtection($true, $false)
        foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {   # SYSTEM, BUILTIN\Administrators
            $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
                (New-Object System.Security.Principal.SecurityIdentifier($sid)), 'FullControl',
                'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        }
        [void][IO.Directory]::CreateDirectory($dir, $sec)
        $dir
    }

    try {
        Write-Host "FleetHub onboarding" -ForegroundColor Cyan
        Show-Info "Hub: $HubUrl"

        $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            Fail "This must run as Administrator. Open PowerShell with 'Run as administrator' and paste the command again."
        }
        # The credential travels in the redeem response, so the hub has to be the hub. Plain
        # http is allowed only to this machine itself, which is a developer's hub.
        if ($HubUrl -notmatch '^https://' -and $HubUrl -notmatch '^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)') {
            Fail "The hub address is not https ($HubUrl). Ask your administrator to set HUB_URL to the hub's https address."
        }
        # Windows PowerShell 5.1 may still default to TLS 1.0; GitHub and most hubs refuse it.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

        Show-Step "Installing the FleetHub agent"
        $installerDir = New-PrivateDirectory
        try {
            $installer = Join-Path $installerDir 'agent-install.ps1'
            Invoke-WebRequest -UseBasicParsing -Uri $InstallerUrl -OutFile $installer
            # Run below under RemoteSigned, which refuses a file marked as downloaded from the
            # web. Invoke-WebRequest does not mark it today; this keeps that true if one does.
            Unblock-File -LiteralPath $installer -ErrorAction SilentlyContinue
            & powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File $installer -HubUrl $HubUrl
            if ($LASTEXITCODE -ne 0) { Fail "The agent installer failed (exit code $LASTEXITCODE). The onboarding code was NOT used; fix the problem above and run this again." }
        } finally {
            Remove-Item -LiteralPath $installerDir -Recurse -Force -ErrorAction SilentlyContinue
        }

        Show-Step "Enrolling this PC"
        # The BIOS serial, read exactly as the agent reads it (Win32_BIOS, trimmed). The hub
        # uses it only when this PC's name already has history, to check it is the same
        # hardware -- see onboarding._check_known_machine.
        $serial = $null
        try { $serial = ([string](Get-CimInstance -ClassName Win32_BIOS).SerialNumber).Trim() } catch { $serial = $null }
        $body = @{ code = $Code; machine = $env:COMPUTERNAME; serial = $serial } | ConvertTo-Json -Compress
        try {
            $identity = Invoke-RestMethod -Method Post -Uri "$HubUrl/api/onboarding/redeem" `
                -ContentType 'application/json' -Body $body -UseBasicParsing
        } catch {
            # The hub's own sentence when it sent one -- "this PC's name is already enrolled"
            # needs a different fix from "this code has expired", and only the body says which.
            $reason = $null
            try { $reason = ($_.ErrorDetails.Message | ConvertFrom-Json).error } catch { $reason = $null }
            if (-not $reason) { $reason = $_.Exception.Message }
            Fail "The hub refused to enrol this PC: $reason"
        }
        if (-not $identity.agent_id -or -not $identity.token) { Fail "The hub's answer did not contain a credential." }

        Show-Step "Saving this PC's credential"
        $ServiceName = $ServiceNames | Where-Object { Get-Service -Name $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
        if (-not $ServiceName) { Fail "The agent service is not installed, although the installer reported success." }
        Stop-Service -Name $ServiceName -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
        if (Test-Path -LiteralPath $IdentityPath) {
            # A PC that was enrolled before. Its old credential is kept beside the new one
            # rather than deleted, so a mistake here is recoverable by hand; the hub simply
            # stops hearing from the old agent id.
            Move-Item -LiteralPath $IdentityPath -Destination ($IdentityPath + ".previous-" + (Get-Date -Format 'yyyyMMddHHmmss')) -Force
        }
        # Written to a temp name, locked down, THEN moved into place, so there is no moment at
        # which the token sits in a file an ordinary user can read. A move on one volume keeps
        # the file's own ACL.
        $tmp = $IdentityPath + ".tmp"
        $json = @{ agent_id = [string]$identity.agent_id; token = [string]$identity.token } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {   # SYSTEM, BUILTIN\Administrators
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
                (New-Object System.Security.Principal.SecurityIdentifier($sid)), 'FullControl', 'Allow')
            $acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $tmp -AclObject $acl
        Move-Item -LiteralPath $tmp -Destination $IdentityPath -Force
        $identity = $null; $json = $null

        Start-Service -Name $ServiceName
        Write-Host "`nDone. $env:COMPUTERNAME is enrolled and should appear in FleetHub within a minute." -ForegroundColor Green
    } catch {
        Write-Host "`nFleetHub onboarding failed: $($_.Exception.Message)" -ForegroundColor Red
    }
}
