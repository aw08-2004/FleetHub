using System.Text.RegularExpressions;

namespace TempMonitorAgent.Update;

/// <summary>Tolerant dotted-numeric version compare, matching the hub's
/// version_tuple/_cmp_versions: reads the leading numeric prefix, ignores any suffix
/// ("3.0.1-rc1" → 3.0.1), and pads so "3.0" == "3.0.0".</summary>
public static partial class VersionUtil
{
    [GeneratedRegex(@"^\s*(\d+(?:\.\d+)*)")]
    private static partial Regex LeadingNumeric();

    public static int[] Parse(string? v)
    {
        var m = LeadingNumeric().Match(v ?? "");
        if (!m.Success) return new[] { 0 };
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

    /// <summary>
    /// Whether a verified manifest should be installed over the running build (roadmap #21).
    ///
    /// <para>Automatic (<paramref name="pin"/> null): only a strictly newer build, the rule
    /// every updater here has always had. Pinned: exactly the pinned version and nothing
    /// else -- a manifest naming any other version is refused even though it is signed,
    /// because a signed manifest for 3.49.0 served at the 3.47.0 path is a replay, not a
    /// pin -- and installed when it DIFFERS from what is running, forward or back. That
    /// "differs" is the one place the strictly-newer guard is set aside, and only for a
    /// version an operator named.</para>
    ///
    /// <para><paramref name="canDowngrade"/> is false on Android, whose package installer
    /// refuses a lower versionCode for an app that is not a system app.</para>
    /// </summary>
    public static UpdateDecision Decide(string running, string manifestVersion, string? pin,
                                        bool canDowngrade = true)
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
