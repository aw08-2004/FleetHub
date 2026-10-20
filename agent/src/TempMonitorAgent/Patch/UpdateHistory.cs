using System.Text.RegularExpressions;

namespace TempMonitorAgent.Patch;

/// <summary>
/// Windows updates that installed here WITHOUT FleetHub, from Windows Update's own history
/// (roadmap #14, issue #140).
///
/// <para><b>This is the evidence that control actually holds.</b> UpdateControl reports what
/// the policy says; this reports what happened. A PC that says "fleethub" and still shows a
/// cumulative installed on Tuesday by <c>UpdateOrchestrator</c> has something overriding the
/// policy -- a GPO applied after ours, a user with an elevated script, a vendor tool -- and
/// the operator needs to see that rather than trust the switch.</para>
///
/// <para><b>Told apart by ClientApplicationID</b>, which every WUA session can set and which
/// history records per entry. The agent sets <see cref="WindowsUpdateApi.ClientId"/> on every
/// session it creates, so anything else is, by definition, an outside install.</para>
///
/// <para><b>Definition updates are dropped.</b> Defender's security-intelligence updates
/// arrive several times a day through their own clients, are deliberately outside the patch
/// approval flow, and would bury the one cumulative that matters under a hundred rows. They
/// are recognised by client AND by title, because the client string differs between the
/// Defender platform, MpSigStub and MoSetup depending on how the update was triggered.</para>
/// </summary>
public static partial class UpdateHistory
{
    /// <summary>How far back counts. Long enough to span a monthly patch cycle, so a Patch
    /// Tuesday install is still visible when somebody looks a few weeks later.</summary>
    public static readonly TimeSpan Window = TimeSpan.FromDays(30);

    /// <summary>Most rows ever reported. The list is a warning sign, not an audit log; past
    /// this the point has been made, and the heartbeat payload stays small.</summary>
    public const int MaxRows = 50;

    /// <summary>One history entry, as read off IUpdateHistoryEntry.</summary>
    public sealed record Entry(
        string Title, DateTimeOffset Date, string Client, int Operation, int ResultCode);

    /// <summary>One outside install, as the wire carries it.</summary>
    public sealed record OutsideInstall(string Title, string Kb, long InstalledAt, string Client);

    /// <summary>uoInstallation = 1 (2 is an uninstall). orcSucceeded = 2,
    /// orcSucceededWithErrors = 3 -- a failed outside install changed nothing.</summary>
    private const int Installation = 1;

    private static readonly string[] NoiseClients = ["defender", "mpsigstub", "mosetup"];
    private static readonly string[] NoiseTitles =
        ["security intelligence update", "definition update"];

    /// <summary>The filter, pure so it is tested without a Windows Update Agent.</summary>
    public static IReadOnlyList<OutsideInstall> Select(IEnumerable<Entry> entries,
                                                       DateTimeOffset now)
    {
        var since = now - Window;
        return entries
            .Where(e => e.Operation == Installation && e.ResultCode is 2 or 3)
            .Where(e => e.Date >= since)
            .Where(e => !string.Equals(e.Client, WindowsUpdateApi.ClientId,
                                       StringComparison.OrdinalIgnoreCase))
            .Where(e => !IsNoise(e))
            .OrderByDescending(e => e.Date)
            .Take(MaxRows)
            .Select(e => new OutsideInstall(e.Title, KbOf(e.Title),
                                            e.Date.ToUnixTimeSeconds(), e.Client))
            .ToList();
    }

    private static bool IsNoise(Entry e)
    {
        var client = e.Client.ToLowerInvariant();
        var title = e.Title.ToLowerInvariant();
        return NoiseClients.Any(client.Contains) || NoiseTitles.Any(title.Contains);
    }

    /// <summary>"KB5060842" out of a history title, or "". History entries carry no
    /// KBArticleIDs, so the title -- where Windows always puts it in parentheses -- is the
    /// only place it is found.</summary>
    public static string KbOf(string title)
    {
        var match = KbPattern().Match(title);
        return match.Success ? "KB" + match.Groups[1].Value : "";
    }

    [GeneratedRegex(@"\bKB(\d{6,8})\b", RegexOptions.IgnoreCase)]
    private static partial Regex KbPattern();
}
