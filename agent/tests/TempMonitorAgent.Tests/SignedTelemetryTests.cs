using TempMonitorAgent.Telemetry;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Covers how /api/report is signed (roadmap #29).
///
/// The hub refuses unsigned telemetry by default, so an agent whose report lost its header
/// does not fail loudly: it keeps running, its heartbeat keeps the machine "online", and the
/// console simply stops getting its temperatures and sensors -- with a 401 in a log file
/// nobody reads. These pin the one function every report goes through.
/// </summary>
public class SignedTelemetryTests
{
    [Fact]
    public void AnEnrolledAgentSignsItsReport()
    {
        using var request = TelemetryReporter.BuildReportRequest(
            "https://hub.example/api/report", "{}", "agent-1:secret");
        Assert.Equal("Bearer", request.Headers.Authorization?.Scheme);
        Assert.Equal("agent-1:secret", request.Headers.Authorization?.Parameter);
    }

    /// <summary>Not enrolled means no header at all -- never an empty or placeholder one,
    /// which the hub would read as a forged token rather than as an unsigned report.</summary>
    [Theory]
    [InlineData(null)]
    [InlineData("")]
    public void AnUnenrolledAgentSendsNoHeader(string? bearer)
    {
        using var request = TelemetryReporter.BuildReportRequest(
            "https://hub.example/api/report", "{}", bearer);
        Assert.Null(request.Headers.Authorization);
    }

    [Fact]
    public void TheBodyIsStillJson()
    {
        using var request = TelemetryReporter.BuildReportRequest(
            "https://hub.example/api/report", "{\"machine\":\"PC-1\"}", "a:b");
        Assert.Equal(HttpMethod.Post, request.Method);
        Assert.Equal("application/json", request.Content?.Headers.ContentType?.MediaType);
    }
}
