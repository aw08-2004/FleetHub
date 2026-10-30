<#
    FleetHub - C#/.NET Fleet Agent Installer
    https://github.com/aw08-2004/FleetHub

    Installs the fleet agent as a Windows Service running under LocalSystem:
      - PawnIO kernel driver (needed by the in-process LibreHardwareMonitorLib for
        sensor access; skipped if already present)
      - The self-contained agent exe -> C:\Program Files\FleetHub\Agent\FleetHubAgent.exe
        (TempMonitorAgent.exe before 2026-10-13; an existing one is removed once the service
        points at the new name)
      - A Windows Service "FleetHubAgent" (Automatic), with SCM failure-recovery
        set to restart -- this is what the agent's self-update relies on when it
        exits with code 17 to swap onto a new binary. A machine still registered under
        the pre-rename name "TempMonitorAgent" is moved onto the new one.
      - The one-time enrollment secret into HKLM\SOFTWARE\FleetHub\Agent

    Unlike the removed Python companion (a per-logon Scheduled Task in a user session),
    this runs in session 0 as SYSTEM, so restart/rename/gpupdate/scripts work with no one
    logged in.

    With neither -AgentUrl nor -AgentExe, the binary is found the way the agent's own
    SelfUpdater finds one: the Ed25519-signed agent.manifest.json, verified against the fleet
    key, then the download checked against the SIGNED sha256. See "signed manifest" below.

    Usage (elevated PowerShell):
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 -AgentExe .\FleetHubAgent.exe -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 `
            -AgentUrl <release-asset-url> -EnrollmentSecret <secret>      # NOT verified
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 -Uninstall
#>

param(
    [switch]$Uninstall,
    [string]$InstallDir = "C:\Program Files\FleetHub\Agent",
    [string]$AgentUrl,                       # download URL for the agent exe -- NOT verified
    [string]$AgentExe,                       # OR a local path to the agent exe -- NOT verified
    [string]$ManifestUrl,                    # signed manifest from a mirror; still verified
    [string]$EnrollmentSecret,               # shared secret for POST /api/agent/enroll
    [string]$HubUrl                          # optional hub base override (FLEETHUB_HUB)
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# The service is FleetHubAgent (renamed 2026-10-12). Machines installed before that are
# registered as TempMonitorAgent until this installer runs on them again, and are migrated
# then (step 4). **The binary does not care which name it runs under**: for an own-process
# service the SCM ignores the name the process hands StartServiceCtrlDispatcher, so the
# ServiceName in Program.cs is cosmetic and a 3.43.0 exe runs fine as FleetHubAgent. An
# earlier comment here said the two had to move in lockstep; that was wrong, and is why the
# rename waited.
#
# Deliberately NOT migrated by the agent's own self-update: a service deleted while running
# is only marked for deletion, and the SCM neither starts nor runs recovery on one so marked
# -- the exit-17 restart would leave the PC dead until someone visits it. The installer
# migrates with the old service stopped and someone at the keyboard. See ROADMAP.MD, "Agent
# service renamed to FleetHubAgent".
$ServiceName       = "FleetHubAgent"
$LegacyServiceName = "TempMonitorAgent"
# The exe followed on 2026-10-13. Same rule as the service: an existing install is renamed
# when this installer runs on it, never by self-update, which writes each new build over the
# path that is already running -- so a PC nobody re-installs keeps TempMonitorAgent.exe, and
# that is fine, because nothing finds the agent by its file name. The service's binPath is
# the record of which one a PC runs.
$ExeName        = "FleetHubAgent.exe"
$ExePath        = Join-Path $InstallDir $ExeName
$LegacyExeName  = "TempMonitorAgent.exe"
$LegacyExePath  = Join-Path $InstallDir $LegacyExeName
$RegPath        = "HKLM:\SOFTWARE\FleetHub\Agent"
$LegacyRegPath  = "HKLM:\SOFTWARE\TempMonitorAgent"
$LegacyInstall  = "C:\Program Files\TempMonitorAgent"
# Agent state, as AgentConfig.ProgramDataDir resolves it: the FleetHub path, and the
# pre-rename one it migrates from. Named here so the uninstall can drop the enrollment
# identity -- everything else under them is logs and is deliberately kept.
$StateDir       = Join-Path $env:ProgramData "FleetHub\Agent"
$LegacyStateDir = Join-Path $env:ProgramData "TempMonitorAgent"
$PawnIoUrl   = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/refs/heads/master/LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# Stop a service and WAIT until it has stopped. sc.exe stop only asks; the blind two-second
# sleep this replaced could overwrite the exe, or start the renamed service, while the old
# process was still shutting down -- two agents heartbeating on one identity for a moment.
function Stop-AgentService([string]$name) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    if ($svc.Status -ne "Stopped") {
        & sc.exe stop $name | Out-Null
        try { $svc.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30)) }
        catch { Warn "$name did not report Stopped within 30s; continuing." }
    }
}

#region signed-manifest
# ----------------------------------------------------------------------
# Signed manifest
# ----------------------------------------------------------------------
# **The installer applies the check the agent's SelfUpdater applies.** Until this existed a
# first install asked the GitHub releases API for the newest agent-v* asset and ran whatever
# came back as LocalSystem -- so the fleet key, which every self-update verifies fail-closed,
# protected every step EXCEPT the one that first runs the agent. install.sh had the same gap
# and closed it in PR #91 (CWE-494); this is the Windows half, mirrored step for step from
# agent/src/TempMonitorAgent/Update/SelfUpdater.cs:
#   1. fetch agent.manifest.json and its detached .sig, as BYTES;
#   2. verify the Ed25519 signature over those exact bytes with the pinned key;
#   3. only then parse the manifest for version, sha256 and url;
#   4. download url and refuse it unless its sha256 equals the SIGNED value.
# Fail closed at every step: nothing here falls back to an unverified download.
#
# **Why a hand-written Ed25519 verifier.** Windows PowerShell 5.1 runs on .NET Framework,
# which has no Ed25519, and CNG exposes Curve25519 for key agreement only. The agent carries
# BouncyCastle, but on a first install there is no agent yet to borrow it from. So this is
# the RFC 8032 verification equation over System.Numerics.BigInteger: slow (a few hundred
# milliseconds, once) and short enough to review against the RFC line by line.
# tests/test_windows_installer.py proves it agrees with a real implementation on random keys
# and on the production signature committed in this repo.
#
# Rejected: shipping BouncyCastle.dll next to this script (a second download, from the same
# place, to verify the first -- and a binary nobody reviews); verifying the binary's
# Authenticode signature instead (the agent is not Authenticode-signed, and the fleet's trust
# root is the offline Ed25519 key, not a CA); keeping the releases-API lookup and verifying
# afterwards (the manifest already names the one allowed URL, and the API could also hand
# back a beta prerelease as "the newest"); a parameter to supply the key (that would make the
# trust root whatever the person running the command types, which is the thing removed).
#
# This region is loaded on its own by tests/test_windows_installer.py. Keep it free of side
# effects and of the Say/Die helpers above -- failures are thrown, and the caller dies.

# The fleet's release-signing public key. **Must equal AgentConfig.UpdatePublicKeyHex** in
# both agents and UPDATE_PUBLIC_KEY_HEX in install.sh; tests/test_windows_installer.py fails
# if it drifts. Deliberately not a parameter -- see above.
$UpdatePublicKeyHex = "9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c"

# AgentConfig.StableManifestUrl -- the same file, so a machine is installed from exactly what
# it would later update from. Stable, never beta: a first install is not where to opt in.
$StableManifestUrl = "https://raw.githubusercontent.com/aw08-2004/FleetHub/main/agent/agent.manifest.json"

$Ed25519Source = @'
using System;
using System.Numerics;
using System.Security.Cryptography;

namespace FleetHubInstaller
{
    // RFC 8032 section 5.1.7, cofactorless: accept iff encode([S]B - [k]A) == R, with S < L
    // and A a valid point. Points are extended coordinates (X, Y, Z, T), section 5.1.4.
    // C# 5 only: Windows PowerShell's Add-Type compiles with the .NET Framework compiler.
    public static class Ed25519
    {
        static readonly BigInteger P = BigInteger.Pow(2, 255) - 19;
        static readonly BigInteger L = BigInteger.Pow(2, 252)
            + BigInteger.Parse("27742317777372353535851937790883648493");
        static readonly BigInteger D = Mod(-121665 * Inv(121666));
        static readonly BigInteger SqrtM1 = BigInteger.ModPow(2, (P - 1) / 4, P);
        static readonly BigInteger[] Base = BasePoint();

        static BigInteger Mod(BigInteger a) { BigInteger r = a % P; return r.Sign < 0 ? r + P : r; }
        static BigInteger Inv(BigInteger a) { return BigInteger.ModPow(Mod(a), P - 2, P); }

        // Little-endian unsigned: BigInteger's byte constructor is two's complement, so a
        // zero byte on top keeps a set high bit from reading as negative.
        static BigInteger FromLE(byte[] b, int off, int len)
        {
            byte[] t = new byte[len + 1];
            Array.Copy(b, off, t, 0, len);
            return new BigInteger(t);
        }

        static BigInteger[] BasePoint()
        {
            BigInteger y = Mod(4 * Inv(5));
            return new[] { RecoverX(y, 0), y, BigInteger.One, Mod(RecoverX(y, 0) * y) };
        }

        // Section 5.1.3. Returns -1 when y does not lie on the curve.
        static BigInteger RecoverX(BigInteger y, int sign)
        {
            if (y >= P) return BigInteger.MinusOne;
            BigInteger x2 = Mod((y * y - 1) * Inv(D * y * y + 1));
            if (x2.IsZero) return sign == 1 ? BigInteger.MinusOne : BigInteger.Zero;
            BigInteger x = BigInteger.ModPow(x2, (P + 3) / 8, P);
            if (!Mod(x * x - x2).IsZero) x = Mod(x * SqrtM1);
            if (!Mod(x * x - x2).IsZero) return BigInteger.MinusOne;
            if ((int)(x & 1) != sign) x = P - x;
            return x;
        }

        static BigInteger[] Decode(byte[] s, int off)
        {
            byte[] b = new byte[32];
            Array.Copy(s, off, b, 0, 32);
            int sign = b[31] >> 7;
            b[31] &= 0x7f;
            BigInteger y = FromLE(b, 0, 32);
            BigInteger x = RecoverX(y, sign);
            if (x.Sign < 0) return null;
            return new[] { x, y, BigInteger.One, Mod(x * y) };
        }

        // The unified addition law; complete on this curve, so it also doubles.
        static BigInteger[] Add(BigInteger[] p, BigInteger[] q)
        {
            BigInteger a = Mod((p[1] - p[0]) * (q[1] - q[0]));
            BigInteger b = Mod((p[1] + p[0]) * (q[1] + q[0]));
            BigInteger c = Mod(2 * p[3] * q[3] * D);
            BigInteger d = Mod(2 * p[2] * q[2]);
            BigInteger e = b - a, f = d - c, g = d + c, h = b + a;
            return new[] { Mod(e * f), Mod(g * h), Mod(f * g), Mod(e * h) };
        }

        static BigInteger[] Mul(BigInteger s, BigInteger[] p)
        {
            BigInteger[] q = { BigInteger.Zero, BigInteger.One, BigInteger.One, BigInteger.Zero };
            while (s.Sign > 0)
            {
                if (!s.IsEven) q = Add(q, p);
                p = Add(p, p);
                s >>= 1;
            }
            return q;
        }

        static byte[] Encode(BigInteger[] p)
        {
            BigInteger zi = Inv(p[2]);
            BigInteger x = Mod(p[0] * zi), y = Mod(p[1] * zi);
            byte[] raw = y.ToByteArray();
            byte[] o = new byte[32];
            Array.Copy(raw, 0, o, 0, Math.Min(32, raw.Length));
            if (!x.IsEven) o[31] |= 0x80;
            return o;
        }

        public static bool Verify(byte[] publicKey, byte[] message, byte[] signature)
        {
            if (publicKey == null || message == null || signature == null) return false;
            if (publicKey.Length != 32 || signature.Length != 64) return false;
            BigInteger[] a = Decode(publicKey, 0);
            if (a == null) return false;
            // S >= L is the malleable twin of a valid signature; RFC 8032 requires refusing it.
            BigInteger s = FromLE(signature, 32, 32);
            if (s >= L) return false;
            byte[] buf = new byte[64 + message.Length];
            Array.Copy(signature, 0, buf, 0, 32);
            Array.Copy(publicKey, 0, buf, 32, 32);
            Array.Copy(message, 0, buf, 64, message.Length);
            byte[] h;
            using (SHA512 sha = SHA512.Create()) h = sha.ComputeHash(buf);
            BigInteger k = FromLE(h, 0, 64) % L;
            BigInteger[] negA = { Mod(-a[0]), a[1], a[2], Mod(-a[3]) };
            byte[] check = Encode(Add(Mul(s, Base), Mul(k, negA)));
            for (int i = 0; i < 32; i++)
                if (check[i] != signature[i]) return false;
            return true;
        }
    }
}
'@

function Initialize-Ed25519 {
    if ('FleetHubInstaller.Ed25519' -as [type]) { return }
    # PowerShell 7 compiles against the whole reference set already; 5.1 needs System.Numerics
    # named, and naming it on 7 is an error. install.ps1 may run this script in either.
    if ($PSVersionTable.PSEdition -eq 'Core') {
        Add-Type -TypeDefinition $Ed25519Source
    } else {
        Add-Type -TypeDefinition $Ed25519Source -ReferencedAssemblies System.Numerics
    }
}

# Hex text -> bytes, or $null for anything that is not an even run of hex digits. Whitespace
# is tolerated because a .sig file may end in a newline; nothing else is.
function ConvertFrom-HexString([string]$hex) {
    if ($null -eq $hex) { return $null }
    $clean = $hex -replace '\s', ''
    if ($clean.Length -eq 0 -or $clean.Length % 2 -ne 0 -or $clean -notmatch '^[0-9a-fA-F]+$') { return $null }
    $bytes = New-Object byte[] ($clean.Length / 2)
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        $bytes[$i] = [Convert]::ToByte($clean.Substring($i * 2, 2), 16)
    }
    return ,$bytes
}

# $true only if $SignatureHex is a valid Ed25519 signature by $UpdatePublicKeyHex over the
# exact bytes in $Message.
function Test-ManifestSignature([byte[]]$Message, [string]$SignatureHex) {
    $key = ConvertFrom-HexString $UpdatePublicKeyHex
    $sig = ConvertFrom-HexString $SignatureHex
    if ($null -eq $Message -or $null -eq $key -or $null -eq $sig) { return $false }
    if ($key.Length -ne 32 -or $sig.Length -ne 64) { return $false }
    Initialize-Ed25519
    return [FleetHubInstaller.Ed25519]::Verify($key, $Message, $sig)
}

# Fetch, verify and download into $Destination, or throw with the reason. $Destination exists
# afterwards only if every check passed. Returns the signed version and sha256: the caller
# re-hashes the installed copy against the latter (see "Installing agent binary").
function Get-VerifiedAgent([string]$Destination, [string]$FromManifestUrl) {
    if (-not $FromManifestUrl) { $FromManifestUrl = $StableManifestUrl }
    # The progress bar on 5.1 costs more than the transfer on an agent-sized download.
    $ProgressPreference = 'SilentlyContinue'
    $work = Join-Path ([IO.Path]::GetTempPath()) ("fleethub-manifest-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    try {
        $mPath = Join-Path $work "manifest.json"
        $sPath = Join-Path $work "manifest.json.sig"
        try {
            # -OutFile, never Invoke-RestMethod: the signature covers bytes, and anything that
            # decodes and re-reads the text is checking something other than what was signed.
            Invoke-WebRequest -Uri $FromManifestUrl -OutFile $mPath -UseBasicParsing
            Invoke-WebRequest -Uri "$FromManifestUrl.sig" -OutFile $sPath -UseBasicParsing
        } catch {
            throw "could not fetch the signed agent manifest from $FromManifestUrl (or its .sig): $($_.Exception.Message). Use -AgentExe with a locally built agent, or -AgentUrl for an unverified download."
        }
        $mBytes = [IO.File]::ReadAllBytes($mPath)
        $sigHex = [IO.File]::ReadAllText($sPath)
        if (-not (Test-ManifestSignature $mBytes $sigHex)) {
            throw "the agent manifest at $FromManifestUrl is NOT signed by the fleet's key. Refusing to install. Nothing on this machine was changed."
        }
        # Parsed only AFTER the signature verified, so a missing field is a signing bug.
        $m = [Text.Encoding]::UTF8.GetString($mBytes) | ConvertFrom-Json
        $want = "$($m.sha256)".ToLowerInvariant()
        if (-not $m.version -or -not $m.url -or $want -notmatch '^[0-9a-f]{64}$') {
            throw "the signed manifest is missing its version, url or sha256 -- a release-signing bug."
        }
        Write-Host "  manifest verified: version $($m.version), signed by the fleet key"
        Write-Host "  binary  <- $($m.url)"
        $bPath = Join-Path $work "agent.exe"
        try {
            Invoke-WebRequest -Uri $m.url -OutFile $bPath -UseBasicParsing
        } catch {
            throw "download failed: $($m.url): $($_.Exception.Message)"
        }
        $got = (Get-FileHash -Path $bPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($got -ne $want) {
            throw "the downloaded binary does not match the signed manifest (sha256 $got, expected $want). Refusing to install."
        }
        Write-Host "  binary  sha256 matches the signed manifest"
        Move-Item -Path $bPath -Destination $Destination -Force
        return [pscustomobject]@{ Version = "$($m.version)"; Sha256 = $want }
    } finally {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
#endregion signed-manifest

# ----------------------------------------------------------------------
# Elevate
# ----------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Elevating..." -ForegroundColor Yellow
    if ($PSCommandPath) {
        $argList = @("-ExecutionPolicy","Bypass","-File","`"$PSCommandPath`"")
        foreach ($key in $PSBoundParameters.Keys) {
            $val = $PSBoundParameters[$key]
            if ($val -is [switch]) { if ($val.IsPresent) { $argList += "-$key" } }
            else { $argList += "-$key"; $argList += "`"$val`"" }
        }
        Start-Process powershell -Verb RunAs -ArgumentList $argList
    } else {
        Die "Re-run from a local .ps1 file as administrator."
    }
    exit
}

# ----------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------
if ($Uninstall) {
    Step "Uninstalling $ServiceName"

    # Both names: a machine not re-installed since the rename is still TempMonitorAgent, and
    # one whose migration was interrupted could have both. Missing one leaves an agent running
    # with nothing on disk behind it, restarted by its own recovery actions every minute.
    $removed = $false
    foreach ($name in @($ServiceName, $LegacyServiceName)) {
        if (-not (Get-Service -Name $name -ErrorAction SilentlyContinue)) { continue }
        Stop-AgentService $name
        & sc.exe delete $name | Out-Null
        Ok "Removed service $name"
        $removed = $true
    }
    if (-not $removed) { Say "Service not present." }

    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        Ok "Deleted $InstallDir"
    }

    # The enrollment identity is the one piece of state that must NOT survive, and it is not
    # in $InstallDir -- it lives in %ProgramData% alongside the logs. Leaving it is what makes
    # "uninstall, then reinstall with the same secret" silently do nothing on a machine the
    # hub has since deleted: the agent finds agent.json, decides it is already enrolled, and
    # never asks the hub again. Both paths, because the agent migrates its state dir from the
    # pre-rename location and would otherwise copy a stale identity forward.
    foreach ($stateDir in @($StateDir, $LegacyStateDir)) {
        $identity = Join-Path $stateDir "agent.json"
        if (Test-Path $identity) {
            Remove-Item $identity -Force -ErrorAction SilentlyContinue
            Ok "Removed enrollment identity $identity"
        }
    }

    # Hand Windows Update back (issue #140). An agent that had taken update control wrote
    # NoAutoUpdate and SetDisableUXWUAccess; uninstalling it without removing them leaves a PC
    # that neither Windows nor FleetHub will ever patch. Same rules as UpdateControl.Release
    # in the agent: only values listed in the marker, only while they still hold the 1 we
    # wrote (a GPO that took one over owns it now), then the marker itself. A PC whose policy
    # came from GPO or Intune has no marker entries, so nothing of theirs is touched.
    $policyRoot = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
    $markerKey  = "HKLM:\SOFTWARE\FleetHub\UpdateControl"
    $marked = (Get-ItemProperty -LiteralPath $markerKey -Name Written -ErrorAction SilentlyContinue).Written
    if ($marked) {
        foreach ($id in @($marked)) {
            # Ids are the path below the policy root, e.g. "AU\NoAutoUpdate". Anything that
            # tries to climb out of it is not one this agent wrote, so it is skipped.
            if ($id -match '\.\.' -or $id -match '^\\') { continue }
            $parent = Split-Path $id -Parent
            $name   = Split-Path $id -Leaf
            $key    = if ($parent) { Join-Path $policyRoot $parent } else { $policyRoot }
            $value  = (Get-ItemProperty -LiteralPath $key -Name $name -ErrorAction SilentlyContinue).$name
            if ($value -is [int] -and $value -eq 1) {
                Remove-ItemProperty -LiteralPath $key -Name $name -ErrorAction SilentlyContinue
                Ok "Released Windows Update policy $id"
            }
        }
    }
    if (Test-Path -LiteralPath $markerKey) {
        Remove-Item -LiteralPath $markerKey -Recurse -Force -ErrorAction SilentlyContinue
    }

    # And the Group Policy registry values (roadmap #32). GroupPolicyRunner.Release in the agent
    # is the full rule -- it also leaves a value somebody changed after FleetHub set it -- and
    # runs when the hub uninstalls the agent. This is the installer's copy, for an uninstall
    # by hand: it removes only values listed in the marker, only under SOFTWARE\Policies, and
    # never the FleetHub or Windows Update keys. A domain GPO's values are never on the marker.
    $gpoMarkerKey = "HKLM:\SOFTWARE\FleetHub\GroupPolicy"
    $gpoMarked = (Get-ItemProperty -LiteralPath $gpoMarkerKey -Name Written -ErrorAction SilentlyContinue).Written
    if ($gpoMarked) {
        # Since agent 3.50 the values live in the PC's local GPO (Machine\Registry.pol), and
        # Windows writes them into the registry from there -- so deleting them from the
        # registry alone would see them put back at the next policy refresh. The marked
        # entries come out of the file first: every other entry is copied byte for byte (it is
        # whoever ran gpedit.msc's), and a file this cannot read is left exactly as it is.
        $gpRoot = Join-Path $env:SystemRoot "System32\GroupPolicy"
        $pol = Join-Path $gpRoot "Machine\Registry.pol"
        if (Test-Path -LiteralPath $pol) {
            try {
                $ours = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
                foreach ($entry in @($gpoMarked)) { [void]$ours.Add($entry) }
                $bytes = [IO.File]::ReadAllBytes($pol)
                $unicode = [Text.Encoding]::Unicode
                if ($bytes.Length -lt 8 -or [Text.Encoding]::ASCII.GetString($bytes, 0, 4) -ne "PReg" -or
                    [BitConverter]::ToUInt32($bytes, 4) -ne 1) { throw "not a version 1 Registry.pol" }
                $kept = [IO.MemoryStream]::new()
                $kept.Write($bytes, 0, 8)
                $at = 8
                $removed = 0
                # [key NUL ; name NUL ; type ; size ; data] -- brackets and semicolons UTF-16LE.
                while ($at -lt $bytes.Length) {
                    $start = $at
                    if ([BitConverter]::ToUInt16($bytes, $at) -ne 0x5B) { throw "unexpected byte at $at" }
                    $at += 2
                    $from = $at
                    while ([BitConverter]::ToUInt16($bytes, $at) -ne 0) { $at += 2 }
                    $key = $unicode.GetString($bytes, $from, $at - $from)
                    if ([BitConverter]::ToUInt16($bytes, $at + 2) -ne 0x3B) { throw "unexpected byte at $at" }
                    $at += 4
                    $from = $at
                    while ([BitConverter]::ToUInt16($bytes, $at) -ne 0) { $at += 2 }
                    $name = $unicode.GetString($bytes, $from, $at - $from)
                    $at += 2
                    $typed = [BitConverter]::ToUInt16($bytes, $at) -eq 0x3B
                    if ($typed) {
                        $size = [BitConverter]::ToUInt32($bytes, $at + 8)
                        $at += 14 + $size
                    }
                    if ($at + 2 -gt $bytes.Length -or [BitConverter]::ToUInt16($bytes, $at) -ne 0x5D) {
                        throw "unexpected byte at $at"
                    }
                    $at += 2
                    if ($typed -and $ours.Contains("$key|$name")) { $removed++; continue }
                    $kept.Write($bytes, $start, $at - $start)
                }
                if ($removed -gt 0) {
                    [IO.File]::WriteAllBytes($pol, $kept.ToArray())
                    # The Group Policy service skips a GPO whose version did not move: bump the
                    # computer half (low word), as GptIni.Bump does in the agent.
                    $ini = Join-Path $gpRoot "gpt.ini"
                    if (Test-Path -LiteralPath $ini) {
                        $lines = [IO.File]::ReadAllLines($ini, [Text.Encoding]::GetEncoding(28591))
                        for ($i = 0; $i -lt $lines.Count; $i++) {
                            if ($lines[$i] -match '^\s*Version\s*=\s*(\d+)\s*$') {
                                # Arithmetic, not -band: PowerShell reads 0xFFFF0000 as a negative int.
                                $version = [int64]$Matches[1]
                                $machine = $version % 65536 + 1
                                if ($machine -gt 65535) { $machine = 1 }
                                $lines[$i] = "Version=$($version - $version % 65536 + $machine)"
                            }
                        }
                        [IO.File]::WriteAllLines($ini, $lines, [Text.Encoding]::GetEncoding(28591))
                    }
                    & gpupdate.exe /target:computer /wait:0 | Out-Null
                    Ok "Removed $removed FleetHub value(s) from the local Group Policy"
                }
            } catch {
                Warn "Left the local Group Policy as it is ($($_.Exception.Message)); its FleetHub values stay until removed in gpedit.msc"
            }
        }
        foreach ($entry in @($gpoMarked)) {
            $at = $entry.IndexOf('|')
            if ($at -le 0) { continue }
            $path = $entry.Substring(0, $at)
            $name = $entry.Substring($at + 1)
            if ($path -notmatch '^SOFTWARE\\Policies\\.' -or $path -match '\.\.' -or
                $path -match '^SOFTWARE\\Policies\\(FleetHub|Microsoft\\Windows\\WindowsUpdate)(\\|$)') { continue }
            Remove-ItemProperty -LiteralPath "HKLM:\$path" -Name $name -ErrorAction SilentlyContinue
            Ok "Removed Group Policy value $path\$name"
        }
    }
    if (Test-Path -LiteralPath $gpoMarkerKey) {
        Remove-Item -LiteralPath $gpoMarkerKey -Recurse -Force -ErrorAction SilentlyContinue
    }

    Warn "Left PawnIO driver and the rest of %ProgramData%\FleetHub\Agent (logs) in place."
    Write-Host "`nDone.`n" -ForegroundColor Green
    exit
}

Write-Host @"

  FleetHub - Fleet Agent Installer
  Machine: $env:COMPUTERNAME
  Target : $InstallDir

"@ -ForegroundColor Cyan

# ----------------------------------------------------------------------
# 1. PawnIO driver (sensor access for the in-process LibreHardwareMonitorLib)
# ----------------------------------------------------------------------
Step "Installing PawnIO driver"
if (Get-Service -Name "PawnIO" -ErrorAction SilentlyContinue) {
    Ok "Already installed, skipping"
} else {
    $pawnioPath = Join-Path $env:TEMP "PawnIO_setup.exe"
    Say "Downloading $PawnIoUrl"
    Invoke-WebRequest -Uri $PawnIoUrl -OutFile $pawnioPath -UseBasicParsing
    Unblock-File -Path $pawnioPath -ErrorAction SilentlyContinue
    $proc = Start-Process -FilePath $pawnioPath -ArgumentList "-install","-silent" -Wait -PassThru -NoNewWindow
    Remove-Item $pawnioPath -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { Warn "PawnIO installer exited $($proc.ExitCode). Sensors may be unreadable." }
    else { Ok "PawnIO installed" }
}

# ----------------------------------------------------------------------
# 2. Agent binary
# ----------------------------------------------------------------------
# Fetched and verified BEFORE the running service is stopped, so a failed download or a
# refused signature leaves a working agent running rather than a machine with none. (It used
# to stop the service first and download straight over the exe.)
Step "Fetching agent binary"
$staged = $null
$signedSha256 = $null
if ($AgentExe) {
    if (-not (Test-Path $AgentExe)) { Die "AgentExe not found: $AgentExe" }
    $source = $AgentExe
    Say "binary  <- $AgentExe (local, not checked against the fleet key)"
} else {
    $staged = Join-Path ([IO.Path]::GetTempPath()) ("fleethub-agent-" + [guid]::NewGuid().ToString('N') + ".exe")
    $source = $staged
    if ($AgentUrl) {
        Say "Downloading $AgentUrl"
        Invoke-WebRequest -Uri $AgentUrl -OutFile $staged -UseBasicParsing
        Warn "-AgentUrl is NOT checked against the fleet's signing key. Omit it to install the signed release."
    } else {
        try {
            $verified = Get-VerifiedAgent -Destination $staged -FromManifestUrl $ManifestUrl
            $signedSha256 = $verified.Sha256
            Ok "Agent $($verified.Version) verified against the fleet key"
        } catch {
            Remove-Item $staged -Force -ErrorAction SilentlyContinue
            Die $_.Exception.Message
        }
    }
}

Step "Installing agent binary"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# **Staged beside the exe and hashed again THERE, before the running agent is touched.** The
# copy in %TEMP% sat in a directory any UNELEVATED process of this same user can write to, so
# the bytes Get-VerifiedAgent hashed are not necessarily the bytes copied (CWE-367, review on
# PR #96). $InstallDir inherits Program Files' ACL, which that process cannot write, so the
# hash below covers what the service will start -- and because it runs before the stop, a
# swapped file is refused with the old agent still running.
# The first fix hashed $ExePath after the copy instead; review pointed out that a mismatch
# there had already stopped the service and overwritten the exe, so refusing left the PC with
# no agent at all. Under a custom -InstallDir outside Program Files the staging is only as
# protected as that directory, which is also all the installed exe ever is.
# `.incoming`, not `.new` or `.old`: SelfUpdater owns `.old` and clears it on its own schedule.
$incoming = "$ExePath.incoming"
Copy-Item -Path $source -Destination $incoming -Force
if ($staged) { Remove-Item $staged -Force -ErrorAction SilentlyContinue }
if ($signedSha256) {
    $got = (Get-FileHash -Path $incoming -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($got -ne $signedSha256) {
        Remove-Item $incoming -Force -ErrorAction SilentlyContinue
        Die "the agent binary changed after it was verified (sha256 $got, signed $signedSha256). Refusing to install; the running agent was not touched."
    }
}

# Stop an existing service before overwriting its exe.
# Under either name: a machine not yet migrated is still TempMonitorAgent.
Stop-AgentService $ServiceName
Stop-AgentService $LegacyServiceName

# A ticket tray (roadmap #31) runs from the same exe, as the signed-in user, and closes when
# the service does -- unless somebody has a ticket window open, which keeps it alive on
# purpose so their text survives an agent update. A reinstall has to replace the exe, which a
# running tray holds open, so stop any that is still there. Both exe names, and only processes
# running from this install, never a FleetHubAgent.exe somebody runs from elsewhere.
foreach ($name in @($ExeName, $LegacyExeName)) {
    foreach ($dir in @($InstallDir, $LegacyInstall)) {
        $path = Join-Path $dir $name
        Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $path } |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

# Pre-rename installs live at C:\Program Files\TempMonitorAgent. Move them under the
# shared FleetHub root so hub and agent sit together, and point the existing service
# registration at the new path (below) rather than leaving a second copy behind.
# %ProgramData% state and the enrollment secret are handled by the agent itself.
if ($InstallDir -ne $LegacyInstall -and (Test-Path $LegacyInstall)) {
    Say "Migrating existing install from $LegacyInstall"
    foreach ($stale in @("$ExeName.old", "$LegacyExeName.old")) {
        Remove-Item (Join-Path $LegacyInstall $stale) -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -File $LegacyInstall -ErrorAction SilentlyContinue | ForEach-Object {
        Move-Item $_.FullName (Join-Path $InstallDir $_.Name) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $LegacyInstall -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Moved agent to $InstallDir"
}

# A rename within one directory: the verified bytes become the exe without passing through
# anywhere writable by anyone else.
Move-Item -Path $incoming -Destination $ExePath -Force
Unblock-File -Path $ExePath -ErrorAction SilentlyContinue
Ok "binary  -> $ExePath"

# ----------------------------------------------------------------------
# 3. Configuration (enrollment secret + optional overrides)
# ----------------------------------------------------------------------
Step "Writing configuration"
New-Item -Path $RegPath -Force | Out-Null

# Carry a secret written by a pre-rename installer forward, so re-running this on an
# enrolled machine without -EnrollmentSecret doesn't silently drop it back to
# telemetry-only. The agent reads both keys, but consolidating here means the legacy
# key can eventually be retired.
$secret = $EnrollmentSecret
if (-not $secret) {
    $secret = (Get-ItemProperty -Path $LegacyRegPath -Name "EnrollmentSecret" -ErrorAction SilentlyContinue).EnrollmentSecret
    if ($secret) { Say "Reusing the enrollment secret from $LegacyRegPath" }
}
if ($secret) {
    New-ItemProperty -Path $RegPath -Name "EnrollmentSecret" -Value $secret -PropertyType String -Force | Out-Null
    Ok "Enrollment secret stored in $RegPath"
} else {
    Warn "No -EnrollmentSecret given; the agent will run telemetry-only until enrolled."
}

if ($HubUrl) {
    [Environment]::SetEnvironmentVariable("FLEETHUB_HUB", $HubUrl, "Machine")
    # Clear the pre-rename variable so the two can't drift apart and leave an operator
    # wondering which one the agent is honouring (it prefers FLEETHUB_HUB).
    if ([Environment]::GetEnvironmentVariable("TEMP_MONITOR_HUB", "Machine")) {
        [Environment]::SetEnvironmentVariable("TEMP_MONITOR_HUB", $null, "Machine")
    }
    Ok "Hub override: $HubUrl"
}
# Commands are no longer signed, so this machine-level key is dead config. Clear a
# stale one left by a pre-1.10 install rather than leaving it to confuse the next
# person who greps the environment for it.
if ([Environment]::GetEnvironmentVariable("COMMAND_SIGNING_PUBLIC_KEY_HEX", "Machine")) {
    [Environment]::SetEnvironmentVariable("COMMAND_SIGNING_PUBLIC_KEY_HEX", $null, "Machine")
    Ok "Removed obsolete COMMAND_SIGNING_PUBLIC_KEY_HEX (commands are no longer signed)"
}

# ----------------------------------------------------------------------
# 4. Service (LocalSystem, Automatic) + failure recovery
# ----------------------------------------------------------------------
Step "Registering Windows Service"
$legacy = Get-Service -Name $LegacyServiceName -ErrorAction SilentlyContinue
# **Order matters on a machine being migrated**: the new service is created and given its
# recovery actions FIRST, and the old one is deleted only after that worked. Deleting first
# would leave a PC with no agent service at all if New-Service then failed; this way a failure
# puts the old service back as it was. Both are stopped by now (step 2), so the two never run
# at once, and the old one is deleted while stopped, so a recovery restart pending on it
# cannot bring it back.
try {
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Say "Service exists; updating binary path."
        # binPath must be re-pointed after a migration moved the exe under the shared root.
        & sc.exe config $ServiceName binPath= "`"$ExePath`"" start= auto DisplayName= "FleetHub Agent" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "sc.exe config exited $LASTEXITCODE" }
    } else {
        New-Service -Name $ServiceName -BinaryPathName "`"$ExePath`"" `
            -DisplayName "FleetHub Agent" -StartupType Automatic `
            -Description "Reports telemetry and executes fleet commands for FleetHub (RMM)." | Out-Null
        Ok "Created service $ServiceName"
    }

    # Restart on failure -- the self-update exits with code 17 and relies on this.
    & sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "sc.exe failure exited $LASTEXITCODE" }
    Ok "Failure recovery: restart x3 @ 60s"
} catch {
    if ($legacy) {
        # Falling back to the old service means the new one must not survive, however far it
        # got: a FleetHubAgent left registered (Automatic, possibly without recovery actions)
        # would start beside TempMonitorAgent on the next boot -- two agents on one identity.
        if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
            & sc.exe delete $ServiceName | Out-Null
        }
        # The exe may have moved under the shared root above; point the old service at it.
        & sc.exe config $LegacyServiceName binPath= "`"$ExePath`"" | Out-Null
        & sc.exe start $LegacyServiceName | Out-Null
        Die "could not register $ServiceName ($($_.Exception.Message)). $LegacyServiceName was restarted on the new binary."
    }
    Die "could not register $ServiceName ($($_.Exception.Message))."
}

if ($legacy) {
    & sc.exe delete $LegacyServiceName | Out-Null
    Ok "Migrated service $LegacyServiceName -> $ServiceName"
}

# **The old exe goes only now**, with the service registered against the new one: removed any
# earlier, a failure above would fall back to a service with no binary behind it. The
# per-session helpers (remote control, recording badge, user messages) are started from the
# service's own path, so one started before this install can still be running from the old
# exe and holding it open; those are stopped first -- the service starts fresh ones from the
# new exe when they are next needed. A delete that still fails is a warning, not a failure:
# the leftover is never run again, and the next install removes it.
if ((Test-Path $LegacyExePath) -and $LegacyExePath -ne $ExePath) {
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $LegacyExePath } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item $LegacyExePath, "$LegacyExePath.old" -Force -ErrorAction SilentlyContinue
    if (Test-Path $LegacyExePath) { Warn "Could not remove $LegacyExePath; it is no longer used." }
    else { Ok "Removed $LegacyExeName (renamed to $ExeName)" }
}

# ----------------------------------------------------------------------
# 4b. Allow the service to press Ctrl+Alt+Del
# ----------------------------------------------------------------------
# The secure attention sequence cannot be synthesised with SendInput -- the kernel intercepts
# it, which is the point of a *secure* attention sequence. SendSAS is the supported route, and
# with SoftwareSASGeneration = 1 it is honoured for a caller running as a service. Without this
# the Ctrl+Alt+Del button in the remote viewer is a silent no-op, which matters most on exactly
# the machines the remote viewer is for: a headless box at the logon screen.
#
# Value 1 = "Services" only. Not 2 (ease-of-access apps) or 3 (both) -- this grants the
# narrowest thing that makes the button work.
Step "Allowing the service to send Ctrl+Alt+Del"
try {
    $sasKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    if (-not (Test-Path $sasKey)) { New-Item -Path $sasKey -Force | Out-Null }
    $existing = (Get-ItemProperty -Path $sasKey -Name SoftwareSASGeneration -ErrorAction SilentlyContinue).SoftwareSASGeneration
    if ($existing -eq 1 -or $existing -eq 3) {
        Ok "SoftwareSASGeneration already permits services ($existing)"
    } else {
        # Don't downgrade a value an admin deliberately set to 3 (services + apps).
        New-ItemProperty -Path $sasKey -Name SoftwareSASGeneration -Value 1 -PropertyType DWord -Force | Out-Null
        Ok "SoftwareSASGeneration = 1 (services may generate Ctrl+Alt+Del)"
    }
} catch {
    # Group Policy may own this key on a domain-joined machine, in which case it is the
    # domain admin's call, not ours. Everything else still works; only the button is dead.
    Warn "Could not set SoftwareSASGeneration ($($_.Exception.Message)). Ctrl+Alt+Del from the remote viewer may not work."
}

# ----------------------------------------------------------------------
# 5. Start
# ----------------------------------------------------------------------
Step "Starting service"
& sc.exe start $ServiceName | Out-Null
Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") { Ok "Service running" }
else { Warn "Service status: $($svc.Status). Check %ProgramData%\FleetHub\Agent\companion.log" }

Write-Host @"

  Done.

  Machine : $env:COMPUTERNAME
  Service : $ServiceName (LocalSystem, Automatic)
  Binary  : $ExePath
  Logs    : $env:ProgramData\FleetHub\Agent\companion.log

  Uninstall: powershell -ExecutionPolicy Bypass -File agent-install.ps1 -Uninstall

"@ -ForegroundColor Green
