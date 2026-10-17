using System.IO.Pipes;
using System.Security.Principal;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.Tickets;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The service's ticket pipe over a real named pipe, with a stand-in hub (roadmap #31).
///
/// The silent failure this exists to catch is the one the whole tray design rests on: **the
/// SID on a filed ticket, and the SID "My tickets" is fetched for, must be the CONNECTING
/// PROCESS's**, read off the pipe -- never the one the tray put in its frame. A unit test of
/// TicketRules proves the frame is ignored; only a real connection proves the server fills the
/// gap from the token rather than from somewhere else. Runs as whoever runs the tests, which
/// is a person in a desktop session -- the same thing a tray is.
/// </summary>
public class TicketPipeTests
{
    private sealed class FakeHub : ITicketUplink
    {
        public readonly List<JsonObject> Submitted = [];
        public readonly List<string> FetchedFor = [];
        public readonly List<(long Id, string Type)> Uploaded = [];
        public readonly List<(long Id, string Sid, string Body)> Replies = [];
        /// <summary>What "My tickets" returns. Big enough, in the stall test, that a push no
        /// longer fits in the pipe's buffer and has to wait for the tray to read.</summary>
        public JsonArray Tickets = [];

        public Task<TicketSubmitResult> SubmitTicketAsync(JsonObject payload, CancellationToken ct)
        {
            lock (Submitted) Submitted.Add(payload);
            return Task.FromResult(new TicketSubmitResult(201, 42, null, null));
        }

        public Task<bool> UploadTicketScreenshotAsync(long id, byte[] image, string type, CancellationToken ct)
        {
            lock (Uploaded) Uploaded.Add((id, type));
            return Task.FromResult(true);
        }

        public Task<JsonArray?> FetchMyTicketsAsync(string sid, CancellationToken ct)
        {
            lock (FetchedFor) FetchedFor.Add(sid);
            return Task.FromResult<JsonArray?>((JsonArray)Tickets.DeepClone());
        }

        public Task<TicketSubmitResult> ReplyToTicketAsync(long id, string sid, string body, CancellationToken ct)
        {
            lock (Replies) Replies.Add((id, sid, body));
            return Task.FromResult(new TicketSubmitResult(201, id, null, null));
        }
    }

    /// <summary>The pipe admits the INTERACTIVE group; a test run from a service account or a
    /// scheduled task has no such membership, and there the test proves nothing.</summary>
    private static bool RunningInteractively()
    {
        using var me = WindowsIdentity.GetCurrent();
        return me.Groups?.Any(g => g.Value == "S-1-5-4") == true;
    }

    private static async Task<JsonObject> Next(Stream pipe, string type)
    {
        while (true)
        {
            var frame = await TrayProtocol.ReadAsync(pipe, default) ?? throw new IOException("closed");
            if (TrayProtocol.TypeOf(frame) == type) return frame;
        }
    }

    [Fact]
    public async Task A_ticket_is_filed_as_whoever_is_on_the_connection()
    {
        if (!RunningInteractively()) return;
        var hub = new FakeHub();
        var server = new TicketPipeServer(NullLogger<TicketPipeServer>.Instance, hub)
        {
            PipeName = $"FleetHub.Tickets.Test.{Guid.NewGuid():N}",
        };
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        var serving = server.RunAsync(cts.Token);
        var me = WindowsIdentity.GetCurrent().User!.Value;

        using (var pipe = new NamedPipeClientStream(".", server.PipeName, PipeDirection.InOut,
                                                    PipeOptions.Asynchronous, TokenImpersonationLevel.Identification))
        {
            await pipe.ConnectAsync(5000, cts.Token);
            await TrayProtocol.WriteAsync(pipe, new JsonObject { ["type"] = "hello", ["version"] = "test" }, cts.Token);

            var policy = await Next(pipe, TrayProtocol.Policy);
            Assert.True(policy["can_submit"]!.GetValue<bool>());
            await Next(pipe, TrayProtocol.Tickets);
            Assert.Contains(me, hub.FetchedFor);
            Assert.Single(server.SessionsWithTray());

            await TrayProtocol.WriteAsync(pipe, new JsonObject
            {
                ["type"] = TrayProtocol.Submit, ["req"] = 9, ["subject"] = "Printer jams",
                ["reporter_sid"] = "S-1-5-21-0-0-0-500",
                ["screenshots"] = new JsonArray(Convert.ToBase64String([0xFF, 0xD8, 0xFF, 0xE0])),
            }, cts.Token);
            var result = await Next(pipe, TrayProtocol.Result);
            Assert.Equal(9, TrayProtocol.ReqOf(result));
            Assert.True(result["ok"]!.GetValue<bool>());
            Assert.Equal(1, result["screenshots_sent"]!.GetValue<int>());

            var filed = Assert.Single(hub.Submitted);
            Assert.Equal(me, filed["reporter_sid"]!.GetValue<string>());
            Assert.Equal(WindowsIdentity.GetCurrent().Name, filed["reporter_name"]!.GetValue<string>());
            Assert.Equal((42L, "image/jpeg"), Assert.Single(hub.Uploaded));

            await TrayProtocol.WriteAsync(pipe, new JsonObject
            {
                ["type"] = TrayProtocol.Reply, ["req"] = 10, ["ticket_id"] = 42, ["body"] = "still jams",
                ["sid"] = "S-1-5-21-0-0-0-500",
            }, cts.Token);
            Assert.True((await Next(pipe, TrayProtocol.Result))["ok"]!.GetValue<bool>());
            Assert.Equal((42L, me, "still jams"), Assert.Single(hub.Replies));
            await Next(pipe, TrayProtocol.Tickets);   // a reply is followed by the fresh list

            // A tray is always reading; the push completes only once it has.
            var pushed = Next(pipe, TrayProtocol.Tickets);
            Assert.True(await server.PushTicketsAsync(cts.Token));
            await pushed;
        }

        // The connection is gone, and so is the session's tray as far as the supervisor knows.
        for (int i = 0; i < 50 && server.SessionsWithTray().Count > 0; i++) await Task.Delay(100);
        Assert.Empty(server.SessionsWithTray());
        cts.Cancel();
        await serving;
    }

    [Fact]
    public async Task A_tray_that_stops_reading_is_dropped_rather_than_stalling_the_service()
    {
        if (!RunningInteractively()) return;
        var hub = new FakeHub
        {
            Tickets = [new JsonObject { ["id"] = 1, ["subject"] = new string('x', 256 * 1024) }],
        };
        var server = new TicketPipeServer(NullLogger<TicketPipeServer>.Instance, hub)
        {
            PipeName = $"FleetHub.Tickets.Test.{Guid.NewGuid():N}",
        };
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(60));
        var serving = server.RunAsync(cts.Token);
        using var pipe = new NamedPipeClientStream(".", server.PipeName, PipeDirection.InOut,
                                                   PipeOptions.Asynchronous, TokenImpersonationLevel.Identification);
        await pipe.ConnectAsync(5000, cts.Token);
        await TrayProtocol.WriteAsync(pipe, new JsonObject { ["type"] = "hello" }, cts.Token);
        // Never read again. The policy and the first list may fit in the pipe; pushes will not.
        for (int i = 0; i < 40 && server.SessionsWithTray().Count == 0; i++) await Task.Delay(50);
        var started = DateTime.UtcNow;
        bool delivered = true;
        for (int i = 0; i < 4 && delivered; i++) delivered = await server.PushTicketsAsync(cts.Token);
        Assert.False(delivered);
        Assert.True(DateTime.UtcNow - started < TimeSpan.FromSeconds(45));
        for (int i = 0; i < 50 && server.SessionsWithTray().Count > 0; i++) await Task.Delay(100);
        Assert.Empty(server.SessionsWithTray());
        cts.Cancel();
        await serving;
    }

    [Fact]
    public async Task A_connection_that_never_says_hello_gets_nothing()
    {
        if (!RunningInteractively()) return;
        var hub = new FakeHub();
        var server = new TicketPipeServer(NullLogger<TicketPipeServer>.Instance, hub)
        {
            PipeName = $"FleetHub.Tickets.Test.{Guid.NewGuid():N}",
        };
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        var serving = server.RunAsync(cts.Token);
        using (var pipe = new NamedPipeClientStream(".", server.PipeName, PipeDirection.InOut,
                                                    PipeOptions.Asynchronous, TokenImpersonationLevel.Identification))
        {
            await pipe.ConnectAsync(5000, cts.Token);
            await TrayProtocol.WriteAsync(pipe, new JsonObject
            {
                ["type"] = TrayProtocol.Submit, ["subject"] = "skipping hello",
            }, cts.Token);
            // The server closes on a first frame that is not hello, without filing anything.
            Assert.Null(await TrayProtocol.ReadAsync(pipe, cts.Token));
        }
        Assert.Empty(hub.Submitted);
        cts.Cancel();
        await serving;
    }
}
