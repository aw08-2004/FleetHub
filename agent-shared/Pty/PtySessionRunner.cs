using System.Collections.Concurrent;
using System.Text;
using Microsoft.Extensions.Logging;

namespace FleetHub.Shared.Pty;

// **Why the terminal is split between agent-shared and each agent.** The Linux terminal was
// first written as a port of the Windows one, and SonarCloud's duplication gate refused the PR
// for it, rightly: the runner below, the registry, the hub calls and the timings are rules of
// the HUB PROTOCOL (hub/terminal.py), and two copies of them would mean a seq or cursor bug
// fixed for Windows terminals surviving in Linux ones. What differs is only how a pty is made:
// ConPTY on Windows (TempMonitorAgent's ConPtyProcess + PtySession), a Unix pty on Linux
// (FleetHubAgent's UnixPty + PtySession). Each agent therefore keeps its own IPtySession and
// passes its Start method in as a PtySessionFactory, and everything else is this file.
// Same arrangement and same reason as agent-shared/UpdatePins.cs.

/// <summary>What the runner needs from a live terminal. Each agent implements it over its own
/// pseudoconsole; tests implement it over nothing, so the runner's bookkeeping can be checked on
/// any machine.</summary>
public interface IPtySession : IDisposable
{
    /// <summary>Set once the shell has exited or its output stream has ended.</summary>
    bool Exited { get; }

    /// <summary>Last time output or input moved, for the backstop reap.</summary>
    DateTime LastActivityUtc { get; }

    /// <summary>Write operator input VERBATIM -- no trimming, no added newline. A lone "\r" is
    /// a perfectly good Enter, and 0x03 is Ctrl-C for the pty to interpret.</summary>
    void Write(string data);

    void Resize(short cols, short rows);
}

/// <summary>Starts a shell on a fresh pty. <paramref name="onOutput"/> is called from the
/// session's read pump with decoded text and must not block or throw.</summary>
public delegate IPtySession PtySessionFactory(
    string sessionId, string shell, short cols, short rows, Action<string> onOutput, ILogger log);

/// <summary>
/// Drives one interactive terminal for its whole life: opens the pty, pumps the operator's
/// keystrokes into it, pumps its output back to the hub, and shuts everything down exactly once
/// when any of the several things that can end a session does.
///
/// THE CADENCE IS THE FEATURE. A fleet command polls every 1-10s, which is fine for "run this
/// script" and hopeless for a terminal -- nobody types into something that echoes a second and a
/// half later. So once `shell_open` is claimed this stops using the command queue and runs its
/// own two loops against the pty endpoints: input every PtyTimings.InputPollMillis while the
/// operator types (backing off when they stop), output coalesced for OutputFlushMillis.
///
/// WAYS A SESSION ENDS, all of which must converge on one clean teardown:
///   1. the operator types `exit` (or the shell crashes)  -> the pty stream ends
///   2. the operator closes the tab                        -> hub reports `closing`
///   3. the hub forgets the session (reaped, DB reset)     -> input poll 404s
///   4. the hub decides nobody came back                   -> `closing` again
///      ...or PtyTimings.IdleTimeoutSeconds elapses        -> agent-side backstop reap
///   5. the hub becomes unreachable                        -> see OfflineGraceSeconds
///   6. the agent shuts down / self-updates                -> cancellation token
/// Case 5 is the one worth being deliberate about: a terminal the hub cannot reach is not a
/// terminal, and buffering hopefully while an operator types into the void is worse than saying
/// so. After OfflineGraceSeconds with no successful exchange in EITHER direction it ends.
/// </summary>
public sealed class PtySessionRunner : IDisposable
{
    private readonly string _sessionId;
    private readonly IPtyChannel _channel;
    private readonly ILogger _log;
    private readonly PtySessionFactory _factory;

    private readonly object _bufferGate = new();
    private StringBuilder _buffer = new();

    private IPtySession? _session;
    private int _outSeq;
    private int _inCursor = -1;
    private DateTime _lastInputUtc = DateTime.UtcNow;
    private DateTime _lastHubOkUtc = DateTime.UtcNow;
    private volatile bool _disposed;

    /// <summary>How long the hub may stay unreachable before the session is called dead.
    /// Measured from the last SUCCESSFUL exchange in either direction, not from the last output
    /// post: an idle prompt posts nothing for minutes while its input poll succeeds every
    /// 150ms, and keying on output alone would reap a healthy terminal whose operator stopped
    /// typing.</summary>
    private const int OfflineGraceSeconds = 45;

    /// <summary>Cap on unposted output. Past this the oldest bytes go: the screen is already
    /// wrong by then, and unbounded growth on an unattended machine is worse.</summary>
    private const int MaxBufferedChars = 512 * 1024;

    public string SessionId => _sessionId;

    public PtySessionRunner(string sessionId, IPtyChannel channel, ILogger log, PtySessionFactory factory)
    {
        _sessionId = sessionId;
        _channel = channel;
        _log = log;
        _factory = factory;
    }

    /// <summary>Run the session to completion. Returns a short human-readable reason, which
    /// becomes both the hub's close_reason and the shell_open command's result text (the audit
    /// record of what the operator's terminal did).</summary>
    public async Task<string> RunAsync(string shell, short cols, short rows, CancellationToken ct)
    {
        try
        {
            _session = _factory(_sessionId, shell, cols, rows, Append, _log);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Could not open a pseudoconsole for session {Id}", _sessionId);
            var failure = $"could not open a terminal: {e.Message}";
            await _channel.ReportPtyClosedAsync(_sessionId, failure, CancellationToken.None);
            return failure;
        }

        using var stop = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var outputPump = Task.Run(() => PumpOutputAsync(stop.Token), CancellationToken.None);

        string reason;
        try
        {
            reason = await PumpInputAsync(stop.Token);
        }
        catch (OperationCanceledException)
        {
            reason = "the agent is shutting down";
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Terminal session {Id} failed", _sessionId);
            reason = $"the terminal failed: {e.Message}";
        }
        finally
        {
            // Close the pty FIRST so the shell stops producing, then let the output pump drain
            // what is already buffered -- the operator should see the shell's parting words
            // ("goodbye", an error) rather than lose the last screenful.
            _session?.Dispose();
            await Task.Delay(PtyTimings.OutputFlushMillis * 2, CancellationToken.None);
            await FlushAsync(CancellationToken.None);
            await stop.CancelAsync();
            try { await outputPump; } catch { /* the pump exits on cancellation */ }
        }

        await _channel.ReportPtyClosedAsync(_sessionId, reason, CancellationToken.None);
        _log.LogInformation("Terminal session {Id} ended: {Reason}", _sessionId, reason);
        return reason;
    }

    /// <summary>Called from the session's read pump. Must not block or throw.</summary>
    private void Append(string text)
    {
        if (_disposed || text.Length == 0) return;
        lock (_bufferGate)
        {
            _buffer.Append(text);
            if (_buffer.Length > MaxBufferedChars)
                _buffer.Remove(0, _buffer.Length - MaxBufferedChars);
        }
    }

    /// <summary>Keystrokes in, plus every "is this session still wanted?" check. This loop's
    /// exit is the session's exit.</summary>
    private async Task<string> PumpInputAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            if (_session is null || _session.Exited)
                return "the shell exited";

            var ended = Apply(await _channel.PullPtyInputAsync(_sessionId, _inCursor, ct))
                        ?? StillWanted();
            if (ended is not null) return ended;

            try { await Task.Delay(NextPollDelay(), ct); }
            catch (OperationCanceledException) { break; }
        }
        return "the agent is shutting down";
    }

    /// <summary>Feed one input batch to the terminal. Returns why the session ends, or null to
    /// carry on.</summary>
    private string? Apply(PtyInputBatch batch)
    {
        if (batch.Gone) return "the hub no longer has this session";
        if (!batch.Ok) return null;

        _lastHubOkUtc = DateTime.UtcNow;
        foreach (var item in batch.Items)
        {
            if (item.Kind == "resize") _session!.Resize(item.Cols, item.Rows);
            else _session!.Write(item.Data);   // verbatim: see IPtySession.Write
        }
        // Only advance on something actually arriving: advancing on an EMPTY batch would push
        // the cursor past input the console had not queued yet, and the operator's next
        // keystrokes would never be fetched.
        if (batch.Items.Count > 0)
        {
            _lastInputUtc = DateTime.UtcNow;
            _inCursor = batch.NextSeq - 1;
        }
        return batch.Closing ? "closed by the operator" : null;
    }

    /// <summary>The two silences that end a session from this side. Null while it is wanted.</summary>
    private string? StillWanted()
    {
        if ((DateTime.UtcNow - _lastHubOkUtc).TotalSeconds > OfflineGraceSeconds)
            return "lost contact with the hub";
        // Measured on the SESSION's own traffic, not on keystrokes alone: a build that prints
        // for an hour with nobody typing is not idle.
        if ((DateTime.UtcNow - _session!.LastActivityUtc).TotalSeconds > PtyTimings.IdleTimeoutSeconds)
            return "idle timeout";
        return null;
    }

    private int NextPollDelay() =>
        (DateTime.UtcNow - _lastInputUtc).TotalMilliseconds < PtyTimings.IdleAfterMillis
            ? PtyTimings.InputPollMillis
            : PtyTimings.InputPollIdleMillis;

    private async Task PumpOutputAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(PtyTimings.OutputFlushMillis, ct); }
            catch (OperationCanceledException) { return; }
            await FlushAsync(ct);
        }
    }

    private async Task FlushAsync(CancellationToken ct)
    {
        while (true)
        {
            string payload;
            lock (_bufferGate)
            {
                if (_buffer.Length == 0) return;
                var take = Math.Min(_buffer.Length, PtyTimings.MaxChunkChars);
                payload = _buffer.ToString(0, take);
                _buffer.Remove(0, take);
            }

            if (await _channel.PostPtyOutputAsync(_sessionId, _outSeq, payload, ct))
            {
                // Only advance on success. Retrying the SAME seq is what makes a post that
                // actually landed before timing out a free no-op on the hub; a fresh seq would
                // splice duplicate bytes into the middle of an escape sequence.
                _outSeq++;
                _lastHubOkUtc = DateTime.UtcNow;
                continue;
            }

            // Put it back at the front for the next tick. PumpInputAsync watches _lastHubOkUtc
            // and ends the session if this never recovers.
            lock (_bufferGate)
            {
                var pending = _buffer.ToString();
                _buffer = new StringBuilder(payload.Length + pending.Length);
                _buffer.Append(payload).Append(pending);
                if (_buffer.Length > MaxBufferedChars)
                    _buffer.Remove(0, _buffer.Length - MaxBufferedChars);
            }
            return;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _session?.Dispose();
    }
}

/// <summary>
/// Registry of the terminals open on this machine. Thin on purpose: each session is owned
/// end-to-end by its runner under a hub-issued id. It exists for two things only:
///
///  * A CAP. Every session is a SYSTEM or root shell; an operator reopening a tab in a loop,
///    or a hub bug re-issuing shell_open, must not be able to fill the machine with them.
///  * SHUTDOWN. A singleton the host disposes at stop, which tears every terminal down. (On
///    Windows ConPtyProcess also enlists each child in a kill-on-close job, and on Linux the
///    shells sit in the unit's cgroup, so even a hard exit cannot orphan one -- this is the
///    graceful half.)
/// </summary>
public sealed class PtySessionManager : IDisposable
{
    private readonly ILogger<PtySessionManager> _log;
    private readonly ConcurrentDictionary<string, PtySessionRunner> _runners = new();

    public PtySessionManager(ILogger<PtySessionManager> log) => _log = log;

    /// <summary>True while any terminal is open. Terminals do not use the command channel once
    /// opened, so nothing changes cadence for them; this is for diagnostics.</summary>
    public bool AnyOpen => !_runners.IsEmpty;

    public int OpenCount => _runners.Count;

    /// <summary>Register a runner for the duration of its session. False when the machine is
    /// already at <see cref="PtyTimings.MaxSessions"/>.</summary>
    public bool TryAdd(PtySessionRunner runner)
    {
        if (_runners.Count >= PtyTimings.MaxSessions)
        {
            _log.LogWarning("Refusing terminal {Id}: already at {Max} open sessions",
                runner.SessionId, PtyTimings.MaxSessions);
            return false;
        }
        return _runners.TryAdd(runner.SessionId, runner);
    }

    public void Remove(string sessionId) => _runners.TryRemove(sessionId, out _);

    public void Dispose()
    {
        foreach (var (id, runner) in _runners.ToArray())
        {
            _runners.TryRemove(id, out _);
            try { runner.Dispose(); } catch { /* shutting down anyway */ }
        }
    }
}

/// <summary>
/// The body of a shell_open command, shared by both agents' ShellOpenExecutor, which differ
/// only in their command and result types. Returns (success, result text).
///
/// The command is the session's LIFETIME: claimed when the operator opens the Terminal tab,
/// completed when the shell exits, so the audit trail reads "this operator held a console on
/// this machine from X to Y". What was typed is not recorded, here or on the hub.
/// </summary>
public static class PtyShellOpen
{
    public static async Task<(bool Ok, string Text)> RunAsync(
        PtySessionManager sessions, IPtyChannel channel, ILogger log, PtySessionFactory factory,
        string sessionId, string shell, short cols, short rows, CancellationToken ct)
    {
        var runner = new PtySessionRunner(sessionId, channel, log, factory);
        if (!sessions.TryAdd(runner))
        {
            // Tell the hub as well as the command: the console is polling the SESSION, so a
            // failure reported only as the command's result would look like a terminal that
            // opened and then said nothing.
            const string reason = "too many terminals are already open on this machine";
            await channel.ReportPtyClosedAsync(sessionId, reason, CancellationToken.None);
            runner.Dispose();
            return (false, reason);
        }
        try
        {
            var reason = await runner.RunAsync(shell, cols, rows, ct);
            // Ending is the normal case, not a failure: `exit` is how you close a terminal.
            return (true, $"terminal session ended: {reason}");
        }
        finally
        {
            sessions.Remove(sessionId);
            runner.Dispose();
        }
    }

    /// <summary>The size the hub asked for, clamped to what a pty will accept.</summary>
    public static (short Cols, short Rows) ClampSize(int cols, int rows) =>
        ((short)Math.Clamp(cols, 20, 500), (short)Math.Clamp(rows, 5, 200));
}
