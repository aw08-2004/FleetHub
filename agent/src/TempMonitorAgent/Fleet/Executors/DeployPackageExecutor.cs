using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using Microsoft.Win32;
using TempMonitorAgent.Update;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// deploy_package: install one package defined in the hub (roadmap #5).
///
/// The params are a full SNAPSHOT of the package recipe taken when the attempt was
/// dispatched — payloads, command line or step list, timeouts, success exit codes,
/// detection rule — not a package id to look up. That is what stops an operator editing a
/// package mid-rollout from giving half the fleet a different install (see
/// packages.build_command_params).
///
/// Four phases, and all four have to pass:
///
///   1. **Resolve the payloads.** A hub-hosted file is downloaded over the authenticated
///      channel and its sha256 checked against the digest the HUB computed at upload.
///      A url/unc payload is fetched/copied and checked only if the operator pinned a
///      hash. winget resolves its own payload and has its own trust chain. Every payload
///      is bound to its slot name, so a step can say which file it means.
///   2. **Run the recipe.** Either `params.steps` in order (see PackageStepRunner) or, for
///      a package written before steps existed, the single command line with {file}
///      replaced by the resolved path. Both shapes arrive on the wire; which one is
///      present is the hub's decision, not a negotiation.
///   3. **Check the exit code** against the success set (0 and 3010 by default — 3010 is
///      "installed, reboot required", and failing it would paint half a fleet's MSI
///      installs red). With steps, each step is judged on its own set.
///   4. **Check detection.** An installer exiting 0 is evidence, not proof: silent
///      installers routinely return 0 having done nothing, and that is exactly the
///      failure a fleet-wide push must not report as success. So the recipe also carries
///      a post-install check — a file, a registry value, or an installed-version floor —
///      and the deploy only succeeds if the software is actually THERE afterward. It runs
///      once, at the end, because it is a claim about the PACKAGE, not about a step.
///
/// Each phase is also REPORTED as it starts and ends (<see cref="IStepProgress"/>, 3.53.0):
/// one event per payload download, per step, and for detection, which the console draws as
/// PDQ's "step 3 of 5". An operator's Abort comes back the same way and cancels the token
/// this runs under; the step's process tree is killed, the result says "aborted", and the
/// working directory is still removed on the way out.
///
/// The whole working directory is deleted afterward, on every path. It lives under the
/// agent's own %ProgramData% (SYSTEM-owned, like the self-updater's staging dir), not
/// %TEMP%, so a half-finished install can't leave an executable somewhere a standard user
/// could swap out before it runs.
/// </summary>
public sealed class DeployPackageExecutor : ICommandExecutor
{
    private readonly ILogger<DeployPackageExecutor> _log;
    private readonly IPackageDownloader _downloader;
    private readonly Func<IReadOnlyDictionary<string, string?>, IMachineFacts> _factsFactory;

    public DeployPackageExecutor(ILogger<DeployPackageExecutor> log, IPackageDownloader downloader)
        : this(log, downloader, hub => new WindowsMachineFacts(hub))
    {
    }

    /// <summary>With the machine's facts supplied -- the tests' seam, so an If can be
    /// exercised against "a Latitude" on whatever machine runs the suite.
    ///
    /// Internal, not public: the service container only considers public constructors, and
    /// it must keep building the one above.</summary>
    internal DeployPackageExecutor(
        ILogger<DeployPackageExecutor> log, IPackageDownloader downloader,
        Func<IReadOnlyDictionary<string, string?>, IMachineFacts> factsFactory)
    {
        _log = log;
        _downloader = downloader;
        _factsFactory = factsFactory;
    }

    /// <summary>The hub-resolved condition variables (AD, custom fields) for this machine,
    /// from params.facts, as text. A bool arrives as JSON true/false and is kept lowercase
    /// so it compares the way the hub's own literals do.</summary>
    private static Dictionary<string, string?> HubFacts(FleetCommand cmd)
    {
        var result = new Dictionary<string, string?>(StringComparer.Ordinal);
        if (cmd.Params.GetObject("facts") is not { } facts) return result;
        foreach (var (name, value) in facts)
        {
            if (value is JsonValue v && v.TryGetValue<bool>(out var b))
                result[name] = b ? "true" : "false";
            else
                result[name] = value?.ToString();
        }
        return result;
    }

    public string Type => "deploy_package";

    /// <summary>Where payloads are staged. Beside the self-updater's staging dir, under
    /// %ProgramData%, for the ACL reason in the class docstring.</summary>
    private static string StagingDir => Path.Combine(AgentConfig.ProgramDataDir, "packages");

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct) =>
        ExecuteAsync(cmd, onOutput, null, ct);

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, IStepProgress? progress, CancellationToken ct)
    {
        var phases = new PhaseTracker(progress);
        var packageName = cmd.Params.GetString("package_name") ?? "package";
        var legacySource = cmd.Params.GetObject("source");
        var steps = cmd.Params.GetArray("steps");
        // `sources` is the current wire shape; `source` is what a hub older than steps
        // sends. Falling back rather than requiring both keeps this agent working against
        // either, which matters because the hub is upgraded first and separately.
        var sources = ResolveSourceList(cmd, legacySource);

        if (legacySource is null && sources.Count == 0 && (steps is null || steps.Count == 0))
            return CommandResult.Fail("deploy_package requires params.source or params.steps");

        var timeout = Math.Clamp(cmd.Params.GetInt("timeout_seconds", 900), 30, 24 * 60 * 60);
        var successCodes = cmd.Params.GetIntSet("success_exit_codes");
        if (successCodes.Count == 0)
        {
            // The hub refuses to store an empty set, so this means a malformed or
            // truncated payload. Guessing {0} here would turn that into a silent
            // "succeeded" — refuse instead.
            return CommandResult.Fail("deploy_package params carry no success_exit_codes");
        }

        var log = new StringBuilder();

        // Say() is for OUR OWN messages: a bare line that still needs terminating.
        void Say(string line) => Emit(line + "\n");

        // Emit() is for text that ALREADY ends in a newline -- which is what
        // ProcessRunner hands its onLine callback (it re-adds the newline the line-event
        // API strips). The two are separate functions rather than one because passing Say
        // straight to onLine appends a SECOND newline, double-spacing every line of
        // installer output in both the live console and the stored result log. That is
        // exactly what happened here until this was split.
        void Emit(string text)
        {
            log.Append(text);
            onOutput?.Invoke(text);
        }

        var multiStep = steps is not null && steps.Count > 0;
        Say($"[deploy] {packageName} ({(multiStep ? $"{steps!.Count} steps" : legacySource.GetString("kind") ?? "")})");

        // One directory per attempt, deleted whole at the end. Per attempt rather than
        // shared, so a retry can never pick up a half-unpacked folder the previous one left
        // behind and install from it.
        await SweepStaleAttemptsAsync(StagingDir, DateTime.UtcNow);
        var workDir = Path.Combine(StagingDir, Guid.NewGuid().ToString("N"));
        FileStream? live = null;
        try
        {
            Directory.CreateDirectory(workDir);
            live = HoldLiveMarker(workDir);
            var vars = new PackageVariables(workDir);
            // What If conditions, download conditions and `{model}` read (3.54.0). Live and
            // lazy -- see WindowsMachineFacts -- so a package with no conditions reads nothing.
            var facts = _factsFactory(HubFacts(cmd));
            vars.BindFacts(facts);

            // ---- 1. payloads ----
            // `download` counts FILE payloads only, in list order -- the same count as the
            // hub's packages.progress_skeleton, or the events land on the wrong rows. A
            // payload skipped by its download condition is still counted (and reported
            // not_run), for the same reason.
            var download = 0;
            foreach (var source in sources)
            {
                var kind = source.GetString("kind") ?? "";
                if (kind is not ("upload" or "url" or "unc")) continue;
                var name = source.GetString("name") ?? "payload";
                var ordinal = ++download;
                if (source["only_if"] is JsonObject onlyIf)
                {
                    var wanted = await new ConditionEvaluator(
                        facts, vars, () => null, Say, Emit, ct).EvaluateAsync(onlyIf);
                    if (wanted == Tri.False)
                    {
                        Say($"[deploy] {name}: not needed on this machine, not downloaded");
                        phases.Mark(StepPhase.Download, ordinal, StepState.NotRun,
                                    "not needed on this machine");
                        var reason = $"the payload {{{name}}} was not downloaded on this " +
                                     "machine -- its download condition is false here";
                        vars.Withhold(name, reason);
                        if (sources.Count == 1) vars.Withhold("file", reason);
                        continue;
                    }
                    if (wanted == Tri.Unknown)
                    {
                        // Fail closed, as an If does: guessing "download it anyway" would
                        // put the wrong model's drivers one step from pnputil.
                        var why = $"could not decide whether {name} is needed on this machine";
                        Say("[deploy] FAILED: " + why);
                        phases.Begin(StepPhase.Download, ordinal);
                        phases.End(StepState.Failed, message: why);
                        return new CommandResult(false, log + "\n[deploy] FAILED: " + why);
                    }
                }
                phases.Begin(StepPhase.Download, ordinal);
                var (path, error) = await ResolvePayloadAsync(source, kind, workDir, Say, ct);
                if (error is not null)
                {
                    phases.End(StepState.Failed, message: error.Replace("[deploy] ", ""));
                    return new CommandResult(false, log + "\n" + error);
                }
                phases.End(StepState.Ok);
                vars.Bind(name, path!);
                // `{file}` is what every package written before steps says, so a package
                // with exactly one payload keeps meaning what it always meant.
                if (sources.Count == 1) vars.Bind("file", path!);
            }

            // ---- 2 + 3. run and judge ----
            bool ran;
            if (multiStep)
            {
                var end = await PackageStepRunner.RunTreeAsync(
                    steps!, vars, new PackageStepRunner.StepDefaults(timeout, successCodes),
                    new PackageStepRunner.StepSinks(Say, Emit, phases), facts, ct);
                if (end == PackageStepRunner.RunEnd.StoppedSuccess)
                {
                    // The package said "nothing to do here". Detection is a claim that the
                    // software IS installed, which a Stop has just said it is not meant to
                    // be -- so it is not run, and the console shows it as not run.
                    phases.Mark(StepPhase.Detect, 1, StepState.NotRun, "stopped before detection");
                    Say($"[deploy] {packageName}: finished early, nothing to install here");
                    return new CommandResult(true, log.ToString());
                }
                ran = end == PackageStepRunner.RunEnd.Completed;
            }
            else
            {
                ran = await RunSingleCommandAsync(
                    cmd, legacySource, vars, timeout, successCodes, Say, Emit, ct, phases);
            }
            if (!ran) return new CommandResult(false, log + Aborted(phases));

            // ---- 4. detection ----
            phases.Begin(StepPhase.Detect, 1);
            var detection = cmd.Params.GetObject("detection");
            var (detected, detail) = EvaluateDetection(detection);
            Say($"[deploy] detection: {detail}");
            if (!detected)
            {
                Say("[deploy] FAILED: the installer reported success but the software " +
                    "was not detected afterward");
                phases.End(StepState.Failed, message: detail);
                return new CommandResult(false, log.ToString());
            }
            phases.End(StepState.Ok, message: detail);

            Say($"[deploy] {packageName} installed");
            return new CommandResult(true, log.ToString());
        }
        catch (Exception e)
        {
            // An abort cancels the token, and wherever that lands -- mid-download, between
            // steps -- it surfaces as an OperationCanceledException. Say so plainly rather
            // than "FAILED: A task was canceled", which reads like a bug.
            var aborted = phases.AbortRequested && ct.IsCancellationRequested;
            phases.Fail(aborted ? null : e.Message, aborted);
            if (aborted) return new CommandResult(false, log + Aborted(phases));
            _log.LogWarning(e, "deploy_package failed for {Package}", packageName);
            return new CommandResult(false, log + $"\n[deploy] FAILED: {e.Message}");
        }
        finally
        {
            // On every path, including a failure: a rejected or spent installer -- and
            // anything a step unpacked next to it -- must not stay on disk waiting to be
            // run by something else.
            // The marker is released first: this attempt's own delete would otherwise trip
            // over the very file that tells a sweep the folder is in use.
            if (live is not null) await live.DisposeAsync();
            await DeleteAttemptDirAsync(workDir);
            // Also on every path: a deploy that failed halfway may still have installed
            // something, and the device sheet (roadmap #25) should say what is actually there
            // within one inventory pass rather than at the top of the next hour.
            TempMonitorAgent.Software.SoftwareInventoryReporter.Invalidate();
        }
    }

    /// <summary>The file a running attempt holds open, exclusively, in its own folder for as
    /// long as it runs. <see cref="SweepStaleAttempts"/> skips any folder whose marker it
    /// cannot open, so a live attempt is never swept, whatever its folder's timestamps say:
    /// a single step can run for hours without touching the folder's top level, so its
    /// last-write time alone would make it look abandoned.</summary>
    private const string LiveMarker = ".live";

    internal static FileStream HoldLiveMarker(string workDir) =>
        new(Path.Combine(workDir, LiveMarker), FileMode.Create, FileAccess.Write, FileShare.None);

    /// <summary>Remove one attempt's folder, retrying briefly. A process killed on a timeout
    /// or an Abort can take a moment to release the folder it ran in (see ProcessRunner), and
    /// what is in here -- installers, unpacked drivers, If-step PowerShell checks -- ran as
    /// SYSTEM and must not be left lying around. A failure is a warning, not a debug line:
    /// <see cref="SweepStaleAttempts"/> retries it on the next deploy, but someone should
    /// be able to see that it happened. Waits with Task.Delay, never a blocking sleep: a sweep
    /// over many leftovers must not hold a thread.</summary>
    private async Task DeleteAttemptDirAsync(string dir)
    {
        const int attempts = 3;
        string? lastError = null;
        for (var attempt = 1; attempt <= attempts; attempt++)
        {
            try
            {
                if (Directory.Exists(dir)) Directory.Delete(dir, recursive: true);
                return;
            }
            catch (Exception e)
            {
                lastError = e.Message;
                _log.LogDebug("Clean-up of {Path} failed (attempt {N}): {Msg}", dir, attempt, e.Message);
                if (attempt < attempts) await Task.Delay(500 * attempt);
            }
        }
        _log.LogWarning("Could not clean up {Path}; the next deploy retries it: {Msg}",
                        dir, lastError);
    }

    /// <summary>Delete attempt folders an earlier deploy could not -- a clean-up that failed,
    /// or a service stopped or a machine powered off mid-install, which no `finally` survives.
    /// A folder whose live marker is held is skipped outright; of the rest, only those
    /// untouched for an hour go, so an attempt that has only just been created is safe too.
    /// Neither rule depends on deploys running one at a time.</summary>
    internal async Task SweepStaleAttemptsAsync(string stagingDir, DateTime nowUtc)
    {
        try
        {
            if (!Directory.Exists(stagingDir)) return;
            foreach (var dir in Directory.EnumerateDirectories(stagingDir))
            {
                if (nowUtc - Directory.GetLastWriteTimeUtc(dir) <= TimeSpan.FromHours(1)) continue;
                if (IsLive(dir)) continue;
                await DeleteAttemptDirAsync(dir);
            }
        }
        catch (Exception e)
        {
            _log.LogDebug("Could not sweep {Path}: {Msg}", stagingDir, e.Message);
        }
    }

    /// <summary>Whether some running attempt holds this folder's live marker.</summary>
    private static bool IsLive(string dir)
    {
        var marker = Path.Combine(dir, LiveMarker);
        if (!File.Exists(marker)) return false;
        try
        {
            using var probe = new FileStream(marker, FileMode.Open, FileAccess.Read, FileShare.None);
            return false;
        }
        catch (IOException)
        {
            return true;   // held open by the attempt that owns it
        }
    }

    /// <summary>The closing line of a run the console stopped -- empty for any other end.</summary>
    private static string Aborted(PhaseTracker phases) =>
        phases.AbortRequested ? "\n[deploy] ABORTED from the console" : "";

    /// <summary>The payload list, from `sources` or from a lone legacy `source`.</summary>
    private static List<JsonObject> ResolveSourceList(FleetCommand cmd, JsonObject? legacySource)
    {
        if (cmd.Params.GetArray("sources") is { } array)
            return array.OfType<JsonObject>().ToList();
        // A legacy `source` of kind "multi" is the marker the hub sends for a step-based
        // package it has no single-command projection for. It names no file, so there is
        // nothing to resolve; a build that understands steps reads them instead.
        if (legacySource is not null && legacySource.GetString("kind") != "multi")
            return new List<JsonObject> { legacySource };
        return new List<JsonObject>();
    }

    /// <summary>The original one-command recipe: build the line, run it, judge the exit
    /// code. Kept whole rather than folded into a one-step list, because it is what every
    /// package written before steps runs through and its behaviour must not shift.</summary>
    private async Task<bool> RunSingleCommandAsync(
        FleetCommand cmd, JsonObject? source, PackageVariables vars, int timeout,
        HashSet<int> successCodes, Action<string> say, Action<string> emit, CancellationToken ct,
        PhaseTracker? phases = null)
    {
        // The one command line is step 1 of 1 to the console, so a pre-steps package gets
        // the same live view as every other.
        phases?.Begin(StepPhase.Step, 1);
        var kind = source.GetString("kind") ?? "";
        var (file, args, buildError) = BuildCommandLine(cmd, source, kind, vars);
        if (file is null)
        {
            var why = buildError ?? "FAILED: deploy_package has no install command";
            say("[deploy] " + why);
            phases?.End(StepState.Failed, message: why);
            return false;
        }

        say($"[deploy] running: {file} {args}");
        // No workingDir, so ProcessRunner's default (System32) still applies. Steps run in
        // the attempt's own directory, which is the better cwd -- but changing it HERE
        // would change what every package written before steps does, and an installer that
        // writes a log next to its cwd would start dropping it somewhere that gets deleted.
        var outcome = await ProcessRunner.RunAsync(
            file, args, ct, timeoutSeconds: timeout, onLine: emit);

        if (outcome.TimedOut && ct.IsCancellationRequested)
        {
            // ProcessRunner reports a cancelled wait as a timeout; it was the Abort, or the
            // service stopping, and the tree is already killed either way.
            say("[deploy] stopped: the run was cancelled");
            phases?.Fail(null, aborted: phases.AbortRequested);
            return false;
        }
        if (outcome.TimedOut)
        {
            say($"[deploy] FAILED: timed out after {timeout}s");
            phases?.End(StepState.Failed, message: $"timed out after {timeout}s");
            return false;
        }
        if (!successCodes.Contains(outcome.ExitCode))
        {
            var why = $"exit code {outcome.ExitCode} is not in " +
                      $"[{string.Join(", ", successCodes.OrderBy(c => c))}]";
            say("[deploy] FAILED: " + why);
            phases?.End(StepState.Failed, outcome.ExitCode, why);
            return false;
        }
        say($"[deploy] exit code {outcome.ExitCode} accepted");
        phases?.End(StepState.Ok, outcome.ExitCode);
        return true;
    }

    /// <summary>Fetch (or copy) one payload and verify it. Returns (path, error).</summary>
    private async Task<(string? Path, string? Error)> ResolvePayloadAsync(
        JsonObject source, string kind, string workDir, Action<string> say, CancellationToken ct)
    {
        var sha = source.GetString("sha256");
        var fileName = source.GetString("file_name");
        // Never trust a name from the params as a path component — a "file_name" of
        // ..\..\Windows\System32\x.dll would otherwise write outside the working dir.
        var safeName = string.IsNullOrWhiteSpace(fileName)
            ? "payload.bin"
            : Path.GetFileName(fileName);
        var dest = Path.Combine(workDir, $"{Guid.NewGuid():N}-{safeName}");

        if (kind == "unc")
        {
            var unc = source.GetString("ref");
            if (string.IsNullOrWhiteSpace(unc))
                return (null, "[deploy] FAILED: the UNC source has no path");
            say($"[deploy] copying {unc}");
            try
            {
                Directory.CreateDirectory(StagingDir);
                File.Copy(unc, dest, overwrite: true);
            }
            catch (Exception e)
            {
                return (null, $"[deploy] FAILED: could not read {unc}: {e.Message}");
            }
            if (!string.IsNullOrEmpty(sha))
            {
                string actual;
                using (var stream = File.OpenRead(dest))
                    actual = Convert.ToHexString(
                        await System.Security.Cryptography.SHA256.HashDataAsync(stream, ct))
                        .ToLowerInvariant();
                if (!string.Equals(actual, sha.Trim().ToLowerInvariant(), StringComparison.Ordinal))
                {
                    try { File.Delete(dest); } catch { /* best effort */ }
                    return (null, $"[deploy] FAILED: sha256 mismatch (got {actual}, expected {sha})");
                }
                say("[deploy] sha256 verified");
            }
            return (dest, null);
        }

        var url = kind == "upload" ? source.GetString("download_url") : source.GetString("ref");
        if (string.IsNullOrWhiteSpace(url))
            return (null, "[deploy] FAILED: the payload source has no URL");
        if (kind == "upload" && string.IsNullOrEmpty(sha))
        {
            // The hub always knows the digest of a file it stores, so its absence means
            // a payload that was never verified — refuse rather than run it unchecked.
            return (null, "[deploy] FAILED: a hub-hosted payload arrived with no sha256");
        }

        say($"[deploy] downloading {url}");
        var error = await _downloader.DownloadPackageAsync(url, dest, sha, ct);
        if (error is not null)
            return (null, $"[deploy] FAILED: {error}");
        say(string.IsNullOrEmpty(sha) ? "[deploy] downloaded (unpinned)" : "[deploy] sha256 verified");
        return (dest, null);
    }

    /// <summary>Substitute {file} and assemble the process to start. A null File means the
    /// command could not be built, and Error says why.</summary>
    private static (string? File, string Args, string? Error) BuildCommandLine(
        FleetCommand cmd, JsonObject? source, string kind, PackageVariables vars)
    {
        var command = cmd.Params.GetString("install_command") ?? "";
        var args = cmd.Params.GetString("install_args") ?? "";

        if (kind == "winget")
        {
            // The agent builds winget's command line itself; the hub refuses to store one
            // for a winget package, so any extra switches are appended rather than
            // replacing ours. Mirrors InstallAppExecutor's flags for consistency.
            var id = source.GetString("id");
            if (string.IsNullOrWhiteSpace(id))
                return (null, "", "FAILED: the winget source has no package id");

            // Not the bare name: as a SYSTEM service we cannot see the per-user App
            // Execution Alias. See WingetLocator.
            var winget = WingetLocator.Find();
            if (winget is null)
                return (null, "", "FAILED: " + WingetLocator.NotFoundMessage);

            var wingetArgs =
                $"install --id {id} --silent --accept-package-agreements --accept-source-agreements";
            if (!string.IsNullOrWhiteSpace(args)) wingetArgs += " " + args;
            return (winget, wingetArgs, null);
        }

        command = vars.Resolve(command);
        args = vars.Resolve(args);
        return (string.IsNullOrWhiteSpace(command) ? null : command, args, null);
    }

    // ---------------------------------------------------------------- detection
    /// <summary>Evaluate the post-install check. Returns (passed, human-readable detail).
    ///
    /// Any failure to evaluate counts as NOT detected, never as detected. A registry read
    /// that throws means we do not know whether the software is there, and "we don't
    /// know" must not be reported to the console as a successful install.</summary>
    private static (bool Passed, string Detail) EvaluateDetection(JsonObject? detection)
    {
        var (passed, detail, _) = Detect(detection);
        return (passed, detail);
    }

    /// <summary>The detection rule's verdict, plus whether it could be evaluated at all.
    ///
    /// <c>Errored</c> is what an If's file / registry / installed-program check needs and
    /// detection does not: for detection, "could not tell" and "not there" both mean the
    /// install is not proven, so both are a fail. For a condition they are different answers
    /// -- "the key is absent" is false, "the registry threw" is unknown -- and conflating them
    /// would send a machine down the Else branch over a read error. Shared rather than copied,
    /// so the two can never disagree about what "installed" means.</summary>
    internal static (bool Passed, string Detail, bool Errored) Detect(JsonObject? detection)
    {
        var kind = detection.GetString("kind") ?? "none";
        try
        {
            return kind switch
            {
                "none" => (true, "no check configured (exit code only)", false),
                "file_exists" => DetectFile(detection),
                "registry_value" => DetectRegistryValue(detection),
                "installed_version" => DetectInstalledVersion(detection),
                // A kind this build doesn't implement (an older agent against a newer hub).
                // Fail closed: reporting success for a check we cannot perform is the one
                // outcome that misleads.
                _ => (false, $"unsupported detection kind '{kind}' — update the agent", true),
            };
        }
        catch (Exception e)
        {
            return (false, $"check failed: {e.Message}", true);
        }
    }

    private static (bool Passed, string Detail, bool Errored) DetectFile(JsonObject? detection)
    {
        var path = detection.GetString("path");
        if (string.IsNullOrWhiteSpace(path))
            return (false, "file_exists rule has no path", true);
        var found = File.Exists(path) || Directory.Exists(path);
        return (found, found ? $"found {path}" : $"NOT found: {path}", false);
    }

    private static (bool Passed, string Detail, bool Errored) DetectRegistryValue(JsonObject? detection)
    {
        var root = OpenRoot(detection.GetString("root"));
        var keyPath = detection.GetString("key");
        var name = detection.GetString("name");
        if (root is null || string.IsNullOrWhiteSpace(keyPath) || string.IsNullOrWhiteSpace(name))
            return (false, "registry rule is incomplete", true);

        using var key = root.OpenSubKey(keyPath);
        var value = key?.GetValue(name);
        if (value is null)
            return (false, $"NOT found: {detection.GetString("root")}\\{keyPath}\\{name}", false);

        // An absent `equals` means "must merely exist"; an empty one is a real exact match
        // against the empty string, so check presence of the property rather than emptiness
        // of the string.
        if (detection is null || !detection.TryGetPropertyValue("equals", out var wanted)
            || wanted is null)
            return (true, $"{name} present", false);
        var want = wanted.ToString();
        var got = value.ToString() ?? "";
        return (string.Equals(got, want, StringComparison.OrdinalIgnoreCase),
                $"{name} = '{got}' (wanted '{want}')", false);
    }

    private static (bool Passed, string Detail, bool Errored) DetectInstalledVersion(JsonObject? detection)
    {
        var product = detection.GetString("name");
        if (string.IsNullOrWhiteSpace(product))
            return (false, "installed_version rule has no product name", true);
        var found = FindInstalledVersion(product);
        if (found is null)
            return (false, $"NOT installed: no entry matching '{product}'", false);

        var min = detection.GetString("min_version");
        if (string.IsNullOrWhiteSpace(min))
            return (true, $"installed: {product} {found}", false);
        var ok = VersionUtil.Compare(found, min) >= 0;
        return (ok, $"installed {found}, required >= {min}", false);
    }

    private static RegistryKey? OpenRoot(string? root) => (root ?? "").ToUpperInvariant() switch
    {
        "HKLM" => Registry.LocalMachine,
        "HKCU" => Registry.CurrentUser,
        "HKCR" => Registry.ClassesRoot,
        "HKU" => Registry.Users,
        _ => null,
    };

    /// <summary>DisplayVersion of the first installed program whose DisplayName contains
    /// <paramref name="product"/>, or null.
    ///
    /// Both registry views are searched: a 32-bit application on 64-bit Windows registers
    /// under WOW6432Node, and checking only the native view would report perfectly
    /// installed software as missing. HKCU is searched last, for per-user installs.</summary>
    private static string? FindInstalledVersion(string product)
    {
        const string uninstall = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall";
        var roots = new (RegistryKey Hive, RegistryView View)[]
        {
            (Registry.LocalMachine, RegistryView.Registry64),
            (Registry.LocalMachine, RegistryView.Registry32),
            (Registry.CurrentUser, RegistryView.Default),
        };

        foreach (var (hive, view) in roots)
        {
            using var baseKey = RegistryKey.OpenBaseKey(
                hive == Registry.CurrentUser ? RegistryHive.CurrentUser : RegistryHive.LocalMachine,
                view);
            using var key = baseKey.OpenSubKey(uninstall);
            if (key is null) continue;

            foreach (var subName in key.GetSubKeyNames())
            {
                using var sub = key.OpenSubKey(subName);
                var displayName = sub?.GetValue("DisplayName") as string;
                if (string.IsNullOrEmpty(displayName)) continue;
                if (displayName.IndexOf(product, StringComparison.OrdinalIgnoreCase) < 0) continue;
                // No DisplayVersion is still "installed" — report 0 so a rule with no
                // minimum passes and one with a minimum correctly fails.
                return sub?.GetValue("DisplayVersion") as string ?? "0";
            }
        }
        return null;
    }
}
