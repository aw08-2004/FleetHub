using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Shell;

/// <summary>
/// Registry of the terminals open on this machine. Thin on purpose: each session is owned
/// end-to-end by its <see cref="PtySessionRunner"/> under a hub-issued id. It exists for two
/// things only:
///
///  * A CAP. Every session is a root shell; an operator reopening a tab in a loop, or a hub
///    bug re-issuing shell_open, must not be able to fill the machine with them.
///  * SHUTDOWN. Disposed by the host at stop, which hangs up every terminal -- a systemd stop
///    or a self-update should not leave root shells behind it. (systemd's own KillMode would
///    catch them too, since they live in the agent's cgroup; this is the graceful half.)
/// </summary>
public sealed class PtySessionManager : IDisposable
{
    private readonly ILogger<PtySessionManager> _log;
    private readonly ConcurrentDictionary<string, PtySessionRunner> _runners = new();

    public PtySessionManager(ILogger<PtySessionManager> log) => _log = log;

    public int OpenCount => _runners.Count;

    /// <summary>Register a runner for the duration of its session. False when the machine is
    /// already at <see cref="AgentConfig.MaxPtySessions"/>.</summary>
    public bool TryAdd(PtySessionRunner runner)
    {
        if (_runners.Count >= AgentConfig.MaxPtySessions)
        {
            _log.LogWarning("Refusing terminal {Id}: already at {Max} open sessions",
                runner.SessionId, AgentConfig.MaxPtySessions);
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
