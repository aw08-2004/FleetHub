using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Update;

/// <summary>
/// Removes this agent from the PC when the hub says its machine was deleted with "uninstall
/// the agent" ticked (roadmap #30).
///
/// **The silent failure this exists to prevent is a deleted machine that keeps coming back.**
/// Deleting a device used to remove only the hub's record: the agent kept running, re-enrolled
/// with the shared secret on its next tick, and the machine reappeared in the console as if
/// the delete had never happened. Now the hub keeps the deleted agent's credential and answers
/// its next call with <c>reason: decommissioned</c>, and this class takes the agent off the PC.
///
/// **A running service cannot delete itself**, so the work is handed to a one-shot scheduled
/// task that runs as SYSTEM, outside this process. It marks the service(s) for deletion, stops
/// them, waits for this exe to exit, then removes exactly what agent-install.ps1 installs and
/// what this agent writes: the install directory, %ProgramData%\FleetHub\Agent (and the
/// pre-rename %ProgramData%\TempMonitorAgent), the HKLM keys and the FLEETHUB_HUB variable.
/// It deletes the task and its own script last. The PawnIO driver is left, as the installer's
/// own -Uninstall leaves it: it is a shared kernel driver other software may use.
///
/// The script is written to %ProgramData%\FleetHub\Uninstall, re-ACLed to SYSTEM and
/// Administrators before anything is written into it. %TEMP% and C:\Windows\Temp were
/// rejected: an ordinary user can pre-create a file there and keep write access to it, and a
/// script that a scheduled task then runs as SYSTEM is the textbook local privilege escalation.
///
/// Rejected: running the uninstall from this process with a detached child. A child of a
/// service is not guaranteed to survive the service's stop, and an uninstall that dies half
/// way leaves a service registered against a deleted exe, restarted every minute by its own
/// recovery actions.
/// </summary>
public sealed class SelfUninstaller(ILogger<SelfUninstaller> log)
{
    public const string TaskName = "FleetHubAgentUninstall";
    public const string Reason = "decommissioned";

    /// <summary>Service names, current first. Both, because a PC not re-installed since the
    /// rename still runs the legacy name, and one whose migration was interrupted has both.</summary>
    private static readonly string[] ServiceNames = ["FleetHubAgent", "TempMonitorAgent"];

    private int _started;

    /// <summary>Is this 401 body the hub saying the machine was deleted with uninstall?</summary>
    public static bool IsDecommissioned(string? body)
    {
        try
        {
            var reason = JsonNode.Parse(string.IsNullOrWhiteSpace(body) ? "{}" : body)
                ?["reason"]?.GetValue<string>();
            return string.Equals(reason, Reason, StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception e) when (e is JsonException or InvalidOperationException or FormatException)
        {
            return false;
        }
    }

    /// <summary>
    /// Is <paramref name="dir"/> safe to delete recursively as "the install directory"?
    ///
    /// It is derived from this exe's own path, so normally it is C:\Program Files\FleetHub\Agent.
    /// But -InstallDir is an installer parameter, and a recursive delete aimed at a drive root,
    /// Program Files or the Windows directory by a misconfigured install must never happen.
    /// Refused unless it is at least two levels below a root and is none of the system folders.
    /// </summary>
    public static bool IsSafeInstallDir(string? dir)
    {
        if (string.IsNullOrWhiteSpace(dir) || !Path.IsPathFullyQualified(dir)) return false;
        var full = Path.TrimEndingDirectorySeparator(Path.GetFullPath(dir));
        var root = Path.GetPathRoot(full) ?? "";
        var rest = full[root.Length..].Split(Path.DirectorySeparatorChar,
                                             StringSplitOptions.RemoveEmptyEntries);
        if (rest.Length < 2) return false;
        var forbidden = new[]
        {
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.System),
        };
        foreach (var f in forbidden)
        {
            if (string.IsNullOrEmpty(f)) continue;
            var ff = Path.TrimEndingDirectorySeparator(Path.GetFullPath(f));
            if (string.Equals(full, ff, StringComparison.OrdinalIgnoreCase)) return false;
            // Nothing inside the Windows directory is ever ours to delete.
            if (f == forbidden[0] && full.StartsWith(ff + Path.DirectorySeparatorChar,
                                                     StringComparison.OrdinalIgnoreCase))
                return false;
        }
        return true;
    }

    /// <summary>A PowerShell single-quoted literal: only the quote is special, doubled.</summary>
    private static string Q(string s) => "'" + s.Replace("'", "''") + "'";

    /// <summary>The uninstall script. Pure, so the exact text a PC will run as SYSTEM is
    /// what the tests read.</summary>
    public static string BuildScript(string exePath, string? installDir,
                                     IReadOnlyList<string> stateDirs, string scriptDir)
    {
        var sb = new StringBuilder();
        sb.AppendLine("# FleetHub agent self-uninstall (roadmap #30), run once as SYSTEM by the");
        sb.AppendLine($"# scheduled task {TaskName}. Written by the agent; see SelfUninstaller.cs.");
        sb.AppendLine("$ErrorActionPreference = 'Continue'");
        sb.AppendLine("Start-Sleep -Seconds 3");
        // Mark for deletion FIRST: the SCM never restarts a service that is marked for
        // deletion, so the recovery actions cannot bring it back between the stop and the delete.
        sb.AppendLine($"foreach ($n in @({string.Join(", ", ServiceNames.Select(Q))})) {{");
        sb.AppendLine("    if (Get-Service -Name $n -ErrorAction SilentlyContinue) {");
        sb.AppendLine("        & sc.exe delete $n | Out-Null");
        sb.AppendLine("        Stop-Service -Name $n -Force -ErrorAction SilentlyContinue");
        sb.AppendLine("    }");
        sb.AppendLine("}");
        sb.AppendLine($"$exe = {Q(exePath)}");
        sb.AppendLine("for ($i = 0; $i -lt 60; $i++) {");
        sb.AppendLine("    if (-not (Get-Process | Where-Object { $_.Path -eq $exe })) { break }");
        sb.AppendLine("    Start-Sleep -Seconds 1");
        sb.AppendLine("}");
        if (IsSafeInstallDir(installDir))
        {
            sb.AppendLine($"Remove-Item -LiteralPath {Q(installDir!)} -Recurse -Force -ErrorAction SilentlyContinue");
            AppendRemoveIfEmpty(sb, Path.GetDirectoryName(installDir!), requireSafe: true);
        }
        foreach (var dir in stateDirs)
            sb.AppendLine($"Remove-Item -LiteralPath {Q(dir)} -Recurse -Force -ErrorAction SilentlyContinue");
        foreach (var key in new[] { AgentConfig.RegistryKeyPath, AgentConfig.LegacyRegistryKeyPath })
            sb.AppendLine($"Remove-Item -LiteralPath {Q(@"HKLM:\" + key)} -Recurse -Force -ErrorAction SilentlyContinue");
        foreach (var name in new[] { "FLEETHUB_HUB", "TEMP_MONITOR_HUB" })
            sb.AppendLine($"[Environment]::SetEnvironmentVariable({Q(name)}, $null, 'Machine')");
        sb.AppendLine($"& schtasks.exe /delete /tn {Q(TaskName)} /f | Out-Null");
        sb.AppendLine($"Remove-Item -LiteralPath {Q(scriptDir)} -Recurse -Force -ErrorAction SilentlyContinue");
        // The shared %ProgramData%\FleetHub parent, only when nothing else is left in it.
        AppendRemoveIfEmpty(sb, Path.GetDirectoryName(scriptDir), requireSafe: false);
        return sb.ToString();
    }

    /// <summary>Remove <paramref name="dir"/> only if it is empty, and never recursively -- so
    /// a FleetHub folder that also holds something else (the client, a log someone copied
    /// there) is left exactly as it was. `requireSafe` runs the install-directory check on the
    /// PARENT as well: it is one level shallower than the install directory, so it passes only
    /// when that is at least three deep, and C:\Program Files is never even a candidate. That
    /// is belt and braces -- a non-recursive delete of an empty folder cannot take anything
    /// with it -- but it keeps a well-known system folder out of the script entirely.</summary>
    private static void AppendRemoveIfEmpty(StringBuilder sb, string? dir, bool requireSafe)
    {
        if (string.IsNullOrEmpty(dir) || (requireSafe && !IsSafeInstallDir(dir))) return;
        sb.AppendLine($"if (-not (Get-ChildItem -LiteralPath {Q(dir)} -Force -ErrorAction SilentlyContinue)) {{");
        sb.AppendLine($"    Remove-Item -LiteralPath {Q(dir)} -Force -ErrorAction SilentlyContinue");
        sb.AppendLine("}");
    }

    /// <summary>Hand the uninstall to a SYSTEM scheduled task and start it. Once per process:
    /// every heartbeat after the hub says "decommissioned" says it again.</summary>
    public async Task StartAsync(CancellationToken ct)
    {
        if (Interlocked.Exchange(ref _started, 1) == 1) return;
        try
        {
            var exePath = Environment.ProcessPath ?? "";
            var installDir = Path.GetDirectoryName(exePath);
            var scriptDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "FleetHub", "Uninstall");
            var note = StateDirectory.HardenPrivate(scriptDir);
            if (note is not null)
            {
                // Never write a script a task will run as SYSTEM into a folder we could not lock.
                log.LogError("Not uninstalling: {Note}", note);
                Interlocked.Exchange(ref _started, 0);
                return;
            }
            var scriptPath = Path.Combine(scriptDir, "uninstall.ps1");
            var stateDirs = new[] { AgentConfig.NewProgramDataDir, AgentConfig.LegacyProgramDataDir };
            await File.WriteAllTextAsync(scriptPath,
                BuildScript(exePath, installDir, stateDirs, scriptDir), Encoding.UTF8, ct);

            var action = $"powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File \"{scriptPath}\"";
            var created = await ProcessRunner.RunAsync("schtasks.exe",
                $"/create /tn {TaskName} /tr \"{action.Replace("\"", "\\\"")}\" /sc once /st 23:59 /ru SYSTEM /rl HIGHEST /f",
                ct, timeoutSeconds: 30);
            if (created.ExitCode != 0)
            {
                log.LogError("Could not create the uninstall task: {Output}", created.Output);
                Interlocked.Exchange(ref _started, 0);
                return;
            }
            log.LogWarning("The hub reports this machine was deleted; uninstalling the agent now");
            await ProcessRunner.RunAsync("schtasks.exe", $"/run /tn {TaskName}", ct, timeoutSeconds: 30);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                       or InvalidOperationException)
        {
            log.LogError(e, "Self-uninstall could not start");
            Interlocked.Exchange(ref _started, 0);
        }
    }
}
