namespace TempMonitorAgent.State;

/// <summary>
/// Release channel names, and the rule that anything else is stable.
///
/// <para>Mirrors <c>hub/channels.py</c>. The two sides share nothing but these strings, so
/// they are spelled here once rather than inlined at each use — a channel name is compared
/// against a value that arrived over the wire, and a typo would silently pin a machine to
/// stable forever while the console showed it as beta.</para>
///
/// <para><b>Names only. There are no URLs in this file, and that is deliberate.</b> The hub
/// sends a channel name; <see cref="AgentConfig.UpdateManifestUrl"/> turns it into one of two
/// compiled-in urls. Keeping the mapping there rather than here means the trust decision
/// lives beside the trust root it protects.</para>
/// </summary>
public static partial class Channels
{
    public const string Stable = "stable";
    public const string Beta = "beta";

    /// <summary>A usable channel name, defaulting to stable.
    ///
    /// Never throws. Every channel value the agent reads — from a heartbeat, from the
    /// persisted config file written by an older build — goes through here, so an
    /// unrecognised or absent value degrades to the safe train. A hub too old to send the
    /// field sends nothing, which lands here as null and reads as stable, exactly like the
    /// watch flags that predate it.</summary>
    public static string Normalize(string? value)
    {
        var text = value?.Trim().ToLowerInvariant();
        return text == Beta ? Beta : Stable;
    }

    /// <summary>A pinned agent version from the hub's <c>update_policy</c>, or null for
    /// "automatic".
    ///
    /// <para><b>Strict on purpose, unlike VersionUtil.</b> The pin becomes a path component
    /// of a compiled-in url (<see cref="AgentConfig.ManifestUrlForPin"/>), so anything but
    /// exactly three short numeric components is refused rather than "tolerantly" read as a
    /// different version than the operator typed. Six digits a component also keeps
    /// VersionUtil's int.Parse from ever overflowing on a value that came off the wire. Mirrors
    /// <c>channels.is_valid_pin</c> on the hub.</para></summary>
    public static string? NormalizePin(string? value)
    {
        var text = value?.Trim();
        return text is not null && PinPattern().IsMatch(text) ? text : null;
    }

    [System.Text.RegularExpressions.GeneratedRegex(@"^\d{1,6}\.\d{1,6}\.\d{1,6}$")]
    private static partial System.Text.RegularExpressions.Regex PinPattern();
}
