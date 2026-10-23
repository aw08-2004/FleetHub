using System.Text.RegularExpressions;

namespace FleetHubAgent.Update;

/// <summary>Tolerant dotted-numeric version compare, matching the hub's
/// version_tuple/cmp_versions and the Windows agent's copy: reads the leading numeric prefix,
/// ignores any suffix ("0.4.0-rc1" becomes 0.4.0), and pads so "0.4" == "0.4.0".
///
/// **Four comparators have to agree, and they only agree on well-formed input** -- see
/// VERSIONING.md, which is why the format rule is three numeric components and nothing else.
/// This one truncates at the first non-digit; `release_client.py` scores the same string -1.
/// Stay inside the format and the disagreement never surfaces.</summary>
public static partial class VersionUtil
{
    [GeneratedRegex(@"^\s*(\d+(?:\.\d+)*)")]
    private static partial Regex LeadingNumeric();

    public static int[] Parse(string? v)
    {
        var m = LeadingNumeric().Match(v ?? "");
        if (!m.Success) return [0];
        return m.Groups[1].Value.Split('.').Select(int.Parse).ToArray();
    }

    /// <summary>1 if a &gt; b, -1 if a &lt; b, 0 if equal.</summary>
    public static int Compare(string? a, string? b)
    {
        var ta = Parse(a);
        var tb = Parse(b);
        int n = Math.Max(ta.Length, tb.Length);
        for (int i = 0; i < n; i++)
        {
            int va = i < ta.Length ? ta[i] : 0;
            int vb = i < tb.Length ? tb[i] : 0;
            if (va != vb) return va > vb ? 1 : -1;
        }
        return 0;
    }

    /// <summary>A pinned agent version from the hub's <c>update_policy</c>, or null for
    /// "automatic". Strict, unlike <see cref="Compare"/>: the pin becomes a path component of a
    /// compiled-in url, so anything but three numeric components of at most six digits is
    /// refused rather than tolerantly read as a different version. Mirrors the hub's
    /// <c>channels.is_valid_pin</c> and the Windows agent's Channels.NormalizePin.</summary>
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
    /// Whether a verified manifest should be installed over the running build (roadmap #21),
    /// the same rule as the Windows agent's VersionUtil.Decide. Automatic: strictly newer only.
    /// Pinned: exactly the pinned version -- a signed manifest naming any other version at the
    /// pinned path is a replay and is refused -- installed when it differs from what runs.
    /// <paramref name="canDowngrade"/> is true here: the binary is a file systemd starts, and an
    /// older one replaces a newer one exactly as a newer one replaces an older.
    /// </summary>
    public static UpdateDecision Decide(string running, string manifestVersion, string? pin,
                                        bool canDowngrade)
    {
        if (pin is null)
            return Compare(manifestVersion, running) > 0 ? UpdateDecision.Install : UpdateDecision.UpToDate;
        if (Compare(manifestVersion, pin) != 0)
            return UpdateDecision.PinMismatch;
        var direction = Compare(manifestVersion, running);
        if (direction == 0) return UpdateDecision.UpToDate;
        if (direction < 0 && !canDowngrade) return UpdateDecision.DowngradeRefused;
        return UpdateDecision.Install;
    }
}

/// <summary>The outcome of <see cref="VersionUtil.Decide"/>.</summary>
public enum UpdateDecision
{
    UpToDate,
    Install,
    /// <summary>The manifest at the pinned path names a different version -- refused.</summary>
    PinMismatch,
    /// <summary>The pin is below the running build on a platform that cannot go back.</summary>
    DowngradeRefused,
}
