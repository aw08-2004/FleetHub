using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Tests;

/// <summary>
/// Covers how /api/report is signed (roadmap #29). The hub refuses unsigned telemetry by
/// default, so a report that lost its header fails quietly: the heartbeat keeps the box
/// "online" while its readings stop arriving. These pin the one function every report uses.
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

    /// <summary>No identity means no header at all -- an empty one would read to the hub as a
    /// forged token rather than an unsigned report.</summary>
    [Theory]
    [InlineData(null)]
    [InlineData("")]
    public void AnUnenrolledAgentSendsNoHeader(string? bearer)
    {
        using var request = TelemetryReporter.BuildReportRequest(
            "https://hub.example/api/report", "{}", bearer);
        Assert.Null(request.Headers.Authorization);
    }
}
