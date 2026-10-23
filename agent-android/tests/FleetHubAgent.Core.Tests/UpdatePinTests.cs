using FleetHubAgent;
using FleetHubAgent.Update;
using FleetHub.Shared;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Exact-version update pins (roadmap #21) on the Android agent. The silent failures: a pin
/// string off the wire becoming a url the agent did not compile in, a signed manifest for some
/// other version installed at the pinned path, and -- specific to this platform -- a pin below
/// the running build retried as an install every fifteen minutes, when PackageInstaller will
/// refuse a lower versionCode every single time.
/// </summary>
public class UpdatePinTests
{
    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("0.4")]
    [InlineData("0.4.0-beta")]
    [InlineData("../0.4.0")]
    public void Anything_but_three_short_numbers_reads_as_automatic(string? raw)
    {
        Assert.Null(UpdatePins.NormalizePin(raw));
        Assert.Null(AgentConfig.ManifestUrlForPin(raw));
    }

    [Fact]
    public void A_pin_maps_onto_this_agents_own_per_version_base()
    {
        Assert.Equal(AgentConfig.PinnedManifestBase + "0.4.0.json", AgentConfig.ManifestUrlForPin("0.4.0"));
        Assert.Contains("/main/agent-android/manifests/", AgentConfig.PinnedManifestBase);
    }

    [Fact]
    public void A_lower_pin_is_refused_rather_than_attempted()
    {
        Assert.Equal(UpdateDecision.DowngradeRefused,
                     Decide("0.4.0", "0.3.0", "0.3.0", canDowngrade: false));
        Assert.Equal(UpdateDecision.Install,
                     Decide("0.3.0", "0.4.0", "0.4.0", canDowngrade: false));
        Assert.Equal(UpdateDecision.PinMismatch,
                     Decide("0.3.0", "0.5.0", "0.4.0", canDowngrade: false));
    }

    private static UpdateDecision Decide(string running, string manifest, string? pin,
                                         bool canDowngrade = true) =>
        UpdatePins.Decide(running, manifest, pin, canDowngrade, VersionUtil.Compare);
}
