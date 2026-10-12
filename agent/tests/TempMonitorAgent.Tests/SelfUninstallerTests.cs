using TempMonitorAgent.Fleet;
using TempMonitorAgent.Update;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Covers the agent removing itself when its machine is deleted with "uninstall the agent"
/// ticked (roadmap #30).
///
/// The failure that matters most is the one that deletes the wrong thing: this script runs as
/// SYSTEM with recursive deletes, so the tests read the exact text a PC would run and pin what
/// may and may not be in it. The second is an agent that hears "decommissioned" and re-enrolls
/// anyway, putting a deleted machine straight back in the console.
/// </summary>
public class SelfUninstallerTests
{
    [Theory]
    [InlineData("{\"error\":\"agent authentication required\",\"reason\":\"decommissioned\"}", true)]
    [InlineData("{\"reason\":\"DECOMMISSIONED\"}", true)]
    [InlineData("{\"reason\":\"revoked\"}", false)]
    [InlineData("{\"reason\":\"unknown\"}", false)]
    [InlineData("<html>401</html>", false)]
    [InlineData("", false)]
    public void HearsOnlyTheDecommissionedReason(string body, bool expected)
        => Assert.Equal(expected, SelfUninstaller.IsDecommissioned(body));

    [Fact]
    public void ADecommissionedAgentDoesNotReenroll()
        => Assert.False(FleetClient.ShouldReenrollAfter401("{\"reason\":\"decommissioned\"}"));

    [Theory]
    [InlineData(@"C:\Program Files\FleetHub\Agent", true)]
    [InlineData(@"D:\Tools\FleetHub", true)]
    [InlineData(@"C:\", false)]
    [InlineData(@"C:\FleetHub", false)]
    [InlineData(@"relative\path", false)]
    [InlineData("", false)]
    public void RefusesToDeleteAnythingShallow(string dir, bool expected)
        => Assert.Equal(expected, SelfUninstaller.IsSafeInstallDir(dir));

    [Fact]
    public void RefusesSystemFolders()
    {
        Assert.False(SelfUninstaller.IsSafeInstallDir(
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles)));
        Assert.False(SelfUninstaller.IsSafeInstallDir(
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "Temp", "x")));
    }

    private static string Script(string? installDir = @"C:\Program Files\FleetHub\Agent")
        => SelfUninstaller.BuildScript(
            @"C:\Program Files\FleetHub\Agent\TempMonitorAgent.exe", installDir,
            [@"C:\ProgramData\FleetHub\Agent", @"C:\ProgramData\TempMonitorAgent"],
            @"C:\ProgramData\FleetHub\Uninstall");

    [Fact]
    public void MarksBothServicesForDeletionBeforeStoppingThem()
    {
        var s = Script();
        Assert.Contains("'FleetHubAgent', 'TempMonitorAgent'", s);
        Assert.True(s.IndexOf("sc.exe delete", StringComparison.Ordinal)
                    < s.IndexOf("Stop-Service", StringComparison.Ordinal));
    }

    [Fact]
    public void RemovesExactlyTheAgentsOwnFootprint()
    {
        var s = Script();
        Assert.Contains(@"Remove-Item -LiteralPath 'C:\Program Files\FleetHub\Agent' -Recurse", s);
        Assert.Contains(@"Remove-Item -LiteralPath 'C:\ProgramData\FleetHub\Agent' -Recurse", s);
        Assert.Contains(@"'HKLM:\SOFTWARE\FleetHub\Agent'", s);
        Assert.Contains("'FLEETHUB_HUB', $null, 'Machine'", s);
        Assert.Contains($"/delete /tn '{SelfUninstaller.TaskName}'", s);
        // The PawnIO driver is shared, and the installer's own -Uninstall leaves it too.
        Assert.DoesNotContain("PawnIO", s, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void LeavesAnUnsafeInstallDirAlone()
        => Assert.DoesNotContain(@"'C:\' -Recurse", Script(@"C:\"));

    [Fact]
    public void QuotesPathsSoAnApostropheCannotEndTheString()
    {
        var s = SelfUninstaller.BuildScript(@"C:\O'Brien\FleetHub\a.exe", @"C:\O'Brien\FleetHub",
                                            [], @"C:\ProgramData\FleetHub\Uninstall");
        Assert.Contains(@"'C:\O''Brien\FleetHub'", s);
    }
}
