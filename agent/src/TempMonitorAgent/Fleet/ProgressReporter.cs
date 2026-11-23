using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Posts a running deployment's step events to the hub, and carries an operator's Abort
/// back (roadmap #5, agent 3.53.0). One instance per deploy_package command; Worker creates
/// it beside the command's OutputStreamer.
///
/// The same discipline as <see cref="OutputStreamer"/>, and for the same reasons:
///
///  * **One pump, one POST in flight, in order.** A `running` event overtaking the `ok`
///    for the step before it would be harmless at the hub (writes are monotonic) but would
///    make the console's "current step" flicker backwards. Don't parallelise it.
///  * **Best effort, never in the way.** Report() is called from the executor between
///    steps and must not block or throw. A hub that cannot be reached costs the console its
///    live view, never the install: after StreamPostRetries an event is dropped.
///  * **Gone means quiet.** A 403 (the run is over, or not ours) or a 404 (a hub that
///    predates step progress) stops reporting for this command. That second case is what
///    lets this agent deploy against an old hub exactly as 3.52 did.
///
/// **The keepalive is the abort channel.** While a phase runs, its `running` event is
/// re-sent every ProgressKeepaliveMillis. The hub ignores the repeat; the point is the
/// ANSWER, whose `cancel` flag is set once an operator presses Abort. Rejected
/// alternatives: a separate abort command (it would queue behind, or race, the very
/// command it is meant to stop, and needs a second executor slot), and a socket from the
/// hub (agents never accept inbound connections). On `cancel` the reporter cancels
/// <see cref="Token"/>, which the executor runs under, so the step's process tree is killed
/// by ProcessRunner exactly as a timeout would kill it.
/// </summary>
public sealed class ProgressReporter : IStepProgress, IAsyncDisposable
{
    private readonly IProgressSink _sink;
    private readonly string _commandId;
    private readonly Func<int?> _mark;
    private readonly ILogger _log;
    private readonly int _keepaliveMillis;
    private readonly CancellationTokenSource _abort;
    private readonly CancellationTokenSource _stop = new();
    private readonly SemaphoreSlim _wake = new(0);
    private readonly object _gate = new();
    private readonly Queue<StepEvent> _queue = new();
    private readonly Task _pump;

    private StepEvent? _running;    // the phase in flight: what the keepalive re-sends
    private bool _gone;
    private bool _completed;
    private volatile bool _abortRequested;

    /// <param name="mark">The output stream's Mark(): the chunk number to stamp on each
    /// event. Returns null once the stream is capped.</param>
    /// <param name="parent">The command's own token; <see cref="Token"/> is it plus Abort.</param>
    public ProgressReporter(IProgressSink sink, string commandId, Func<int?> mark,
                            CancellationToken parent, ILogger log,
                            int keepaliveMillis = AgentConfig.ProgressKeepaliveMillis)
    {
        _sink = sink;
        _commandId = commandId;
        _mark = mark;
        _log = log;
        _keepaliveMillis = keepaliveMillis;
        _abort = CancellationTokenSource.CreateLinkedTokenSource(parent);
        _pump = Task.Run(PumpAsync);
    }

    /// <summary>The token to run the executor under: cancelled by the service stopping,
    /// or by the hub answering an event with `cancel`.</summary>
    public CancellationToken Token => _abort.Token;

    public bool AbortRequested => _abortRequested;

    public void Report(StepEvent e)
    {
        try
        {
            lock (_gate)
            {
                if (_gone || _completed) return;
            }
            var stamped = e with { OutSeq = _mark() };
            lock (_gate)
            {
                if (_gone || _completed) return;
                _queue.Enqueue(stamped);
                _running = stamped.State == StepState.Running ? stamped : null;
            }
            try { _wake.Release(); } catch (SemaphoreFullException) { /* already awake */ }
        }
        catch (Exception ex)
        {
            // Never into the executor: narration must not break the install it narrates.
            _log.LogDebug("Progress report for {Id} dropped: {Msg}", _commandId, ex.Message);
        }
    }

    /// <summary>Send whatever is still queued and stop. Awaited BEFORE the command's
    /// result is posted: the hub refuses progress for a finished command, so a final
    /// `ok` that lands after the result would be lost and the last step would read
    /// "running" for ever.</summary>
    public async Task CompleteAsync(CancellationToken ct)
    {
        lock (_gate)
        {
            if (_completed) return;
            _completed = true;
            _running = null;
        }
        _stop.Cancel();
        try { await _pump.WaitAsync(ct); } catch (OperationCanceledException) { }
        await DrainAsync(CancellationToken.None);
    }

    private async Task PumpAsync()
    {
        while (true)
        {
            bool signalled;
            try
            {
                signalled = await _wake.WaitAsync(_keepaliveMillis, _stop.Token);
            }
            catch (OperationCanceledException)
            {
                return;     // CompleteAsync drains what is left
            }
            await DrainAsync(_stop.Token);
            if (!signalled) await KeepaliveAsync(_stop.Token);
        }
    }

    private async Task DrainAsync(CancellationToken ct)
    {
        while (true)
        {
            StepEvent next;
            lock (_gate)
            {
                if (_gone || _queue.Count == 0) return;
                next = _queue.Peek();
            }
            await PostWithRetriesAsync(next, ct);
            lock (_gate)
            {
                // Dequeued after the attempt, delivered or given up on, so a concurrent
                // Report() can never be posted ahead of the event before it.
                if (_queue.Count > 0) _queue.Dequeue();
            }
            if (ct.IsCancellationRequested) return;
        }
    }

    private async Task KeepaliveAsync(CancellationToken ct)
    {
        StepEvent? running;
        lock (_gate)
        {
            if (_gone || _completed) return;
            running = _running;
        }
        if (running is { } e) await PostWithRetriesAsync(e, ct);
    }

    private async Task PostWithRetriesAsync(StepEvent e, CancellationToken ct)
    {
        for (var attempt = 1; attempt <= AgentConfig.StreamPostRetries; attempt++)
        {
            ProgressPostResult result;
            try
            {
                result = await _sink.PostProgressAsync(_commandId, e, ct);
            }
            catch (Exception ex)
            {
                _log.LogDebug("Progress post threw for {Id}: {Msg}", _commandId, ex.Message);
                result = new ProgressPostResult(Ok: false, Cancel: false, Gone: false);
            }

            if (result.Gone)
            {
                lock (_gate) { _gone = true; _queue.Clear(); _running = null; }
                return;
            }
            if (result.Cancel) RequestAbort();
            if (result.Ok) return;

            if (attempt < AgentConfig.StreamPostRetries)
            {
                try { await Task.Delay(200 * attempt, ct); }
                catch (OperationCanceledException) { return; }
            }
        }
        _log.LogDebug("Dropped progress event {Phase} {Ordinal} for {Id}",
            e.Phase, e.Ordinal, _commandId);
    }

    private void RequestAbort()
    {
        if (_abortRequested) return;
        _abortRequested = true;
        _log.LogInformation("Deployment {Id} aborted from the console", _commandId);
        try { _abort.Cancel(); } catch (ObjectDisposedException) { /* already finished */ }
    }

    public async ValueTask DisposeAsync()
    {
        await CompleteAsync(CancellationToken.None);
        _stop.Dispose();
        _wake.Dispose();
        _abort.Dispose();
    }
}
