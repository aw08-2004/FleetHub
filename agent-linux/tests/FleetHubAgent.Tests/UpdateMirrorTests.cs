using FleetHubAgent.Update;
using FleetHub.Shared;
using Xunit;

namespace FleetHubAgent.Tests;

/// <summary>
/// The hub's release mirror as the Linux agent's second update source (roadmap #33, phase 3).
/// The fallback itself is agent-shared/UpdateMirror.cs and is exercised in the Windows agent's
/// UpdateMirrorTests; what is Linux's own is which urls it compiles in. The silent failure here
/// is a Linux manifest url with no hub copy, so a box behind a firewall keeps failing exactly as
/// before while the hub's mirror looks healthy -- or a copy at a path the hub does not serve
/// (hub/release_mirror_web.py lays files out under their repository paths).
/// </summary>
public class UpdateMirrorTests
{
    private const string Hub = "https://hub.example.com";

    [Fact]
    public void Every_compiled_in_manifest_has_a_hub_copy_under_its_repository_path()
    {
        Assert.Equal($"{Hub}/api/agent/dist/repo/agent-linux/agent-linux.manifest.json",
            UpdateMirror.ManifestOnHub(AgentConfig.StableManifestUrl, Hub));
        Assert.Equal($"{Hub}/api/agent/dist/repo/agent-linux/agent-linux.manifest.beta.json",
            UpdateMirror.ManifestOnHub(AgentConfig.BetaManifestUrl, Hub));
        Assert.Equal($"{Hub}/api/agent/dist/repo/agent-linux/manifests/0.5.0.json",
            UpdateMirror.ManifestOnHub(AgentConfig.ManifestUrlForPin("0.5.0")!, Hub));
    }

    [Fact]
    public void The_binary_comes_from_the_hub_by_its_signed_hash()
    {
        var sha = new string('b', 64);
        Assert.Equal($"{Hub}/api/agent/dist/bin/{sha}", UpdateMirror.BinaryOnHub(Hub + "/", sha));
    }

    [Fact]
    public void A_test_server_override_gets_no_hub_fallback()
    {
        Assert.Null(UpdateMirror.ManifestOnHub("http://10.0.0.5:8000/agent-linux.manifest.json", Hub));
    }
}
