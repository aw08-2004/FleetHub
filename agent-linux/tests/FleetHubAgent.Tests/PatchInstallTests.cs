using System.Text.Json.Nodes;
using FleetHubAgent.Patch;

namespace FleetHubAgent.Tests;

/// <summary>
/// install_patches on Linux (0.5.0): what reaches the package manager, and when it restarts.
///
/// **The silent failure this file exists to catch is a uid becoming an option.** Every uid is
/// an argument to apt-get or dnf running as root; one that starts with a dash is read as a
/// flag (`-o APT::...`), and one with a space is two packages. The hub's normalize_uid already
/// refuses these, so nothing would ever look wrong until something got past it.
///
/// The second is a restart that does not happen when it should (a new kernel left unbooted
/// while the run reads Applied) or happens when the window said never.
/// </summary>
public class PatchInstallTests
{
    [Fact]
    public void Only_package_names_are_kept()
    {
        var parameters = JsonNode.Parse(
            "{\"uids\": [\"openssl\", \"libc6:amd64\", \"g++-12\", \"-o\", \"APT::Get::Assume-Yes=1\","
            + " \"a b\", \"$(reboot)\", \"\", \"openssl\", \"linux-image-6.1.0-18-amd64\"]}");
        // "-o" is dropped for its dash, and the option body after it for its "=", so the pair
        // cannot reassemble into a config override on the command line.
        Assert.Equal(["openssl", "libc6:amd64", "g++-12", "linux-image-6.1.0-18-amd64"],
                     InstallPatchesExecutor.ReadUids(parameters));
    }

    [Fact]
    public void Nothing_named_is_an_empty_list_not_a_crash()
    {
        Assert.Empty(InstallPatchesExecutor.ReadUids(JsonNode.Parse("{}")));
        Assert.Empty(InstallPatchesExecutor.ReadUids(JsonNode.Parse("{\"uids\": \"openssl\"}")));
    }

    [Theory]
    [InlineData("always", false, true)]
    [InlineData("never", true, false)]
    [InlineData("if_required", true, true)]
    [InlineData("if_required", false, false)]
    [InlineData(null, true, true)]
    [InlineData("garbage", false, false)]
    public void The_restart_follows_the_window(string? policy, bool required, bool restart)
    {
        Assert.Equal(restart, InstallPatchesExecutor.ShouldRestart(policy, required));
    }

    [Theory]
    [InlineData("E: Failed to fetch http://deb.debian.org/pool/x.deb  404  Not Found [IP: 1.2.3.4 80]\nE: Unable to fetch some archives", true)]
    [InlineData("E: Could not get lock /var/lib/dpkg/lock-frontend", false)]
    [InlineData("", false)]
    public void A_stale_index_is_recognised(string output, bool stale)
    {
        Assert.Equal(stale, InstallPatchesExecutor.IndexIsStale(output));
    }
}
