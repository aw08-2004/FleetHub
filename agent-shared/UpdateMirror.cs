using System.Security.Cryptography;
using System.Text;

namespace FleetHub.Shared;

/// <summary>
/// The hub's release mirror as a second download source for self-update (roadmap #33, phases 2
/// and 3), compiled into the Windows and Linux agents from this one file (each csproj links it).
///
/// <para><b>Why it exists.</b> An agent reads its signed manifest from raw.githubusercontent.com
/// and its binary from a GitHub release. A site that blocks the internet by default can reach
/// the hub but not GitHub, and the agent runs as SYSTEM / root, which no per-user allowance
/// covers -- so such a PC stayed on the version it was installed with, forever, and a failed
/// check is quiet by design. Since hub 1.192.0 the hub keeps a verified copy of the release files
/// under <c>/api/agent/dist/</c>; this is how an agent uses it.</para>
///
/// <para><b>The hub is a source of bytes, never a trust root.</b> Every manifest is checked
/// against the compiled-in release key and every binary against the SIGNED sha256, whichever
/// source served it, by exactly the code that checks GitHub's copy. The roadmap #21 rule --
/// the hub sends a NAME, never a url -- still holds: the hub address here is the agent's own
/// configured <c>HubBase</c>, the path under it is compiled in, and a binary is fetched from
/// the mirror by the hash the signed manifest already gave, not from a location the hub chose.
/// A compromised hub can serve an old, validly signed manifest, which no updater installs
/// unless it is newer (or is the operator's pin, which the hub could set anyway).</para>
///
/// <para><b>One file, for the reason UpdatePins.cs gives:</b> a fallback that behaved
/// differently on one platform would be a fleet that updates through the hub on one train and
/// silently does not on the other. Each agent keeps its own HttpClient, logging and swap; this
/// is only where to look, in which order, and what counts as a verified file.</para>
///
/// <para><b>Each file falls back on its own</b>, and on any failure to get a VERIFIED file --
/// DNS, connect, TLS (an inspecting proxy), timeout, an HTTP error page, a bad signature, a hash
/// that does not match. A site may allow raw.githubusercontent.com and block the release host,
/// so "the manifest came from GitHub" says nothing about the binary. Narrowing the fallback to
/// "connection refused" was rejected: firewalls fail in every one of those ways, and nothing is
/// gained by being picky when the mirror's copy is checked just as strictly. A manifest and its
/// .sig always come from the SAME source, so a release landing between two requests cannot
/// pair one version's manifest with another's signature.</para>
/// </summary>
public static class UpdateMirror
{
    /// <summary>The prefix every compiled-in manifest url starts with. Only urls under it have a
    /// mirror copy: the hub mirrors this repository's <c>main</c>, nothing else. An env override
    /// pointing at a test server therefore gets no fallback, which is what a test wants.</summary>
    public const string GitHubRawMain = "https://raw.githubusercontent.com/aw08-2004/FleetHub/main/";

    /// <summary>Where the hub serves its copy. Must match hub/release_mirror_web.py.</summary>
    public const string DistPath = "/api/agent/dist";

    /// <summary>How long a connection attempt may take before the next source is tried. A
    /// firewall that drops packets rather than refusing them would otherwise hold every check
    /// for the whole request timeout before the hub got a turn.</summary>
    public static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(10);

    /// <summary>The hub's copy of <paramref name="githubUrl"/>, or null when it has none (not a
    /// url under <see cref="GitHubRawMain"/>, or no usable hub address).</summary>
    public static string? ManifestOnHub(string githubUrl, string? hubBase)
    {
        var dist = DistBase(hubBase);
        if (dist is null || githubUrl is null
            || !githubUrl.StartsWith(GitHubRawMain, StringComparison.Ordinal))
            return null;
        var path = githubUrl[GitHubRawMain.Length..];
        return path.Length == 0 ? null : $"{dist}/repo/{path}";
    }

    /// <summary>The hub's copy of a binary, addressed by its SIGNED sha256, or null when the hub
    /// address or the hash is unusable.</summary>
    public static string? BinaryOnHub(string? hubBase, string? sha256)
    {
        var dist = DistBase(hubBase);
        var sha = sha256?.Trim().ToLowerInvariant();
        if (dist is null || sha is null || sha.Length != 64 || !sha.All(Uri.IsHexDigit))
            return null;
        return $"{dist}/bin/{sha}";
    }

    private static string? DistBase(string? hubBase)
    {
        var text = hubBase?.Trim().TrimEnd('/');
        if (string.IsNullOrEmpty(text)
            || !Uri.TryCreate(text, UriKind.Absolute, out var u)
            || (u.Scheme != Uri.UriSchemeHttps && u.Scheme != Uri.UriSchemeHttp))
            return null;
        return text + DistPath;
    }

    /// <summary>The sources to try, in order: GitHub, then the hub when it has a copy.</summary>
    public static IReadOnlyList<string> Sources(string primary, string? mirror) =>
        string.IsNullOrEmpty(mirror) ? new[] { primary } : new[] { primary, mirror };

    /// <summary>A url as it may appear in a log: scheme, host, port and path. An operator's env
    /// override may carry credentials or a signed query string.</summary>
    public static string ForLog(string url) =>
        Uri.TryCreate(url, UriKind.Absolute, out var u)
            ? u.GetComponents(UriComponents.SchemeAndServer | UriComponents.Path, UriFormat.UriEscaped)
            : "(unparseable url)";

    /// <summary>The first manifest, from <paramref name="sources"/> in order, whose signature
    /// <paramref name="verify"/> accepts -- with the source it came from -- or null when none
    /// did, with one line per failed source added to <paramref name="failures"/>. Never throws
    /// for a fetch failure; a cancelled <paramref name="ct"/> still throws.</summary>
    public static async Task<(byte[] Manifest, string Source)?> FetchVerifiedManifestAsync(
        IReadOnlyList<string> sources,
        Func<string, CancellationToken, Task<byte[]>> getBytes,
        Func<byte[], string, bool> verify,
        List<string> failures,
        CancellationToken ct)
    {
        foreach (var src in sources)
        {
            byte[] manifest;
            string sig;
            try
            {
                manifest = await getBytes(src, ct);
                sig = Encoding.ASCII.GetString(await getBytes(src + ".sig", ct)).Trim();
            }
            catch (Exception e) when (IsFetchFailure(e, ct))
            {
                failures.Add($"{ForLog(src)}: {e.Message}");
                continue;
            }
            if (!verify(manifest, sig))
            {
                failures.Add($"{ForLog(src)}: NOT signed by the fleet's release key");
                continue;
            }
            return (manifest, src);
        }
        return null;
    }

    /// <summary>The first binary, from <paramref name="sources"/> in order, whose sha256 equals
    /// <paramref name="signedSha256"/> -- with the source and the hash -- or null when none did,
    /// with one line per failed source added to <paramref name="failures"/>.</summary>
    public static async Task<(byte[] Bytes, string Source)?> FetchVerifiedBinaryAsync(
        IReadOnlyList<string> sources,
        Func<string, CancellationToken, Task<byte[]>> getBytes,
        string signedSha256,
        List<string> failures,
        CancellationToken ct)
    {
        var want = signedSha256.Trim().ToLowerInvariant();
        foreach (var src in sources)
        {
            byte[] bytes;
            try
            {
                bytes = await getBytes(src, ct);
            }
            catch (Exception e) when (IsFetchFailure(e, ct))
            {
                failures.Add($"{ForLog(src)}: {e.Message}");
                continue;
            }
            var got = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
            if (!string.Equals(got, want, StringComparison.Ordinal))
            {
                failures.Add($"{ForLog(src)}: sha256 {got}, signed {want}");
                continue;
            }
            return (bytes, src);
        }
        return null;
    }

    /// <summary>A failure that means "try the next source": the network, a timeout, an HTTP
    /// error. A cancellation the caller asked for is not one -- the host is stopping.</summary>
    private static bool IsFetchFailure(Exception e, CancellationToken ct) =>
        !ct.IsCancellationRequested && e is HttpRequestException or TaskCanceledException or IOException;
}
