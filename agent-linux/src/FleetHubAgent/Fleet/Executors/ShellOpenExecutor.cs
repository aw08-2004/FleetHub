using Microsoft.Extensions.Logging;
using FleetHub.Shared.Pty;
using FleetHubAgent.Fleet.Shell;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// shell_open: attach a Unix pseudoterminal to the terminal session the hub has already
/// created, and stay attached until it ends. The session itself is agent-shared/Pty
/// (PtyShellOpen, the same code the Windows agent runs); what is this agent's own is
/// <see cref="PtySession.Start"/>, a login shell on a Unix pty.
///
/// **Registering this is what turns the console's Linux terminal interactive.** The hub picks
/// between the real terminal and the one-shot run_script box by whether a Linux machine's
/// capability report lists `shell_open` (see fleet-terminal.js), and that report is derived
/// from the dispatcher's executor set -- so there is no version constant to bump and no flag
/// to remember.
///
/// The only LONG-LIVED command, so the Worker exempts it from MaxConcurrentCommands: counting
/// it would let four idle terminals block every restart and script on the machine.
/// </summary>
public sealed class ShellOpenExecutor(PtySessionManager sessions, IPtyChannel channel,
                                      ILogger<ShellOpenExecutor> log) : ICommandExecutor
{
    public string Type => "shell_open";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var sessionId = cmd.Params.GetString("session_id");
        if (string.IsNullOrWhiteSpace(sessionId))
            return CommandResult.Fail("shell_open requires params.session_id");

        var shell = cmd.Params.GetString("shell") ?? "bash";
        var (cols, rows) = PtyShellOpen.ClampSize(cmd.Params.GetInt("cols", 120), cmd.Params.GetInt("rows", 30));
        log.LogInformation("Opening terminal {Id} ({Shell} {Cols}x{Rows})", sessionId, shell, cols, rows);

        var (ok, text) = await PtyShellOpen.RunAsync(
            sessions, channel, log, PtySession.Start, sessionId, shell, cols, rows, ct);
        return ok ? CommandResult.Ok(text) : CommandResult.Fail(text);
    }
}
