using System.Diagnostics;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Patch;

/// <summary>
/// Reads what this machine currently has available to install, from both sources.
///
/// <para><b>A source that fails contributes nothing and does not stop the other.</b> Windows
/// Server has no winget at all and never will (no Store, no App Installer — see
/// <c>WingetLocator.NotFoundMessage</c>), and a machine pointed at an unreachable WSUS cannot
/// search Windows Update. Either is normal somewhere in a fleet, and treating one as fatal
/// would mean a server reports no OS updates because it has no winget.</para>
///
/// <para><b>An EMPTY result is a real answer and must be reported as one.</b> That is the
/// single most important thing this class produces: an update that stops being offered is the
/// only honest evidence it installed, so a fully patched machine reporting nothing is what
/// closes out a patch run. See <c>PatchInventoryReporter</c>, which sends the empty payload,
/// and <c>hub/patches.py confirm_from_inventory</c>, which consumes the absence.</para>
/// </summary>
public static class PatchScanner
{
    /// <summary>How long winget gets before it is abandoned. `winget upgrade` refreshes its
    /// sources over the network on first use, which on a cold machine is slow but bounded;
    /// past this the answer is not worth a scan thread.</summary>
    private static readonly TimeSpan WingetTimeout = TimeSpan.FromMinutes(3);

    /// <summary>What a scan found. <c>Control</c> is null only in tests that build a Scan by
    /// hand; Read() always fills it.</summary>
    public sealed record Scan(IReadOnlyList<AvailableUpdate> Updates, string Error,
                              ControlReport? Control = null);

    /// <summary>Who decides when Windows updates install here, and what installed without
    /// FleetHub anyway (issue #140). See UpdateControl and UpdateHistory.</summary>
    public sealed record ControlReport(string State,
                                       IReadOnlyList<UpdateHistory.OutsideInstall> OutsideInstalls);

    public static Scan Read()
    {
        var updates = new List<AvailableUpdate>();
        var problems = new List<string>();

        var windows = WindowsUpdateApi.Search();
        updates.AddRange(windows.Updates);
        if (windows.Error.Length > 0) problems.Add(windows.Error);

        try
        {
            var winget = ReadWinget(out var wingetError);
            updates.AddRange(winget);
            if (wingetError.Length > 0) problems.Add(wingetError);
        }
        catch (Exception e) when (e is IOException or InvalidOperationException
                                       or UnauthorizedAccessException)
        {
            problems.Add($"winget could not be run: {e.Message}");
        }

        // Two sources can name the same thing (a store-delivered driver, say). The hub keys
        // on uid and would collapse them anyway; doing it here keeps the payload honest about
        // how many updates this machine actually has.
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var deduped = new List<AvailableUpdate>(updates.Count);
        foreach (var update in updates)
        {
            if (seen.Add(update.Uid)) deduped.Add(update);
        }

        return new Scan(deduped, string.Join("; ", problems), ReadControl(problems));
    }

    /// <summary>The control half of the scan. Read here, on the inventory loop, because the
    /// history query is a WUA call with the same latency profile as the search above.
    ///
    /// A registry failure is reported as "windows" -- the state that makes the hub's
    /// reconcile loop try again -- rather than dropping the block, which the hub would read
    /// as an agent too old to have the feature at all.</summary>
    private static ControlReport ReadControl(List<string> problems)
    {
        string state;
        try { state = UpdateControl.ForMachine().State(); }
        catch (Exception e) when (e is UnauthorizedAccessException or IOException
                                       or System.Security.SecurityException)
        {
            problems.Add($"update control could not be read: {e.Message}");
            state = UpdateControl.Windows;
        }
        var outside = UpdateHistory.Select(WindowsUpdateApi.ReadHistory(), DateTimeOffset.UtcNow);
        return new ControlReport(state, outside);
    }

    private static IReadOnlyList<AvailableUpdate> ReadWinget(out string error)
    {
        error = "";
        var winget = WingetLocator.Find();
        if (winget is null)
        {
            // Not an error worth surfacing on every server in the fleet, forever. The
            // absence is expected on Windows Server and the message is long; the OS-update
            // half of this scan is unaffected either way.
            return [];
        }

        var psi = new ProcessStartInfo(winget)
        {
            // --include-unknown: a package whose installed version winget cannot determine is
            // still upgradable, and omitting it hides exactly the stale software an operator
            // is looking for. --disable-interactivity and the two agreement flags are what
            // stop this blocking forever on a prompt nobody can answer: this runs as SYSTEM
            // with no console attached.
            Arguments = "upgrade --include-unknown --disable-interactivity " +
                        "--accept-source-agreements",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        using var process = Process.Start(psi);
        if (process is null)
        {
            error = "winget did not start";
            return [];
        }
        var stdout = process.StandardOutput.ReadToEnd();
        if (!process.WaitForExit((int)WingetTimeout.TotalMilliseconds))
        {
            try { process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
            error = "winget did not finish in time";
            return [];
        }
        // A non-zero exit with parseable output still counts: winget returns a failure code
        // when a source could not be refreshed while still listing what it knows.
        return WingetUpgradeParser.Parse(stdout);
    }
}
