using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Patch;

/// <summary>
/// install_patches on Linux (roadmap #14's Linux half, listed under #22): install the approved
/// updates a patch run names, with apt-get or dnf, then report the new inventory at once.
///
/// **The result is not the verdict; the next inventory is.** hub/patches.py moves a target to
/// REBOOTING on a successful result and closes it only when the machine's patch report no
/// longer offers what was attempted (confirm_from_inventory). So the last thing this does,
/// whatever happened, is re-scan and leave that report for the very next heartbeat. A run
/// whose install failed outright would otherwise never close, because an unchanged list is
/// never re-sent.
///
/// **Only what the machine still offers is installed.** The uids are re-read against a fresh
/// cache-only scan before anything runs. The hub names packages, never commands; intersecting
/// with the scan also means a hub cannot use this to install a package that was never an
/// upgrade -- `install --only-upgrade` and `dnf upgrade` refuse that too, and the two checks
/// are independent.
///
/// **No interactive prompt can stall it.** apt runs with DEBIAN_FRONTEND=noninteractive,
/// keeps an admin's edited config files (`--force-confold`), and waits up to five minutes for
/// the dpkg lock rather than failing because unattended-upgrades was mid-run -- the most
/// common reason a scheduled install fails on Ubuntu. stdin is closed by ProcessRunner, so
/// anything that asks anyway fails rather than hangs.
///
/// Rejected: `apt-get upgrade` of everything. A run installs what was APPROVED; the window
/// policy that approved three packages did not approve the forty others the box also offers.
/// </summary>
public sealed partial class InstallPatchesExecutor(
    ILogger<InstallPatchesExecutor> log, PatchScanner scanner, PatchInventoryReporter inventory,
    Software.SoftwareInventoryReporter software)
    : ICommandExecutor
{
    public string Type => "install_patches";

    /// <summary>A kernel update plus a desktop's worth of libraries over a slow mirror is
    /// tens of minutes. The hub's command TTL is the real bound; this only stops a wedged
    /// package manager from holding the command forever.</summary>
    private const int InstallTimeoutSeconds = 60 * 60;

    /// <summary>The Windows agent's delay, so a person at the machine gets the same warning
    /// whichever platform they are on.</summary>
    private const int RestartDelayMinutes = 5;

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var uids = ReadUids(cmd.Params);
        if (uids.Count == 0)
            return CommandResult.Fail("install_patches was given no updates to install.");
        var policy = cmd.Params.GetString("reboot_policy") ?? "if_required";

        try
        {
            var scan = await scanner.ReadAsync(ct);
            if (scan.Error is { Length: > 0 } scanError)
                return CommandResult.Fail($"Could not read what this machine offers: {scanError}");

            var offered = scan.Updates.ToDictionary(u => u.Uid, StringComparer.Ordinal);
            var attempt = uids.Where(offered.ContainsKey).ToList();
            var lines = new List<string>();
            var skipped = uids.Count - attempt.Count;
            if (skipped > 0)
                lines.Add($"{skipped} of the {uids.Count} updates are no longer offered here "
                          + "(already installed, or superseded), so they were skipped.");
            if (attempt.Count == 0)
            {
                lines.Add("Nothing was left to install on this machine.");
                return CommandResult.Ok(string.Join(Environment.NewLine, lines));
            }

            var source = offered[attempt[0]].Source;
            var run = source == PatchSources.Apt
                ? await InstallAptAsync(attempt, onOutput, ct)
                : await InstallDnfAsync(attempt, onOutput, ct);
            if (!run.Succeeded)
            {
                var why = run.TimedOut ? $"did not finish within {InstallTimeoutSeconds / 60} minutes"
                                       : $"exited {run.ExitCode}";
                return CommandResult.Fail($"{source} {why}. {LastLines(run.Output, 6)}");
            }
            lines.Add($"Installed {attempt.Count} update(s): {string.Join(", ", attempt.Take(20))}"
                      + (attempt.Count > 20 ? ", ..." : "") + ".");

            var rebootRequired = await RebootRequiredAsync(source, attempt, ct);
            if (ShouldRestart(policy, rebootRequired))
            {
                lines.Add($"Restarting in {RestartDelayMinutes} minutes to finish the install.");
                onOutput?.Invoke(lines[^1] + "\n");
                var restart = await ProcessRunner.RunAsync(
                    ProcessRunner.ProgramPath("/sbin/shutdown", "/usr/sbin/shutdown", "/usr/bin/shutdown"),
                    ["-r", $"+{RestartDelayMinutes}", "FleetHub patch install"],
                    timeoutSeconds: 30, onOutput: null, ct);
                if (!restart.Succeeded)
                {
                    // The updates are in either way; say so, because the hub will otherwise
                    // wait out its confirm timeout on a machine nobody is going to reboot.
                    log.LogWarning("Could not schedule the patch restart: shutdown -r exited {Code}",
                                   restart.ExitCode);
                    lines.Add($"The restart could not be scheduled (shutdown -r exited "
                              + $"{restart.ExitCode}). A new kernel takes effect at the next restart.");
                }
            }
            else if (rebootRequired)
            {
                lines.Add("A restart is required to finish (a new kernel or core library), and the "
                          + "window's policy is not to restart. It takes effect at the next restart.");
            }
            return CommandResult.Ok(string.Join(Environment.NewLine, lines));
        }
        finally
        {
            // Whatever happened above. CancellationToken.None: the host stopping mid-install is
            // exactly when the next start's first heartbeat most needs this report, and a scan
            // is a few seconds of cache reads.
            try { await inventory.RefreshNowAsync(CancellationToken.None); }
            catch (Exception e) { log.LogWarning(e, "Could not re-scan after install_patches"); }
            // The installed versions just changed, so the Software section is stale too.
            software.Invalidate();
        }
    }

    private static async Task<ProcessRun> InstallAptAsync(IReadOnlyList<string> packages,
                                                         Action<string>? onOutput, CancellationToken ct)
    {
        var apt = ProcessRunner.ProgramPath("/usr/bin/apt-get", "/bin/apt-get");
        var env = new Dictionary<string, string>
        {
            ["DEBIAN_FRONTEND"] = "noninteractive",
            // Ubuntu's needrestart otherwise opens a whiptail dialog after the install.
            ["NEEDRESTART_MODE"] = "a",
        };
        string[] options =
        [
            "-y", "-q",
            "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold",
            // apt 1.9.11+; an older apt-get accepts and ignores an unknown option.
            "-o", "DPkg::Lock::Timeout=300",
        ];
        var install = options.Concat(["install", "--only-upgrade"]).Concat(packages).ToArray();
        var run = await ProcessRunner.RunAsync(apt, install, InstallTimeoutSeconds, onOutput, ct, env);
        if (run.Succeeded || !IndexIsStale(run.Output)) return run;

        // The scan reads the local index and never refreshes it (see PatchScanner), so the
        // version it offered can have left the mirror since. Refresh once and try again --
        // the one case where touching the index here is the fix rather than a side effect.
        onOutput?.Invoke("The package index is out of date; refreshing it and trying again.\n");
        var update = await ProcessRunner.RunAsync(apt, options.Concat(["update"]),
                                                  InstallTimeoutSeconds, onOutput, ct, env);
        return update.Succeeded
            ? await ProcessRunner.RunAsync(apt, install, InstallTimeoutSeconds, onOutput, ct, env)
            : run;
    }

    private static Task<ProcessRun> InstallDnfAsync(IReadOnlyList<string> packages,
                                                    Action<string>? onOutput, CancellationToken ct)
    {
        // Not -C: unlike the scan, an install has to be allowed to refresh metadata, or a
        // package whose mirror moved fails for a reason the operator cannot see.
        var dnf = ProcessRunner.ProgramPath("/usr/bin/dnf", "/bin/dnf");
        return ProcessRunner.RunAsync(dnf, new[] { "-y", "-q", "upgrade" }.Concat(packages),
                                      InstallTimeoutSeconds, onOutput, ct);
    }

    /// <summary>apt's ways of saying the package it was told about is no longer on the mirror.</summary>
    internal static bool IndexIsStale(string output) =>
        output.Contains("Unable to fetch", StringComparison.Ordinal)
        || output.Contains("404  Not Found", StringComparison.Ordinal)
        || output.Contains("404 Not Found", StringComparison.Ordinal);

    /// <summary>Does the machine need a restart for what was just installed?
    ///
    /// Debian-family writes /run/reboot-required from the package's own postinst, which is the
    /// authoritative answer. On dnf, `needs-restarting -r` (dnf-utils) answers exit 1 for yes;
    /// where it is not installed, the scanner's kernel-package rule is the fallback.</summary>
    private static async Task<bool> RebootRequiredAsync(string source, IReadOnlyList<string> attempted,
                                                        CancellationToken ct)
    {
        if (source == PatchSources.Apt)
            return File.Exists("/run/reboot-required") || File.Exists("/var/run/reboot-required")
                   || attempted.Any(PatchScanner.NeedsReboot);

        foreach (var candidate in new[] { "/usr/bin/needs-restarting", "/bin/needs-restarting" })
        {
            if (!File.Exists(candidate)) continue;
            var run = await ProcessRunner.RunAsync(candidate, ["-r"], 60, null, ct);
            if (!run.TimedOut && run.ExitCode is 0 or 1) return run.ExitCode == 1;
        }
        return attempted.Any(PatchScanner.NeedsReboot);
    }

    internal static bool ShouldRestart(string? policy, bool rebootRequired) =>
        policy?.Trim().ToLowerInvariant() switch
        {
            "always" => true,
            "never" => false,
            _ => rebootRequired,
        };

    /// <summary>The uids the command names, keeping only well-formed package names.
    ///
    /// **Each one becomes an argument to a package manager running as root**, so a name that
    /// starts with a dash -- which apt-get and dnf would read as an option -- or carries a
    /// space or a shell character is dropped rather than passed. The hub's normalize_uid has
    /// already refused those; this is the end that runs them.</summary>
    internal static List<string> ReadUids(JsonNode? parameters)
    {
        var uids = new List<string>();
        if (parameters?["uids"] is not JsonArray array) return uids;
        foreach (var item in array)
        {
            var text = item?.ToString().Trim() ?? "";
            if (PackageName().IsMatch(text) && !uids.Contains(text)) uids.Add(text);
        }
        return uids;
    }

    [GeneratedRegex(@"^[A-Za-z0-9][A-Za-z0-9+._~:-]{0,199}$")]
    private static partial Regex PackageName();

    private static string LastLines(string text, int count) =>
        string.Join(" | ", (text ?? "").Split('\n').Select(l => l.Trim())
            .Where(l => l.Length > 0).TakeLast(count));
}
