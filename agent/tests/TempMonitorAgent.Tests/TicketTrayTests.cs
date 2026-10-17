using System.Buffers.Binary;
using System.Text.Json.Nodes;
using TempMonitorAgent.State;
using TempMonitorAgent.Tickets;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The ticket tray (roadmap #31 phases A and D), minus the parts that need a desktop.
///
/// The silent failures this file exists to catch:
///   * **A ticket filed as somebody else.** The reporter must come from the pipe connection,
///     never from the tray's frame -- a tray that could name its own SID would let anybody at
///     the PC file, and read "My tickets", as anybody.
///   * **A SYSTEM process buffering whatever a user process sends it.** The frame length is
///     checked before the body is read; a pipe that trusted it would let any signed-in user
///     grow the service's memory without bound.
///   * **A tray restarted forever.** One that dies on startup must stop being relaunched, not
///     become a process started every ten seconds for the life of the PC.
///   * **A reply announced on every restart, or never.** The tray restarts on every sign-in and
///     every agent update; "seen" has to survive that without swallowing the reply that came
///     in overnight.
///   * **The tray switched on by an agent update.** It is off until the hub says otherwise,
///     and a payload without the key must not change that.
/// </summary>
public class TicketTrayTests
{
    private static readonly TrayCaller Ana = new("S-1-5-21-1-1001", @"CORP\ana", "domain", 2);
    private static readonly byte[] Jpeg = [0xFF, 0xD8, 0xFF, 0xE0, 0, 0, 0, 0];
    private static readonly byte[] Png = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0];

    // ------------------------------------------------------------------ framing

    [Fact]
    public async Task A_frame_round_trips()
    {
        using var stream = new MemoryStream();
        await TrayProtocol.WriteAsync(stream, new JsonObject { ["type"] = "hello", ["req"] = 7 }, default);
        stream.Position = 0;
        var frame = await TrayProtocol.ReadAsync(stream, default);
        Assert.NotNull(frame);
        Assert.Equal("hello", TrayProtocol.TypeOf(frame!));
        Assert.Equal(7, TrayProtocol.ReqOf(frame!));
        Assert.Null(await TrayProtocol.ReadAsync(stream, default));   // clean end between frames
    }

    [Fact]
    public async Task An_oversized_length_is_refused_before_anything_is_buffered()
    {
        var header = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(header, TrayProtocol.MaxFrameBytes + 1);
        using var stream = new MemoryStream(header);   // no body at all: it must not wait for one
        await Assert.ThrowsAsync<InvalidDataException>(() => TrayProtocol.ReadAsync(stream, default));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-5)]
    public async Task A_zero_or_negative_length_is_refused(int length)
    {
        var header = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(header, length);
        await Assert.ThrowsAsync<InvalidDataException>(
            () => TrayProtocol.ReadAsync(new MemoryStream(header), default));
    }

    [Fact]
    public async Task A_truncated_or_non_object_frame_is_refused()
    {
        var header = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(header, 100);
        await Assert.ThrowsAsync<InvalidDataException>(
            () => TrayProtocol.ReadAsync(new MemoryStream([.. header, 1, 2, 3]), default));

        var body = "[1,2]"u8.ToArray();
        BinaryPrimitives.WriteInt32LittleEndian(header, body.Length);
        await Assert.ThrowsAsync<InvalidDataException>(
            () => TrayProtocol.ReadAsync(new MemoryStream([.. header, .. body]), default));
    }

    [Fact]
    public void A_hostile_frame_cannot_throw_out_of_the_accessors()
    {
        var frame = new JsonObject { ["type"] = 5, ["req"] = "x" };
        Assert.Equal("", TrayProtocol.TypeOf(frame));
        Assert.Equal(0, TrayProtocol.ReqOf(frame));
    }

    // ------------------------------------------------------------------ the ticket

    [Fact]
    public void The_reporter_comes_from_the_connection_never_the_frame()
    {
        var frame = new JsonObject
        {
            ["subject"] = "Outlook will not start",
            ["reporter_sid"] = "S-1-5-21-9-500", ["reporter_name"] = @"CORP\boss",
            ["reporter_kind"] = "domain",
        };
        var (error, ticket) = TicketRules.Check(frame, Ana);
        Assert.Null(error);
        Assert.Equal(Ana.Sid, ticket!.HubPayload["reporter_sid"]!.GetValue<string>());
        Assert.Equal(Ana.Name, ticket.HubPayload["reporter_name"]!.GetValue<string>());
        Assert.Equal("domain", ticket.HubPayload["reporter_kind"]!.GetValue<string>());
    }

    [Fact]
    public void A_ticket_needs_a_subject_and_a_known_urgency()
    {
        Assert.NotNull(TicketRules.Check(new JsonObject { ["subject"] = "   " }, Ana).Error);
        Assert.NotNull(TicketRules.Check(new JsonObject { ["subject"] = "x", ["urgency"] = "panic" }, Ana).Error);
        var (_, ticket) = TicketRules.Check(new JsonObject { ["subject"] = "x" }, Ana);
        Assert.Equal("normal", ticket!.HubPayload["urgency"]!.GetValue<string>());
    }

    [Fact]
    public void Long_text_is_trimmed_not_refused()
    {
        var (error, ticket) = TicketRules.Check(new JsonObject
        {
            ["subject"] = new string('s', 500), ["body"] = new string('b', 20_000),
        }, Ana);
        Assert.Null(error);
        Assert.Equal(TicketRules.MaxSubjectChars, ticket!.HubPayload["subject"]!.GetValue<string>().Length);
        Assert.Equal(TicketRules.MaxBodyChars, ticket.HubPayload["body"]!.GetValue<string>().Length);
    }

    [Fact]
    public void A_due_time_is_kept_only_when_it_is_a_real_time()
    {
        var with = TicketRules.Check(new JsonObject { ["subject"] = "x", ["due_at"] = 1_900_000_000L }, Ana);
        Assert.Equal(1_900_000_000L, with.Ticket!.HubPayload["due_at"]!.GetValue<long>());
        var without = TicketRules.Check(new JsonObject { ["subject"] = "x", ["due_at"] = "tomorrow" }, Ana);
        Assert.False(without.Ticket!.HubPayload.ContainsKey("due_at"));
    }

    [Fact]
    public void Screenshots_are_checked_by_their_bytes_count_and_size()
    {
        static JsonObject With(params byte[][] shots) => new()
        {
            ["subject"] = "x",
            ["screenshots"] = new JsonArray(shots.Select(s => (JsonNode)Convert.ToBase64String(s)).ToArray()),
        };
        var ok = TicketRules.Check(With(Jpeg, Png), Ana);
        Assert.Null(ok.Error);
        Assert.Equal(2, ok.Ticket!.Screenshots.Count);

        Assert.NotNull(TicketRules.Check(With("<svg onload=x>"u8.ToArray()), Ana).Error);
        Assert.NotNull(TicketRules.Check(With(Enumerable.Repeat(Jpeg, TrayProtocol.MaxScreenshots + 1).ToArray()), Ana).Error);
        var huge = new byte[TrayProtocol.MaxScreenshotBytes + 1];
        Jpeg.CopyTo(huge, 0);
        Assert.NotNull(TicketRules.Check(With(huge), Ana).Error);
        var notBase64 = new JsonObject { ["subject"] = "x", ["screenshots"] = new JsonArray("!!!") };
        Assert.NotNull(TicketRules.Check(notBase64, Ana).Error);
    }

    [Fact]
    public void The_service_cap_sits_under_the_hubs()
    {
        // tickets.MAX_ATTACHMENT_BYTES is 5 MB and MAX_ATTACHMENTS 6. A screenshot the service
        // accepts and the hub then refuses is a ticket the person was told was complete.
        Assert.True(TrayProtocol.MaxScreenshotBytes < 5 * 1024 * 1024);
        Assert.Equal(6, TrayProtocol.MaxScreenshots);
        // ...and a frame carrying all of them at the cap still fits through the pipe.
        Assert.True(TrayProtocol.MaxFrameBytes > TrayProtocol.MaxScreenshots
                    * (TrayProtocol.MaxScreenshotBytes / 3 * 4 + 4));
    }

    [Theory]
    [InlineData("PC-7", "PC-7", "local")]
    [InlineData("pc-7", "PC-7", "local")]
    [InlineData("CORP", "PC-7", "domain")]
    [InlineData("AzureAD", "PC-7", "domain")]
    public void Domain_or_local_is_decided_by_the_account_domain(string domain, string machine, string kind)
        => Assert.Equal(kind, TicketRules.ReporterKind(domain, machine));

    [Fact]
    public void The_submit_button_follows_the_hubs_policy()
    {
        Assert.True(TicketRules.CanSubmit("any", "local"));
        Assert.True(TicketRules.CanSubmit("domain", "domain"));
        Assert.False(TicketRules.CanSubmit("domain", "local"));
    }

    // ------------------------------------------------------------------ limits

    [Fact]
    public void Submits_are_limited_per_person_and_a_refusal_is_not_counted()
    {
        var limiter = new SubmitLimiter(perWindow: 2, window: TimeSpan.FromMinutes(10));
        var t0 = DateTimeOffset.UnixEpoch;
        Assert.True(limiter.TryTake("ana", t0));
        Assert.True(limiter.TryTake("ana", t0.AddMinutes(1)));
        Assert.False(limiter.TryTake("ana", t0.AddMinutes(2)));
        Assert.True(limiter.TryTake("ben", t0.AddMinutes(2)));          // per person
        Assert.True(limiter.TryTake("ana", t0.AddMinutes(10)));         // the first aged out
        Assert.False(limiter.TryTake("ana", t0.AddMinutes(10.5)));
    }

    [Fact]
    public void A_tray_that_keeps_dying_stops_being_restarted()
    {
        var budget = new TrayLaunchBudget();
        var t = DateTimeOffset.UnixEpoch;
        Assert.Equal(TrayLaunchBudget.Decision.Launch, budget.Decide(3, t));
        budget.Record(3, t);
        Assert.Equal(TrayLaunchBudget.Decision.Wait, budget.Decide(3, t.AddSeconds(10)));
        for (int i = 1; i < TrayLaunchBudget.MaxPerHour; i++)
        {
            t = t.AddMinutes(1);
            Assert.Equal(TrayLaunchBudget.Decision.Launch, budget.Decide(3, t));
            budget.Record(3, t);
        }
        t = t.AddMinutes(1);
        Assert.Equal(TrayLaunchBudget.Decision.GiveUp, budget.Decide(3, t));
        Assert.True(budget.TakeGiveUpNotice(3));
        Assert.False(budget.TakeGiveUpNotice(3));                       // logged once
        Assert.Equal(TrayLaunchBudget.Decision.Launch, budget.Decide(4, t));   // per session
        Assert.Equal(TrayLaunchBudget.Decision.Launch, budget.Decide(3, t.AddHours(1)));
    }

    // ------------------------------------------------------------------ phase D

    private static JsonArray Tickets(params (long Id, string Status, string[] Operator)[] tickets) =>
        new(tickets.Select(t => (JsonNode)new JsonObject
        {
            ["id"] = t.Id, ["subject"] = $"Ticket {t.Id}", ["status"] = t.Status,
            ["messages"] = new JsonArray(t.Operator.Select(b => (JsonNode)new JsonObject
            {
                ["author_kind"] = "operator", ["body"] = b,
            }).Append(new JsonObject { ["author_kind"] = "reporter", ["body"] = "me" }).ToArray()),
        }).ToArray());

    [Fact]
    public void The_first_list_on_a_pc_is_taken_as_read()
    {
        var (notices, now) = ReplyTracker.Diff(null, Tickets((1, "open", ["Please reboot"])));
        Assert.Empty(notices);
        Assert.Equal(1, now[1].OperatorMessages);
    }

    [Fact]
    public void A_new_reply_is_announced_once()
    {
        var (_, seen) = ReplyTracker.Diff(null, Tickets((1, "open", [])));
        var (notices, after) = ReplyTracker.Diff(seen, Tickets((1, "open", ["Please reboot"])));
        Assert.Single(notices);
        Assert.Equal("Please reboot", notices[0].Text);
        Assert.Empty(ReplyTracker.Diff(after, Tickets((1, "open", ["Please reboot"]))).Notices);
    }

    [Fact]
    public void The_persons_own_message_is_not_announced_to_them()
    {
        var (_, seen) = ReplyTracker.Diff(null, Tickets((1, "open", ["a"])));
        var withMine = Tickets((1, "open", ["a"]));
        Assert.Empty(ReplyTracker.Diff(seen, withMine).Notices);
    }

    [Fact]
    public void Solving_a_ticket_is_announced_and_other_status_moves_are_not()
    {
        var (_, seen) = ReplyTracker.Diff(null, Tickets((1, "new", [])));
        Assert.Empty(ReplyTracker.Diff(seen, Tickets((1, "open", []))).Notices);
        var solved = ReplyTracker.Diff(seen, Tickets((1, "resolved", []))).Notices;
        Assert.Single(solved);
    }

    [Fact]
    public void Seen_survives_a_restart_through_its_file()
    {
        var path = Path.Combine(Path.GetTempPath(), $"tickets-seen-{Guid.NewGuid():N}.json");
        try
        {
            Assert.Null(ReplyTracker.Load(path));
            var (_, seen) = ReplyTracker.Diff(null, Tickets((1, "open", ["a"])));
            ReplyTracker.Save(path, seen);
            var loaded = ReplyTracker.Load(path);
            Assert.NotNull(loaded);
            // The reply that came in while the tray was not running is still announced.
            Assert.Single(ReplyTracker.Diff(loaded, Tickets((1, "open", ["a", "b"]))).Notices);
            File.WriteAllText(path, "not json");
            Assert.Null(ReplyTracker.Load(path));
        }
        finally { File.Delete(path); }
    }

    // ------------------------------------------------------------------ screenshots & config

    [Theory]
    [InlineData(3840, 2160, 1920, 1080)]
    [InlineData(1280, 1024, 1280, 1024)]   // never upscaled
    [InlineData(1080, 2400, 864, 1920)]    // portrait: the long side is the height
    [InlineData(100000, 1, 1920, 1)]       // never a zero side
    public void Screenshots_are_scaled_to_fit(int w, int h, int ew, int eh)
    {
        var size = ScreenGrabber.FitWithin(w, h, ScreenGrabber.MaxEdge);
        Assert.Equal(ew, size.Width);
        Assert.Equal(eh, size.Height);
    }

    [Fact]
    public void The_tray_is_off_until_the_hub_turns_it_on()
    {
        Assert.False(RuntimeConfig.Default.TrayEnabled);
        var silent = RuntimeConfig.Default.Apply(new Dictionary<string, object?>(), "v1");
        Assert.False(silent.TrayEnabled);
        var on = silent.Apply(new Dictionary<string, object?> { ["tickets.tray_enabled"] = "true" }, "v2");
        Assert.True(on.TrayEnabled);
        // A payload without the key, or with garbage in it, leaves the tray as it was.
        Assert.True(on.Apply(new Dictionary<string, object?> { ["tickets.tray_enabled"] = "maybe" }, "v3").TrayEnabled);
        Assert.False(on.Apply(new Dictionary<string, object?> { ["tickets.tray_enabled"] = "false" }, "v4").TrayEnabled);
    }

    [Fact]
    public void Anything_but_domain_reads_as_anyone_may_submit()
    {
        var d = RuntimeConfig.Default.Apply(new Dictionary<string, object?> { ["tickets.submitters"] = "domain" }, "v");
        Assert.Equal("domain", d.TicketSubmitters);
        var odd = d.Apply(new Dictionary<string, object?> { ["tickets.submitters"] = "admins" }, "v");
        Assert.Equal("any", odd.TicketSubmitters);
    }
}
