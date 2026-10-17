using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Remote;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Tickets;

/// <summary>
/// Keeps one ticket tray running in every signed-in session while the hub has the tray on
/// (roadmap #31 phase A), and carries the hub's "your tickets changed" marker to them
/// (phase D). Hosts <see cref="TicketPipeServer"/> for the life of the service.
///
/// **"Has a tray" means "has a tray CONNECTED", not "we launched one".** The pipe server knows
/// which sessions have a live connection, so the supervisor needs no pid bookkeeping that a
/// service restart would lose: after a self-update the old trays lose their pipe and exit,
/// this pass sees sessions with no connection, and starts the new build's tray in each. The
/// same rule covers a tray that crashed, one the person ended in Task Manager, and a session
/// that signed in a moment ago -- all one case, "no connection from session N". A second tray
/// started by mistake finds the first one's mutex and leaves on its own.
///
/// **Started as the user** through <see cref="UserSessionLauncher"/>, never through
/// <see cref="SessionInjector"/>: the tray holds a text box and will hold a file picker, and a
/// SYSTEM process holding those on somebody's desktop is a SYSTEM shell for whoever sits there.
///
/// **Bounded relaunching**, as RemoteSessionSupervisor bounds its own: a tray that dies on
/// startup on one PC must not become a process started every ten seconds forever. See
/// <see cref="TrayLaunchBudget"/>. Rejected: an HKLM Run key (nothing restarts a dead tray,
/// the hub cannot switch it off, and the key outlives an uninstall that forgets it), and
/// session-change notifications (the host's service plumbing does not surface them, and a
/// poll also catches the crashed tray, which no notification would).
/// </summary>
public sealed class TraySupervisor(ILogger<TraySupervisor> log, TicketPipeServer server)
    : BackgroundService
{
    private static readonly TimeSpan PassInterval = TimeSpan.FromSeconds(10);

    private readonly TrayLaunchBudget _budget = new();
    private bool _wasEnabled;
    private string _submitters = "";
    private string _marker = "";
    private int _pushing;

    /// <summary>The ticket marker this agent last delivered to its trays, echoed on every
    /// heartbeat while the tray is on. "" until the first answer, which no hub hashes to, so a
    /// restarted service is always told the current one.</summary>
    public string Marker => Volatile.Read(ref _marker);

    /// <summary>The heartbeat carried a new marker: push every connected person their
    /// tickets, and adopt the marker only once that worked. A failed fetch keeps the old one,
    /// so the next heartbeat brings the same answer and the push is tried again -- a reply is
    /// never lost to one bad request. Fire-and-forget, so a slow hub never stalls the
    /// heartbeat that brought the news; one push at a time.</summary>
    public void OnMarker(string marker, CancellationToken ct)
    {
        if (marker == Marker) return;
        if (server.SessionsWithTray().Count == 0)
        {
            // Nobody to tell. A tray that connects later is sent its tickets on hello.
            Volatile.Write(ref _marker, marker);
            return;
        }
        if (Interlocked.Exchange(ref _pushing, 1) == 1) return;
        _ = Task.Run(async () =>
        {
            try
            {
                if (await server.PushTicketsAsync(ct)) Volatile.Write(ref _marker, marker);
            }
            catch (Exception e) { log.LogDebug("Ticket push failed: {Msg}", e.Message); }
            finally { Volatile.Write(ref _pushing, 0); }
        }, CancellationToken.None);
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var pipe = server.RunAsync(stoppingToken);
        while (!stoppingToken.IsCancellationRequested)
        {
            try { await PassAsync(stoppingToken); }
            catch (Exception e) { log.LogWarning(e, "Ticket tray supervision pass failed"); }

            try { await Task.Delay(PassInterval, stoppingToken); }
            catch (OperationCanceledException) { break; }
        }
        try { await pipe; } catch { /* cancelled with the host */ }
    }

    private async Task PassAsync(CancellationToken ct)
    {
        var config = RuntimeConfigStore.Current;
        if (_wasEnabled && !config.TrayEnabled)
        {
            log.LogInformation("The hub switched the ticket tray off; closing every tray.");
            await server.CloseAllAsync(ct);
        }
        _wasEnabled = config.TrayEnabled;
        if (config.TicketSubmitters != _submitters)
        {
            _submitters = config.TicketSubmitters;
            await server.BroadcastPolicyAsync(ct);
        }
        if (!config.TrayEnabled) return;

        var exePath = Environment.ProcessPath;
        if (string.IsNullOrEmpty(exePath)) return;
        var withTray = server.SessionsWithTray();
        var now = DateTimeOffset.UtcNow;
        foreach (var session in SessionProbe.Enumerate())
        {
            // Active only: a disconnected session (fast user switching, a dropped RDP link) has
            // nobody looking at it, and its tray -- if it has one -- is still running there.
            if (session.SessionId == 0 || session.State != SessionProbe.WtsActive
                || string.IsNullOrEmpty(session.User) || withTray.Contains(session.SessionId))
                continue;
            switch (_budget.Decide(session.SessionId, now))
            {
                case TrayLaunchBudget.Decision.Wait:
                    continue;
                case TrayLaunchBudget.Decision.GiveUp:
                    if (_budget.TakeGiveUpNotice(session.SessionId))
                        log.LogWarning(
                            "The ticket tray in session {Session} ({Account}) keeps exiting; not " +
                            "starting it again for an hour. tray.log in that user's " +
                            "%LOCALAPPDATA%\\FleetHub says why.", session.SessionId, session.Account);
                    continue;
            }
            _budget.Record(session.SessionId, now);
            var result = UserSessionLauncher.LaunchAsSessionUser(
                exePath, Path.GetDirectoryName(exePath) ?? "", session.SessionId,
                AgentConfig.TrayArg, noWindow: true);
            if (result.Ok)
                log.LogInformation("Started the ticket tray for {Account} in session {Session} (pid {Pid}).",
                                   session.Account, session.SessionId, result.Pid);
            else
                log.LogInformation("Could not start the ticket tray in session {Session}: {Error}",
                                   session.SessionId, result.Error);
        }
    }
}

/// <summary>
/// When the supervisor may start a tray in a session again. Pure, so the rules are tested
/// without a session or a process.
///
/// <list type="bullet">
/// <item>Not within <see cref="Settle"/> of the last start: a tray takes a moment to connect,
/// and a pass that ran in that moment would start a second one.</item>
/// <item>At most <see cref="MaxPerHour"/> starts in an hour, after which the session is left
/// alone until the oldest start ages out. A healthy session needs one start per sign-in; five
/// in an hour is a tray failing on startup, and starting it harder will not fix it.</item>
/// </list>
/// </summary>
public sealed class TrayLaunchBudget
{
    public static readonly TimeSpan Settle = TimeSpan.FromSeconds(30);
    public const int MaxPerHour = 5;

    public enum Decision { Launch, Wait, GiveUp }

    private readonly Dictionary<uint, List<DateTimeOffset>> _starts = new();
    private readonly HashSet<uint> _noticed = new();

    public Decision Decide(uint session, DateTimeOffset now)
    {
        if (!_starts.TryGetValue(session, out var starts)) return Decision.Launch;
        starts.RemoveAll(t => now - t >= TimeSpan.FromHours(1));
        if (starts.Count == 0) { _noticed.Remove(session); return Decision.Launch; }
        if (now - starts[^1] < Settle) return Decision.Wait;
        if (starts.Count >= MaxPerHour) return Decision.GiveUp;
        _noticed.Remove(session);
        return Decision.Launch;
    }

    public void Record(uint session, DateTimeOffset now)
    {
        if (!_starts.TryGetValue(session, out var starts)) _starts[session] = starts = [];
        starts.Add(now);
    }

    /// <summary>True the first time a session is given up on, so the warning is logged once
    /// per episode rather than every ten seconds.</summary>
    public bool TakeGiveUpNotice(uint session) => _noticed.Add(session);
}
