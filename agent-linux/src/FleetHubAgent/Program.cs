using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Serilog;
using FleetHubAgent;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;
using FleetHubAgent.State;
using FleetHubAgent.Update;
using FleetHubAgent.Telemetry;
using FleetHubAgent.Patch;

// Composition root, and nothing else -- the same rule the hub's app.py follows.
//
// It is much shorter than the Windows agent's Program.cs, and the difference is almost
// entirely the branches that are missing: that binary re-invokes ITSELF as a session-injected
// helper (--remote-helper, --show-message) because Windows session 0 has no desktop to draw
// on. Linux has no session-0 problem to solve and no desktop features here to solve it for, so
// this starts a host and nothing more.

// stdout only. systemd journals it, rotates it and expires it; a second copy under /var/log
// would be one more thing that can fill a disk. The template matches the Windows agent's file
// log so a line reads the same in either place.
Log.Logger = new LoggerConfiguration()
    .MinimumLevel.Information()
    .WriteTo.Console(
        outputTemplate: "{Timestamp:yyyy-MM-dd HH:mm:ss} {Level:u3} {Message:lj}{NewLine}{Exception}")
    .CreateLogger();

try
{
    var builder = Host.CreateApplicationBuilder(args);

    // Type=notify: systemd learns the agent is UP when the host has actually started, not when
    // the process was forked. Without it a unit ordered after this one starts against an agent
    // that has not read its config yet.
    builder.Services.AddSystemd();
    builder.Services.AddSerilog();

    // Core state + telemetry
    builder.Services.AddSingleton<AgentState>();
    builder.Services.AddSingleton<ISensorSource, ProcSensorReader>();
    builder.Services.AddSingleton(sp =>
        SystemInfo.Read(sp.GetRequiredService<ILoggerFactory>().CreateLogger("SystemInfo")));
    builder.Services.AddSingleton<TelemetryReporter>();

    // Fleet command channel.
    //
    // The client is handed a FUNCTION that builds the capability report rather than a report,
    // and the function resolves the dispatcher from the container rather than closing over one
    // built here. Both are the same decision: the report has to describe the executor set the
    // command loop actually uses, and anything captured or duplicated would describe an agent
    // that does not exist -- which the hub would then believe, and refuse commands on. Lazy
    // resolution also breaks what would otherwise be a construction cycle, since the dispatcher
    // is registered below the client that reports on it.
    builder.Services.AddSingleton<CommandDispatcher>();
    builder.Services.AddSingleton<FleetHubAgent.Update.SelfUninstaller>();
    builder.Services.AddSingleton(sp => new FleetClient(
        sp.GetRequiredService<ILogger<FleetClient>>(),
        sp.GetRequiredService<AgentState>(),
        () => AgentCapabilities.For(sp.GetRequiredService<CommandDispatcher>(),
                                    AgentCapabilities.Implemented))
    {
        // The self-uninstall (roadmap #30). Not a constructor argument: it depends on nothing
        // the client does, and the client's signature is shared by the tests.
        Uninstaller = sp.GetRequiredService<FleetHubAgent.Update.SelfUninstaller>(),
    });

    // The executors, which are the whole of what this agent can be TOLD to do today. Five out
    // of the hub's ~thirty command types, and the gap is deliberate rather than unfinished:
    // the console reads this machine's capability report (derived from exactly this list) and
    // offers nothing else. Anything that arrives anyway gets a result naming the platform --
    // see CommandDispatcher.
    builder.Services.AddSingleton<ICommandExecutor, RestartExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, ShutdownExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RenameExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RunScriptExecutor>();

    // The interactive terminal (0.4.0). Registering ShellOpenExecutor is what puts shell_open
    // in the capability report, and that report is what switches the console's Terminal tab
    // from the one-shot box to a real xterm for this machine. The manager is a singleton so
    // the host disposes it at stop and hangs up every open shell.
    builder.Services.AddSingleton<FleetHub.Shared.Pty.PtySessionManager>();
    builder.Services.AddSingleton<FleetHub.Shared.Pty.IPtyChannel>(sp => sp.GetRequiredService<FleetClient>());
    builder.Services.AddSingleton<ICommandExecutor, ShellOpenExecutor>();

    // What the machine's package manager says is available (roadmap #14's Linux half). Its
    // own loop in Worker, not the heartbeat: an apt dependency solve is seconds of work and
    // the hub calls a machine offline after ninety of them.
    builder.Services.AddSingleton<PatchScanner>();
    builder.Services.AddSingleton<PatchInventoryReporter>();
    builder.Services.AddSingleton<ProcessReporter>();

    // Signed self-update (roadmap #22). Its own manifest and its own train -- the hub
    // picks which by the platform this agent reports, so it can never be handed the
    // Windows build. See SelfUpdater.
    builder.Services.AddSingleton<SelfUpdater>();

    builder.Services.AddHostedService<Worker>();

    var host = builder.Build();
    host.Run();
}
catch (Exception ex)
{
    Log.Fatal(ex, "Agent terminated unexpectedly");
}
finally
{
    Log.CloseAndFlush();
}

return 0;
