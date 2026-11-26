using System.Security.Cryptography;
using System.Text;
using TempMonitorAgent.Update;
using FleetHub.Shared;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The hub's release mirror as the second self-update source (roadmap #33, phase 2).
///
/// The silent failures this file exists for: a compiled-in manifest url that has no hub copy,
/// so a PC behind a firewall keeps failing exactly as before while the mirror looks healthy; a
/// mirror path that does not match what the hub serves (hub/release_mirror_web.py), which is
/// the same failure from the other side; a hub-served file accepted without the checks GitHub's
/// copy gets; a manifest from one source paired with another source's signature; and a binary
/// fetched from a location the hub chose rather than by the hash the signed manifest gave.
/// </summary>
public class UpdateMirrorTests
{
    private const string Hub = "https://hub.example.com";

    [Fact]
    public void Every_compiled_in_manifest_has_a_hub_copy_under_its_repository_path()
    {
        Assert.Equal($"{Hub}/api/agent/dist/repo/agent/agent.manifest.json",
            UpdateMirror.ManifestOnHub(AgentConfig.StableManifestUrl, Hub));
        Assert.Equal($"{Hub}/api/agent/dist/repo/agent/agent.manifest.beta.json",
            UpdateMirror.ManifestOnHub(AgentConfig.BetaManifestUrl, Hub));
        Assert.Equal($"{Hub}/api/agent/dist/repo/agent/manifests/3.50.0.json",
            UpdateMirror.ManifestOnHub(AgentConfig.ManifestUrlForPin("3.50.0")!, Hub));
    }

    [Theory]
    [InlineData("https://hub.example.com/")]
    [InlineData(" https://hub.example.com ")]
    public void A_trailing_slash_or_space_on_the_hub_address_changes_nothing(string hub)
    {
        Assert.Equal($"{Hub}/api/agent/dist/repo/agent/agent.manifest.json",
            UpdateMirror.ManifestOnHub(AgentConfig.StableManifestUrl, hub));
    }

    [Fact]
    public void A_test_server_override_gets_no_hub_fallback()
    {
        Assert.Null(UpdateMirror.ManifestOnHub("http://127.0.0.1:8000/agent.manifest.json", Hub));
        // The pre-rename repository path is not this repo's main either.
        Assert.Null(UpdateMirror.ManifestOnHub(
            "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/agent.manifest.json", Hub));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("hub.example.com")]
    [InlineData("ftp://hub.example.com")]
    public void An_unusable_hub_address_gives_no_mirror(string? hub)
    {
        Assert.Null(UpdateMirror.ManifestOnHub(AgentConfig.StableManifestUrl, hub));
        Assert.Null(UpdateMirror.BinaryOnHub(hub, new string('a', 64)));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("abc")]
    [InlineData("../../../etc/passwd")]
    public void A_binary_is_asked_for_by_a_well_formed_sha256_only(string? sha)
    {
        Assert.Null(UpdateMirror.BinaryOnHub(Hub, sha));
        var good = new string('A', 64);
        Assert.Equal($"{Hub}/api/agent/dist/bin/{good.ToLowerInvariant()}", UpdateMirror.BinaryOnHub(Hub, good));
    }

    [Fact]
    public void Sources_are_GitHub_first_then_the_hub()
    {
        Assert.Equal(new[] { "g", "h" }, UpdateMirror.Sources("g", "h"));
        Assert.Equal(new[] { "g" }, UpdateMirror.Sources("g", null));
    }

    // ---------------------------------------------------------------- fetching

    private static Func<string, CancellationToken, Task<byte[]>> Server(Dictionary<string, byte[]> files) =>
        (url, _) => files.TryGetValue(url, out var b)
            ? Task.FromResult(b)
            : Task.FromException<byte[]>(new HttpRequestException($"no route to {url}"));

    private static bool SignedBy(byte[] manifest, string sig) => sig == "good:" + manifest.Length;

    [Fact]
    public async Task GitHub_unreachable_the_hub_copy_is_used_and_verified()
    {
        var m = Encoding.UTF8.GetBytes("{\"version\":\"3.99.0\"}");
        var files = new Dictionary<string, byte[]>
        {
            ["h/m"] = m, ["h/m.sig"] = Encoding.ASCII.GetBytes("good:" + m.Length + "\n"),
        };
        var got = await UpdateMirror.FetchVerifiedManifestAsync(
            new[] { "g/m", "h/m" }, Server(files), SignedBy, CancellationToken.None);
        Assert.Equal("h/m", got.Source);
        Assert.Equal(m, got.Bytes);
        Assert.Single(got.Failures);
        Assert.False(got.SignatureRefused);
    }

    [Fact]
    public async Task A_hub_copy_signed_by_anyone_else_is_refused()
    {
        var m = Encoding.UTF8.GetBytes("{}");
        var files = new Dictionary<string, byte[]> { ["h/m"] = m, ["h/m.sig"] = Encoding.ASCII.GetBytes("forged") };
        var got = await UpdateMirror.FetchVerifiedManifestAsync(
            new[] { "g/m", "h/m" }, Server(files), SignedBy, CancellationToken.None);
        Assert.Null(got.Bytes);
        Assert.Equal(2, got.Failures.Count);
        Assert.Contains("NOT signed", got.Failures[1]);
        // The one failure the Windows agent raises to a Warning.
        Assert.True(got.SignatureRefused);
    }

    [Fact]
    public async Task A_bad_signature_from_GitHub_falls_back_to_the_hub()
    {
        var m = Encoding.UTF8.GetBytes("{}");
        var files = new Dictionary<string, byte[]>
        {
            ["g/m"] = m, ["g/m.sig"] = Encoding.ASCII.GetBytes("block page"),
            ["h/m"] = m, ["h/m.sig"] = Encoding.ASCII.GetBytes("good:" + m.Length),
        };
        var got = await UpdateMirror.FetchVerifiedManifestAsync(
            new[] { "g/m", "h/m" }, Server(files), SignedBy, CancellationToken.None);
        Assert.Equal("h/m", got.Source);
        Assert.True(got.SignatureRefused);
    }

    [Fact]
    public async Task A_manifest_and_signature_never_come_from_two_sources()
    {
        // GitHub serves the manifest but not its .sig; the hub serves a DIFFERENT manifest with
        // its own valid signature. The hub's pair must be used whole -- never GitHub's manifest
        // checked against the hub's signature.
        var gm = Encoding.UTF8.GetBytes("{\"version\":\"1\"}");
        var hm = Encoding.UTF8.GetBytes("{\"version\":\"22\"}");
        var files = new Dictionary<string, byte[]>
        {
            ["g/m"] = gm,
            ["h/m"] = hm, ["h/m.sig"] = Encoding.ASCII.GetBytes("good:" + hm.Length),
        };
        var seen = new List<(byte[], string)>();
        var got = await UpdateMirror.FetchVerifiedManifestAsync(
            new[] { "g/m", "h/m" }, Server(files),
            (b, s) => { seen.Add((b, s)); return SignedBy(b, s); },
            CancellationToken.None);
        Assert.Equal(hm, got.Bytes);
        Assert.Single(seen);
        Assert.Equal(hm, seen[0].Item1);
    }

    [Fact]
    public async Task The_binary_is_accepted_only_under_the_signed_hash()
    {
        var real = Encoding.UTF8.GetBytes("MZ the real build");
        var sha = Convert.ToHexString(SHA256.HashData(real)).ToLowerInvariant();
        var files = new Dictionary<string, byte[]> { ["g/b"] = Encoding.UTF8.GetBytes("<html>blocked</html>"), ["h/b"] = real };
        var got = await UpdateMirror.FetchVerifiedBinaryAsync(
            new[] { "g/b", "h/b" }, Server(files), sha.ToUpperInvariant(), CancellationToken.None);
        Assert.Equal("h/b", got.Source);
        Assert.Equal(real, got.Bytes);
        Assert.Contains("sha256", got.Failures.Single());

        files["h/b"] = Encoding.UTF8.GetBytes("MZ something else");
        var none = await UpdateMirror.FetchVerifiedBinaryAsync(
            new[] { "g/b", "h/b" }, Server(files), sha, CancellationToken.None);
        Assert.Null(none.Bytes);
        Assert.Equal(2, none.Failures.Count);
    }

    [Fact]
    public async Task A_timeout_is_a_reason_to_try_the_hub()
    {
        var real = Encoding.UTF8.GetBytes("MZ");
        var sha = Convert.ToHexString(SHA256.HashData(real)).ToLowerInvariant();
        Func<string, CancellationToken, Task<byte[]>> get = (url, _) => url == "g/b"
            ? Task.FromException<byte[]>(new TaskCanceledException("timed out"))
            : Task.FromResult(real);
        var got = await UpdateMirror.FetchVerifiedBinaryAsync(
            new[] { "g/b", "h/b" }, get, sha, CancellationToken.None);
        Assert.Equal("h/b", got.Source);
    }

    [Fact]
    public async Task A_stopping_host_is_not_a_reason_to_try_the_hub()
    {
        using var cts = new CancellationTokenSource();
        cts.Cancel();
        var asked = new List<string>();
        Func<string, CancellationToken, Task<byte[]>> get = (url, ct) =>
        {
            asked.Add(url);
            return Task.FromException<byte[]>(new TaskCanceledException("cancelled", null, ct));
        };
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            UpdateMirror.FetchVerifiedBinaryAsync(new[] { "g/b", "h/b" }, get, new string('a', 64), cts.Token));
        Assert.Equal(new[] { "g/b" }, asked);
    }
}
