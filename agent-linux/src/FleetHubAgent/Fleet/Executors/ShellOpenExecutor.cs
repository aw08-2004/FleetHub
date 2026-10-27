using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet.Shell;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// shell_open: attach a real pseudoterminal to the terminal session the hub has already
/// created, and stay attached until it ends. The Windows agent's ShellOpenExecutor, on a Unix
/// pty instead of ConPTY.
///
/// **Registering this is what turns the console's Linux terminal interactive.** The hub decides
/// between the real terminal and the one-shot run_script box by whether a Linux machine's
/// capability report lists `shell_open` (see fleet-terminal.js), and that report is derived
/// from the dispatcher's executor set -- so there is no version constant to bump and no flag
/// to remember. An agent without this class keeps the one-shot box; one with it gets xterm.
///
/// The only LONG-LIVED command, on purpose: claimed when the operator opens the Terminal tab,
/// completed when the shell exits, so the audit trail reads "this operator held a root shell
/// on this machine from X to Y". What was typed is not recorded here or on the hub. No output
/// travels through the command channel -- the stream goes over the pty endpoints on its own
/// cadence (see PtySessionRunner) -- and the Worker exempts this type from
/// MaxConcurrentCommands, or four idle terminals would block every restart and script.
/// </summary>
public sealed class ShellOpenExecutor : ICommandExecutor
{
    private readonly PtySessionManager _sessions;
    private readonly IPtyChannel _channel;
    private readonly ILogger<ShellOpenExecutor> _log;

    public ShellOpenExecutor(PtySessionManager sessions, IPtyChannel channel,
                             ILogger<ShellOpenExecutor> log)
    {
        _sessions = sessions;
        _channel = channel;
        _log = log;
    }

    public string Type => "shell_open";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var sessionId = cmd.Params.GetString("session_id");
        if (string.IsNullOrWhiteSpace(sessionId))
            return CommandResult.Fail("shell_open requires params.session_id");

        var shell = cmd.Params.GetString("shell") ?? "bash";
        var cols = (short)Math.Clamp(cmd.Params.GetInt("cols", 120), 20, 500);
        var rows = (short)Math.Clamp(cmd.Params.GetInt("rows", 30), 5, 200);

        var runner = new PtySessionRunner(sessionId, _channel, _log);
        if (!_sessions.TryAdd(runner))
        {
            // Tell the hub as well as the command: the console is polling the SESSION, so a
            // failure reported only here would look like a terminal that opened and went quiet.
            const string reason = "too many terminals are already open on this machine";
            await _channel.ReportPtyClosedAsync(sessionId, reason, CancellationToken.None);
            return CommandResult.Fail(reason);
        }

        _log.LogInformation("Opening terminal {Id} ({Shell} {Cols}x{Rows})", sessionId, shell, cols, rows);

        try
        {
            var reason = await runner.RunAsync(shell, cols, rows, ct);
            // Ending is the normal case, not a failure: `exit` is how you close a terminal.
            return CommandResult.Ok($"terminal session ended: {reason}");
        }
        finally
        {
            _sessions.Remove(sessionId);
            runner.Dispose();
        }
    }
}
