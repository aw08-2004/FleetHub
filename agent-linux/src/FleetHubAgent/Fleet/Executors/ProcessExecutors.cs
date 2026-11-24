using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// What must not be ended on a Linux box, and why the check lives on the machine (roadmap #22,
/// the Processes card's End task and Restart).
///
/// The Windows agent's ProcessGuard is the model, and the reasoning carries over unchanged:
/// the hub's list renders seconds before the click, so THIS copy, checked against what holds the
/// pid right now, is the authority. Only the list itself is Linux's own.
///
/// **Names match exactly, case and all.** Linux names are case-sensitive and a process's
/// comm is whatever it set itself, so the Windows guard's lower-casing and `.exe` stripping
/// would make `Systemd` -- a user binary -- read as init.
/// </summary>
public static partial class ProcessGuard
{
    private static readonly HashSet<string> Protected = new(StringComparer.Ordinal)
    {
        // The init system, and also every user's `systemd --user` manager: ending that one
        // logs the person out of everything at once, which is never what End task means.
        "systemd", "init",
        // The plumbing a running system cannot lose without falling over or locking people
        // out: the journal (logging stops, and with it every other service's diagnostics),
        // logind (sessions and seats), udev (device events), and the system bus.
        "systemd-journald", "systemd-logind", "systemd-udevd", "dbus-daemon", "dbus-broker",
        // Ourselves. Ending the agent loses the machine from the console for ten seconds and
        // loses this command's result for good -- the operator would watch a spinner for work
        // that happened. systemd brings it back; restart_process on it is refused for the
        // same reason (restart the service from the Terminal tab if that is really meant).
        "fleethub-agent",
    };

    /// <summary>Why <paramref name="pid"/> may not be ended or restarted, or null if it may.
    /// The pid checks come first because they hold whatever a process calls itself.</summary>
    public static string? RefusalFor(int pid, string name, int? parentPid, int selfPid)
    {
        if (pid == 1)
            return $"{name} (1) is the init system -- ending it panics the kernel";
        // kthreadd is pid 2 and every kernel thread is its child. They cannot be signalled
        // usefully at all, and "it did not exit" would read like a permissions problem.
        if (pid == 2 || parentPid == 2)
            return $"{name} ({pid}) is a kernel thread";
        if (pid == selfPid || Protected.Contains(name))
            return name == "fleethub-agent" || pid == selfPid
                ? $"{name} ({pid}) is the FleetHub agent itself"
                : $"{name} ({pid}) is a core system process";
        return null;
    }

    /// <summary>Does the pid still answer to the name the operator clicked? The guard against
    /// pid reuse, and the reason both travel in the command.</summary>
    public static bool NameMatches(string? expected, string? actual) =>
        !string.IsNullOrEmpty(actual) && string.Equals(expected?.Trim(), actual, StringComparison.Ordinal);

    /// <summary>The SYSTEM service a process belongs to, from <c>/proc/&lt;pid&gt;/cgroup</c>,
    /// or null.
    ///
    /// **Only a unit directly under <c>/system.slice</c> counts.** The Processes card's
    /// Services column takes the innermost <c>.service</c> anywhere in the path, which is right
    /// for a label and wrong for an action: a desktop application lives under
    /// <c>/user.slice/user-1000.slice/user@1000.service/app.slice/...</c>, and restarting
    /// <c>user@1000.service</c> -- what that rule finds -- ends the person's whole session.</summary>
    public static string? SystemServiceFor(string cgroupText)
    {
        foreach (var line in (cgroupText ?? "").Split('\n'))
        {
            // cgroup v2 is "0::/path"; v1 has one line per controller, and the systemd
            // hierarchy ("1:name=systemd:/path") carries the same path.
            var parts = line.Trim().Split(':', 3);
            if (parts.Length != 3) continue;
            if (parts[1].Length > 0 && parts[1] != "name=systemd") continue;
            var segments = parts[2].Split('/', StringSplitOptions.RemoveEmptyEntries);
            if (segments.Length >= 2 && segments[0] == "system.slice"
                && UnitName().IsMatch(segments[1]))
                return segments[1];
        }
        return null;
    }

    /// <summary>Units whose restart is refused whatever process inside them was clicked.
    ///
    /// **The process guard alone is not enough for Restart, because Restart acts on the
    /// UNIT.** Everything the agent starts -- a Terminal-tab shell and whatever runs in it, the
    /// apt-get an install runs -- lives in the agent's own cgroup, so Restart on a harmless
    /// `sleep` started from the Terminal tab resolved to fleethub-agent.service and restarted
    /// the agent mid-command, losing the result. The same holds for any helper a core service
    /// forks. Caught in review of the 0.5.0 PR. TempMonitorAgent.service is listed for the
    /// legacy name rule in CLAUDE.md, though no Linux install has ever used it.</summary>
    private static readonly HashSet<string> ProtectedUnits = new(StringComparer.Ordinal)
    {
        "fleethub-agent.service", "TempMonitorAgent.service",
        "systemd-journald.service", "systemd-logind.service", "systemd-udevd.service",
        "dbus.service", "dbus-broker.service",
    };

    /// <summary>Why restarting <paramref name="unit"/> is refused, or null if it may be
    /// restarted. <paramref name="ownUnit"/> is the agent's own unit as its cgroup says, so a
    /// package or test install under another unit name is covered too.</summary>
    public static string? RefusalForUnit(string unit, string? ownUnit)
    {
        if (unit == ownUnit || unit is "fleethub-agent.service" or "TempMonitorAgent.service")
            return $"it runs inside {unit}, the FleetHub agent's own service -- restarting that "
                   + "would restart the agent and lose this result. End it instead";
        if (ProtectedUnits.Contains(unit))
            return $"it runs inside {unit}, a core system service";
        return null;
    }

    /// <summary>A unit name safe to hand to systemctl as one argument: no leading dash, no
    /// path, no whitespace. The cgroup path is kernel-written, but it names what a local
    /// process put itself in, and this is about to run as root.</summary>
    [GeneratedRegex(@"^[A-Za-z0-9_][A-Za-z0-9:_.@\\-]*\.service$")]
    private static partial Regex UnitName();
}

/// <summary>
/// Reads of one process under a <c>/proc</c> root. A class rather than statics so the tests can
/// point it at a directory they built: every rule above is decided from these reads, and none
/// of it should need a real process to check.
/// </summary>
public sealed class ProcTable(string procRoot = "/proc")
{
    public string Dir(int pid) => Path.Combine(procRoot, pid.ToString(System.Globalization.CultureInfo.InvariantCulture));

    public string? Name(int pid) => ProcessReader.NameOf(Dir(pid));

    /// <summary>The process's start time in ticks, which with the pid is its identity -- a
    /// survivor check that compared pids alone would wait on, or SIGKILL, whatever landed on a
    /// reused number.</summary>
    public long? StartTicks(int pid) => ProcessReader.ParseStat(Read(pid, "stat"))?.StartTicks;

    public int? ParentPid(int pid) => ProcessReader.ParseParentPid(Read(pid, "stat"));

    public string Cgroup(int pid) => Read(pid, "cgroup");

    public bool IsAlive(int pid, long startTicks) => StartTicks(pid) == startTicks;

    /// <summary>Every descendant of <paramref name="pid"/>, children before grandchildren.
    /// One walk of /proc builds the parent map; a process started mid-walk is missed, which is
    /// what Windows' own tree kill does too.</summary>
    public IReadOnlyList<int> Descendants(int pid)
    {
        var children = ChildrenByParent();
        var result = new List<int>();
        var queue = new Queue<int>([pid]);
        var seen = new HashSet<int> { pid };
        while (queue.Count > 0)
        {
            if (!children.TryGetValue(queue.Dequeue(), out var list)) continue;
            foreach (var child in list)
            {
                if (!seen.Add(child)) continue;   // a cycle cannot happen, but /proc is a snapshot
                result.Add(child);
                queue.Enqueue(child);
            }
        }
        return result;
    }

    /// <summary>One walk of /proc: every pid, filed under its parent.</summary>
    private Dictionary<int, List<int>> ChildrenByParent()
    {
        var children = new Dictionary<int, List<int>>();
        List<string> dirs;
        try { dirs = Directory.EnumerateDirectories(procRoot).ToList(); }
        catch { return children; }
        foreach (var dir in dirs)
        {
            if (!int.TryParse(Path.GetFileName(dir), out var child)) continue;
            if (ParentPid(child) is not { } parent) continue;
            if (!children.TryGetValue(parent, out var list)) children[parent] = list = [];
            list.Add(child);
        }
        return children;
    }

    private string Read(int pid, string file)
    {
        try { return File.ReadAllText(Path.Combine(Dir(pid), file)); }
        catch { return ""; }
    }
}

/// <summary>kill(2), and nothing else. Its own class so the executors read as policy.</summary>
internal static class Signals
{
    public const int SIGKILL = 9;
    public const int SIGTERM = 15;

    [DllImport("libc.so.6", EntryPoint = "kill", SetLastError = true)]
    private static extern int SysKill(int pid, int sig);

    /// <summary>True if the signal was delivered or the process was already gone (ESRCH).</summary>
    public static bool Send(int pid, int signal) =>
        SysKill(pid, signal) == 0 || Marshal.GetLastPInvokeError() == 3;
}

/// <summary>
/// kill_process on Linux: end one process, or every instance of one name the operator selected,
/// optionally with everything it started.
///
/// **SIGTERM first, SIGKILL only for what is still there five seconds later.** The Windows
/// agent can only TerminateProcess, which is a hard kill; on Linux a graceful request exists,
/// and a database or an editor given it flushes and exits cleanly where SIGKILL would leave a
/// recovery for the next start. The result says which processes needed the hard kill, so an
/// operator learns that the thing they ended was hung rather than merely busy.
///
/// Every pid is checked against the name that came with it, and a mismatch ends NOTHING for
/// that pid. A pid that has already exited counts as success -- it is gone, as asked.
/// </summary>
public sealed class KillProcessExecutor(ILogger<KillProcessExecutor> log) : ICommandExecutor
{
    public string Type => "kill_process";

    private static readonly TimeSpan TermGrace = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan KillGrace = TimeSpan.FromSeconds(2);

    internal ProcTable Table { get; init; } = new();
    internal int SelfPid { get; init; } = Environment.ProcessId;

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var expected = cmd.Params.GetString("name")?.Trim();
        if (string.IsNullOrEmpty(expected))
            return CommandResult.Fail("kill_process requires params.name");
        var pids = ReadPids(cmd.Params);
        if (pids.Count == 0)
            return CommandResult.Fail("kill_process requires params.pids");
        var tree = cmd.Params.GetBool("tree");

        var plan = Plan(expected, pids, tree);
        foreach (var target in plan.Targets)
            Signals.Send(target.Pid, Signals.SIGTERM);

        var survivors = await WaitAsync(plan.Targets, TermGrace, ct);
        foreach (var target in survivors)
            Signals.Send(target.Pid, Signals.SIGKILL);
        var stuck = survivors.Count > 0 ? await WaitAsync(survivors, KillGrace, ct) : [];

        var hard = survivors.Except(stuck).Where(t => t.Requested).Select(t => t.Pid).ToList();
        var ended = plan.Targets.Where(t => t.Requested).Select(t => t.Pid)
            .Except(survivors.Select(t => t.Pid)).ToList();
        var refused = plan.Refused.ToList();
        refused.AddRange(stuck.Where(t => t.Requested).Select(t => $"{expected} ({t.Pid}) did not exit"));
        if (stuck.Any(t => !t.Requested))
            refused.Add($"{stuck.Count(t => !t.Requested)} of the processes it started did not exit");
        var children = plan.Targets.Count(t => !t.Requested);

        var parts = new List<string>();
        if (ended.Count > 0) parts.Add($"ended {expected} ({string.Join(", ", ended)})");
        if (hard.Count > 0)
            parts.Add($"force-killed {expected} ({string.Join(", ", hard)}) after it ignored a "
                      + $"request to exit for {TermGrace.TotalSeconds:0} s");
        if (children > 0) parts.Add($"with {children} process(es) it started");
        if (plan.Gone.Count > 0) parts.Add($"already gone ({string.Join(", ", plan.Gone)})");
        if (refused.Count > 0) parts.Add($"refused: {string.Join("; ", refused)}");
        var summary = parts.Count > 0 ? string.Join(". ", parts) : "nothing to do";
        if (refused.Count > 0) log.LogInformation("kill_process {Name}: {Summary}", expected, summary);

        // A refusal is a failure even when something else succeeded -- "ended 2 of 3" must
        // not come back green. The Windows agent's rule, for its reason.
        return refused.Count == 0 ? CommandResult.Ok(summary) : CommandResult.Fail(summary);
    }

    internal sealed record Target(int Pid, long Start, bool Requested);

    internal sealed record KillPlan(List<Target> Targets, List<int> Gone, List<string> Refused);

    /// <summary>Decide what will be signalled, before anything is. Pure over <see cref="Table"/>
    /// so the refusals -- the part that protects a machine -- are testable without root.</summary>
    internal KillPlan Plan(string expected, IReadOnlyList<int> pids, bool tree)
    {
        var targets = new List<Target>();
        var gone = new List<int>();
        var refused = new List<string>();
        var taken = new HashSet<int>();

        foreach (var pid in pids)
        {
            var actual = Table.Name(pid);
            var start = Table.StartTicks(pid);
            if (actual is null || start is null)
            {
                gone.Add(pid);
                continue;
            }
            if (RequestedRefusal(expected, pid, actual) is { } why)
            {
                refused.Add(why);
                continue;
            }
            if (taken.Add(pid)) targets.Add(new Target(pid, start.Value, Requested: true));
            if (tree) AddDescendants(pid, targets, taken);
        }
        return new KillPlan(targets, gone, refused);
    }

    /// <summary>Why a pid the operator named may not be ended: reused, or guarded.</summary>
    private string? RequestedRefusal(string expected, int pid, string actual) =>
        !ProcessGuard.NameMatches(expected, actual)
            ? $"{pid} is now {actual}, not {expected}"
            : ProcessGuard.RefusalFor(pid, actual, Table.ParentPid(pid), SelfPid);

    /// <summary>Add everything <paramref name="pid"/> started. A descendant is held to the same
    /// guard: ending a shell's tree must not take the agent down because the agent happened to
    /// be started from it in a test.</summary>
    private void AddDescendants(int pid, List<Target> targets, HashSet<int> taken)
    {
        foreach (var child in Table.Descendants(pid))
        {
            var name = Table.Name(child);
            var start = Table.StartTicks(child);
            if (name is null || start is null) continue;
            if (ProcessGuard.RefusalFor(child, name, Table.ParentPid(child), SelfPid) is not null) continue;
            if (taken.Add(child)) targets.Add(new Target(child, start.Value, Requested: false));
        }
    }

    private async Task<List<Target>> WaitAsync(IReadOnlyCollection<Target> targets, TimeSpan grace,
                                               CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + grace;
        var alive = targets.ToList();
        while (true)
        {
            alive = alive.Where(t => Table.IsAlive(t.Pid, t.Start)).ToList();
            if (alive.Count == 0 || DateTime.UtcNow >= deadline) return alive;
            await Task.Delay(100, ct);
        }
    }

    internal static List<int> ReadPids(System.Text.Json.Nodes.JsonNode? parameters)
    {
        var pids = new List<int>();
        if (parameters?["pids"] is not System.Text.Json.Nodes.JsonArray array) return pids;
        foreach (var node in array)
        {
            if (node is null) continue;
            try
            {
                var pid = node.GetValue<int>();
                if (pid > 0 && !pids.Contains(pid)) pids.Add(pid);
            }
            catch { /* not a number -- the hub validates, so this is belt and braces */ }
        }
        return pids;
    }
}

/// <summary>
/// restart_process on Linux: restart the system service a process belongs to.
///
/// **Only a systemd system service is restarted, and anything else is refused with the
/// reason.** On Windows the agent relaunches an ordinary program from its own image in the
/// user's session. The Linux equivalent was rejected: a process's argv, environment, working
/// directory, controlling terminal and session (a desktop app needs the user's Wayland or X
/// socket and D-Bus session; a daemon started by hand needs whatever shell variables it was
/// given) cannot be read back faithfully from /proc, so the relaunch would start a different
/// program that merely shares a path -- and that difference shows up as "restart broke it".
/// A service has none of that problem, because systemd holds its whole definition, and
/// `systemctl restart` is exactly what an administrator would have typed.
/// </summary>
public sealed class RestartProcessExecutor(ILogger<RestartProcessExecutor> log) : ICommandExecutor
{
    public string Type => "restart_process";

    /// <summary>systemctl waits for the unit's own stop and start timeouts (90 s each by
    /// default); this is the outer bound on a unit that hangs past them.</summary>
    private const int RestartTimeoutSeconds = 200;

    internal ProcTable Table { get; init; } = new();
    internal int SelfPid { get; init; } = Environment.ProcessId;

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var decision = Decide(cmd.Params.GetString("name")?.Trim(), cmd.Params.GetInt("pid", 0));
        if (decision.Unit is not { } unit) return CommandResult.Fail(decision.Refusal!);

        var systemctl = ProcessRunner.ProgramPath("/usr/bin/systemctl", "/bin/systemctl");
        onOutput?.Invoke($"systemctl restart {unit}\n");
        var run = await ProcessRunner.RunAsync(systemctl, ["restart", "--", unit],
                                               RestartTimeoutSeconds, onOutput, ct);
        if (run.TimedOut)
            return CommandResult.Fail($"{unit} did not finish restarting within {RestartTimeoutSeconds} s");
        if (!run.Succeeded)
        {
            log.LogWarning("systemctl restart {Unit} exited {Code}", unit, run.ExitCode);
            return CommandResult.Fail($"systemctl restart {unit} exited {run.ExitCode}: {run.Output.Trim()}"
                                      + $" -- `systemctl status {unit}` in the Terminal tab says why");
        }
        return CommandResult.Ok($"restarted {unit} (the service {decision.Name} ran in)");
    }

    internal sealed record RestartDecision(string? Unit, string? Name, string? Refusal);

    /// <summary>Which unit to restart, or why not. Pure over <see cref="Table"/>.</summary>
    internal RestartDecision Decide(string? expected, int pid)
    {
        if (string.IsNullOrEmpty(expected) || pid <= 0)
            return new(null, null, "restart_process requires params.name and params.pid");

        var actual = Table.Name(pid);
        if (actual is null)
            return new(null, null, $"{expected} ({pid}) is no longer running");
        if (!ProcessGuard.NameMatches(expected, actual))
            return new(null, null, $"{pid} is now {actual}, not {expected} -- the list had moved on, "
                                   + "so nothing was restarted");
        if (ProcessGuard.RefusalFor(pid, actual, Table.ParentPid(pid), SelfPid) is { } why)
            return new(null, null, why);

        // Read fresh, never from the sampler's cache: the question is where it lives now.
        if (ProcessGuard.SystemServiceFor(Table.Cgroup(pid)) is not { } unit)
            return new(null, null,
                $"{actual} ({pid}) is not part of a system service, so there is nothing that "
                + "knows how to start it again the way it was started. End it, then start it "
                + "again from the Terminal tab (or have its user reopen it).");
        var ownUnit = ProcessGuard.SystemServiceFor(Table.Cgroup(SelfPid));
        if (ProcessGuard.RefusalForUnit(unit, ownUnit) is { } unitWhy)
            return new(null, null, $"{actual} ({pid}) cannot be restarted: {unitWhy}");
        return new(unit, actual, null);
    }
}
