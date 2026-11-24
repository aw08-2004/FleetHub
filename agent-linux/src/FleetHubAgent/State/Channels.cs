namespace FleetHubAgent.State;

/// <summary>
/// Release channel names, and the rule that anything else is stable (roadmap #21).
///
/// **Before this file existed, the Linux agent had no channel at all.** The hub sent
/// `update_channel` on every heartbeat from 0.1.0 onwards and this agent never read it, so a
/// Linux box moved onto beta in the console kept reading the stable manifest -- and the console
/// showed it as beta the whole time. Found on 2026-11-24, with 0.4.0 sitting in the beta
/// manifest and the pilot box still on 0.3.0.
///
/// Mirrors <c>agent/src/TempMonitorAgent/State/Channels.cs</c> and <c>hub/channels.py</c>.
/// Restated rather than shared, for the reason Models.cs gives. **Names only, no urls**: the
/// hub sends a name and <see cref="AgentConfig.ManifestUrlFor"/> turns it into one of two
/// compiled-in urls, which is what keeps a compromised hub from choosing where code comes from.
/// </summary>
public static class Channels
{
    public const string Stable = "stable";
    public const string Beta = "beta";

    /// <summary>A usable channel name, defaulting to stable. Never throws: every value read
    /// here came off the wire or out of a file an older build may have written, and anything
    /// unrecognised degrading to the safe train is the whole contract.</summary>
    public static string Normalize(string? value)
    {
        var text = value?.Trim().ToLowerInvariant();
        return text == Beta ? Beta : Stable;
    }
}
