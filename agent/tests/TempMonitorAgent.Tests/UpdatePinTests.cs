using TempMonitorAgent.State;
using TempMonitorAgent.Update;
using FleetHub.Shared;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Exact-version update pins (roadmap #21): the parse, the url, and the install decision.
///
/// The silent failures this file exists for: a pin string off the wire that turns into a
/// url the agent did not compile in; a signed manifest for some OTHER version installed
/// because it was served at the pinned path; a pin that quietly stops being honoured because
/// one heartbeat left the field out, letting the release under review install; and a pinned
/// downgrade on a platform that cannot do one being retried forever.
/// </summary>
public class UpdatePinTests
{
    [Theory]
    [InlineData("3.48.0", "3.48.0")]
    [InlineData(" 3.48.0 ", "3.48.0")]
    [InlineData("0.4.0", "0.4.0")]
    public void A_well_formed_pin_is_kept(string raw, string expected)
    {
        Assert.Equal(expected, UpdatePins.NormalizePin(raw));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("3.48")]
    [InlineData("3.48.0.1")]
    [InlineData("3.48.0-rc1")]
    [InlineData("../../evil/3.48.0")]
    [InlineData("3.48.0/../x")]
    [InlineData("1234567.0.0")]
    public void Anything_else_reads_as_automatic(string? raw)
    {
        Assert.Null(UpdatePins.NormalizePin(raw));
        Assert.Null(AgentConfig.ManifestUrlForPin(raw));
    }

    [Fact]
    public void A_pin_maps_onto_the_compiled_in_per_version_base()
    {
        Assert.Equal(AgentConfig.PinnedManifestBase + "3.47.0.json", AgentConfig.ManifestUrlForPin("3.47.0"));
        Assert.StartsWith("https://raw.githubusercontent.com/aw08-2004/FleetHub/main/agent/manifests/",
                          AgentConfig.PinnedManifestBase);
    }

    [Fact]
    public void Update_policy_parse()
    {
        var pinned = RuntimeConfig.Default.WithUpdatePolicy("pinned", "3.47.0");
        Assert.Equal("3.47.0", pinned.PinnedVersion);
        Assert.Null(pinned.WithUpdatePolicy("auto", "3.47.0").PinnedVersion);
        Assert.Null(pinned.WithUpdatePolicy("PINNED", "not-a-version").PinnedVersion);
        Assert.Null(pinned.WithUpdatePolicy(null, null).PinnedVersion);
        // The channel is a different field and a policy change must not touch it.
        var beta = RuntimeConfig.Default with { Channel = Channels.Beta };
        Assert.Equal(Channels.Beta, beta.WithUpdatePolicy("pinned", "3.47.0").Channel);
    }

    [Fact]
    public void A_config_block_cannot_set_a_pin()
    {
        // Like the channel, the pin rides its own heartbeat field; the fleet-wide config block
        // is content-hashed and must not be a second way in.
        var applied = RuntimeConfig.Default.Apply(
            new Dictionary<string, object?> { ["PinnedVersion"] = "1.0.0", ["update_policy"] = "1.0.0" }, "v1");
        Assert.Null(applied.PinnedVersion);
    }

    [Fact]
    public void Automatic_installs_only_what_is_strictly_newer()
    {
        Assert.Equal(UpdateDecision.Install, Decide("3.48.0", "3.48.1", null));
        Assert.Equal(UpdateDecision.UpToDate, Decide("3.48.1", "3.48.1", null));
        Assert.Equal(UpdateDecision.UpToDate, Decide("3.48.1", "3.47.0", null));
    }

    [Fact]
    public void A_pin_installs_exactly_that_version_forward_or_back()
    {
        Assert.Equal(UpdateDecision.Install, Decide("3.47.0", "3.48.0", "3.48.0"));
        Assert.Equal(UpdateDecision.Install, Decide("3.48.1", "3.47.0", "3.47.0"));
        Assert.Equal(UpdateDecision.UpToDate, Decide("3.48.0", "3.48.0", "3.48.0"));
    }

    [Fact]
    public void A_signed_manifest_for_another_version_is_refused_at_the_pinned_path()
    {
        Assert.Equal(UpdateDecision.PinMismatch, Decide("3.47.0", "3.49.0", "3.48.0"));
    }

    [Fact]
    public void A_platform_that_cannot_downgrade_refuses_a_lower_pin()
    {
        Assert.Equal(UpdateDecision.DowngradeRefused,
                     Decide("0.4.0", "0.3.0", "0.3.0", canDowngrade: false));
        Assert.Equal(UpdateDecision.Install,
                     Decide("0.3.0", "0.4.0", "0.4.0", canDowngrade: false));
    }

    private static UpdateDecision Decide(string running, string manifest, string? pin,
                                         bool canDowngrade = true) =>
        UpdatePins.Decide(running, manifest, pin, canDowngrade, VersionUtil.Compare);
}
