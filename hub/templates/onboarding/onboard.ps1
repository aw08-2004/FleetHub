# FleetHub onboarding script (roadmap #29). Rendered by the hub for ONE onboarding code;
# see hub/onboarding_web.py. Run it in an elevated PowerShell:
#
#     irm <this url> | iex
#
# or double-click the .cmd launcher the onboarding page offers, which elevates first.
#
# What it does, in this order, and why the order:
#   1. Installs the agent with agent-install.ps1 and NO enrollment secret. The installer
#      checks the binary against the signed manifest, exactly as a technician's install does.
#   2. Only then spends the code. A failed install must not burn a one-time code.
#   3. Writes the credential the hub returned to agent.json, readable only by SYSTEM and
#      Administrators, and restarts the service so the agent loads it.
# The token goes from the hub straight into that file. It is never printed.
#
# Wrapped in a script block so `irm | iex` can stop on failure without closing the window
# it was pasted into -- `exit` here would end the operator's elevated session.
& {
    $ErrorActionPreference = 'Stop'
    $HubUrl       = {{ hub_url }}
    $Code         = {{ code }}
    $InstallerUrl = {{ installer_url }}
    $ServiceName  = 'TempMonitorAgent'
    $StateDir     = Join-Path $env:ProgramData 'FleetHub\Agent'
    $IdentityPath = Join-Path $StateDir 'agent.json'

    function Show-Info($msg) { Write-Host "  $msg" }
    function Show-Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
    function Fail($msg) { throw [System.Exception]::new($msg) }

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
        $installer = Join-Path ([IO.Path]::GetTempPath()) ("fleethub-agent-install-" + [guid]::NewGuid().ToString('N') + ".ps1")
        Invoke-WebRequest -UseBasicParsing -Uri $InstallerUrl -OutFile $installer
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -HubUrl $HubUrl
            if ($LASTEXITCODE -ne 0) { Fail "The agent installer failed (exit code $LASTEXITCODE). The onboarding code was NOT used; fix the problem above and run this again." }
        } finally {
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
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
