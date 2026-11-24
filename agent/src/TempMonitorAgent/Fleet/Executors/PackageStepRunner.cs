using System.IO.Compression;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// The step engine behind <see cref="DeployPackageExecutor"/>: run `params.steps` in order
/// inside one working directory, with `{variables}` resolved from the payloads the hub
/// staged and from what earlier steps produced.
///
/// Split out of the executor because the two answer different questions. The executor owns
/// the DEPLOY — fetch the payloads, decide whether the software is really there afterward,
/// clean up on every path. This owns one STEP: build a command line, run it, judge its exit
/// code. Keeping them apart is also what lets the single-command recipe and the step list
/// share the payload and detection code instead of forking it.
///
/// Three properties are load-bearing:
///
///   * **A step is judged on its own exit codes.** Chaining an unpack and a driver install
///     into one `cmd /c a &amp;&amp; b` reports one number for two programs, and pnputil's
///     dialect (259 is a clean run that simply ran out of INFs) does not survive being
///     merged with someone else's. Each step carries its own set, defaulted by the hub.
///   * **The first failure stops the deploy**, unless the step says `continue_on_error`.
///     Carrying on past a failed unpack would hand pnputil an empty folder, which it would
///     cheerfully report success for.
///   * **Variables never escape the working directory by accident.** They are substituted
///     as plain text into command lines the operator wrote, but the paths they hold are all
///     produced here, under the attempt's own directory.
///   * **An If that cannot decide fails, it does not fall through to Else** (3.54.0). The
///     list is a tree since then -- an If step holds a Then lane and an Else lane -- and the
///     one way it could quietly do damage is an unreadable model taking the generic branch.
///     See <see cref="ConditionEvaluator"/> and hub/package_conditions.py.
/// </summary>
public static class PackageStepRunner
{
    /// <summary>What one step did. `Continue` is separate from `Succeeded` because a step
    /// marked continue_on_error can fail and still let the deploy proceed -- and the log
    /// has to say both things happened.
    ///
    /// `ExitCode` and `Why` are what the console's step list shows beside a red step (PDQ's
    /// "More Info"); null where there was no process, or nothing went wrong.</summary>
    public readonly record struct StepOutcome(
        bool Succeeded, bool Continue, int? ExitCode = null, string? Why = null,
        bool Aborted = false);

    /// <summary>The package-wide timeout and exit codes a step falls back to when it sets
    /// neither. One value so RunAllAsync stays within a readable parameter list.</summary>
    public readonly record struct StepDefaults(int Timeout, IReadOnlyCollection<int> ExitCodes);

    /// <summary>How a step list ended. <see cref="StoppedSuccess"/> is a Stop step saying
    /// "nothing to do on this machine": the deploy succeeds WITHOUT running detection, because
    /// the package itself has said the software is not meant to be here.</summary>
    public enum RunEnd { Completed, Failed, StoppedSuccess }

    /// <summary>Run every step in order. Returns false as soon as one fails without
    /// `continue_on_error` set, or the run is cancelled.</summary>
    public static Task<bool> RunAllAsync(
        JsonArray steps, PackageVariables vars, int defaultTimeout,
        IReadOnlyCollection<int> defaultExitCodes, Action<string> say, Action<string> emit,
        CancellationToken ct) =>
        RunAllAsync(steps, vars, new StepDefaults(defaultTimeout, defaultExitCodes),
                    say, emit, null, ct);

    /// <summary>As above, with <paramref name="phases"/> told about each step as it starts
    /// and ends (see IStepProgress). A successful Stop counts as success here.</summary>
    public static async Task<bool> RunAllAsync(
        JsonArray steps, PackageVariables vars, StepDefaults defaults,
        Action<string> say, Action<string> emit, PhaseTracker? phases, CancellationToken ct) =>
        await RunTreeAsync(steps, vars, defaults, say, emit, phases, null, ct) != RunEnd.Failed;

    /// <summary>Run a step list that may branch (agent 3.54.0) -- what DeployPackageExecutor
    /// calls. <paramref name="facts"/> answers If conditions; without it every condition about
    /// the machine reads unknown, which fails the If closed.
    ///
    /// **Numbering is depth-first, exactly as the hub's packages.iter_steps:** an If, then
    /// every step of its Then lane, then every step of its Else lane, then what follows. The
    /// lane NOT taken is still counted -- each of its steps reported <c>not_run</c> -- or every
    /// ordinal after it would land on the wrong row of the console's step list.
    ///
    /// A cancelled run is never "carried on" past, whatever a step's continue_on_error says:
    /// that flag is for a step that failed, and an operator's Abort is not a failure to
    /// tolerate.</summary>
    public static Task<RunEnd> RunTreeAsync(
        JsonArray steps, PackageVariables vars, StepDefaults defaults,
        Action<string> say, Action<string> emit, PhaseTracker? phases, IMachineFacts? facts,
        CancellationToken ct) =>
        new TreeRun(vars, defaults, say, emit, phases, facts, ct).ListAsync(steps);

    /// <summary>One run's walk through the tree: the shared ordinal counter and the last exit
    /// code, which both have to survive descending into a lane and coming back out.</summary>
    private sealed class TreeRun(
        PackageVariables vars, StepDefaults defaults, Action<string> say, Action<string> emit,
        PhaseTracker? phases, IMachineFacts? facts, CancellationToken ct)
    {
        private int _ordinal;
        private int? _lastExitCode;
        private readonly IMachineFacts _facts = facts ?? new NoFacts();

        public async Task<RunEnd> ListAsync(JsonArray? list)
        {
            if (list is null) return RunEnd.Completed;
            foreach (var node in list)
            {
                var index = ++_ordinal;
                if (ct.IsCancellationRequested)
                {
                    say($"[deploy] stopped before step {index}: the run was cancelled");
                    return RunEnd.Failed;
                }
                phases?.Begin(StepPhase.Step, index);
                if (node is not JsonObject step)
                {
                    say($"[deploy] step {index}: FAILED: not an object");
                    phases?.End(StepState.Failed, message: "not an object");
                    return RunEnd.Failed;
                }

                var kind = step.GetString("kind") ?? "";
                var label = step.GetString("name");
                say($"[deploy] step {index} — {kind}" +
                    (string.IsNullOrWhiteSpace(label) ? "" : $": {label}"));

                if (kind == "if")
                {
                    var end = await IfAsync(step, index);
                    if (end != RunEnd.Completed) return end;
                    continue;
                }
                if (kind == "stop")
                {
                    var success = step.GetString("outcome") == "success";
                    var message = step.GetString("message");
                    var text = string.IsNullOrWhiteSpace(message)
                        ? (success ? "stopped: nothing to do on this machine" : "stopped by the package")
                        : message!;
                    say($"[deploy] {(success ? "STOP (success)" : "STOP (failure)")}: {text}");
                    phases?.End(success ? StepState.Ok : StepState.Failed, message: text);
                    return success ? RunEnd.StoppedSuccess : RunEnd.Failed;
                }

                var outcome = await RunOneAsync(step, kind, vars, defaults.Timeout,
                                                defaults.ExitCodes, say, emit, ct);
                if (outcome.ExitCode is { } code) _lastExitCode = code;
                if (outcome.Aborted || (!outcome.Succeeded && ct.IsCancellationRequested))
                {
                    phases?.Fail(null, aborted: phases.AbortRequested);
                    say($"[deploy] stopped at step {index}: the run was cancelled");
                    return RunEnd.Failed;
                }
                if (outcome.Succeeded)
                {
                    phases?.End(StepState.Ok, outcome.ExitCode);
                    continue;
                }
                if (outcome.Continue)
                {
                    phases?.End(StepState.Skipped, outcome.ExitCode, outcome.Why);
                    say($"[deploy] step {index} failed, carrying on as configured");
                    continue;
                }
                phases?.End(StepState.Failed, outcome.ExitCode, outcome.Why);
                say($"[deploy] FAILED at step {index}");
                return RunEnd.Failed;
            }
            return RunEnd.Completed;
        }

        private async Task<RunEnd> IfAsync(JsonObject step, int index)
        {
            var evaluator = new ConditionEvaluator(_facts, vars, () => _lastExitCode, say, emit, ct);
            Tri answer;
            try
            {
                answer = await evaluator.EvaluateAsync(step["condition"]);
            }
            catch (OperationCanceledException)
            {
                phases?.Fail(null, aborted: phases.AbortRequested);
                say($"[deploy] stopped at step {index}: the run was cancelled");
                return RunEnd.Failed;
            }
            var seen = string.Join("; ", evaluator.Notes.Distinct().Take(6));
            say($"[deploy] condition is {answer.ToString().ToLowerInvariant()}" +
                (seen.Length > 0 ? $" ({seen})" : ""));

            var takeThen = answer == Tri.True;
            if (answer == Tri.Unknown)
            {
                // UNKNOWN never quietly becomes Else -- see hub/package_conditions.py. Only
                // the operator's own on_unknown: "else" turns it into one.
                if (step.GetString("on_unknown") == "else")
                {
                    say("[deploy] the machine could not tell; running Else as configured");
                }
                else
                {
                    var why = "could not decide: " + (seen.Length > 0 ? seen : "the condition is unknown here");
                    var carryOn = step.GetBool("continue_on_error");
                    phases?.End(carryOn ? StepState.Skipped : StepState.Failed, message: Trim(why));
                    // Both lanes are left unrun either way; count them so later ordinals
                    // stay where the console expects them.
                    MarkNotRun(step["then"] as JsonArray);
                    MarkNotRun(step["else"] as JsonArray);
                    if (carryOn)
                    {
                        say($"[deploy] step {index}: {why} -- skipping both branches as configured");
                        return RunEnd.Completed;
                    }
                    say($"[deploy] FAILED at step {index}: {why}");
                    return RunEnd.Failed;
                }
            }

            var lane = takeThen ? "Then" : "Else";
            phases?.End(StepState.Ok, message: Trim($"{lane}: {seen}"));
            if (takeThen)
            {
                var end = await ListAsync(step["then"] as JsonArray);
                if (end != RunEnd.Completed) return end;
                MarkNotRun(step["else"] as JsonArray);
                return RunEnd.Completed;
            }
            MarkNotRun(step["then"] as JsonArray);
            return await ListAsync(step["else"] as JsonArray);
        }

        /// <summary>Count -- and report as not_run -- every step of a lane that will not run,
        /// nested lanes included.</summary>
        private void MarkNotRun(JsonArray? list)
        {
            if (list is null) return;
            foreach (var node in list)
            {
                var index = ++_ordinal;
                phases?.Mark(StepPhase.Step, index, StepState.NotRun);
                if (node is JsonObject step && step.GetString("kind") == "if")
                {
                    MarkNotRun(step["then"] as JsonArray);
                    MarkNotRun(step["else"] as JsonArray);
                }
            }
        }

        private static string Trim(string text) => text.Length <= 400 ? text : text[..400] + "…";
    }

    /// <summary>The facts a run has when it was given none: every one unknown.</summary>
    private sealed class NoFacts : IMachineFacts
    {
        public string? Get(string name) => null;
    }

    private static async Task<StepOutcome> RunOneAsync(
        JsonObject step, string kind, PackageVariables vars, int defaultTimeout,
        IReadOnlyCollection<int> defaultExitCodes, Action<string> say, Action<string> emit,
        CancellationToken ct)
    {
        // continue_on_error is read up front so it applies to a malformed step too --
        // otherwise the one failure an operator most wants to skip past (a step this agent
        // is too old to understand) would be the one that ignores the flag.
        var carryOn = step["continue_on_error"] is JsonValue flag
                      && bool.TryParse(flag.ToString(), out var parsed) && parsed;
        StepOutcome Fail(string why)
        {
            say($"[deploy] {why}");
            return new StepOutcome(false, carryOn, Why: why.StartsWith("FAILED: ")
                ? why["FAILED: ".Length..] : why);
        }

        var timeout = Math.Clamp(step.GetInt("timeout_seconds", defaultTimeout), 30, 24 * 60 * 60);
        var codes = step.GetIntSet("success_exit_codes");
        if (codes.Count == 0) codes = new HashSet<int>(defaultExitCodes);

        try
        {
            switch (kind)
            {
                case "extract":
                {
                    // Pure .NET rather than shelling out to Expand-Archive: it is faster,
                    // it does not depend on a PowerShell version, and the zip-slip check
                    // below is ours to make rather than someone else's to have made.
                    var archive = vars.Resolve(step.GetString("archive"));
                    if (string.IsNullOrWhiteSpace(archive))
                        return Fail("FAILED: the unpack step has no archive");
                    var dest = vars.Resolve(step.GetString("dest"));
                    var saveAs = step.GetString("save_as") ?? "extracted";
                    if (string.IsNullOrWhiteSpace(dest)) dest = vars.PathIn(saveAs);

                    say($"[deploy] unpacking {archive} into {dest}");
                    var entries = Extract(archive, dest);
                    say($"[deploy] unpacked {entries} entries");
                    vars.Bind(saveAs, dest);
                    return new StepOutcome(true, carryOn);
                }

                case "run":
                {
                    var command = vars.Resolve(step.GetString("command"));
                    if (string.IsNullOrWhiteSpace(command))
                        return Fail("FAILED: the run step has no command");
                    return Judge(await Start(command, vars.Resolve(step.GetString("args"))),
                                 codes, timeout, say, carryOn);
                }

                case "powershell":
                {
                    var script = vars.Resolve(step.GetString("script"));
                    if (string.IsNullOrWhiteSpace(script))
                        return Fail("FAILED: the PowerShell step has no script");
                    // Written to a file and run with -File rather than passed with
                    // -Command: a script long enough to be worth writing here will contain
                    // quotes, and quoting it through a command line is a losing game. The
                    // file lands in the attempt's own directory, which is SYSTEM-owned and
                    // deleted afterward.
                    var path = vars.PathIn($"step-{Guid.NewGuid():N}.ps1");
                    await File.WriteAllTextAsync(path, script, ct);
                    var args = $"-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"{path}\"";
                    return Judge(await Start("powershell.exe", args), codes, timeout, say, carryOn);
                }

                case "winget":
                {
                    var id = step.GetString("id");
                    if (string.IsNullOrWhiteSpace(id))
                        return Fail("FAILED: the winget step has no package id");
                    var winget = WingetLocator.Find();
                    if (winget is null) return Fail("FAILED: " + WingetLocator.NotFoundMessage);
                    var args = $"install --id {id} --silent --accept-package-agreements " +
                               "--accept-source-agreements";
                    var extra = vars.Resolve(step.GetString("args"));
                    if (!string.IsNullOrWhiteSpace(extra)) args += " " + extra;
                    return Judge(await Start(winget, args), codes, timeout, say, carryOn);
                }

                case "pnputil":
                {
                    var path = vars.Resolve(step.GetString("path"));
                    if (string.IsNullOrWhiteSpace(path))
                        return Fail("FAILED: the driver step has no path");
                    var subdirs = step["subdirs"] is not JsonValue s
                                  || !bool.TryParse(s.ToString(), out var wants) || wants;
                    // /add-driver takes an .inf or a pattern, never a bare folder -- point
                    // it at a directory and it fails with a usage error that says nothing
                    // about what was actually wrong. A driver pack IS a folder as far as an
                    // operator is concerned, so accept one and expand it here.
                    var target = Directory.Exists(path)
                        ? Path.Combine(path, "*.inf")
                        : path;
                    var args = $"/add-driver \"{target}\" /install" + (subdirs ? " /subdirs" : "");
                    var pnputil = Path.Combine(Environment.SystemDirectory, "pnputil.exe");
                    return Judge(await Start(pnputil, args), codes, timeout, say, carryOn);
                }

                default:
                    // A kind this build does not implement: an agent older than the hub
                    // that wrote the package. Fail closed and say which word it did not
                    // know, exactly like an unsupported detection kind.
                    return Fail($"FAILED: unsupported step kind '{kind}' — update the agent");
            }
        }
        catch (Exception e)
        {
            return Fail($"FAILED: {e.Message}");
        }

        Task<ProcessOutcome> Start(string file, string args)
        {
            say($"[deploy] running: {file} {args}");
            return ProcessRunner.RunAsync(file, args, ct, timeoutSeconds: timeout,
                                          workingDir: vars.WorkDir, onLine: emit);
        }

        StepOutcome Judge(ProcessOutcome outcome, HashSet<int> accepted, int seconds,
                          Action<string> log, bool keepGoing)
        {
            // ProcessRunner reports a cancelled wait as a timeout. When the token is what
            // ended it -- an Abort, or the service stopping -- say that, not "timed out".
            if (outcome.TimedOut && ct.IsCancellationRequested)
                return new StepOutcome(false, false, Aborted: true);
            if (outcome.TimedOut)
            {
                log($"[deploy] FAILED: timed out after {seconds}s");
                return new StepOutcome(false, keepGoing, Why: $"timed out after {seconds}s");
            }
            if (!accepted.Contains(outcome.ExitCode))
            {
                var why = $"exit code {outcome.ExitCode} is not in " +
                          $"[{string.Join(", ", accepted.OrderBy(c => c))}]";
                log("[deploy] FAILED: " + why);
                return new StepOutcome(false, keepGoing, outcome.ExitCode, why);
            }
            log($"[deploy] exit code {outcome.ExitCode} accepted");
            return new StepOutcome(true, keepGoing, outcome.ExitCode);
        }
    }

    /// <summary>Unpack a zip into a directory, refusing entries that would land outside it.
    /// Returns the entry count.
    ///
    /// ZipFile.ExtractToDirectory does check this in current .NET, but the check is the
    /// entire security property of unpacking an archive an operator downloaded from a
    /// vendor, so it is made HERE rather than assumed of whatever runtime the agent is
    /// running on: an entry named ..\..\Windows\System32\driver.sys must not be written.
    /// </summary>
    public static int Extract(string archivePath, string destination)
    {
        Directory.CreateDirectory(destination);
        var root = Path.GetFullPath(destination);
        // The trailing separator matters: without it "C:\work\drivers-evil" passes a
        // StartsWith test against "C:\work\drivers".
        var rootPrefix = root.EndsWith(Path.DirectorySeparatorChar)
            ? root : root + Path.DirectorySeparatorChar;

        using var zip = ZipFile.OpenRead(archivePath);
        var count = 0;
        foreach (var entry in zip.Entries)
        {
            var target = Path.GetFullPath(Path.Combine(root, entry.FullName));
            if (!target.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase))
                throw new IOException($"the archive entry '{entry.FullName}' points outside the folder");

            if (string.IsNullOrEmpty(entry.Name))
            {
                Directory.CreateDirectory(target);   // a directory entry
                continue;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            entry.ExtractToFile(target, overwrite: true);
            count++;
        }
        return count;
    }
}

/// <summary>
/// The `{name}` bindings for one deploy attempt, and the directory they live in.
///
/// Substitution is textual and deliberately narrow: only `{lowercase_words}` are replaced,
/// so an MSI product code like {90160000-008C-0000-1000-0000000FF1CE} in an operator's
/// arguments passes through untouched. The hub validates the same grammar at save time
/// (see packages._VARIABLE_RE), so an unknown name is refused there rather than reaching a
/// machine as a literal brace pair in a command line.
/// </summary>
public sealed class PackageVariables
{
    private readonly Dictionary<string, string> _bindings = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _withheld = new(StringComparer.Ordinal);
    private IMachineFacts? _facts;

    /// <summary>`{name}` -> the machine fact it substitutes. hub/package_conditions.py
    /// FACT_BINDINGS, spelled identically; the hub refuses a payload or extract step that
    /// claims one of these names, so they never collide with a binding.</summary>
    public static readonly IReadOnlyDictionary<string, string> FactBindings =
        new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["manufacturer"] = "hw.manufacturer",
            ["model"] = "hw.model",
            ["model_name"] = "hw.model_name",
            ["sku"] = "hw.sku",
            ["os_build"] = "os.build",
            ["arch"] = "os.arch",
            ["hostname"] = "sys.hostname",
        };

    public PackageVariables(string workDir)
    {
        WorkDir = workDir;
        Bind("work", workDir);
    }

    public string WorkDir { get; }

    public void Bind(string name, string value)
    {
        _withheld.Remove(name);
        _bindings[name] = value;
    }

    /// <summary>A name that is deliberately NOT bound on this machine, with the reason -- a
    /// payload whose download condition was false. A step that uses it fails with that
    /// reason rather than running with a literal "{drivers}" in its arguments.</summary>
    public void Withhold(string name, string reason)
    {
        _bindings.Remove(name);
        _withheld[name] = reason;
    }

    /// <summary>Let `{model}` and friends resolve, from <paramref name="facts"/>, on first
    /// use. Lazy: a package that never says `{model}` never pays for the WMI read.</summary>
    public void BindFacts(IMachineFacts facts) => _facts = facts;

    /// <summary>A path inside the attempt's working directory. The name is stripped to a
    /// file name first: a binding is ours, but the SUFFIX can come from a package
    /// definition, and joining an unchecked one would write outside the directory the
    /// executor promises to delete.</summary>
    public string PathIn(string name) =>
        Path.Combine(WorkDir, Path.GetFileName(name) is { Length: > 0 } safe ? safe : "out");

    /// <summary>Substitute every bound `{name}`. Throws InvalidOperationException, with the
    /// sentence the console shows, for a withheld name or a fact this machine could not read
    /// -- the runner turns that into a failed step, which is the whole point: an unreadable
    /// model must not become a path with "{model}" in it.</summary>
    public string Resolve(string? text)
    {
        if (string.IsNullOrEmpty(text)) return text ?? "";
        foreach (var (name, reason) in _withheld)
        {
            if (text.Contains("{" + name + "}", StringComparison.Ordinal))
                throw new InvalidOperationException(reason);
        }
        foreach (var (name, value) in _bindings)
            text = text.Replace("{" + name + "}", value, StringComparison.Ordinal);
        if (_facts is not null)
        {
            foreach (var (name, variable) in FactBindings)
            {
                var token = "{" + name + "}";
                if (!text.Contains(token, StringComparison.Ordinal)) continue;
                var value = _facts.Get(variable)
                    ?? throw new InvalidOperationException(
                        $"{token} is this machine's {variable}, which could not be read");
                text = text.Replace(token, value, StringComparison.Ordinal);
            }
        }
        return text;
    }
}
