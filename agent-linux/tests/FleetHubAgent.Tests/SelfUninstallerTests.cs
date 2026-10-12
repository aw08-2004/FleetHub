using FleetHubAgent.Update;

namespace FleetHubAgent.Tests;

/// <summary>
/// Covers the Linux agent removing itself when its machine is deleted with "uninstall the
/// agent" ticked (roadmap #30). The script runs as root with rm -rf, so these read the exact
/// text and pin what it may delete -- the failure worth a test is the one that deletes the
/// wrong directory.
/// </summary>
public class SelfUninstallerTests
{
    [Theory]
    [InlineData("{\"reason\":\"decommissioned\"}", true)]
    [InlineData("{\"reason\":\"revoked\"}", false)]
    [InlineData("not json", false)]
    [InlineData("", false)]
    public void HearsOnlyTheDecommissionedReason(string body, bool expected)
        => Assert.Equal(expected, SelfUninstaller.IsDecommissioned(body));

    [Theory]
    [InlineData("/opt/fleethub/agent", true)]
    [InlineData("/opt/fleethub/agent/", true)]
    [InlineData("/opt/fleethub", false)]
    [InlineData("/opt", false)]
    [InlineData("/home/alice", false)]
    [InlineData("/usr/local/bin", false)]
    [InlineData("/opt/fleethub/../../etc", false)]
    [InlineData("relative/path", false)]
    public void DeletesOnlyUnderOptFleethub(string dir, bool expected)
        => Assert.Equal(expected, SelfUninstaller.IsSafeInstallDir(dir));

    [Fact]
    public void RemovesTheUnitBinaryStateAndConfig()
    {
        var s = SelfUninstaller.BuildScript("/opt/fleethub/agent", ["/var/lib/fleethub/agent"]);
        Assert.StartsWith("#!/bin/sh\n", s);
        Assert.Contains("systemctl disable --now fleethub-agent", s);
        Assert.Contains("rm -f '/etc/systemd/system/fleethub-agent.service'", s);
        Assert.Contains("rm -rf '/opt/fleethub/agent'", s);
        Assert.Contains("rm -rf '/var/lib/fleethub/agent'", s);
        Assert.Contains("rm -rf '/etc/fleethub'", s);
        Assert.DoesNotContain("\r", s);
    }

    [Fact]
    public void LeavesAnUnexpectedInstallDirAlone()
        => Assert.DoesNotContain("rm -rf '/usr/bin'",
                                 SelfUninstaller.BuildScript("/usr/bin", []));
}
