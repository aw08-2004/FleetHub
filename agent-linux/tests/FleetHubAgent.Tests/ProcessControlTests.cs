using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Tests;

/// <summary>
/// End task and Restart on Linux (0.5.0), decided against a fake /proc.
///
/// **The silent failure this file exists to catch is a one-click outage.** The card's row menu
/// appears for any process an operator may see, and the agent is the only thing between that
/// menu and pid 1, the journal, or the agent itself. A guard that matched names the Windows way
/// (case-folded) or a restart that took the innermost `.service` in a cgroup path -- which for
/// a desktop app is `user@1000.service`, the person's whole session -- would pass every test
/// that only checked the happy path.
///
/// The second is a pid reused between the snapshot and the click: the name check must stop
/// it, and must compute the name exactly as the card did.
/// </summary>
public sealed class ProcessControlTests : IDisposable
{
    private readonly string _proc = Path.Combine(Path.GetTempPath(), "fh-proc-" + Guid.NewGuid().ToString("N"));

    public ProcessControlTests() => Directory.CreateDirectory(_proc);

    public void Dispose()
    {
        try { Directory.Delete(_proc, recursive: true); } catch { /* temp */ }
    }

    /// <summary>A process directory with the three files the agent reads. No `exe` link, so
    /// the name falls back to status's Name -- the same rule the card applies.</summary>
    private void Add(int pid, string name, int ppid, long start = 1000,
                     string cgroup = "0::/user.slice/user-1000.slice/session-2.scope")
    {
        var dir = Path.Combine(_proc, pid.ToString());
        Directory.CreateDirectory(dir);
        var rest = new string[22];
        Array.Fill(rest, "0");
        rest[0] = "S";
        rest[1] = ppid.ToString();
        rest[19] = start.ToString();
        File.WriteAllText(Path.Combine(dir, "stat"), $"{pid} ({name}) {string.Join(' ', rest)}");
        File.WriteAllText(Path.Combine(dir, "status"), $"Name:\t{name}\nUid:\t0\t0\t0\t0\n");
        File.WriteAllText(Path.Combine(dir, "cgroup"), cgroup + "\n");
    }

    private KillProcessExecutor Killer(int self = 99999) =>
        new(NullLogger<KillProcessExecutor>.Instance) { Table = new ProcTable(_proc), SelfPid = self };

    private RestartProcessExecutor Restarter(int self = 99999) =>
        new(NullLogger<RestartProcessExecutor>.Instance) { Table = new ProcTable(_proc), SelfPid = self };

    // ---------------------------------------------------------------- the guard

    [Theory]
    [InlineData(1, "systemd", 0)]
    [InlineData(1, "bash", 0)]                  // pid 1 whatever it calls itself
    [InlineData(2, "kthreadd", 0)]
    [InlineData(57, "kworker/0:1", 2)]          // any child of kthreadd
    [InlineData(800, "systemd", 1)]             // a user's systemd --user
    [InlineData(400, "systemd-journald", 1)]
    [InlineData(410, "dbus-daemon", 1)]
    [InlineData(500, "fleethub-agent", 1)]
    public void Core_processes_are_refused(int pid, string name, int ppid)
    {
        Assert.NotNull(ProcessGuard.RefusalFor(pid, name, ppid, selfPid: 99999));
    }

    [Fact]
    public void The_agent_is_refused_by_pid_whatever_its_name()
    {
        Assert.Contains("agent itself", ProcessGuard.RefusalFor(4242, "dotnet", 1, selfPid: 4242));
    }

    [Theory]
    [InlineData("firefox")]
    [InlineData("Systemd")]                     // case matters on Linux: a user binary
    [InlineData("nginx")]
    public void Ordinary_processes_are_allowed(string name)
    {
        Assert.Null(ProcessGuard.RefusalFor(3000, name, 1, selfPid: 99999));
    }

    [Fact]
    public void Names_match_exactly()
    {
        Assert.True(ProcessGuard.NameMatches(" nginx ", "nginx"));
        Assert.False(ProcessGuard.NameMatches("Nginx", "nginx"));
        Assert.False(ProcessGuard.NameMatches("nginx", ""));
    }

    // ---------------------------------------------------------------- which service

    [Theory]
    [InlineData("0::/system.slice/nginx.service", "nginx.service")]
    [InlineData("0::/system.slice/getty@tty1.service", "getty@tty1.service")]
    [InlineData("12:pids:/system.slice/x.service\n1:name=systemd:/system.slice/cron.service", "cron.service")]
    [InlineData("0::/system.slice/docker.service/some/child", "docker.service")]
    public void A_system_service_is_found(string cgroup, string unit)
    {
        Assert.Equal(unit, ProcessGuard.SystemServiceFor(cgroup));
    }

    [Theory]
    // The case the card's Services column gets "right" and a restart must not: the innermost
    // .service here is the user's whole session manager.
    [InlineData("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox.scope")]
    [InlineData("0::/user.slice/user-1000.slice/session-2.scope")]
    [InlineData("0::/init.scope")]
    [InlineData("0::/system.slice/-evil.service")]
    [InlineData("0::/system.slice/a b.service")]
    [InlineData("")]
    public void Anything_else_is_not_a_service_to_restart(string cgroup)
    {
        Assert.Null(ProcessGuard.SystemServiceFor(cgroup));
    }

    // ---------------------------------------------------------------- kill planning

    [Fact]
    public void A_reused_pid_ends_nothing()
    {
        Add(3000, "bash", ppid: 1);
        var plan = Killer().Plan("firefox", [3000], tree: false);
        Assert.Empty(plan.Targets);
        Assert.Contains("3000 is now bash, not firefox", Assert.Single(plan.Refused));
    }

    [Fact]
    public void A_gone_pid_is_gone_not_refused()
    {
        var plan = Killer().Plan("firefox", [3000], tree: false);
        Assert.Empty(plan.Refused);
        Assert.Equal([3000], plan.Gone);
    }

    [Fact]
    public void A_protected_pid_is_refused_even_when_the_name_matches()
    {
        Add(400, "systemd-journald", ppid: 1);
        var plan = Killer().Plan("systemd-journald", [400], tree: false);
        Assert.Empty(plan.Targets);
        Assert.Single(plan.Refused);
    }

    [Fact]
    public void A_tree_kill_takes_descendants_but_never_the_agent()
    {
        Add(3000, "bash", ppid: 1);
        Add(3001, "sleep", ppid: 3000);
        Add(3002, "make", ppid: 3001);
        Add(3003, "fleethub-agent", ppid: 3000);   // started from that shell in a test
        Add(3004, "cron", ppid: 1);                // unrelated

        var plan = Killer().Plan("bash", [3000], tree: true);
        Assert.Equal([3000, 3001, 3002], plan.Targets.Select(t => t.Pid).Order());
        Assert.True(plan.Targets.Single(t => t.Pid == 3000).Requested);
        Assert.False(plan.Targets.Single(t => t.Pid == 3002).Requested);
        Assert.Empty(plan.Refused);
    }

    [Fact]
    public void Without_tree_only_the_named_pids_are_taken()
    {
        Add(3000, "bash", ppid: 1);
        Add(3001, "sleep", ppid: 3000);
        Assert.Equal([3000], Killer().Plan("bash", [3000], tree: false).Targets.Select(t => t.Pid));
    }

    [Fact]
    public void Liveness_is_pid_and_start_time()
    {
        Add(3000, "bash", ppid: 1, start: 5000);
        var table = new ProcTable(_proc);
        Assert.True(table.IsAlive(3000, 5000));
        Assert.False(table.IsAlive(3000, 4999));   // a new process on a reused pid
    }

    [Fact]
    public void Pids_are_read_defensively()
    {
        var parameters = JsonNode.Parse("{\"pids\": [12, \"x\", -1, 12, 0, 40]}");
        Assert.Equal([12, 40], KillProcessExecutor.ReadPids(parameters));
    }

    // ---------------------------------------------------------------- restart decisions

    [Fact]
    public void A_service_process_restarts_its_unit()
    {
        Add(700, "nginx", ppid: 1, cgroup: "0::/system.slice/nginx.service");
        var decision = Restarter().Decide("nginx", 700);
        Assert.Equal("nginx.service", decision.Unit);
        Assert.Null(decision.Refusal);
    }

    [Fact]
    public void A_desktop_app_is_refused_with_the_reason()
    {
        Add(900, "firefox", ppid: 800,
            cgroup: "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox.scope");
        var decision = Restarter().Decide("firefox", 900);
        Assert.Null(decision.Unit);
        Assert.Contains("not part of a system service", decision.Refusal);
    }

    [Fact]
    public void A_child_of_the_agent_does_not_restart_the_agent()
    {
        // The beta checklist's own `sleep 600`, started from the Terminal tab: an ordinary
        // name, inside the agent's cgroup. Restart must not resolve to the agent's unit.
        Add(3100, "sleep", ppid: 3000, cgroup: "0::/system.slice/fleethub-agent.service");
        var decision = Restarter().Decide("sleep", 3100);
        Assert.Null(decision.Unit);
        Assert.Contains("FleetHub agent's own service", decision.Refusal);
    }

    [Fact]
    public void The_agents_unit_is_read_from_its_own_cgroup_too()
    {
        // An install under a different unit name is still the agent.
        Add(4242, "fleethub-agent", ppid: 1, cgroup: "0::/system.slice/fh-test.service");
        Add(3100, "sleep", ppid: 4242, cgroup: "0::/system.slice/fh-test.service");
        Assert.Contains("own service", Restarter(self: 4242).Decide("sleep", 3100).Refusal);
    }

    [Theory]
    [InlineData("systemd-journald.service")]
    [InlineData("dbus.service")]
    [InlineData("systemd-logind.service")]
    public void A_helper_inside_a_core_unit_does_not_restart_it(string unit)
    {
        Add(3200, "helper", ppid: 1, cgroup: $"0::/system.slice/{unit}");
        Assert.Contains("core system service", Restarter().Decide("helper", 3200).Refusal);
    }

    [Fact]
    public void Restart_checks_the_name_and_the_guard_first()
    {
        Add(700, "nginx", ppid: 1, cgroup: "0::/system.slice/nginx.service");
        Assert.Contains("is now nginx", Restarter().Decide("apache2", 700).Refusal);
        Assert.Contains("agent itself", Restarter(self: 700).Decide("nginx", 700).Refusal);
        Assert.Contains("no longer running", Restarter().Decide("nginx", 701).Refusal);
    }
}
