<#
.SYNOPSIS
    Authenticode-sign FleetHub release files with Azure Trusted Signing, then prove it worked.

.DESCRIPTION
    The ONE place anything in this repo gets an Authenticode signature: the agent exe
    (agent/release.ps1), the client's exe and DLLs (release_client.py) and the two installer
    scripts (install.ps1, agent/install/agent-install.ps1, signed by hand -- see below). One
    signer so the publisher Windows shows is the same on every file, which is the point of
    having one at all.

    **This is not the fleet's trust root and must not become it.** Updates are still decided
    by the offline Ed25519 key over the manifests (sign_release.py). Authenticode is what
    WINDOWS checks -- Explorer's Digital Signatures tab, SmartScreen, Defender's reputation, an
    AllSigned execution policy -- and what an installer can check before running a script it
    just downloaded. A binary is signed here BEFORE it is hashed into its manifest, so the
    Ed25519 signature covers the Authenticode-signed bytes; signing after would make every
    manifest's sha256 wrong and the fleet would refuse the release.

    **Fails closed.** A missing signtool, dlib or metadata.json is an error naming the missing
    piece, never a skipped step -- a release that quietly shipped unsigned looks exactly like
    one that was signed until somebody opens Properties. The callers have their own explicit,
    loud opt-out (-NoCodeSign / --no-codesign) for the time before the Azure account existed.

    After signtool reports success, every file is re-read with Get-AuthenticodeSignature and
    must come back Valid: signtool's exit code has been known to be 0 with the dlib failing
    to authenticate, and an unsigned file is what that leaves behind.

    Rejected:
      - Set-AuthenticodeSignature. It needs the certificate in a local store with its private
        key, which Trusted Signing deliberately never hands out -- the key lives in Azure.
      - A PFX on disk. The thing Trusted Signing is for is that there is no key file to leak.
      - Pinning the certificate thumbprint anywhere. Trusted Signing issues a new short-lived
        certificate every few days; only the subject (the validated publisher) is stable.

    Windows PowerShell 5.1 compatible, like install.ps1.

    Setup, once per release PC:
      1. Windows SDK (signtool 10.0.22621 or newer).
      2. The dlib: nuget install Microsoft.ArtifactSigning.Client -OutputDirectory
         $HOME\.fleethub_codesign  (or point -Dlib at Azure.CodeSigning.Dlib.dll).
      3. $HOME\.fleethub_codesign\metadata.json:
           { "Endpoint": "https://eus.codesigning.azure.net/",
             "CodeSigningAccountName": "<account>",
             "CertificateProfileName": "<profile>" }
      4. az login, as an identity holding "Trusted Signing Certificate Profile Signer".

.PARAMETER Path
    Files to sign: .exe, .dll, .ps1 (signtool signs scripts through PowerShell's SIP).

.PARAMETER SkipSigned
    Leave alone any file that already carries a valid signature -- for the client's build
    folder, where the Visual C++ runtime DLLs arrive signed by Microsoft and re-signing them
    as FleetHub would claim authorship of somebody else's code.

.PARAMETER Check
    Only report whether everything signing needs is present, and exit 0/1. The release
    scripts call this in preflight, before anything is published.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\codesign.ps1 -Path install.ps1, agent\install\agent-install.ps1
#>
param(
    [string[]]$Path,
    [string]$ConfigDir = (Join-Path $HOME '.fleethub_codesign'),
    [string]$Metadata,
    [string]$Dlib,
    [string]$SignTool,
    [switch]$Check,
    [switch]$SkipSigned,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# Microsoft's timestamp authority for Trusted Signing. A timestamp is what keeps a signature
# valid after the short-lived certificate expires -- without one, every file signed here
# would turn "invalid" within days.
$TimestampUrl = 'http://timestamp.acs.microsoft.com'

function Fail($msg) { Write-Host "  [xx] $msg" -ForegroundColor Red; exit 1 }
function Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }

function Find-SignTool {
    if ($SignTool) { if (Test-Path -LiteralPath $SignTool) { return $SignTool }; return $null }
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    # The SDK installs one per version and puts none on PATH. Newest first: the dlib needs
    # 10.0.22621 or later, and an older one fails with an error that does not say so.
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (-not (Test-Path -LiteralPath $kits)) { return $null }
    $found = Get-ChildItem -LiteralPath $kits -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending |
        ForEach-Object { Join-Path $_.FullName 'x64\signtool.exe' } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    return $found
}

function Find-Dlib {
    if ($Dlib) { if (Test-Path -LiteralPath $Dlib) { return $Dlib }; return $null }
    if (-not (Test-Path -LiteralPath $ConfigDir)) { return $null }
    # Under whatever version folder nuget made. x64 because signtool above is the x64 one; a
    # bitness mismatch fails to load with no useful message.
    return Get-ChildItem -LiteralPath $ConfigDir -Recurse -Filter 'Azure.CodeSigning.Dlib.dll' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' } |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

$tool = Find-SignTool
$dll  = Find-Dlib
$meta = $Metadata
if (-not $meta) { $meta = Join-Path $ConfigDir 'metadata.json' }

$missing = @()
if (-not $tool) { $missing += 'signtool.exe (install the Windows SDK, 10.0.22621 or newer)' }
if (-not $dll)  { $missing += "Azure.CodeSigning.Dlib.dll (nuget install Microsoft.ArtifactSigning.Client -OutputDirectory `"$ConfigDir`")" }
if (-not (Test-Path -LiteralPath $meta)) {
    $missing += "$meta (Endpoint, CodeSigningAccountName, CertificateProfileName)"
} else {
    try {
        $m = Get-Content -LiteralPath $meta -Raw | ConvertFrom-Json
        foreach ($k in @('Endpoint', 'CodeSigningAccountName', 'CertificateProfileName')) {
            if (-not $m.$k) { $missing += "$meta has no $k" }
        }
    } catch { $missing += "$meta is not valid JSON" }
}

if ($missing.Count -gt 0) {
    Write-Host "  Code signing is not set up on this machine:" -ForegroundColor Red
    foreach ($x in $missing) { Write-Host "    - $x" -ForegroundColor Red }
    exit 1
}
if ($Check) { Ok "Code signing ready (signtool, dlib, $meta)"; exit 0 }

if (-not $Path -or $Path.Count -eq 0) { Fail "Nothing to sign: pass -Path." }
$files = foreach ($p in $Path) {
    if (-not (Test-Path -LiteralPath $p)) { Fail "No such file: $p" }
    (Resolve-Path -LiteralPath $p).Path
}
if ($SkipSigned) {
    $files = @($files | Where-Object {
        $already = (Get-AuthenticodeSignature -LiteralPath $_).Status -eq 'Valid'
        if ($already) { Write-Host "  [--] already signed, left alone: $_" }
        -not $already
    })
    if ($files.Count -eq 0) { Ok "Everything was already signed."; exit 0 }
}

$signArgs = @('sign', '/v', '/fd', 'SHA256', '/tr', $TimestampUrl, '/td', 'SHA256',
              '/dlib', $dll, '/dmdf', $meta) + $files
if ($DryRun) {
    Write-Host "  [dry-run] `"$tool`" $($signArgs -join ' ')"
    exit 0
}

& $tool @signArgs
if ($LASTEXITCODE -ne 0) { Fail "signtool failed (exit $LASTEXITCODE). Is 'az login' current?" }

# Proof, not trust: see the description.
$publishers = @{}
foreach ($f in $files) {
    $sig = Get-AuthenticodeSignature -LiteralPath $f
    if ($sig.Status -ne 'Valid') { Fail "$f is not validly signed after signing ($($sig.Status): $($sig.StatusMessage))" }
    if (-not $sig.TimeStamperCertificate) { Fail "$f was signed without a timestamp; it would stop validating when the certificate expires." }
    $publishers[$sig.SignerCertificate.Subject] = $true
    Ok "$f"
}
# Every file in one call must name one publisher -- two would mean two profiles were in play.
if ($publishers.Count -ne 1) { Fail "Files were signed by more than one publisher: $($publishers.Keys -join ' | ')" }
Ok "Publisher: $(@($publishers.Keys)[0])"
