using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The console's live "step 3 of 5" view and its Abort button (roadmap #5, agent 3.53.0).
///
/// The silent failure this file exists to catch is a step list that LIES: an event on the
/// wrong ordinal (so the console paints step 2 green while step 3 runs), a phase left
/// "running" after an exception, a final event posted after the result (which the hub then
/// refuses, so the last step reads "running" for ever), or an Abort that is acknowledged in
/// the console while the installer carries on. None of those fails a deploy, so none of
/// them would be noticed until an operator trusted the view.
/// </summary>
public class StepProgressTests
{
    // ------------------------------------------------------------------ executor events

    private sealed class Recorder : IStepProgress
    {
        private readonly object _gate = new();
        public List<StepEvent> Events { get; } = new();
        public Action<StepEvent>? OnReport { get; set; }
        public bool AbortRequested { get; set; }

        public void Report(StepEvent e)
        {
            lock (_gate) Events.Add(e);
            OnReport?.Invoke(e);
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

    private static DeployPackageExecutor NewExecutor() =>
        new(NullLogger<DeployPackageExecutor>.Instance, new UnusedDownloader());

    private static FleetCommand Stepped(params JsonObject[] steps) => new()
    {
        Id = "cmd-steps",
        Type = "deploy_package",
        Params = new JsonObject
        {
            ["package_name"] = "Stepped",
            ["source"] = new JsonObject { ["kind"] = "multi" },
            ["sources"] = new JsonArray(),
            ["steps"] = new JsonArray(steps.Select(s => (JsonNode)s).ToArray()),
            ["install_command"] = "",
            ["success_exit_codes"] = new JsonArray(0),
            ["detection"] = new JsonObject { ["kind"] = "none" },
            ["timeout_seconds"] = 120,
        },
    };

    private static JsonObject Run(string args, bool carryOn = false) => new()
    {
        ["kind"] = "run", ["command"] = "cmd.exe", ["args"] = args,
        ["continue_on_error"] = carryOn,
    };

    [Fact]
    public async Task EveryPhaseIsReportedInOrderWithItsOrdinal()
    {
        var progress = new Recorder();
        var result = await NewExecutor().ExecuteAsync(
            Stepped(Run("/c echo one"), Run("/c echo two")), null, progress,
            CancellationToken.None);

        Assert.True(result.Success);
        Assert.Equal(new[]
        {
            "step1:running", "step1:ok", "step2:running", "step2:ok",
            "detect1:running", "detect1:ok",
        }, progress.Trace());
        // A successful step still says how it ended: the console shows the exit code.
        Assert.Equal(0, progress.Events[1].ExitCode);
    }

    [Fact]
    public async Task AFailedStepIsClosedWithItsExitCodeAndNothingAfterItStarts()
    {
        var progress = new Recorder();
        var result = await NewExecutor().ExecuteAsync(
            Stepped(Run("/c exit 5"), Run("/c echo never")), null, progress,
            CancellationToken.None);

        Assert.False(result.Success);
        Assert.Equal(new[] { "step1:running", "step1:failed" }, progress.Trace());
        Assert.Equal(5, progress.Events[1].ExitCode);
        Assert.Contains("exit code 5", progress.Events[1].Message);
    }

    [Fact]
    public async Task AStepThatFailsButCarriesOnIsSkippedNotFailed()
    {
        var progress = new Recorder();
        var result = await NewExecutor().ExecuteAsync(
            Stepped(Run("/c exit 7", carryOn: true), Run("/c echo two")), null, progress,
            CancellationToken.None);

        Assert.True(result.Success);
        Assert.Equal("step1:skipped", progress.Trace()[1]);
        Assert.Equal("step2:ok", progress.Trace()[3]);
    }

    [Fact]
    public async Task ThePreStepsCommandLineIsStepOneOfOne()
    {
        // Every package written before steps runs through this path, and it must get the
        // same live view rather than a blank one.
        var cmd = new FleetCommand
        {
            Id = "cmd-legacy",
            Type = "deploy_package",
            Params = new JsonObject
            {
                ["package_name"] = "Legacy",
                ["source"] = new JsonObject { ["kind"] = "command" },
                ["install_command"] = "cmd.exe",
                ["install_args"] = "/c echo hi",
                ["success_exit_codes"] = new JsonArray(0),
                ["detection"] = new JsonObject { ["kind"] = "none" },
                ["timeout_seconds"] = 60,
            },
        };
        var progress = new Recorder();
        var result = await NewExecutor().ExecuteAsync(cmd, null, progress, CancellationToken.None);

        Assert.True(result.Success);
        Assert.Equal(new[] { "step1:running", "step1:ok", "detect1:running", "detect1:ok" },
                     progress.Trace());
    }

    [Fact]
    public async Task AnAbortStopsTheRunningStepAndNothingAfterIt()
    {
        // What the reporter does when the hub answers `cancel`: flag it, cancel the token
        // the executor runs under. A thirty-second step must end in seconds, as aborted.
        using var cts = new CancellationTokenSource();
        var progress = new Recorder();
        progress.OnReport = e =>
        {
            if (e is { Phase: StepPhase.Step, Ordinal: 1, State: StepState.Running })
            {
                progress.AbortRequested = true;
                cts.CancelAfter(500);
            }
        };

        var watch = System.Diagnostics.Stopwatch.StartNew();
        var result = await NewExecutor().ExecuteAsync(
            Stepped(Run("/c ping -n 30 127.0.0.1 >nul"), Run("/c echo never")), null,
            progress, cts.Token);
        watch.Stop();

        Assert.False(result.Success);
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(20), $"took {watch.Elapsed}");
        Assert.Equal(new[] { "step1:running", "step1:aborted" }, progress.Trace());
        Assert.Contains("ABORTED from the console", result.Output);
        Assert.DoesNotContain("step 2/2", result.Output);
    }

    [Fact]
    public async Task NoProgressSinkIsFineAndChangesNothing()
    {
        // Every executor call that predates 3.53.0 passes no sink; the deploy must run the
        // same, and the three-argument overload must still be the same run.
        var result = await NewExecutor().ExecuteAsync(
            Stepped(Run("/c echo one")), null, CancellationToken.None);
        Assert.True(result.Success);
    }

    // ------------------------------------------------------------------ the reporter

    private sealed class FakeProgressSink : IProgressSink
    {
        private readonly object _gate = new();
        public List<StepEvent> Posts { get; } = new();
        public Func<StepEvent, ProgressPostResult>? Respond { get; set; }

        public Task<ProgressPostResult> PostProgressAsync(string commandId, StepEvent e,
                                                          CancellationToken ct)
        {
            lock (_gate)
            {
                Posts.Add(e);
                return Task.FromResult(Respond?.Invoke(e)
                                       ?? new ProgressPostResult(Ok: true, Cancel: false, Gone: false));
            }
        }

        public StepEvent[] Snapshot() { lock (_gate) return Posts.ToArray(); }
    }

    private static ProgressReporter NewReporter(IProgressSink sink, int keepaliveMillis = 60_000,
                                                Func<int?>? mark = null) =>
        new(sink, "cmd-1", mark ?? (() => 7), CancellationToken.None, NullLogger.Instance,
            keepaliveMillis);

    [Fact]
    public async Task EventsArePostedInOrderAndAllBeforeCompleteReturns()
    {
        var sink = new FakeProgressSink();
        await using var reporter = NewReporter(sink);
        reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Running));
        reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Ok, 0));
        reporter.Report(new StepEvent(StepPhase.Step, 2, StepState.Running));
        reporter.Report(new StepEvent(StepPhase.Step, 2, StepState.Ok, 0));
        await reporter.CompleteAsync(CancellationToken.None);

        // The point of the ordering: Worker posts the result right after this, and the
        // hub refuses progress for a finished command. Anything still queued here is lost.
        Assert.Equal(new[] { "step1:running", "step1:ok", "step2:running", "step2:ok" },
                     sink.Snapshot().Select(e => $"{e.Phase}{e.Ordinal}:{e.State}"));
        // Every event carries the output stream's chunk number of its moment.
        Assert.All(sink.Snapshot(), e => Assert.Equal(7, e.OutSeq));
    }

    [Fact]
    public async Task ACancelAnswerCancelsTheTokenTheExecutorRunsUnder()
    {
        var sink = new FakeProgressSink
        {
            Respond = _ => new ProgressPostResult(Ok: true, Cancel: true, Gone: false),
        };
        await using var reporter = NewReporter(sink);
        Assert.False(reporter.Token.IsCancellationRequested);

        reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Running));
        await WaitFor(() => reporter.Token.IsCancellationRequested);

        Assert.True(reporter.AbortRequested);
    }

    [Fact]
    public async Task TheRunningEventIsRepeatedSoALongStepCanStillHearAnAbort()
    {
        var sink = new FakeProgressSink();
        await using var reporter = NewReporter(sink, keepaliveMillis: 100);
        reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Running));

        // No further Report() at all -- an installer that runs for an hour.
        await WaitFor(() => sink.Snapshot().Length >= 3);
        Assert.All(sink.Snapshot(), e => Assert.Equal(StepState.Running, e.State));
    }

    [Fact]
    public async Task AFinishedPhaseIsNotKeptAlive()
    {
        var sink = new FakeProgressSink();
        await using var reporter = NewReporter(sink, keepaliveMillis: 100);
        reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Running));
        reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Ok));
        await Task.Delay(600);

        // Re-sending a `running` for a step already reported ok is harmless at the hub
        // (writes are monotonic), but it is traffic for nothing.
        Assert.Equal(2, sink.Snapshot().Length);
    }

    [Fact]
    public async Task GoneMeansQuietAgainstAHubThatPredatesProgress()
    {
        // A 404 from a hub with no progress endpoint, or a 403 for a finished command.
        var sink = new FakeProgressSink
        {
            Respond = _ => new ProgressPostResult(Ok: true, Cancel: false, Gone: true),
        };
        await using var reporter = NewReporter(sink);
        reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Running));
        await WaitFor(() => sink.Snapshot().Length == 1);
        reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Ok));
        reporter.Report(new StepEvent(StepPhase.Step, 2, StepState.Running));
        await reporter.CompleteAsync(CancellationToken.None);

        Assert.Single(sink.Snapshot());
        Assert.False(reporter.Token.IsCancellationRequested);
    }

    [Fact]
    public async Task AnUnreachableHubNeverReachesTheExecutor()
    {
        // Report() is called between steps: it must not throw, whatever the sink or the
        // stream does, and CompleteAsync must still return.
        var down = new FakeProgressSink { Respond = _ => throw new HttpRequestException("down") };
        await using (var reporter = NewReporter(down))
        {
            reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Running));
            await reporter.CompleteAsync(CancellationToken.None);
            Assert.Equal(AgentConfig.StreamPostRetries, down.Snapshot().Length);
        }

        await using (var reporter = NewReporter(new FakeProgressSink(),
                                                mark: () => throw new InvalidOperationException()))
        {
            reporter.Report(new StepEvent(StepPhase.Step, 1, StepState.Running));
            await reporter.CompleteAsync(CancellationToken.None);
        }
    }

    // ------------------------------------------------------------------ OutputStreamer.Mark

    private sealed class SeqSink : IOutputSink
    {
        private readonly object _gate = new();
        public List<(int Seq, string Text)> Posts { get; } = new();

        public Task<OutputPostResult> PostOutputAsync(string commandId, int seq, string text,
                                                      CancellationToken ct)
        {
            lock (_gate) Posts.Add((seq, text));
            return Task.FromResult(new OutputPostResult(Ok: true, Truncated: false));
        }
    }

    [Fact]
    public async Task MarkSplitsTheStreamExactlyAtTheStepBoundary()
    {
        // The hub shows one step's log by slicing the chunks between two marks. Text from
        // before the mark in a chunk at or after it -- the buffer simply not having been
        // flushed yet -- would put step 1's last lines at the top of step 2's log.
        var sink = new SeqSink();
        var streamer = new OutputStreamer(sink, "cmd-1", NullLogger.Instance);
        streamer.Add("before-1\n");
        streamer.Add("before-2\n");
        var boundary = streamer.Mark();
        streamer.Add("after\n");
        await streamer.CompleteAsync(CancellationToken.None);

        Assert.NotNull(boundary);
        var before = string.Concat(sink.Posts.Where(p => p.Seq < boundary).Select(p => p.Text));
        var after = string.Concat(sink.Posts.Where(p => p.Seq >= boundary).Select(p => p.Text));
        Assert.Equal("before-1\nbefore-2\n", before);
        Assert.Equal("after\n", after);
        // And the stream is still one ordered sequence with no holes.
        Assert.Equal(Enumerable.Range(0, sink.Posts.Count), sink.Posts.Select(p => p.Seq));
    }

    [Fact]
    public async Task AMarkOnAnEmptyBufferIsTheNextNumberAndPostsNothing()
    {
        var sink = new SeqSink();
        var streamer = new OutputStreamer(sink, "cmd-1", NullLogger.Instance);
        Assert.Equal(0, streamer.Mark());
        Assert.Equal(0, streamer.Mark());
        await streamer.CompleteAsync(CancellationToken.None);
        Assert.Empty(sink.Posts);
    }

    private static async Task WaitFor(Func<bool> condition, int timeoutMillis = 5000)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMillis);
        while (!condition())
        {
            if (DateTime.UtcNow > deadline) throw new TimeoutException("condition never held");
            await Task.Delay(20);
        }
    }
}
