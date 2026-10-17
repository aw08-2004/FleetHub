using System.Collections.Concurrent;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text.Json.Nodes;
using Microsoft.Win32.SafeHandles;

namespace TempMonitorAgent.Tickets;

/// <summary>
/// The tray's end of <see cref="AgentConfig.TicketPipeName"/>: one held connection to the
/// agent service, requests answered by <c>req</c>, everything else a push.
///
/// **It checks who it is talking to before it says anything.** The far end must be in session
/// 0, where only services run. Before the service has created the pipe, any process could
/// create one under the same name, and a tray that connected to it would hand over a picture
/// of the person's screen; the service's ACL closes that once it holds the name, and this
/// check closes the window before it does. Connected at Identification level, so the service
/// can read who the person is and cannot act as them.
/// </summary>
internal sealed class TrayPipeClient : IDisposable
{
    private NamedPipeClientStream? _pipe;
    private readonly SemaphoreSlim _write = new(1, 1);
    private readonly ConcurrentDictionary<long, TaskCompletionSource<JsonObject?>> _pending = new();
    private long _nextReq;

    /// <summary>A frame nobody asked for: the policy, new tickets, or the order to close.
    /// Raised on the read loop's thread -- the tray marshals it onto its UI thread.</summary>
    public event Action<JsonObject>? Pushed;

    /// <summary>The service went away (stopped, restarting for an update, or it closed us).</summary>
    public event Action? Disconnected;

    public bool IsConnected => _pipe?.IsConnected == true;

    public async Task<bool> ConnectAsync(TimeSpan timeout, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline && !ct.IsCancellationRequested)
        {
            var pipe = new NamedPipeClientStream(".", AgentConfig.TicketPipeName, PipeDirection.InOut,
                                                 PipeOptions.Asynchronous,
                                                 TokenImpersonationLevel.Identification);
            try
            {
                await pipe.ConnectAsync(2000, ct);
                if (!GetNamedPipeServerSessionId(pipe.SafePipeHandle, out uint session) || session != 0)
                {
                    TrayLog.Write($"Refusing a ticket pipe served from session {session}, not the agent service.");
                    pipe.Dispose();
                    return false;
                }
                _pipe = pipe;
                _ = Task.Run(ReadLoopAsync, CancellationToken.None);
                await SendAsync(new JsonObject { ["type"] = TrayProtocol.Hello, ["version"] = AgentConfig.Version }, ct);
                return true;
            }
            catch (Exception e) when (e is TimeoutException or IOException)
            {
                pipe.Dispose();
                try { await Task.Delay(1000, ct); } catch (OperationCanceledException) { break; }
            }
            catch (OperationCanceledException)
            {
                pipe.Dispose();
                break;
            }
        }
        return false;
    }

    /// <summary>Send a request and wait for its answer; null when the service went away or did
    /// not answer in time.</summary>
    public async Task<JsonObject?> RequestAsync(JsonObject message, TimeSpan timeout)
    {
        long req = Interlocked.Increment(ref _nextReq);
        message["req"] = req;
        var answer = new TaskCompletionSource<JsonObject?>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[req] = answer;
        try
        {
            using var cts = new CancellationTokenSource(timeout);
            await SendAsync(message, cts.Token);
            using (cts.Token.Register(() => answer.TrySetResult(null)))
                return await answer.Task;
        }
        catch (Exception e) when (e is IOException or OperationCanceledException or ObjectDisposedException
                                      or InvalidOperationException)
        {
            return null;
        }
        finally
        {
            _pending.TryRemove(req, out _);
        }
    }

    private async Task SendAsync(JsonObject message, CancellationToken ct)
    {
        var pipe = _pipe ?? throw new InvalidOperationException("not connected");
        await _write.WaitAsync(ct);
        try { await TrayProtocol.WriteAsync(pipe, message, ct); }
        finally { _write.Release(); }
    }

    private async Task ReadLoopAsync()
    {
        try
        {
            while (_pipe is { } pipe)
            {
                var frame = await TrayProtocol.ReadAsync(pipe, CancellationToken.None);
                if (frame is null) break;
                long req = TrayProtocol.ReqOf(frame);
                if (req != 0 && _pending.TryRemove(req, out var waiter)) waiter.TrySetResult(frame);
                else Pushed?.Invoke(frame);
            }
        }
        catch (Exception e) when (e is IOException or InvalidDataException or ObjectDisposedException)
        {
            TrayLog.Write($"Ticket pipe closed: {e.Message}");
        }
        foreach (var waiter in _pending.Values) waiter.TrySetResult(null);
        _pipe?.Dispose();
        _pipe = null;
        Disconnected?.Invoke();
    }

    public void Dispose()
    {
        _pipe?.Dispose();
        _pipe = null;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetNamedPipeServerSessionId(SafePipeHandle pipe, out uint sessionId);
}
