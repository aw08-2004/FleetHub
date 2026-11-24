using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.State;

namespace FleetHubAgent.Tests;

/// <summary>
/// The channel survives a restart, and a heartbeat reply is what changes it (0.4.1).
///
/// **The silent failure is a beta box whose first update check after a reboot reads the
/// stable manifest.** The check runs before the first heartbeat, so the only thing that knows
/// the box is on beta at that moment is update-channel.json. If the write or the read lost the
/// value, the box would still update correctly most of the time -- whenever a heartbeat landed
/// first -- and fail only on the reboot path, which nobody watches.
///
/// Runs against the temp state root TestStateDir sets up, never /var/lib.
/// </summary>
[Collection(TestStateDir.Collection)]
public sealed class ChannelPersistenceTests : IDisposable
{
    private readonly AgentState _state = new();

    public ChannelPersistenceTests() => _state.SaveUpdateChannel(Channels.Stable);

    public void Dispose() => _state.SaveUpdateChannel(Channels.Stable);

    private FleetClient Client() => new(NullLogger<FleetClient>.Instance, _state);

    [Fact]
    public void The_state_root_is_the_test_one()
    {
        Assert.DoesNotContain("/var/lib/fleethub", AgentConfig.StateDir);
    }

    [Fact]
    public void Beta_round_trips_through_the_file()
    {
        _state.SaveUpdateChannel("beta");
        Assert.True(File.Exists(AgentConfig.UpdateChannelPath));
        Assert.Equal(Channels.Beta, AgentState.LoadUpdateChannel());
    }

    [Fact]
    public void Stable_is_no_file_at_all()
    {
        _state.SaveUpdateChannel("beta");
        _state.SaveUpdateChannel("stable");
        Assert.False(File.Exists(AgentConfig.UpdateChannelPath));
        Assert.Equal(Channels.Stable, AgentState.LoadUpdateChannel());
    }

    [Fact]
    public void A_corrupt_file_reads_as_stable()
    {
        Directory.CreateDirectory(AgentConfig.StateDir);
        File.WriteAllText(AgentConfig.UpdateChannelPath, "{not json");
        Assert.Equal(Channels.Stable, AgentState.LoadUpdateChannel());
    }

    [Fact]
    public void A_heartbeat_naming_beta_moves_the_manifest_to_beta()
    {
        using var client = Client();
        client.ApplyUpdateChannel("{\"update_channel\": \"beta\"}");
        Assert.Equal(Channels.Beta, AgentState.LoadUpdateChannel());
        Assert.Equal(AgentConfig.BetaManifestUrl, AgentConfig.UpdateManifestUrl);
    }

    [Fact]
    public void A_heartbeat_without_the_field_means_stable()
    {
        // The Windows agent's rule: a hub too old to send a channel never had a beta for
        // this box to be on.
        using var client = Client();
        client.ApplyUpdateChannel("{\"update_channel\": \"beta\"}");
        client.ApplyUpdateChannel("{\"processes_wanted\": false}");
        Assert.Equal(Channels.Stable, AgentState.LoadUpdateChannel());
        Assert.Equal(AgentConfig.StableManifestUrl, AgentConfig.UpdateManifestUrl);
    }

    [Fact]
    public void An_unparseable_reply_changes_nothing()
    {
        using var client = Client();
        client.ApplyUpdateChannel("{\"update_channel\": \"beta\"}");
        client.ApplyUpdateChannel("not json at all");
        Assert.Equal(Channels.Beta, AgentState.LoadUpdateChannel());
    }
}
