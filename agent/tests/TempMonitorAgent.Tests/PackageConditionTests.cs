using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Tests;

/// <summary>
/// If / Else steps, Stop steps and payload download conditions (agent 3.54.0, roadmap #5).
///
/// The silent failure this file exists to catch is **the wrong driver pack on a machine
/// nobody looks at**: a condition that compares differently here than in the console's
/// preview ("LENOVO" is not "Lenovo", 10.0.9200 sorts above 10.0.22631), an unreadable model
/// quietly taking the Else branch, or an untaken branch that shifts every later step onto the
/// wrong row of the console's step list. None of those fails a deploy. Each of them installs
/// something, reports green, and is found months later.
/// </summary>
public class PackageConditionTests
{
    private sealed class Facts(Dictionary<string, string?> values) : IMachineFacts
    {
        public string? Get(string name) => values.TryGetValue(name, out var v) ? v : null;
    }

    private sealed class Recorder : IStepProgress
    {
        private readonly object _gate = new();
        public List<StepEvent> Events { get; } = new();
        public bool AbortRequested => false;

        public void Report(StepEvent e)
        {
            lock (_gate) Events.Add(e);
        }

        public string[] Trace()
        {
            lock (_gate) return Events.Select(e => $"{e.Phase}{e.Ordinal}:{e.State}").ToArray();
        }
    }

    private sealed class UnusedDownloader : IPackageDownloader
    {
        public Task<string?> DownloadPackageAsync(string url, string destPath,
                                                  string? sha256, CancellationToken ct)
            => throw new InvalidOperationException("no payload should be fetched");
    }

    private static readonly Dictionary<string, string?> Latitude = new()
    {
        ["hw.manufacturer"] = "Dell Inc.",
        ["hw.model"] = "Latitude 5440",
        ["os.version"] = "10.0.22631",
        ["os.build"] = "22631",
        ["hw.memory_gb"] = "16",
    };

    private static DeployPackageExecutor Executor(Dictionary<string, string?> facts) =>
        new(NullLogger<DeployPackageExecutor>.Instance, new UnusedDownloader(),
            _ => new Facts(facts));

    private static FleetCommand Package(JsonArray steps, JsonArray? sources = null) => new()
    {
        Id = "cmd-cond",
        Type = "deploy_package",
        Params = new JsonObject
        {
            ["package_name"] = "Drivers by model",
            ["source"] = new JsonObject { ["kind"] = "multi" },
            ["sources"] = sources ?? new JsonArray(),
            ["steps"] = steps,
            ["install_command"] = "",
            ["success_exit_codes"] = new JsonArray(0),
            ["detection"] = new JsonObject { ["kind"] = "none" },
            ["timeout_seconds"] = 120,
        },
    };

    private static JsonObject Run(string args, params (string Key, JsonNode? Value)[] extra)
    {
        var step = new JsonObject { ["kind"] = "run", ["command"] = "cmd.exe", ["args"] = args };
        foreach (var (key, value) in extra) step[key] = value;
        return step;
    }

    private static JsonObject Leaf(string variable, string cmp, JsonNode? value) =>
        new() { ["var"] = variable, ["cmp"] = cmp, ["value"] = value };

    private static JsonObject If(JsonNode condition, JsonArray then, JsonArray? otherwise = null,
                                 string? onUnknown = null)
    {
        var step = new JsonObject
        {
            ["kind"] = "if", ["condition"] = condition,
            ["then_steps"] = then, ["else_steps"] = otherwise ?? new JsonArray(),
        };
        if (onUnknown is not null) step["on_unknown"] = onUnknown;
        return step;
    }

    private static JsonArray List(params JsonNode[] nodes) => new(nodes);

    // ------------------------------------------------------------------ comparisons

    [Theory]
    // Text equality is case-insensitive and trimmed: a vendor's capitalisation is not a
    // different machine. Mirrors rules._loose_equal.
    [InlineData("text", "==", "LENOVO", "Lenovo", Tri.True)]
    [InlineData("text", "!=", " Dell Inc. ", "dell inc.", Tri.False)]
    [InlineData("text", "contains", "Latitude 5440", "latitude", Tri.True)]
    [InlineData("text", "starts_with", "ThinkPad X1 Carbon Gen 11", "thinkpad x1", Tri.True)]
    [InlineData("text", "matches", "Latitude 5440", "latitude 54?0", Tri.True)]
    [InlineData("text", "matches", "Latitude 7440", "latitude 5*", Tri.False)]
    // Versions are compared as versions. As text, 10.0.9200 sorts ABOVE 10.0.22631.
    [InlineData("version", ">=", "10.0.22631", "10.0.9200", Tri.True)]
    [InlineData("version", "<", "10.0.9200", "10.0.22631", Tri.True)]
    [InlineData("version", "==", "10.0", "10.0.0", Tri.True)]
    // A version that does not start with a number is unknown, never "version 0".
    [InlineData("version", ">=", "N3KET60W (1.40)", "1.0", Tri.Unknown)]
    [InlineData("number", ">=", "16", "8", Tri.True)]
    [InlineData("number", ">", "abc", "8", Tri.Unknown)]
    [InlineData("bool", "==", "true", "true", Tri.True)]
    public void ComparisonsMatchTheHub(string kind, string cmp, string left, string right, Tri expected)
    {
        Assert.Equal(expected, ConditionEvaluator.Compare(kind, cmp, left, JsonValue.Create(right)));
    }

    [Fact]
    public void AnUnknownValueIsUnknownExceptToThePresenceOperators()
    {
        Assert.Equal(Tri.Unknown, ConditionEvaluator.Compare("text", "==", null, JsonValue.Create("x")));
        Assert.Equal(Tri.Unknown, ConditionEvaluator.Compare("text", "!=", null, JsonValue.Create("x")));
        Assert.Equal(Tri.True, ConditionEvaluator.Compare("text", "is_unknown", null, null));
        Assert.Equal(Tri.False, ConditionEvaluator.Compare("text", "is_known", null, null));
    }

    [Fact]
    public void MembershipUsesTheSameEquality()
    {
        var list = new JsonArray("Latitude 5440", "Latitude 7440");
        Assert.Equal(Tri.True, ConditionEvaluator.Compare("text", "in", "LATITUDE 7440", list));
        Assert.Equal(Tri.False, ConditionEvaluator.Compare("text", "not_in", "latitude 5440", list));
    }

    [Fact]
    public void AStarInTheTextDoesNotEatThePatternsStar()
    {
        // The same edge rules.wildcard_match documents: the * branch is tested first.
        Assert.True(ConditionEvaluator.WildcardMatch("*x", "*abx"));
    }

    [Fact]
    public async Task AndAndOrAreKleene()
    {
        var vars = new PackageVariables(Path.GetTempPath());
        var eval = new ConditionEvaluator(new Facts(Latitude), vars, () => null, _ => { }, _ => { },
                                          CancellationToken.None);
        var unknown = Leaf("hw.model_name", "==", "x");
        var falseLeaf = Leaf("hw.model", "==", "Precision 3581");
        var trueLeaf = Leaf("hw.model", "contains", "Latitude");

        // A definite false settles an `and` even next to an unknown; a definite true an `or`.
        Assert.Equal(Tri.False, await eval.EvaluateAsync(
            new JsonObject { ["op"] = "and", ["nodes"] = List(unknown.DeepClone(), falseLeaf.DeepClone()) }));
        Assert.Equal(Tri.True, await eval.EvaluateAsync(
            new JsonObject { ["op"] = "or", ["nodes"] = List(unknown.DeepClone(), trueLeaf.DeepClone()) }));
        Assert.Equal(Tri.Unknown, await eval.EvaluateAsync(
            new JsonObject { ["op"] = "and", ["nodes"] = List(unknown.DeepClone(), trueLeaf.DeepClone()) }));
        Assert.Equal(Tri.Unknown, await eval.EvaluateAsync(
            new JsonObject { ["op"] = "not", ["nodes"] = List(unknown.DeepClone()) }));
    }

    // ------------------------------------------------------------------ the step tree

    [Fact]
    public async Task TheMatchingBranchRunsAndTheOtherIsReportedNotRun()
    {
        var progress = new Recorder();
        var result = await Executor(Latitude).ExecuteAsync(Package(List(
            If(Leaf("hw.model", "contains", "latitude"),
               List(Run("/c echo dell-pack")),
               List(Run("/c echo generic-pack"))),
            Run("/c echo after"))), null, progress, CancellationToken.None);

        Assert.True(result.Success);
        Assert.Contains("dell-pack", result.Output);
        Assert.DoesNotContain("generic-pack", result.Output);
        // Depth-first numbering: If=1, Then=2, Else=3, then the step after the If is 4. The
        // untaken lane is still counted, or "after" would be reported on Else's row.
        Assert.Equal(new[]
        {
            "step1:running", "step1:ok", "step2:running", "step2:ok", "step3:not_run",
            "step4:running", "step4:ok", "detect1:running", "detect1:ok",
        }, progress.Trace());
        Assert.StartsWith("Then", progress.Events[1].Message);
    }

    [Fact]
    public async Task TheElseBranchRunsWhenTheConditionIsFalse()
    {
        var progress = new Recorder();
        var result = await Executor(Latitude).ExecuteAsync(Package(List(
            If(Leaf("hw.model", "==", "ThinkPad X1"),
               List(Run("/c echo lenovo-pack"), Run("/c echo lenovo-two")),
               List(Run("/c echo generic-pack"))))), null, progress, CancellationToken.None);

        Assert.True(result.Success);
        Assert.Contains("generic-pack", result.Output);
        Assert.Equal(new[]
        {
            "step1:running", "step1:ok", "step2:not_run", "step3:not_run",
            "step4:running", "step4:ok", "detect1:running", "detect1:ok",
        }, progress.Trace());
    }

    [Fact]
    public async Task AnUnreadableModelFailsTheIfInsteadOfRunningElse()
    {
        // THE case this feature must never get wrong: WMI did not answer, and the generic
        // pack would otherwise land on a machine nobody identified.
        var progress = new Recorder();
        var result = await Executor(new Dictionary<string, string?>()).ExecuteAsync(Package(List(
            If(Leaf("hw.model", "contains", "latitude"),
               List(Run("/c echo dell-pack")),
               List(Run("/c echo generic-pack"))))), null, progress, CancellationToken.None);

        Assert.False(result.Success);
        Assert.DoesNotContain("generic-pack", result.Output);
        Assert.Equal("step1:failed", progress.Trace()[1]);
        Assert.Contains("could not decide", progress.Events[1].Message);
    }

    [Fact]
    public async Task OnUnknownElseIsTheOperatorsExplicitChoice()
    {
        var result = await Executor(new Dictionary<string, string?>()).ExecuteAsync(Package(List(
            If(Leaf("hw.model", "contains", "latitude"),
               List(Run("/c echo dell-pack")),
               List(Run("/c echo generic-pack")), onUnknown: "else"))),
            null, new Recorder(), CancellationToken.None);

        Assert.True(result.Success);
        Assert.Contains("generic-pack", result.Output);
    }

    [Fact]
    public async Task ANestedIfInAnUntakenLaneIsCountedWholeAsNotRun()
    {
        var progress = new Recorder();
        await Executor(Latitude).ExecuteAsync(Package(List(
            If(Leaf("hw.manufacturer", "starts_with", "lenovo"),
               List(If(Leaf("hw.model", "==", "x"), List(Run("/c echo a")), List(Run("/c echo b")))),
               List(Run("/c echo dell"))))), null, progress, CancellationToken.None);

        // 1 = outer If, 2 = inner If, 3/4 = its lanes, 5 = the outer Else step that runs.
        Assert.Equal(new[]
        {
            "step1:running", "step1:ok", "step2:not_run", "step3:not_run", "step4:not_run",
            "step5:running", "step5:ok", "detect1:running", "detect1:ok",
        }, progress.Trace());
    }

    [Fact]
    public async Task ASuccessfulStopEndsTheDeployWithoutDetection()
    {
        var progress = new Recorder();
        var cmd = Package(List(
            If(Leaf("hw.model", "contains", "latitude"),
               List(new JsonObject { ["kind"] = "stop", ["outcome"] = "success",
                                     ["message"] = "already current" })),
            Run("/c echo never")));
        // A detection rule that would FAIL if it ran: a Stop is the package saying the
        // software is not meant to be installed here.
        cmd.Params["detection"] = new JsonObject
            { ["kind"] = "file_exists", ["path"] = @"C:\definitely\not\here.exe" };

        var result = await Executor(Latitude).ExecuteAsync(cmd, null, progress, CancellationToken.None);

        Assert.True(result.Success);
        Assert.DoesNotContain("echo never", result.Output);
        Assert.Contains("detect1:not_run", progress.Trace());
    }

    [Fact]
    public async Task AFailingStopFailsTheDeployWithItsMessage()
    {
        var progress = new Recorder();
        var result = await Executor(Latitude).ExecuteAsync(Package(List(
            new JsonObject { ["kind"] = "stop", ["outcome"] = "failure",
                             ["message"] = "Unsupported model" })),
            null, progress, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Equal("step1:failed", progress.Trace()[1]);
        Assert.Equal("Unsupported model", progress.Events[1].Message);
    }

    [Fact]
    public async Task AFactVariableIsSubstitutedAndAnUnreadableOneFailsTheStep()
    {
        var ok = await Executor(Latitude).ExecuteAsync(Package(List(
            Run("/c echo pack-for-{model}"))), null, new Recorder(), CancellationToken.None);
        Assert.True(ok.Success);
        Assert.Contains("pack-for-Latitude 5440", ok.Output);

        var progress = new Recorder();
        var failed = await Executor(new Dictionary<string, string?>()).ExecuteAsync(Package(List(
            Run("/c echo pack-for-{model}"))), null, progress, CancellationToken.None);
        Assert.False(failed.Success);
        Assert.DoesNotContain("pack-for-{model}", failed.Output.Replace("echo pack-for-{model}", ""));
        Assert.Contains("could not be read", progress.Events[1].Message);
    }

    [Theory]
    // An OEM writes these strings, and they land in a command line that runs as SYSTEM. A
    // quote or a command separator must fail the step, not change what it runs.
    [InlineData("Latitude 5440\" & calc & \"")]
    [InlineData("Model'; Remove-Item C:\\x; '")]
    [InlineData("$(whoami)")]
    [InlineData("50%PATH%")]
    public async Task AFactValueThatCouldChangeTheCommandIsRefused(string model)
    {
        var progress = new Recorder();
        var result = await Executor(new Dictionary<string, string?> { ["hw.model"] = model })
            .ExecuteAsync(Package(List(Run("/c echo pack-for-{model}"))), null, progress,
                          CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("not safe to put into a command line", progress.Events[1].Message);
    }

    [Theory]
    [InlineData("ThinkPad X1 Carbon Gen 11")]
    [InlineData("VMware7,1")]
    [InlineData("HP EliteBook 840 G8 Notebook PC")]
    [InlineData("Surface Laptop 5 (1950)")]
    public async Task RealModelNamesAreSubstituted(string model)
    {
        var result = await Executor(new Dictionary<string, string?> { ["hw.model"] = model })
            .ExecuteAsync(Package(List(Run("/c echo pack-for-{model}"))), null, new Recorder(),
                          CancellationToken.None);

        Assert.True(result.Success);
        Assert.Contains("pack-for-" + model, result.Output);
    }

    [Fact]
    public async Task APayloadWhoseConditionIsFalseIsNotDownloadedAndCannotBeUsed()
    {
        var sources = new JsonArray(new JsonObject
        {
            ["name"] = "lenovo_pack", ["kind"] = "url", ["ref"] = "https://example.invalid/x.zip",
            ["only_if"] = Leaf("hw.manufacturer", "starts_with", "lenovo"),
        });
        var progress = new Recorder();
        // UnusedDownloader throws if anything is fetched, so success here also proves the
        // download never happened.
        var result = await Executor(Latitude).ExecuteAsync(Package(List(
            Run("/c echo {lenovo_pack}")), sources), null, progress, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Equal("download1:not_run", progress.Trace()[0]);
        Assert.Contains("was not downloaded on this machine", progress.Events[2].Message);
    }

    [Fact]
    public async Task ThePreviousStepsExitCodeCanBeTested()
    {
        var result = await Executor(Latitude).ExecuteAsync(Package(List(
            Run("/c exit 3010", ("success_exit_codes", new JsonArray(0, 3010))),
            If(Leaf("step.last_exit_code", "==", 3010), List(Run("/c echo needs-reboot"))))),
            null, new Recorder(), CancellationToken.None);

        Assert.True(result.Success);
        Assert.Contains("needs-reboot", result.Output);
    }

    [Theory]
    // An explicit answer is the answer.
    [InlineData("exit 0", Tri.True)]
    [InlineData("exit 1", Tri.False)]
    [InlineData("exit 7", Tri.Unknown)]
    // A script that ends without `exit` and without an error answers yes, as it would alone.
    [InlineData("$x = 1", Tri.True)]
    // ...even when its last line is a native command that "found nothing" (review on #228):
    // findstr / reg query exit 1, and that must not leak out as the check's answer.
    [InlineData("cmd.exe /c exit 1", Tri.True)]
    // Passing a native command's code on is still the script's own choice.
    [InlineData("cmd.exe /c exit 1\nexit $LASTEXITCODE", Tri.False)]
    // The review finding on #228: under `powershell -File` all three of these exit 1, which
    // read as FALSE and sent every machine down Else. A broken check is unknown, never no.
    [InlineData("throw 'typo in the check'", Tri.Unknown)]
    [InlineData("Get-NoSuchCmdlet -ErrorAction Stop", Tri.Unknown)]
    [InlineData("if (", Tri.Unknown)]
    public async Task APowerShellCheckThatBreaksIsUnknownNotFalse(string script, Tri expected)
    {
        var dir = Path.Combine(Path.GetTempPath(), "pkgcond-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var eval = new ConditionEvaluator(new Facts(Latitude), new PackageVariables(dir),
                                              () => null, _ => { }, _ => { }, CancellationToken.None);
            var result = await eval.EvaluateAsync(
                new JsonObject { ["probe"] = "powershell", ["script"] = script });
            Assert.Equal(expected, result);
        }
        finally
        {
            try { Directory.Delete(dir, recursive: true); } catch { /* best effort */ }
        }
    }

    [Fact]
    public async Task AFolderATimedOutProcessRanInCanBeDeletedAtOnce()
    {
        // The review question on #228: an If's PowerShell check is written into the attempt
        // folder, and on a timeout or an Abort the process is killed -- which only ASKS. The
        // folder is the process's working directory, and Windows refuses to delete that while
        // the process is still going. Pins the contract ProcessRunner now keeps by waiting
        // for the exit; the race itself is timing-dependent and did not reproduce on the
        // machine this was written on, so this test passing does not prove it was ever hit.
        var dir = Path.Combine(Path.GetTempPath(), "pkgcwd-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, "check.ps1"), "Start-Sleep 30");

        var outcome = await ProcessRunner.RunAsync(
            "cmd.exe", "/c ping -n 30 127.0.0.1 > nul", CancellationToken.None,
            timeoutSeconds: 1, workingDir: dir);

        Assert.True(outcome.TimedOut);
        Directory.Delete(dir, recursive: true);   // throws if the folder is still in use
        Assert.False(Directory.Exists(dir));
    }

    [Fact]
    public void TheSweepRemovesOnlyAttemptFoldersLeftOverFromBefore()
    {
        var staging = Path.Combine(Path.GetTempPath(), "pkgsweep-" + Guid.NewGuid().ToString("N"));
        var stale = Path.Combine(staging, "old-attempt");
        var live = Path.Combine(staging, "live-attempt");
        Directory.CreateDirectory(stale);
        Directory.CreateDirectory(live);
        File.WriteAllText(Path.Combine(stale, "check.ps1"), "exit 0");
        var now = DateTime.UtcNow;
        Directory.SetLastWriteTimeUtc(stale, now.AddHours(-2));
        try
        {
            Executor(Latitude).SweepStaleAttempts(staging, now);

            Assert.False(Directory.Exists(stale));
            Assert.True(Directory.Exists(live));   // a running attempt is never swept
        }
        finally
        {
            try { Directory.Delete(staging, recursive: true); } catch { /* best effort */ }
        }
    }

    [Theory]
    [InlineData(9, "laptop")]
    [InlineData(10, "laptop")]
    [InlineData(31, "laptop")]
    [InlineData(3, "desktop")]
    [InlineData(35, "desktop")]
    [InlineData(30, "tablet")]
    [InlineData(23, "server")]
    [InlineData(12, "other")]
    public void ChassisTypesFoldToTheFiveWords(int type, string word)
    {
        Assert.Equal(word, WindowsMachineFacts.ChassisWord(type));
    }
}
