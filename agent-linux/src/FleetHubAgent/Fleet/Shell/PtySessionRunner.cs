using System.Text;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Shell;

/// <summary>
/// Drives one interactive terminal for its whole life: opens the pty, pumps the operator's
/// keystrokes into it, pumps its output back to the hub, and tears everything down exactly
/// once when any of the several things that can end a session does.
///
/// **A port of the Windows agent's PtySessionRunner, kept line-for-line where it can be.** The
/// cadence, the seq rules and the six ways a session ends are facts about the hub protocol
/// (hub/terminal.py), not about the operating system, and two runners that drifted apart would
/// mean a bug fixed for Windows terminals quietly surviving in Linux ones. The one structural
/// difference is the <see cref="PtySessionFactory"/>: injected so these rules can be tested
/// against a fake terminal on a machine that cannot open a real one.
///
/// THE CADENCE IS THE FEATURE. The command queue polls every 1-10s, hopeless for typing, so
/// once `shell_open` is claimed this runs its own two loops against the pty endpoints: input
/// every PtyInputPollMillis while the operator types (backing off when they stop), output
/// coalesced for PtyOutputFlushMillis and posted.
///
/// WAYS A SESSION ENDS, all converging on one teardown:
///   1. the operator types `exit` (or the shell dies)  -> the pty stream ends
///   2. the operator closes the tab                     -> hub reports `closing`
///   3. the hub forgets the session                     -> input poll 404s
///   4. the hub decides nobody came back                -> `closing` again
///      ...or PtyIdleTimeoutSeconds elapses             -> agent-side backstop
///   5. the hub becomes unreachable                     -> OfflineGraceSeconds
///   6. the agent stops / self-updates                  -> cancellation token
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

    /// <summary>How long the hub may stay unreachable before the session is called dead,
    /// measured from the last SUCCESSFUL exchange in either direction -- an idle prompt posts
    /// nothing for minutes while its input poll succeeds every 150ms.</summary>
    private const int OfflineGraceSeconds = 45;

    /// <summary>Cap on unposted output. Past this the oldest bytes go: the screen is already
    /// wrong by then, and unbounded growth on an unattended machine is worse.</summary>
    private const int MaxBufferedChars = 512 * 1024;

    public string SessionId => _sessionId;

    public PtySessionRunner(string sessionId, IPtyChannel channel, ILogger log,
                            PtySessionFactory? factory = null)
    {
        _sessionId = sessionId;
        _channel = channel;
        _log = log;
        _factory = factory ?? PtySession.Start;
    }

    /// <summary>Run the session to completion. Returns a short human-readable reason, which
    /// becomes both the hub's close_reason and the shell_open command's result text.</summary>
    public async Task<string> RunAsync(string shell, short cols, short rows, CancellationToken ct)
    {
        try
        {
            _session = _factory(_sessionId, shell, cols, rows, Append, _log);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Could not open a pseudoterminal for session {Id}", _sessionId);
            var failure = $"could not open a terminal: {e.Message}";
            await _channel.ReportPtyClosedAsync(_sessionId, failure, CancellationToken.None);
            return failure;
        }

        using var stop = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var outputPump = Task.Run(() => PumpOutputAsync(stop.Token), CancellationToken.None);

        var reason = "the shell exited";
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
            // Close the pty FIRST so the shell stops producing, then drain what is already
            // buffered -- the operator should see the shell's parting words.
            _session?.Dispose();
            await Task.Delay(AgentConfig.PtyOutputFlushMillis * 2, CancellationToken.None);
            await FlushAsync(CancellationToken.None);
            stop.Cancel();
            try { await outputPump; } catch { /* pump exits on cancellation */ }
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

            var batch = await _channel.PullPtyInputAsync(_sessionId, _inCursor, ct);
            if (batch.Gone) return "the hub no longer has this session";
            if (batch.Ok)
            {
                _lastHubOkUtc = DateTime.UtcNow;
                foreach (var item in batch.Items)
                {
                    if (item.Kind == "resize") _session.Resize(item.Cols, item.Rows);
                    else _session.Write(item.Data);   // verbatim: see PtySession.Write
                }
                // Only advance on something actually arriving: advancing on an EMPTY batch
                // would push the cursor past input the console had not queued yet.
                if (batch.Items.Count > 0)
                {
                    _lastInputUtc = DateTime.UtcNow;
                    _inCursor = batch.NextSeq - 1;
                }
                if (batch.Closing) return "closed by the operator";
            }

            if ((DateTime.UtcNow - _lastHubOkUtc).TotalSeconds > OfflineGraceSeconds)
                return "lost contact with the hub";

            // The backstop reap, measured on the session's own traffic: a build printing for
            // an hour with nobody typing is not idle.
            var quietFor = DateTime.UtcNow - _session.LastActivityUtc;
            if (quietFor.TotalSeconds > AgentConfig.PtyIdleTimeoutSeconds)
                return "idle timeout";

            var typingRecently = (DateTime.UtcNow - _lastInputUtc).TotalMilliseconds < AgentConfig.PtyIdleAfterMillis;
            var delay = typingRecently ? AgentConfig.PtyInputPollMillis : AgentConfig.PtyInputPollIdleMillis;
            try { await Task.Delay(delay, ct); }
            catch (OperationCanceledException) { break; }
        }
        return "the agent is shutting down";
    }

    private async Task PumpOutputAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(AgentConfig.PtyOutputFlushMillis, ct); }
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
                var take = Math.Min(_buffer.Length, AgentConfig.PtyMaxChunkChars);
                payload = _buffer.ToString(0, take);
                _buffer.Remove(0, take);
            }

            var posted = await _channel.PostPtyOutputAsync(_sessionId, _outSeq, payload, ct);
            if (posted)
            {
                // Only advance on success. Retrying the SAME seq is what makes a post that
                // landed before timing out a free no-op on the hub; a fresh seq would splice
                // duplicate bytes into the middle of an escape sequence.
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
