using System.Net;
using System.Text;
using System.Text.Json;
using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The remote helper's ways of failing SILENTLY, all seen in one OPENCLAW log on 2026-10-12: a
/// stall report that went to a WebRTC peer that never connected and was never repeated for the
/// hub relay viewer (who then waited forever), a capture retry with no display that wrote
/// 2,000 log lines in 80 seconds and told the viewer nothing specific, and a 409 from the hub
/// that crashed the helper so the service re-injected it again and again. Each piece here is
/// the part of that logic reachable without a desktop, a GPU or a hub.
/// </summary>
public class RemoteHelperFailureTests
{
    // ---------------------------------------------------------------- stall report
    [Fact]
    public void StallLatch_ReportsOncePerStall_NotBeforeTheThreshold()
    {
        var latch = new CaptureEncodePipeline.StallLatch();
        Assert.False(latch.ShouldReport(1000));
        Assert.True(latch.ShouldReport(6000));
        // Not once per loop tick.
        Assert.False(latch.ShouldReport(7000));
        Assert.False(latch.ShouldReport(60_000));
    }

    [Fact]
    public void StallLatch_Rearm_RepeatsTheReportForAViewerThatStartedOver()
    {
        // The OPENCLAW case: reported to the peer, the console switched to the relay, and the
        // relay viewer must hear it too -- at once, since the stall is long past the threshold.
        var latch = new CaptureEncodePipeline.StallLatch();
        Assert.True(latch.ShouldReport(6000));
        latch.Rearm();
        Assert.True(latch.ShouldReport(17_000));
        Assert.False(latch.ShouldReport(18_000));
    }

    [Fact]
    public void StallLatch_Clear_MakesTheNextStallNews()
    {
        var latch = new CaptureEncodePipeline.StallLatch();
        Assert.True(latch.ShouldReport(6000));
        latch.Clear();
        // A picture arrived and the clock restarted; a fresh stall still waits its threshold.
        Assert.False(latch.ShouldReport(100));
        Assert.True(latch.ShouldReport(5000));
    }

    [Fact]
    public void StallMessage_WithoutReason_IsTheShapeOlderViewersRead()
    {
        // No "reason" key at all, not "reason": null -- the general stall must stay byte-for-byte
        // what it was, so a viewer that predates the field sees nothing new.
        var json = RemoteHelper.StallMessage(new CaptureEncodePipeline.Stall("Default", null));
        Assert.Equal("""{"t":"capture","state":"stalled","desktop":"Default"}""", json);
    }

    [Fact]
    public void StallMessage_NoDisplay_CarriesTheReasonRemoteJsMatches()
    {
        var json = RemoteHelper.StallMessage(
            new CaptureEncodePipeline.Stall("", CaptureEncodePipeline.Stall.NoDisplay));
        using var doc = JsonDocument.Parse(json);
        Assert.Equal("capture", doc.RootElement.GetProperty("t").GetString());
        Assert.Equal("stalled", doc.RootElement.GetProperty("state").GetString());
        // remote.js compares this literal; a rename here falls back to the generic advice.
        Assert.Equal("no_display", doc.RootElement.GetProperty("reason").GetString());
    }

    // ---------------------------------------------------------------- capture backoff
    [Fact]
    public void OpenBackoff_StartsQuick_DoublesAndCaps()
    {
        Assert.Equal(200, CaptureEncodePipeline.OpenBackoffMs(1));
        Assert.Equal(400, CaptureEncodePipeline.OpenBackoffMs(2));
        Assert.Equal(800, CaptureEncodePipeline.OpenBackoffMs(3));
        Assert.Equal(10_000, CaptureEncodePipeline.OpenBackoffMs(20));
        // A helper left in a dead session for days must not overflow into a tight loop.
        Assert.Equal(10_000, CaptureEncodePipeline.OpenBackoffMs(int.MaxValue));
    }

    [Fact]
    public void OpenBackoff_NeverShrinksAsFailuresGrow()
    {
        int previous = 0;
        for (int n = 1; n < 100; n++)
        {
            int ms = CaptureEncodePipeline.OpenBackoffMs(n);
            Assert.True(ms >= previous, $"backoff fell from {previous} to {ms} at {n}");
            previous = ms;
        }
    }

    // ---------------------------------------------------------------- 409 on signaling
    private sealed class StubHub(HttpStatusCode status, string body) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken ct) =>
            Task.FromResult(new HttpResponseMessage(status)
            {
                Content = new StringContent(body, Encoding.UTF8, "application/json"),
            });
    }

    [Fact]
    public async Task PostSignal_409_IsSessionSuperseded_WithTheHubsReason()
    {
        using var client = new RemoteSignalingClient(
            "s1", "token", new StubHub(HttpStatusCode.Conflict, """{"error":"session is not active"}"""));
        var e = await Assert.ThrowsAsync<SessionSupersededException>(
            () => client.PostSignalAsync("offer", new { sdp = "x" }, CancellationToken.None));
        Assert.Equal("offer", e.Kind);
        Assert.Equal("session is not active", e.HubMessage);
    }

    [Fact]
    public async Task PostSignal_409_WithAnUnreadableBody_IsStillSuperseded()
    {
        using var client = new RemoteSignalingClient(
            "s1", "token", new StubHub(HttpStatusCode.Conflict, "<html>proxy</html>"));
        var e = await Assert.ThrowsAsync<SessionSupersededException>(
            () => client.PostSignalAsync("ice", new { }, CancellationToken.None));
        Assert.Equal("", e.HubMessage);
    }

    [Fact]
    public async Task PostSignal_OtherFailures_AreNotMistakenForSuperseded()
    {
        // A 500 or a 502 from a proxy is a transport problem, not the hub ending the session;
        // treating it as superseded would end a live session on a blip.
        using var client = new RemoteSignalingClient(
            "s1", "token", new StubHub(HttpStatusCode.BadGateway, ""));
        var e = await Assert.ThrowsAnyAsync<Exception>(
            () => client.PostSignalAsync("ice", new { }, CancellationToken.None));
        Assert.IsNotType<SessionSupersededException>(e);
        Assert.IsType<HttpRequestException>(e);
    }

    [Fact]
    public async Task PostSignal_Success_ReturnsTheHubsSeq()
    {
        using var client = new RemoteSignalingClient(
            "s1", "token", new StubHub(HttpStatusCode.OK, """{"seq":42}"""));
        Assert.Equal(42, await client.PostSignalAsync("offer", new { }, CancellationToken.None));
    }
}
