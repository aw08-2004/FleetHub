using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using Microsoft.Win32.SafeHandles;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Tickets;

/// <summary>What the pipe server needs from the hub. FleetClient is the real one; an
/// interface so the server's refusals can be reasoned about without a hub.</summary>
public interface ITicketUplink
{
    Task<TicketSubmitResult> SubmitTicketAsync(JsonObject payload, CancellationToken ct);
    Task<bool> UploadTicketScreenshotAsync(long ticketId, byte[] image, string contentType,
                                           CancellationToken ct);
    Task<JsonArray?> FetchMyTicketsAsync(string sid, CancellationToken ct);
    Task<TicketSubmitResult> ReplyToTicketAsync(long ticketId, string sid, string body,
                                                CancellationToken ct);
}

/// <summary>The hub's answer to a submit or a reply: the HTTP status (0 when it could not be
/// reached), the new ticket's id, and the hub's own sentence and code when it refused.</summary>
public readonly record struct TicketSubmitResult(int Status, long TicketId, string? Error, string? Code)
{
    public bool Ok => Status is >= 200 and < 300;
}

/// <summary>
/// The service's end of the ticket tray's pipe (roadmap #31 phase A). Runs in LocalSystem,
/// in session 0, for the life of the service.
///
/// **Who is asking is read off the connection, not out of the message.** Each connection's
/// session comes from <c>GetNamedPipeClientSessionId</c> and its user from impersonating the
/// client for long enough to read its token -- the tray connects at Identification level, which
/// lets the service see who it is and nothing more. The SID and account that go on the ticket
/// are those, so a tray cannot file as somebody else, and "My tickets" for a connection is that
/// connection's person's tickets only. This is what lets the tray run as the user without the
/// agent token: the token never leaves this process.
///
/// **The pipe's ACL is the other half.** Interactive users may connect (read/write) but may
/// not create instances, so once the service holds the name no user process can stand up a
/// second server under it and catch another person's tray; the tray checks the far end is in
/// session 0 for the window before the service starts. Network logons are denied outright:
/// this is a local channel, and a named pipe is reachable over SMB unless something says not.
///
/// **Bounded everywhere a user process could push.** A frame is length-checked before it is
/// buffered (<see cref="TrayProtocol"/>), each session gets at most
/// <see cref="MaxConnectionsPerSession"/> connections, a connection that does not say hello
/// within <see cref="HelloTimeout"/> is dropped, and submits are rate-limited per person.
/// Rejected: one connection per request. The supervisor needs to know which sessions HAVE a
/// tray, and pushing a reply to a tray needs somewhere to push it; a held connection is both.
/// </summary>
public sealed class TicketPipeServer(ILogger<TicketPipeServer> log, ITicketUplink uplink)
{
    /// <summary>Two, not one: a tray that is restarting overlaps its successor for a moment,
    /// and refusing the new one would leave a session trayless until the next pass.</summary>
    public const int MaxConnectionsPerSession = 2;
    private const int MaxInstances = 32;
    private static readonly TimeSpan HelloTimeout = TimeSpan.FromSeconds(15);

    /// <summary>The pipe's name. A property only so a test can serve on a private name and
    /// never collide with an agent service running on the same PC.</summary>
    internal string PipeName { get; init; } = AgentConfig.TicketPipeName;

    private readonly List<TrayConnection> _connections = [];
    private readonly object _lock = new();
    private readonly SubmitLimiter _limiter = new();
    /// <summary>Replies and "My tickets" requests, per person. Looser than submits -- a
    /// conversation is several messages -- but bounded for the same reason: each one is a hub
    /// request, and a reply is a row and an alert refresh, so a script looping them from a
    /// standard account must not cost the hub without limit. Found in review on PR #146.</summary>
    private readonly SubmitLimiter _replyLimiter = new(perWindow: 20);
    private readonly SubmitLimiter _fetchLimiter = new(perWindow: 60);

    /// <summary>The Windows sessions that currently have a tray connected -- what the
    /// supervisor checks before starting another.</summary>
    public IReadOnlySet<uint> SessionsWithTray()
    {
        lock (_lock) return _connections.Select(c => c.Caller.Session).ToHashSet();
    }

    /// <summary>Accept trays until the service stops. Never throws: a pipe that cannot be
    /// created (the name squatted before the service started, say) is logged and retried,
    /// because the rest of the agent must not go down with the ticket button.</summary>
    public async Task RunAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            NamedPipeServerStream? server = null;
            try
            {
                server = CreateServer();
                await server.WaitForConnectionAsync(ct);
                var accepted = server;
                server = null;
                _ = Task.Run(() => ServeAsync(accepted, ct), CancellationToken.None);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception e)
            {
                log.LogWarning("Ticket pipe could not accept a tray ({Msg}); retrying.", e.Message);
                try { await Task.Delay(TimeSpan.FromSeconds(5), ct); }
                catch (OperationCanceledException) { break; }
            }
            finally
            {
                if (server is not null) await server.DisposeAsync();
            }
        }
    }

    /// <summary>Tell every tray what it may offer now -- after a config change, so a policy
    /// flip reaches a running tray without waiting for its next connection.</summary>
    public async Task BroadcastPolicyAsync(CancellationToken ct)
    {
        foreach (var c in Snapshot())
            await c.TrySendAsync(PolicyFor(c.Caller), ct);
    }

    /// <summary>Close every tray: the hub switched the tray off.</summary>
    public async Task CloseAllAsync(CancellationToken ct)
    {
        foreach (var c in Snapshot())
            await c.TrySendAsync(new JsonObject { ["type"] = TrayProtocol.Exit }, ct);
    }

    /// <summary>Re-fetch each connected person's tickets and push them (phase D). Returns
    /// false when any fetch failed, so the caller keeps the old marker and tries again on the
    /// next heartbeat rather than adopting a marker some tray never saw.</summary>
    public async Task<bool> PushTicketsAsync(CancellationToken ct)
    {
        bool allOk = true;
        foreach (var c in Snapshot())
            allOk &= await SendTicketsAsync(c, 0, ct);
        return allOk;
    }

    private TrayConnection[] Snapshot()
    {
        lock (_lock) return _connections.ToArray();
    }

    private async Task ServeAsync(NamedPipeServerStream pipe, CancellationToken ct)
    {
        TrayConnection? connection = null;
        try
        {
            var caller = Identify(pipe);
            if (caller is null) return;
            connection = new TrayConnection(caller, pipe);
            lock (_lock)
            {
                if (_connections.Count(c => c.Caller.Session == caller.Session) >= MaxConnectionsPerSession)
                {
                    log.LogWarning("Refusing a third tray connection from session {Session}.", caller.Session);
                    return;
                }
                _connections.Add(connection);
            }

            using (var helloWait = CancellationTokenSource.CreateLinkedTokenSource(ct))
            {
                helloWait.CancelAfter(HelloTimeout);
                var hello = await TrayProtocol.ReadAsync(pipe, helloWait.Token);
                if (hello is null || TrayProtocol.TypeOf(hello) != TrayProtocol.Hello) return;
                var trayVersion = hello["version"]?.ToString() ?? "?";
                log.LogInformation(
                    "Ticket tray connected: {Account} in session {Session} (tray {TrayVersion}).",
                    caller.Name, caller.Session, trayVersion);
            }
            await connection.TrySendAsync(PolicyFor(caller), ct);
            await SendTicketsAsync(connection, 0, ct);

            while (!ct.IsCancellationRequested)
            {
                var frame = await TrayProtocol.ReadAsync(pipe, ct);
                if (frame is null) break;
                await HandleAsync(connection, frame, ct);
            }
        }
        catch (OperationCanceledException)
        {
            // The service is stopping, or the hello did not come in time: either way the
            // connection simply ends, which the finally below records.
        }
        catch (Exception e) when (e is IOException or InvalidDataException or ObjectDisposedException)
        {
            log.LogInformation("Ticket tray connection ended: {Msg}", e.Message);
        }
        catch (Exception e)
        {
            log.LogWarning(e, "Ticket tray connection failed");
        }
        finally
        {
            if (connection is not null) lock (_lock) _connections.Remove(connection);
            await pipe.DisposeAsync();
        }
    }

    private async Task HandleAsync(TrayConnection c, JsonObject frame, CancellationToken ct)
    {
        long req = TrayProtocol.ReqOf(frame);
        switch (TrayProtocol.TypeOf(frame))
        {
            case TrayProtocol.Submit:
                await c.TrySendAsync(WithReq(await SubmitAsync(c.Caller, frame, ct), req), ct);
                break;
            case TrayProtocol.Mine:
                // Over the limit the request is answered with a refusal, not with an empty
                // list: an empty list would wipe the person's view of their own tickets.
                if (_fetchLimiter.TryTake(c.Caller.Sid, DateTimeOffset.UtcNow))
                    await SendTicketsAsync(c, req, ct);
                else
                    await c.TrySendAsync(WithReq(Failure("Please wait a moment.", "rate_limited"), req), ct);
                break;
            case TrayProtocol.Reply:
                await c.TrySendAsync(WithReq(await ReplyAsync(c.Caller, frame, ct), req), ct);
                await SendTicketsAsync(c, 0, ct);
                break;
            default:
                // Ignored rather than fatal: a newer tray on an older service (the moment
                // between a self-update's swap and the tray's restart) may say something this
                // build has no word for, and dropping its connection would only flap it.
                break;
        }
    }

    private async Task<JsonObject> SubmitAsync(TrayCaller caller, JsonObject frame, CancellationToken ct)
    {
        if (!TicketRules.CanSubmit(RuntimeConfigStore.Current.TicketSubmitters, caller.Kind))
            return Failure("Your helpdesk only takes tickets from domain accounts.", "submitter_not_allowed");
        var (error, ticket) = TicketRules.Check(frame, caller);
        if (ticket is null) return Failure(error!, "invalid");
        if (!_limiter.TryTake(caller.Sid, DateTimeOffset.UtcNow))
            return Failure("You have sent several tickets in the last few minutes. Please wait a "
                           + "little before sending another.", "rate_limited");

        var sent = await uplink.SubmitTicketAsync(ticket.HubPayload, ct);
        if (!sent.Ok)
        {
            log.LogWarning("Hub refused a ticket from {Account} ({Status}: {Error}).",
                           caller.Name, sent.Status, sent.Error);
            return sent.Status == 0
                ? Failure("The helpdesk could not be reached. Your ticket was NOT sent -- please "
                          + "try again in a few minutes, or call IT.", "unreachable")
                : Failure(sent.Error ?? "The helpdesk did not accept this ticket.", sent.Code ?? "refused");
        }

        // One upload per screenshot, after the ticket exists: a picture lost to a network
        // error leaves a ticket without it, which is still a ticket -- and the person is told
        // how many went, rather than "sent" over a ticket missing the screen that explained it.
        int uploaded = 0;
        foreach (var shot in ticket.Screenshots)
            if (await uplink.UploadTicketScreenshotAsync(sent.TicketId, shot,
                    TicketRules.ImageType(shot) ?? "image/jpeg", ct))
                uploaded++;
        log.LogInformation("Ticket {Id} filed for {Account} with {Uploaded}/{Total} screenshot(s).",
                           sent.TicketId, caller.Name, uploaded, ticket.Screenshots.Count);
        return new JsonObject
        {
            ["type"] = TrayProtocol.Result, ["ok"] = true, ["ticket_id"] = sent.TicketId,
            ["screenshots_sent"] = uploaded, ["screenshots_total"] = ticket.Screenshots.Count,
        };
    }

    private async Task<JsonObject> ReplyAsync(TrayCaller caller, JsonObject frame, CancellationToken ct)
    {
        var text = TicketRules.ReplyText(frame);
        var id = TicketRules.TicketId(frame);
        if (text.Length == 0 || id == 0) return Failure("Please write a reply first.", "invalid");
        if (!_replyLimiter.TryTake(caller.Sid, DateTimeOffset.UtcNow))
            return Failure("You have sent a lot of replies in the last few minutes. Please wait a "
                           + "little, or call IT.", "rate_limited");
        // The SID is the CONNECTION's: a tray naming another person's ticket gets the hub's 404.
        var sent = await uplink.ReplyToTicketAsync(id, caller.Sid, text, ct);
        if (sent.Ok) return new JsonObject { ["type"] = TrayProtocol.Result, ["ok"] = true };
        return sent.Status switch
        {
            0 => Failure("The helpdesk could not be reached. Your reply was NOT sent.", "unreachable"),
            409 => Failure("This ticket is closed. Please open a new one.", "closed"),
            429 => Failure(sent.Error ?? "Too many replies on this ticket. Please call IT.", "rate_limited"),
            _ => Failure(sent.Error ?? "The helpdesk did not accept this reply.", "refused"),
        };
    }

    private async Task<bool> SendTicketsAsync(TrayConnection c, long req, CancellationToken ct)
    {
        var list = await uplink.FetchMyTicketsAsync(c.Caller.Sid, ct);
        if (list is null) return false;
        var message = new JsonObject { ["type"] = TrayProtocol.Tickets, ["tickets"] = list };
        if (req != 0) message["req"] = req;
        return await c.TrySendAsync(message, ct);
    }

    private static JsonObject PolicyFor(TrayCaller caller) => new()
    {
        ["type"] = TrayProtocol.Policy,
        ["can_submit"] = TicketRules.CanSubmit(RuntimeConfigStore.Current.TicketSubmitters, caller.Kind),
        ["account"] = caller.Name,
    };

    private static JsonObject Failure(string error, string code) => new()
    {
        ["type"] = TrayProtocol.Result, ["ok"] = false, ["error"] = error, ["code"] = code,
    };

    private static JsonObject WithReq(JsonObject message, long req)
    {
        if (req != 0) message["req"] = req;
        return message;
    }

    /// <summary>Who connected: the session from the kernel, the user from their own token.
    /// Null -- connection dropped -- for anything that is not a person in an interactive
    /// session, which is the only thing a tray ever is.</summary>
    private TrayCaller? Identify(NamedPipeServerStream pipe)
    {
        if (!GetNamedPipeClientSessionId(pipe.SafePipeHandle, out uint session) || session == 0)
        {
            log.LogWarning("Ticket pipe: dropping a connection from session 0 or of unknown session.");
            return null;
        }
        string? sid = null, account = null;
        try
        {
            pipe.RunAsClient(() =>
            {
                using var identity = WindowsIdentity.GetCurrent(TokenAccessLevels.Query);
                sid = identity.User?.Value;
                account = identity.Name;
            });
        }
        catch (Exception e)
        {
            log.LogWarning("Ticket pipe: could not read who connected ({Msg}).", e.Message);
            return null;
        }
        // S-1-5-18/19/20 are SYSTEM, LOCAL SERVICE and NETWORK SERVICE: no person to ring back.
        if (string.IsNullOrEmpty(sid) || string.IsNullOrEmpty(account)
            || sid is "S-1-5-18" or "S-1-5-19" or "S-1-5-20")
            return null;
        var slash = account.IndexOf('\\');
        var domain = slash > 0 ? account[..slash] : "";
        return new TrayCaller(sid, account, TicketRules.ReporterKind(domain, Environment.MachineName),
                              session);
    }

    private NamedPipeServerStream CreateServer()
    {
        var security = new PipeSecurity();
        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
            PipeAccessRights.FullControl, AccessControlType.Allow));
        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
            PipeAccessRights.FullControl, AccessControlType.Allow));
        // ReadWrite and nothing else: no CreateNewInstance, so a user process cannot add a
        // server instance of its own under this name and receive somebody else's tray.
        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.InteractiveSid, null),
            PipeAccessRights.ReadWrite, AccessControlType.Allow));
        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.NetworkSid, null),
            PipeAccessRights.FullControl, AccessControlType.Deny));
        return NamedPipeServerStreamAcl.Create(
            PipeName, PipeDirection.InOut, MaxInstances,
            // A small out-buffer so a policy or a short list does not wait on the tray; the
            // in-buffer stays 0 because the reader is this process and frames are length-checked.
            PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 0, 64 * 1024, security);
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetNamedPipeClientSessionId(SafePipeHandle pipe, out uint sessionId);

    /// <summary>One connected tray. Writes are serialised: a push and an answer going out at
    /// once would interleave two frames into garbage on the tray's side.
    ///
    /// **And bounded in time.** A pipe write completes only once the other end reads, so a tray
    /// that stops reading -- hung, suspended in a debugger, or written to do exactly that --
    /// would park this write forever, and with it every push queued behind it: one stuck tray
    /// would stop replies reaching every other person on the PC. A send that has not finished
    /// in <see cref="SendTimeout"/> drops the connection instead. Cancelling mid-frame leaves the
    /// stream unusable anyway, and a tray that was merely slow reconnects. Found by
    /// TicketPipeTests, whose first version did not read one frame and stalled the next push.</summary>
    private sealed class TrayConnection(TrayCaller caller, NamedPipeServerStream pipe)
    {
        private static readonly TimeSpan SendTimeout = TimeSpan.FromSeconds(15);
        private readonly SemaphoreSlim _write = new(1, 1);
        public TrayCaller Caller { get; } = caller;

        public async Task<bool> TrySendAsync(JsonObject message, CancellationToken ct)
        {
            using var bounded = CancellationTokenSource.CreateLinkedTokenSource(ct);
            bounded.CancelAfter(SendTimeout);
            try
            {
                await _write.WaitAsync(bounded.Token);
                try { await TrayProtocol.WriteAsync(pipe, message, bounded.Token); }
                finally { _write.Release(); }
                return true;
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                await pipe.DisposeAsync();   // ends the read loop, which removes this connection
                return false;
            }
            catch (Exception e) when (e is IOException or ObjectDisposedException
                                          or OperationCanceledException or InvalidDataException)
            {
                return false;
            }
        }
    }
}
