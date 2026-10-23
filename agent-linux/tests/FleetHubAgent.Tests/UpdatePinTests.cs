using FleetHubAgent.Update;
using FleetHub.Shared;
using Xunit;

namespace FleetHubAgent.Tests;

/// <summary>
/// Exact-version update pins (roadmap #21) on the Linux agent. The silent failures: a pin
/// string off the wire becoming a url the agent did not compile in, a signed manifest for some
/// other version installed because it was served at the pinned path, and a pinned downgrade
/// that systemd could perfectly well run being refused as if this were Android.
/// </summary>
public class UpdatePinTests
{
    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("0.4")]
    [InlineData("0.4.0.1")]
    [InlineData("0.4.0-rc1")]
    [InlineData("../0.4.0")]
    [InlineData("1234567.0.0")]
    public void Anything_but_three_short_numbers_reads_as_automatic(string? raw)
    {
        Assert.Null(UpdatePins.NormalizePin(raw));
        Assert.Null(AgentConfig.ManifestUrlForPin(raw));
    }

    [Fact]
    public void A_pin_maps_onto_this_agents_own_per_version_base()
    {
        Assert.Equal(AgentConfig.PinnedManifestBase + "0.4.0.json", AgentConfig.ManifestUrlForPin(" 0.4.0 "));
        Assert.Contains("/main/agent-linux/manifests/", AgentConfig.PinnedManifestBase);
    }

    [Fact]
    public void Policy_parse()
    {
        Assert.Equal("0.4.0", UpdatePins.PinFromPolicy("pinned", "0.4.0"));
        Assert.Null(UpdatePins.PinFromPolicy("auto", "0.4.0"));
        Assert.Null(UpdatePins.PinFromPolicy("pinned", "latest"));
    }

    [Fact]
    public void Decisions()
    {
        Assert.Equal(UpdateDecision.Install, Decide("0.3.0", "0.4.0", null, canDowngrade: true));
        Assert.Equal(UpdateDecision.UpToDate, Decide("0.4.0", "0.3.0", null, canDowngrade: true));
        Assert.Equal(UpdateDecision.Install, Decide("0.4.0", "0.3.0", "0.3.0", canDowngrade: true));
        Assert.Equal(UpdateDecision.PinMismatch, Decide("0.3.0", "0.5.0", "0.4.0", canDowngrade: true));
        Assert.Equal(UpdateDecision.UpToDate, Decide("0.4.0", "0.4.0", "0.4.0", canDowngrade: true));
    }

    private static UpdateDecision Decide(string running, string manifest, string? pin,
                                         bool canDowngrade = true) =>
        UpdatePins.Decide(running, manifest, pin, canDowngrade, VersionUtil.Compare);
}
