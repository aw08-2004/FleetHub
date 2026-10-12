using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using FleetHubAgent.Fleet;
using FleetHubAgent.State;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Update;

/// <summary>
/// Removes this agent from the machine when the hub says it was deleted with "uninstall the
/// agent" ticked (roadmap #30). The Linux counterpart of the Windows agent's SelfUninstaller,
/// and the same failure is why it exists: a deleted machine whose agent keeps running simply
/// re-enrolls and reappears in the console.
///
/// **A unit cannot remove itself from inside its own cgroup**: stopping fleethub-agent.service
/// kills every process in it, including a child doing the uninstall. So the script runs as a
/// transient unit of its own (<c>systemd-run --unit fleethub-agent-uninstall</c>), outside
/// this one, and does what install.sh --uninstall does plus what that deliberately keeps for a
/// reinstall: the identity in /var/lib/fleethub/agent and /etc/fleethub (the secret and the hub
/// override). Deleting a machine with uninstall means "this machine is gone", so nothing is
/// kept for a reinstall that is not coming.
///
/// The script lives in /var/lib/fleethub/uninstall at 0700, root's alone; /tmp was rejected
/// because a file there that a root unit then runs is the local privilege escalation it sounds
/// like.
/// </summary>
public sealed class SelfUninstaller(ILogger<SelfUninstaller> log)
{
    public const string UnitName = "fleethub-agent-uninstall";
    public const string Reason = "decommissioned";
    public const string ServiceName = "fleethub-agent";
    public const string UnitPath = "/etc/systemd/system/fleethub-agent.service";
    public const string ConfDir = "/etc/fleethub";
    public const string ScriptDir = "/var/lib/fleethub/uninstall";

    private int _started;

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

    /// <summary>The only tree this deletes as "the install directory".</summary>
    public const string InstallRoot = "/opt/fleethub";

    /// <summary>Safe to delete recursively as the install directory? **Only something under
    /// /opt/fleethub.** install.sh installs to /opt/fleethub/agent and has no option to put it
    /// anywhere else, so an allowlist costs nothing -- and a denylist of system paths was
    /// rejected after its first draft let a home directory through.</summary>
    public static bool IsSafeInstallDir(string? dir)
    {
        if (string.IsNullOrWhiteSpace(dir) || !dir.StartsWith('/')) return false;
        // Textual, not Path.GetFullPath: no dot segments at all, so there is nothing to
        // resolve -- and the check reads the same on the Windows box the tests run on.
        var parts = dir.Split('/', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Any(p => p is "." or "..")) return false;
        var full = "/" + string.Join('/', parts);
        return full.StartsWith(InstallRoot + "/", StringComparison.Ordinal);
    }

    /// <summary>A POSIX shell single-quoted literal: a quote ends the string, so it is closed,
    /// escaped and reopened.</summary>
    private static string Q(string s) => "'" + s.Replace("'", "'\\''") + "'";

    /// <summary>The uninstall script. Pure, so the exact text a machine will run as root is
    /// what the tests read.</summary>
    public static string BuildScript(string? installDir, IReadOnlyList<string> stateDirs)
    {
        var sb = new StringBuilder();
        sb.Append("#!/bin/sh\n");
        sb.Append("# FleetHub agent self-uninstall (roadmap #30), run once as root by the transient\n");
        sb.Append($"# unit {UnitName}. Written by the agent; see SelfUninstaller.cs.\n");
        sb.Append("sleep 3\n");
        sb.Append($"systemctl disable --now {ServiceName} 2>/dev/null || true\n");
        sb.Append($"rm -f {Q(UnitPath)}\n");
        sb.Append("systemctl daemon-reload 2>/dev/null || true\n");
        if (IsSafeInstallDir(installDir))
        {
            sb.Append($"rm -rf {Q(installDir!)}\n");
            sb.Append($"rmdir {Q(InstallRoot)} 2>/dev/null || true\n");
        }
        foreach (var dir in stateDirs)
            sb.Append($"rm -rf {Q(dir)}\n");
        sb.Append($"rm -rf {Q(ConfDir)}\n");
        sb.Append($"rm -rf {Q(ScriptDir)}\n");
        sb.Append("rmdir /var/lib/fleethub 2>/dev/null || true\n");
        return sb.ToString();
    }

    /// <summary>Write the script and start it as its own transient unit. Once per process.</summary>
    public async Task StartAsync(CancellationToken ct)
    {
        if (Interlocked.Exchange(ref _started, 1) == 1) return;
        try
        {
            var note = StateDirectory.Ensure(ScriptDir);
            if (note is not null)
            {
                log.LogError("Not uninstalling: {Note}", note);
                Interlocked.Exchange(ref _started, 0);
                return;
            }
            var scriptPath = Path.Combine(ScriptDir, "uninstall.sh");
            var installDir = Path.GetDirectoryName(Environment.ProcessPath ?? "");
            await File.WriteAllTextAsync(scriptPath,
                BuildScript(installDir, [AgentConfig.StateDir]), Encoding.UTF8, ct);
            StateDirectory.RestrictFile(scriptPath);

            log.LogWarning("The hub reports this machine was deleted; uninstalling the agent now");
            var run = await ProcessRunner.RunAsync(
                ProcessRunner.ProgramPath("/usr/bin/systemd-run", "/bin/systemd-run"),
                ["--unit", UnitName, "--collect", "--quiet", "/bin/sh", scriptPath],
                timeoutSeconds: 30, onOutput: null, ct);
            if (run.ExitCode != 0)
            {
                log.LogError("Could not start the uninstall unit: {Output}", run.Output);
                Interlocked.Exchange(ref _started, 0);
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                       or InvalidOperationException)
        {
            log.LogError(e, "Self-uninstall could not start");
            Interlocked.Exchange(ref _started, 0);
        }
    }
}
