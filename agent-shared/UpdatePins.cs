using System.Text.RegularExpressions;

namespace FleetHub.Shared;

/// <summary>
/// Exact-version update pins (roadmap #21): the parse and the install decision, compiled into
/// all three agents from this one file (each csproj links it).
///
/// <para><b>Why one linked file, when the agents are otherwise ports of each other.</b> Each
/// agent keeps its own VersionUtil, its own updater and its own config, and that is deliberate
/// -- they differ in what they install and how. This rule does not: a pin read differently on
/// one platform is a fleet where "updates are held for review" is true on two of three trains,
/// and nothing would say so. Three copies of it were also what SonarCloud's duplication gate
/// refused on the PR that introduced it. The tolerant version compare stays per agent and is
/// passed in, so this file depends on nothing but the BCL.</para>
/// </summary>
public static partial class UpdatePins
{
    /// <summary>A pinned agent version from the hub's <c>update_policy</c>, or null for
    /// "automatic".
    ///
    /// <para><b>Strict on purpose, unlike VersionUtil.</b> The pin becomes a path component of
    /// a compiled-in url, so anything but exactly three numeric components is refused rather
    /// than "tolerantly" read as a different version than the operator typed. Six digits a
    /// component also keeps VersionUtil's int.Parse from ever overflowing on a value that came
    /// off the wire. Mirrors <c>channels.is_valid_pin</c> on the hub.</para></summary>
    public static string? NormalizePin(string? value)
    {
        var text = value?.Trim();
        return text is not null && PinPattern().IsMatch(text) ? text : null;
    }

    [GeneratedRegex(@"^\d{1,6}\.\d{1,6}\.\d{1,6}$")]
    private static partial Regex PinPattern();

    /// <summary>The hub's <c>update_policy</c> block read as a pin: <c>mode == "pinned"</c>
    /// with a usable version, else null (automatic).</summary>
    public static string? PinFromPolicy(string? mode, string? version) =>
        mode?.Trim().ToLowerInvariant() == "pinned" ? NormalizePin(version) : null;

    /// <summary>
    /// Whether a verified manifest should be installed over the running build.
    ///
    /// <para>Automatic (<paramref name="pin"/> null): only a strictly newer build, the rule
    /// every updater here has always had. Pinned: exactly the pinned version and nothing
    /// else -- a manifest naming any other version is refused even though it is signed,
    /// because a signed manifest for 3.49.0 served at the 3.47.0 path is a replay, not a pin --
    /// and installed when it DIFFERS from what is running, forward or back. That "differs" is
    /// the one place the strictly-newer guard is set aside, and only for a version an operator
    /// named.</para>
    ///
    /// <para><paramref name="canDowngrade"/> is false on Android, whose package installer
    /// refuses a lower versionCode for an app that is not a system app.
    /// <paramref name="compare"/> is the agent's own VersionUtil.Compare.</para>
    /// </summary>
    public static UpdateDecision Decide(string running, string manifestVersion, string? pin,
                                        bool canDowngrade, Func<string?, string?, int> compare)
    {
        if (pin is null)
            return compare(manifestVersion, running) > 0 ? UpdateDecision.Install : UpdateDecision.UpToDate;
        if (compare(manifestVersion, pin) != 0)
            return UpdateDecision.PinMismatch;
        var direction = compare(manifestVersion, running);
        if (direction == 0) return UpdateDecision.UpToDate;
        if (direction < 0 && !canDowngrade) return UpdateDecision.DowngradeRefused;
        return UpdateDecision.Install;
    }
}

/// <summary>The outcome of <see cref="UpdatePins.Decide"/>.</summary>
public enum UpdateDecision
{
    UpToDate,
    Install,
    /// <summary>The manifest at the pinned path names a different version -- refused.</summary>
    PinMismatch,
    /// <summary>The pin is below the running build on a platform that cannot go back.</summary>
    DowngradeRefused,
}
