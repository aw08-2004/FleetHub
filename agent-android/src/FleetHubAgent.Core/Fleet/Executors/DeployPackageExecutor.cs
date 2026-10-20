using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// deploy_package: install one or more Android apps from a package defined in the hub --
/// roadmap #5, on a phone (roadmap #23).
///
/// **The same command and the same params snapshot the Windows agent reads**
/// (hub/packages.build_command_params), restricted to the one step kind a phone can run:
/// `apk`, whose `apk` field names a payload (`{payload}`). The hub refuses to save an Android
/// package with any other step, an unpinned payload or a detection rule, and refuses to
/// dispatch a Windows package to a phone (packages._platform_refusal). Each of those is checked
/// here AGAIN and refused with the reason, because the hub that sent this may be older than
/// the rule, and the cost of guessing is an app installed silently on somebody's phone.
///
/// Three phases, all of which have to pass:
///
///   1. **Payloads.** Downloaded into this app's private storage and checked against the
///      sha256 the hub sent. Unlike the Windows agent, an UNPINNED payload is refused rather
///      than installed: the device owner installs with no prompt and no second look, so
///      "whatever that URL serves today" is not something it may accept.
///   2. **Install.** Each `apk` step hands its file to the platform's package installer as the
///      device owner (IAppInstaller), and waits for the platform's verdict.
///   3. **Detection.** Android's own answer: after the install, is the package the APK
///      declares actually installed, at the version it declares? That replaces the Windows
///      detection rules, which are a file path, a registry value and an uninstall list.
///
/// **Device owner or nothing.** Without it the package installer raises a confirmation dialog
/// for every app, on a phone that may be in a drawer -- an install that waits forever for a tap
/// nobody makes. So a device that is not fully managed fails at once and says why, the same
/// line LockDeviceExecutor draws.
///
/// The staging directory is deleted on every path, for the Windows agent's reason: an
/// installer left on disk is an installer waiting for something else to run it.
/// </summary>
public sealed class DeployPackageExecutor(
    ILogger<DeployPackageExecutor> log,
    IAppInstaller installer,
    IPayloadDownloader downloader) : ICommandExecutor
{
    public string Type => "deploy_package";

    /// <summary>hub/packages.STEP_APK.</summary>
    internal const string StepApk = "apk";

    /// <summary>Under the update staging directory, which AgentService points at this app's
    /// private files -- the one place nothing else on the device can swap a file in.</summary>
    private static string StagingDir => Path.Combine(AgentConfig.UpdateStagingDir, "packages");

    /// <summary>What an unmanaged device says, before anything is downloaded.</summary>
    internal const string NotManaged =
        "this device is not fully managed, so it cannot install an app without somebody " +
        "tapping Install on the device. Provisioning it as a device owner needs a factory reset.";

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var transcript = new Transcript(onOutput);
        var parameters = cmd.Params as JsonObject;
        var packageName = cmd.Params.GetString("package_name") ?? "package";
        var steps = (parameters?["steps"] as JsonArray)?.OfType<JsonObject>().ToList() ?? [];
        var sources = (parameters?["sources"] as JsonArray)?.OfType<JsonObject>().ToList() ?? [];
        var timeout = TimeSpan.FromSeconds(
            Math.Clamp(cmd.Params.GetInt("timeout_seconds", 900), 30, 24 * 60 * 60));

        transcript.Say($"[deploy] {packageName} ({steps.Count} step(s))");

        // Refusals that need no download.
        var refusal = Refuse(steps, parameters?["detection"] as JsonObject)
                      ?? (installer.CanInstallSilently ? null : NotManaged);
        if (refusal is not null) return transcript.Fail(refusal);

        var workDir = Path.Combine(StagingDir, Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(workDir);

            // ---- 1. payloads ----
            var (bound, fetchError) = await FetchAllAsync(sources, workDir, transcript.Say, ct);
            if (fetchError is not null) return transcript.Fail(fetchError);

            // ---- 2 + 3. install and check, step by step ----
            var index = 0;
            foreach (var step in steps)
            {
                var stepError = await InstallStepAsync(step, ++index, bound, timeout, transcript.Say, ct);
                if (stepError is not null) return transcript.Fail(stepError);
            }

            transcript.Say($"[deploy] {packageName} installed");
            return CommandResult.Ok(transcript.Text);
        }
        catch (System.OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception e)
        {
            log.LogWarning(e, "deploy_package failed for {Package}", packageName);
            return transcript.Fail(e.Message);
        }
        finally
        {
            try { if (Directory.Exists(workDir)) Directory.Delete(workDir, recursive: true); }
            catch (Exception e) { log.LogDebug(e, "Could not clean up {Path}", workDir); }
        }
    }

    /// <summary>Download and verify every payload, bound to its slot name. Returns the map,
    /// or the first error.</summary>
    private async Task<(Dictionary<string, string> Bound, string? Error)> FetchAllAsync(
        List<JsonObject> sources, string workDir, Action<string> say, CancellationToken ct)
    {
        var bound = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var source in sources)
        {
            var (path, error) = await FetchAsync(source, workDir, say, ct);
            if (error is not null) return (bound, error);
            bound[source.GetString("name") ?? "payload"] = path!;
            // `{file}` keeps meaning the one payload, exactly as on Windows.
            if (sources.Count == 1) bound["file"] = path!;
        }
        return (bound, null);
    }

    /// <summary>Install one apk step. Null when it installed (or failed with
    /// `continue_on_error`), else the line the deploy fails with.</summary>
    private async Task<string?> InstallStepAsync(JsonObject step, int index,
        IReadOnlyDictionary<string, string> bound, TimeSpan timeout, Action<string> say,
        CancellationToken ct)
    {
        var label = step.GetString("name") ?? $"step {index}";
        var reference = step.GetString(StepApk) ?? "";
        var path = Resolve(reference, bound);
        if (path is null) return $"{label}: {reference} names no payload in this package";

        say($"[deploy] {label}: installing {Path.GetFileName(path)}");
        var stepTimeout = step["timeout_seconds"] is JsonValue v && v.TryGetValue<int>(out var s)
            ? TimeSpan.FromSeconds(Math.Clamp(s, 30, 24 * 60 * 60))
            : timeout;
        AppInstallResult result;
        try
        {
            result = await installer.InstallAsync(path, stepTimeout, ct);
        }
        catch (Exception e) when (e is not System.OperationCanceledException)
        {
            result = AppInstallResult.Refused(e.Message);
        }

        if (result.Installed)
        {
            say($"[deploy] {label}: {result.PackageName} {result.VersionName} installed");
            return null;
        }
        var line = $"{label}: {result.Error ?? "the installer refused the app"}";
        if (step["continue_on_error"] is JsonValue c && c.TryGetValue<bool>(out var go) && go)
        {
            say($"[deploy] {line} (continuing, as this step allows)");
            return null;
        }
        return line;
    }

    /// <summary>The deploy's output, as the hub stores it and the console streams it.</summary>
    private sealed class Transcript(Action<string>? onOutput)
    {
        private readonly StringBuilder _text = new();
        public string Text => _text.ToString();

        public void Say(string line)
        {
            _text.AppendLine(line);
            onOutput?.Invoke(line + "\n");
        }

        public CommandResult Fail(string line)
        {
            Say("[deploy] FAILED: " + line);
            return CommandResult.Fail(Text);
        }
    }

    /// <summary>Why this recipe cannot run on a phone, or null. Internal so a test can hold
    /// these rules against hub/packages._validate_platform -- the two are one rule in two
    /// places, and the agent's copy is the one that matters if the hub's ever drifts.</summary>
    internal static string? Refuse(IReadOnlyList<JsonObject> steps, JsonObject? detection)
    {
        if (steps.Count == 0)
            return "this package has no apk steps. An Android device can only install apps; " +
                   "a package for Windows PCs cannot run here.";
        foreach (var step in steps)
        {
            var kind = step.GetString("kind") ?? "";
            if (kind != StepApk)
                return $"step kind '{kind}' cannot run on Android -- there is no shell, no " +
                       "winget and no driver store for an app to use. Only apk steps can.";
        }
        var detect = detection.GetString("kind") ?? "none";
        if (detect != "none")
            return $"detection rule '{detect}' is a Windows check. Android packages are " +
                   "checked by asking the platform whether the app is installed.";
        return null;
    }

    /// <summary>`{payload}` to the downloaded file, or null. A whole-field reference only: an
    /// apk step names a file, it does not build a command line.</summary>
    internal static string? Resolve(string reference, IReadOnlyDictionary<string, string> bound)
    {
        var trimmed = reference.Trim();
        if (trimmed.Length < 3 || trimmed[0] != '{' || trimmed[^1] != '}') return null;
        return bound.TryGetValue(trimmed[1..^1], out var path) ? path : null;
    }

    private async Task<(string? Path, string? Error)> FetchAsync(
        JsonObject source, string workDir, Action<string> say, CancellationToken ct)
    {
        var kind = source.GetString("kind") ?? "";
        var name = source.GetString("name") ?? "payload";
        var sha = (source.GetString("sha256") ?? "").Trim().ToLowerInvariant();
        var url = kind switch
        {
            "upload" => source.GetString("download_url"),
            "url" => source.GetString("ref"),
            _ => null,
        };
        if (kind is not ("upload" or "url"))
            return (null, $"payload {name} is a '{kind}' source, which a phone cannot read");
        if (string.IsNullOrWhiteSpace(url))
            return (null, $"payload {name} has no URL");
        if (sha.Length != 64)
            return (null, $"payload {name} has no sha256 pin. The device installs it without " +
                          "asking anyone, so it must be exactly the file the hub vouched for.");

        // Never a name from the params as a path component: "../../x" would escape workDir.
        var fileName = Path.GetFileName(source.GetString("file_name") ?? "");
        var dest = Path.Combine(workDir,
            $"{Guid.NewGuid():N}-{(string.IsNullOrWhiteSpace(fileName) ? "app.apk" : fileName)}");

        say($"[deploy] downloading {name}");
        var error = await downloader.DownloadAsync(url, dest, ct);
        if (error is not null) return (null, $"could not download {name}: {error}");

        string actual;
        await using (var stream = File.OpenRead(dest))
            actual = Convert.ToHexString(await SHA256.HashDataAsync(stream, ct)).ToLowerInvariant();
        if (!string.Equals(actual, sha, StringComparison.Ordinal))
        {
            try { File.Delete(dest); } catch { /* best effort */ }
            return (null, $"sha256 mismatch on {name} (got {actual}, expected {sha})");
        }
        say($"[deploy] {name}: sha256 verified");
        return (dest, null);
    }
}

/// <summary>Installing an app on this device, however the platform allows it.</summary>
public interface IAppInstaller
{
    /// <summary>Whether an install can happen with nobody at the device -- on Android, whether
    /// this app is the device owner.</summary>
    bool CanInstallSilently { get; }

    /// <summary>Install the APK at <paramref name="path"/> and wait for the platform's verdict,
    /// then confirm the package is really there. Must not throw for a refused install.</summary>
    Task<AppInstallResult> InstallAsync(string path, TimeSpan timeout, CancellationToken ct);
}

/// <summary>What one install came to.</summary>
public sealed record AppInstallResult(bool Installed, string? PackageName, string? VersionName,
    string? Error)
{
    public static AppInstallResult Refused(string error) => new(false, null, null, error);
}

/// <summary>Fetching a payload to a file. Returns null on success, else why not.</summary>
public interface IPayloadDownloader
{
    Task<string?> DownloadAsync(string url, string destination, CancellationToken ct);
}

/// <summary>
/// Downloads a payload, authenticating to the hub and to nothing else.
///
/// A hub-hosted payload (`/api/agent/packages/&lt;sha256&gt;`) needs this agent's bearer token;
/// a pinned URL elsewhere must NOT get it, or every package pointing at a vendor's CDN would
/// hand that vendor a credential for this fleet. So the header is attached only when the URL
/// being fetched is on the configured hub. A relative URL -- what the hub sends when it has no
/// HUB_URL configured -- is resolved against the hub base, which is the address the agent
/// already trusts.
///
/// **Redirects are followed here, by hand, rather than by the HTTP stack.** Whether a
/// platform handler drops the Authorization header on a redirect to another host is that
/// handler's decision, and on Android the default handler is the platform's, not .NET's -- so
/// "the runtime strips it" is a claim nobody here has verified. Following them here makes the
/// rule the same one as above, applied per hop: the token goes to the hub's host and nowhere
/// else. A redirect from https to http is refused outright. (Raised in review of PR #161.)
///
/// **The body is capped.** The sha256 is checked once the file is on disk, so without a cap a
/// pinned URL serving an endless body would fill the phone's private storage before the
/// mismatch was ever noticed. <see cref="MaxPayloadBytes"/> is refused from Content-Length when
/// the server states one, and enforced on the copy when it does not.
/// </summary>
public sealed class HubPayloadDownloader(Func<string?> bearer, HttpMessageHandler? handler = null)
    : IPayloadDownloader, IDisposable
{
    /// <summary>Half a gigabyte. Large Android apps are a few hundred megabytes; anything past
    /// this is not an app a fleet pushes to phones over their own connection.</summary>
    internal const long MaxPayloadBytes = 512L * 1024 * 1024;

    /// <summary>Enough for a CDN's usual hop or two; a loop is refused rather than chased.</summary>
    internal const int MaxRedirects = 5;

    private readonly HttpClient _http = new(
        handler ?? new HttpClientHandler { AllowAutoRedirect = false },
        disposeHandler: handler is null)
    {
        Timeout = TimeSpan.FromMinutes(15),
    };

    internal static Uri? ResolveUrl(string url)
    {
        if (Uri.TryCreate(url, UriKind.Absolute, out var absolute)
            && (absolute.Scheme == Uri.UriSchemeHttps || absolute.Scheme == Uri.UriSchemeHttp))
            return absolute;
        if (url.StartsWith('/') && Uri.TryCreate(AgentConfig.HubBase + url, UriKind.Absolute, out var rel))
            return rel;
        return null;
    }

    internal static bool IsHub(Uri url)
        => Uri.TryCreate(AgentConfig.HubBase, UriKind.Absolute, out var hub)
           && string.Equals(url.Scheme, hub.Scheme, StringComparison.OrdinalIgnoreCase)
           && string.Equals(url.Host, hub.Host, StringComparison.OrdinalIgnoreCase)
           && url.Port == hub.Port;

    public async Task<string?> DownloadAsync(string url, string destination, CancellationToken ct)
    {
        var target = ResolveUrl(url);
        if (target is null) return $"not a usable URL: {url}";
        try
        {
            for (var hop = 0; hop <= MaxRedirects; hop++)
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, target);
                if (IsHub(target) && bearer() is { } value)
                    req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", value);
                using var resp = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);

                if ((int)resp.StatusCode is >= 300 and < 400)
                {
                    var (next, refusal) = NextHop(target, resp.Headers.Location);
                    if (refusal is not null) return refusal;
                    target = next!;
                    continue;
                }
                if (!resp.IsSuccessStatusCode) return $"HTTP {(int)resp.StatusCode}";
                return await SaveBoundedAsync(resp, destination, ct);
            }
            return $"more than {MaxRedirects} redirects";
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or IOException)
        {
            return e.Message;
        }
    }

    /// <summary>Where a redirect points, or why it will not be followed.</summary>
    internal static (Uri? Next, string? Refusal) NextHop(Uri current, Uri? location)
    {
        if (location is null) return (null, "a redirect with no Location");
        var next = location.IsAbsoluteUri ? location : new Uri(current, location);
        if (next.Scheme != Uri.UriSchemeHttps && next.Scheme != Uri.UriSchemeHttp)
            return (null, $"a redirect to an unusable URL: {next}");
        if (current.Scheme == Uri.UriSchemeHttps && next.Scheme == Uri.UriSchemeHttp)
            return (null, "a redirect from https to http");
        return (next, null);
    }

    private static async Task<string?> SaveBoundedAsync(
        HttpResponseMessage resp, string destination, CancellationToken ct)
    {
        if (resp.Content.Headers.ContentLength is > MaxPayloadBytes)
            return $"the payload is larger than {MaxPayloadBytes / (1024 * 1024)} MB";

        await using var source = await resp.Content.ReadAsStreamAsync(ct);
        await using var file = File.Create(destination);
        var buffer = new byte[81920];
        long total = 0;
        int read;
        while ((read = await source.ReadAsync(buffer, ct)) > 0)
        {
            total += read;
            if (total > MaxPayloadBytes)
                return $"the payload is larger than {MaxPayloadBytes / (1024 * 1024)} MB";
            await file.WriteAsync(buffer.AsMemory(0, read), ct);
        }
        return null;
    }

    public void Dispose() => _http.Dispose();
}
