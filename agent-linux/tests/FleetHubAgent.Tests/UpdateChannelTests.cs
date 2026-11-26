using FleetHubAgent.State;
using FleetHubAgent.Update;

namespace FleetHubAgent.Tests;

/// <summary>
/// Release channels and the give-up guard's expiry on the Linux agent (0.4.1).
///
/// **The silent failure this file exists to catch is a beta box that reads the stable
/// manifest.** Until 0.4.1 the Linux agent ignored the heartbeat's `update_channel` entirely:
/// the console showed a machine on beta, the hub's version hint read the beta manifest, and
/// the agent kept installing nothing, with no line anywhere saying why. A mapping that sent
/// beta to the stable url -- or to the WINDOWS beta manifest, a win-x64 executable -- would
/// pass every other test here.
///
/// The second is a guard that gives up forever. A target that failed three restarts for a
/// passing reason was never retried, so the machine sat on its old build indefinitely.
/// </summary>
public class UpdateChannelTests
{
    [Theory]
    [InlineData("beta", "beta")]
    [InlineData(" BETA ", "beta")]
    [InlineData("stable", "stable")]
    [InlineData(null, "stable")]
    [InlineData("", "stable")]
    [InlineData("nightly", "stable")]
    [InlineData("https://evil.example/m.json", "stable")]
    public void Anything_but_beta_reads_as_stable(string? raw, string expected)
    {
        Assert.Equal(expected, Channels.Normalize(raw));
    }

    [Fact]
    public void Beta_maps_to_this_agents_own_beta_manifest()
    {
        Assert.Equal(AgentConfig.BetaManifestUrl, AgentConfig.ManifestUrlFor("beta"));
        Assert.EndsWith("/main/agent-linux/agent-linux.manifest.beta.json", AgentConfig.BetaManifestUrl);
        Assert.DoesNotContain("/agent/agent.manifest", AgentConfig.BetaManifestUrl);
        Assert.NotEqual(AgentConfig.StableManifestUrl, AgentConfig.BetaManifestUrl);
    }

    [Fact]
    public void Everything_else_maps_to_stable()
    {
        Assert.Equal(AgentConfig.StableManifestUrl, AgentConfig.ManifestUrlFor(null));
        Assert.Equal(AgentConfig.StableManifestUrl, AgentConfig.ManifestUrlFor("stable"));
        Assert.Equal(AgentConfig.StableManifestUrl, AgentConfig.ManifestUrlFor("garbage"));
    }

    [Fact]
    public void A_pin_still_wins_over_the_channel()
    {
        // SelfUpdater reads ManifestUrlForPin first and the channel url only when it is null;
        // a pin's per-version base must never be either channel file.
        var pinned = AgentConfig.ManifestUrlForPin("0.4.0");
        Assert.NotNull(pinned);
        Assert.NotEqual(AgentConfig.BetaManifestUrl, pinned);
        Assert.NotEqual(AgentConfig.StableManifestUrl, pinned);
    }

    // ---------------------------------------------------------------- the give-up guard

    private static readonly DateTimeOffset Now = new(2026, 11, 24, 12, 0, 0, TimeSpan.Zero);

    private static RestartState Spent(DateTimeOffset? at) => new()
    {
        Target = "0.4.1", Count = AgentConfig.MaxChainRestarts, LastAttemptUtc = at,
    };

    [Fact]
    public void The_guard_blocks_a_spent_target_inside_the_window()
    {
        Assert.True(SelfUpdater.GuardBlocks(Spent(Now.AddHours(-1)), "0.4.1", Now));
    }

    [Fact]
    public void The_guard_lets_a_spent_target_through_after_the_window()
    {
        Assert.False(SelfUpdater.GuardBlocks(
            Spent(Now - AgentConfig.GiveUpRetryAfter - TimeSpan.FromMinutes(1)), "0.4.1", Now));
    }

    [Fact]
    public void A_state_file_from_before_timestamps_is_retried()
    {
        // The boxes stuck on the old give-up-forever rule have no timestamp at all.
        Assert.False(SelfUpdater.GuardBlocks(Spent(null), "0.4.1", Now));
    }

    [Fact]
    public void The_guard_ignores_other_targets_and_unspent_counts()
    {
        Assert.False(SelfUpdater.GuardBlocks(null, "0.4.1", Now));
        Assert.False(SelfUpdater.GuardBlocks(Spent(Now), "0.4.2", Now));
        Assert.False(SelfUpdater.GuardBlocks(
            new RestartState { Target = "0.4.1", Count = 1, LastAttemptUtc = Now }, "0.4.1", Now));
    }

    [Theory]
    [InlineData("https://user:s3cret@test.example:8443/m.json?token=abc#x",
                "https://test.example:8443/m.json")]
    [InlineData("https://raw.githubusercontent.com/aw08-2004/FleetHub/main/agent-linux/agent-linux.manifest.beta.json",
                "https://raw.githubusercontent.com/aw08-2004/FleetHub/main/agent-linux/agent-linux.manifest.beta.json")]
    [InlineData("not a url with pass=hunter2", "(unparseable url)")]
    public void A_url_reaches_the_journal_without_credentials(string url, string logged)
    {
        // The override is free text an operator typed. A password or a signed query string in
        // it must not be copied into a journal that gets pasted into tickets.
        Assert.Equal(logged, SelfUpdater.ForLog(url));
    }

    [Fact]
    public void The_download_has_more_time_than_the_manifest()
    {
        // ~70 MB under the manifest's 30 seconds was the timeout that never logged.
        Assert.True(AgentConfig.DownloadTimeout > TimeSpan.FromMinutes(5));
    }
}
