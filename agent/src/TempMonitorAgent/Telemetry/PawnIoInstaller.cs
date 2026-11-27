using System.Diagnostics;
using System.Security.Cryptography;
using System.ServiceProcess;
using Microsoft.Extensions.Logging;
using FleetHub.Shared;

namespace TempMonitorAgent.Telemetry;

/// <summary>
/// Installs the PawnIO driver when it is missing, from the service itself (roadmap #33).
///
/// <para><b>Why the agent and not the installer.</b> agent-install.ps1 used to download
/// PawnIO_setup.exe and start it, and an endpoint-security behaviour engine killed the
/// onboarding script at exactly that step -- silently, leaving behind the FleetHub-PawnIO-*
/// folder the script's own finally always deletes. A script that was itself just downloaded,
/// downloading an exe and running it, is a dropper to such an engine; the same file run by hand
/// from a remote session installed fine. The service
/// is not a script, it is the process that already downloads and swaps in its own signed
/// builds, so the driver install moved here. agent-install.ps1 skips its own PawnIO step once
/// the agent it installs is at least <c>$MinPawnIoAgent</c> (3.55.0).</para>
///
/// <para><b>It also heals PCs installed without it.</b> Before this, a PawnIO step that failed
/// -- a blocked download, a hash mismatch, an AV kill -- left a PC with no temperatures until
/// someone re-ran the installer there. Every agent from this build on retries on its own.</para>
///
/// <para><b>Pinned by hash, three copies.</b> <see cref="Sha256"/> must equal
/// <c>$PawnIoSha256</c> in agent-install.ps1 and <c>release_mirror.PAWNIO_SHA256</c>; the hub
/// mirror serves PawnIO only under that hash, and tests/test_release_mirror.py fails if any of
/// the three differ. When upstream ships a new PawnIO the GitHub copy stops matching, the hub's
/// copy keeps serving the pinned one, and a PC that can reach neither installs nothing and says
/// so in its log -- the same rule as the installer: a PC without temperatures is better than
/// one running an unchecked driver installer as SYSTEM.</para>
///
/// <para>Rejected: a scheduled task or a temporary service to launch the setup from the
/// installer. Both are persistence patterns, which these engines watch harder than a download.
/// Rejected: an antivirus exclusion, for the reason ROADMAP.MD gives under hub 1.193.0 -- every
/// site with a behaviour engine would need its own.</para>
/// </summary>
public sealed class PawnIoInstaller(ILogger<PawnIoInstaller> log)
{
    public const string ServiceName = "PawnIO";

    /// <summary>Must equal <c>$PawnIoUrl</c> in agent-install.ps1 and
    /// <c>release_mirror.PAWNIO_URL</c>.</summary>
    public const string Url = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/refs/heads/master/LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe";

    /// <summary>Must equal <c>$PawnIoSha256</c> in agent-install.ps1 and
    /// <c>release_mirror.PAWNIO_SHA256</c> -- see the class summary.</summary>
    public const string Sha256 = "a3a46226c5e2824f4cdd42be0eecbabfc672c86f7889710f5ab1e6ad385b47a0";

    /// <summary>How long after a failed attempt the next one runs. Six hours: a pin that no
    /// longer matches upstream fails the same way every time, and a PC behind a firewall
    /// should not fetch a 1 MB file from GitHub every inventory tick to learn that again.
    /// **Kept in memory only**, so each service start -- a reboot or a self-update -- also
    /// tries once. Persisting it was rejected: those starts are days apart, and a restart is
    /// exactly when a fixed network or a new pin should be tried without waiting.</summary>
    public static readonly TimeSpan RetryInterval = TimeSpan.FromHours(6);

    /// <summary>The setup normally finishes in seconds. A hang is killed rather than holding
    /// the inventory loop it runs on.</summary>
    private static readonly TimeSpan RunTimeout = TimeSpan.FromMinutes(3);

    private static readonly HttpClient Http =
        new(new SocketsHttpHandler { ConnectTimeout = UpdateMirror.ConnectTimeout })
            { Timeout = AgentConfig.DownloadTimeout };

    /// <summary>Set once a setup run left the driver installed. SensorReader claims it and
    /// reopens LibreHardwareMonitor, which probed for the driver when it first opened and
    /// would otherwise report no temperatures until the next service restart.</summary>
    private static int _reopenSensors;

    public static bool TakeSensorReopen() => Interlocked.Exchange(ref _reopenSensors, 0) == 1;

    private DateTime _lastAttemptUtc = DateTime.MinValue;

    // Seams for tests. The defaults are the real machine.
    internal Func<bool> IsInstalled { get; init; } = ServiceExists;
    internal Func<string, CancellationToken, Task<byte[]>> GetBytes { get; init; } = Http.GetByteArrayAsync;
    internal Func<string, CancellationToken, Task<int>> RunSetup { get; init; } = RunSetupAsync;
    internal Func<DateTime> UtcNow { get; init; } = () => DateTime.UtcNow;
    internal string? HubBase { get; init; }
    internal string StagingDir { get; init; } = Path.Combine(AgentConfig.ProgramDataDir, "pawnio");

    /// <summary>Install the driver if it is missing and an attempt is due. Never throws for a
    /// download or setup failure; returns whether the driver is installed afterwards.</summary>
    public async Task<bool> EnsureAsync(CancellationToken ct)
    {
        if (IsInstalled()) return true;
        var now = UtcNow();
        if (now - _lastAttemptUtc < RetryInterval) return false;
        _lastAttemptUtc = now;

        var hub = HubBase ?? AgentConfig.HubBase;
        var got = await UpdateMirror.FetchVerifiedBinaryAsync(
            UpdateMirror.Sources(Url, UpdateMirror.BinaryOnHub(hub, Sha256)), GetBytes, Sha256, ct);
        if (got.Bytes is null)
        {
            log.LogWarning("[pawnio] driver missing and no copy matched the pinned sha256: {Failures}; " +
                           "sensors stay unreadable, next try in {Hours} h", got.Why, RetryInterval.TotalHours);
            return false;
        }
        if (got.Source != Url)
            log.LogInformation("[pawnio] downloaded through the hub ({Why})", got.Why);

        Directory.CreateDirectory(StagingDir);
        var path = Path.Combine(StagingDir, "PawnIO_setup.exe");
        try
        {
            await File.WriteAllBytesAsync(path, got.Bytes, ct);
            // Re-hashed FROM DISK while a handle that refuses writers is held, and run while it
            // is still held: what starts as SYSTEM is the file, not the array checked above.
            // Same reasoning as SelfUpdater step 6b, one step tighter because this file is
            // executed by us rather than moved and started by the SCM.
            await using var pin = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
            var onDisk = Convert.ToHexString(await SHA256.HashDataAsync(pin, ct)).ToLowerInvariant();
            if (!string.Equals(onDisk, Sha256, StringComparison.Ordinal))
            {
                log.LogWarning("[pawnio] staged setup changed after verification -- not run");
                return false;
            }

            log.LogInformation("[pawnio] driver missing; running PawnIO setup");
            var exit = await RunSetup(path, ct);
            var installed = IsInstalled();
            if (exit == 0 && installed)
            {
                log.LogInformation("[pawnio] driver installed; reopening the sensors");
                Interlocked.Exchange(ref _reopenSensors, 1);
            }
            else
            {
                log.LogWarning("[pawnio] setup exited {Exit}, driver installed: {Installed}; next try in {Hours} h",
                    exit, installed, RetryInterval.TotalHours);
            }
            return installed;
        }
        catch (Exception e) when (e is not OperationCanceledException || !ct.IsCancellationRequested)
        {
            log.LogWarning(e, "[pawnio] setup could not be run; next try in {Hours} h", RetryInterval.TotalHours);
            return false;
        }
        finally
        {
            try { File.Delete(path); } catch (IOException) { /* the next attempt overwrites it */ }
        }
    }

    private static bool ServiceExists()
    {
        try
        {
            using var svc = new ServiceController(ServiceName);
            _ = svc.Status;   // throws InvalidOperationException when no such service exists
            return true;
        }
        catch (InvalidOperationException) { return false; }
    }

    private static async Task<int> RunSetupAsync(string path, CancellationToken ct)
    {
        // The arguments agent-install.ps1 has always passed.
        using var p = Process.Start(new ProcessStartInfo(path, "-install -silent")
                          { UseShellExecute = false, CreateNoWindow = true })
                      ?? throw new InvalidOperationException("PawnIO setup did not start");
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(RunTimeout);
        try
        {
            await p.WaitForExitAsync(cts.Token);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            try { p.Kill(entireProcessTree: true); } catch { /* already gone */ }
            throw new System.TimeoutException($"PawnIO setup did not finish within {RunTimeout.TotalMinutes} minutes");
        }
        return p.ExitCode;
    }
}
