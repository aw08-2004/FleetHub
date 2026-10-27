using System.Collections.Concurrent;
using System.Text;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;
using FleetHubAgent.Fleet.Shell;

namespace FleetHubAgent.Tests;

/// <summary>
/// The interactive terminal (0.4.0), in two halves.
///
/// <see cref="PtySessionRunnerTests"/> drive the session loop against a fake hub AND a fake
/// terminal, so they run on the Windows box this agent is usually built on. They catch the
/// quiet bugs -- a cursor advanced on an empty batch, a seq reused or skipped, a session that
/// never notices it was closed -- every one of which looks like "the terminal stopped
/// responding" rather than like a crash.
///
/// <see cref="UnixPtyTests"/> open a real pseudoterminal and therefore only run on Linux. They
/// catch the failure the fake cannot: a shell that starts but has no CONTROLLING terminal, which
/// looks fine until Ctrl-C does nothing and bash prints "no job control in this shell". That is
/// the one property the posix_spawn ordering in UnixPty exists to get right.
/// </summary>
public class PtySessionRunnerTests
{
    /// <summary>A hub that behaves like the real one: hands out queued input once, tracks the
    /// agent's cursor, records the stream it is posted.</summary>
    internal sealed class FakeHub : IPtyChannel
    {
        private readonly ConcurrentQueue<PtyInputItem> _queued = new();
        private readonly StringBuilder _output = new();
        private readonly object _gate = new();
        private int _seq;

        public volatile bool Closing;
        public volatile bool Gone;
        public volatile bool Reachable = true;
        public string? ClosedReason;
        public readonly ConcurrentBag<int> PostedSeqs = new();
        public readonly ConcurrentQueue<int> PulledAfter = new();

        public string Output { get { lock (_gate) return _output.ToString(); } }

        public void Type(string data) => _queued.Enqueue(new PtyInputItem(_seq++, "data", data, 0, 0));
        public void Resize(short cols, short rows) => _queued.Enqueue(new PtyInputItem(_seq++, "resize", "", cols, rows));

        public Task<PtyInputBatch> PullPtyInputAsync(string sessionId, int afterSeq, CancellationToken ct)
        {
            PulledAfter.Enqueue(afterSeq);
            if (Gone)
                return Task.FromResult(new PtyInputBatch(true, Array.Empty<PtyInputItem>(), afterSeq, true, true));
            if (!Reachable)
                return Task.FromResult(new PtyInputBatch(false, Array.Empty<PtyInputItem>(), afterSeq, false, false));

            var items = new List<PtyInputItem>();
            while (_queued.TryDequeue(out var item)) items.Add(item);
            var next = items.Count > 0 ? items[^1].Seq + 1 : afterSeq + 1;
            return Task.FromResult(new PtyInputBatch(true, items, next, Closing, false));
        }

        public Task<bool> PostPtyOutputAsync(string sessionId, int seq, string chunk, CancellationToken ct)
        {
            if (!Reachable) return Task.FromResult(false);
            PostedSeqs.Add(seq);
            lock (_gate) _output.Append(chunk);
            return Task.FromResult(true);
        }

        public Task ReportPtyClosedAsync(string sessionId, string reason, CancellationToken ct)
        {
            ClosedReason = reason;
            return Task.CompletedTask;
        }

        public async Task<bool> WaitForOutput(string needle, int timeoutMs = 20_000)
        {
            var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            while (DateTime.UtcNow < deadline)
            {
                if (Output.Contains(needle)) return true;
                await Task.Delay(25);
            }
            return Output.Contains(needle);
        }
    }

    /// <summary>A terminal that echoes what it is written, answers "exit\r" by ending, and
    /// records resizes -- enough of a tty to exercise the runner's bookkeeping.</summary>
    private sealed class FakeTerminal(Action<string> onOutput) : IPtySession
    {
        public volatile bool Ended;
        public volatile bool Disposed;
        public (short Cols, short Rows) Size;
        public bool Exited => Ended || Disposed;
        public DateTime LastActivityUtc { get; private set; } = DateTime.UtcNow;

        public void Write(string data)
        {
            LastActivityUtc = DateTime.UtcNow;
            if (data == "exit\r") { Ended = true; return; }
            onOutput(data);
        }

        public void Resize(short cols, short rows) => Size = (cols, rows);
        public void Dispose() => Disposed = true;
    }

    private static (PtySessionRunner Runner, Func<FakeTerminal?> Terminal) NewRunner(FakeHub hub)
    {
        FakeTerminal? terminal = null;
        var runner = new PtySessionRunner(Guid.NewGuid().ToString("N"), hub, NullLogger.Instance,
            (id, shell, cols, rows, onOutput, log) =>
            {
                terminal = new FakeTerminal(onOutput) { Size = (cols, rows) };
                onOutput("$ ");
                return terminal;
            });
        return (runner, () => terminal);
    }

    [Fact]
    public async Task TypedInput_ReachesTheTerminalAndItsOutputReachesTheHub()
    {
        var hub = new FakeHub();
        var (runner, _) = NewRunner(hub);
        using var _r = runner;
        var run = runner.RunAsync("bash", 100, 30, CancellationToken.None);

        Assert.True(await hub.WaitForOutput("$ "));
        hub.Type("echo round-trip-ok\r");
        Assert.True(await hub.WaitForOutput("round-trip-ok"), $"got: {hub.Output}");

        hub.Closing = true;
        Assert.Equal("closed by the operator", await run.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("closed by the operator", hub.ClosedReason);
    }

    [Fact]
    public async Task Cursor_AdvancesOnlyWhenInputArrives()
    {
        // The agent-side half of the seq-rewind bug tests/test_terminal.py guards: advancing
        // the cursor on an EMPTY batch would push it past input not yet queued, and the
        // operator's next keystrokes would never be fetched.
        var hub = new FakeHub();
        var (runner, _) = NewRunner(hub);
        using var _r = runner;
        var run = runner.RunAsync("bash", 100, 30, CancellationToken.None);

        hub.Type("first\r");
        Assert.True(await hub.WaitForOutput("first"));
        await Task.Delay(500);                   // several polls with nothing queued
        hub.Type("second\r");
        Assert.True(await hub.WaitForOutput("second"), $"got: {hub.Output}");

        hub.Closing = true;
        await run.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.All(hub.PulledAfter, after => Assert.InRange(after, -1, 1));
    }

    [Fact]
    public async Task OutputSequence_IsGaplessAndMonotonic()
    {
        var hub = new FakeHub();
        var (runner, _) = NewRunner(hub);
        using var _r = runner;
        var run = runner.RunAsync("bash", 100, 30, CancellationToken.None);
        for (var i = 0; i < 5; i++)
        {
            hub.Type($"line{i}\r");
            Assert.True(await hub.WaitForOutput($"line{i}"));
        }

        hub.Closing = true;
        await run.WaitAsync(TimeSpan.FromSeconds(10));

        var seqs = hub.PostedSeqs.OrderBy(x => x).ToList();
        Assert.Equal(Enumerable.Range(0, seqs.Count), seqs);
    }

    [Fact]
    public async Task ShellExiting_EndsTheSession()
    {
        var hub = new FakeHub();
        var (runner, _) = NewRunner(hub);
        using var _r = runner;
        var run = runner.RunAsync("bash", 100, 30, CancellationToken.None);
        Assert.True(await hub.WaitForOutput("$ "));

        hub.Type("exit\r");

        Assert.Equal("the shell exited", await run.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("the shell exited", hub.ClosedReason);
    }

    [Fact]
    public async Task HubForgettingTheSession_EndsIt()
    {
        var hub = new FakeHub();
        var (runner, terminal) = NewRunner(hub);
        using var _r = runner;
        var run = runner.RunAsync("bash", 100, 30, CancellationToken.None);
        Assert.True(await hub.WaitForOutput("$ "));

        hub.Gone = true;

        Assert.Equal("the hub no longer has this session", await run.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.True(terminal()!.Disposed, "the shell must be hung up, not left running");
    }

    [Fact]
    public async Task Cancellation_TearsTheSessionDownAndSaysWhy()
    {
        var hub = new FakeHub();
        using var cts = new CancellationTokenSource();
        var (runner, _) = NewRunner(hub);
        using var _r = runner;
        var run = runner.RunAsync("bash", 100, 30, cts.Token);
        Assert.True(await hub.WaitForOutput("$ "));

        cts.Cancel();   // a systemd stop or a self-update

        Assert.Equal("the agent is shutting down", await run.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("the agent is shutting down", hub.ClosedReason);
    }

    [Fact]
    public async Task Resize_IsForwardedToTheTerminal()
    {
        var hub = new FakeHub();
        var (runner, terminal) = NewRunner(hub);
        using var _r = runner;
        var run = runner.RunAsync("bash", 80, 24, CancellationToken.None);
        Assert.True(await hub.WaitForOutput("$ "));

        hub.Resize(137, 41);
        hub.Type("after-resize\r");
        Assert.True(await hub.WaitForOutput("after-resize"));
        Assert.Equal(((short)137, (short)41), terminal()!.Size);

        hub.Closing = true;
        await run.WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task ATerminalThatCannotOpen_IsReportedToTheHub()
    {
        // The console polls the SESSION, not the command -- a failure reported only as the
        // command's result would look like a terminal that opened and then said nothing.
        var hub = new FakeHub();
        using var runner = new PtySessionRunner("s1", hub, NullLogger.Instance,
            (id, shell, cols, rows, onOutput, log) => throw new IOException("openpty failed"));

        var reason = await runner.RunAsync("bash", 80, 24, CancellationToken.None);

        Assert.StartsWith("could not open a terminal", reason);
        Assert.Equal(reason, hub.ClosedReason);
    }

    [Fact]
    public void ShellOpen_IsTheHubsCommandType()
    {
        // The capability report is derived from executor Types, and the console switches a
        // Linux machine to the real terminal on seeing exactly "shell_open" in it. A typo
        // here would leave every Linux box on the one-shot box with nothing failing anywhere.
        var executor = new ShellOpenExecutor(
            new PtySessionManager(NullLogger<PtySessionManager>.Instance),
            new FakeHub(), NullLogger<ShellOpenExecutor>.Instance);
        Assert.Equal("shell_open", executor.Type);
    }

    [Theory]
    [InlineData("sh", "sh")]
    [InlineData("SH", "sh")]
    public void ResolveShell_HonoursSh(string requested, string expected)
    {
        Assert.Equal(expected, PtySession.ResolveShell(requested).Name);
    }

    [Theory]
    [InlineData("bash")]
    [InlineData("powershell")]   // an older console, or a shell box still on its Windows default
    [InlineData("")]
    [InlineData(null)]
    public void ResolveShell_GivesEverythingElseBashOrSh(string? requested)
    {
        // Never refused: a terminal that will not open over a shell NAME is worse than one
        // that opens in bash and shows its own prompt.
        Assert.Contains(PtySession.ResolveShell(requested).Name, new[] { "bash", "sh" });
    }

    [Fact]
    public void ShellEnvironment_DropsAgentSettingsAndSetsTerm()
    {
        // The hub keeps a replay buffer of every terminal, so an `env` typed in the shell
        // would otherwise copy the agent's settings into it.
        Environment.SetEnvironmentVariable("FLEETHUB_TEST_SECRET", "x");
        try
        {
            var env = PtySession.ShellEnvironment("/bin/bash");
            Assert.False(env.ContainsKey("FLEETHUB_TEST_SECRET"));
            Assert.Equal("xterm-256color", env["TERM"]);
            Assert.Equal("/bin/bash", env["SHELL"]);
        }
        finally
        {
            Environment.SetEnvironmentVariable("FLEETHUB_TEST_SECRET", null);
        }
    }
}

public class UnixPtyTests
{
    private static async Task<(PtySessionRunnerTests.FakeHub Hub, PtySessionRunner Runner, Task<string> Run)> Open()
    {
        var hub = new PtySessionRunnerTests.FakeHub();
        var runner = new PtySessionRunner(Guid.NewGuid().ToString("N"), hub, NullLogger.Instance);
        var run = runner.RunAsync("bash", 100, 30, CancellationToken.None);
        // A marker of our own rather than waiting for a prompt: root's PS1 differs per distro.
        hub.Type("echo READY-$((40+2))\r");
        Assert.True(await hub.WaitForOutput("READY-42"), $"shell never answered; got: {hub.Output}");
        return (hub, runner, run);
    }

    [Fact]
    public async Task Shell_HasAControllingTerminal()
    {
        if (!OperatingSystem.IsLinux()) return;
        var (hub, runner, run) = await Open();
        using var _r = runner;

        // `tty` prints the device only on a real terminal, and `ps -o tty=` of the shell names
        // its CONTROLLING terminal -- "?" here is the bug: Ctrl-C would do nothing.
        hub.Type("echo CTTY=$(ps -o tty= -p $$ | tr -d ' ')\r");
        Assert.True(await hub.WaitForOutput("CTTY=pts/"), $"no controlling tty; got: {hub.Output}");

        hub.Closing = true;
        await run.WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task CtrlC_InterruptsTheForegroundJob()
    {
        if (!OperatingSystem.IsLinux()) return;
        var (hub, runner, run) = await Open();
        using var _r = runner;

        // Arithmetic markers, so what the shell PRINTS differs from what the tty ECHOES:
        // "SLEPT-5" can only appear if the sleep ran to completion.
        hub.Type("sleep 30; echo SLEPT-$((2+3))\r");
        await Task.Delay(500);
        hub.Type("\x03");
        hub.Type("echo AFTER-$((1+1))\r");

        Assert.True(await hub.WaitForOutput("AFTER-2", 5000), $"Ctrl-C did not interrupt; got: {hub.Output}");
        Assert.DoesNotContain("SLEPT-5", hub.Output);

        hub.Closing = true;
        await run.WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task Resize_ReachesTheTerminal()
    {
        if (!OperatingSystem.IsLinux()) return;
        var (hub, runner, run) = await Open();
        using var _r = runner;

        hub.Resize(137, 41);
        await Task.Delay(300);
        hub.Type("echo SIZE=$(stty size)\r");
        Assert.True(await hub.WaitForOutput("SIZE=41 137"), $"got: {hub.Output}");

        hub.Closing = true;
        await run.WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task Exit_EndsTheSession()
    {
        if (!OperatingSystem.IsLinux()) return;
        var (hub, runner, run) = await Open();
        using var _r = runner;

        hub.Type("exit\r");

        Assert.Equal("the shell exited", await run.WaitAsync(TimeSpan.FromSeconds(10)));
    }

    [Fact]
    public async Task BrokenPipe_IsQuiet()
    {
        // The runtime ignores SIGPIPE and an ignored disposition survives exec. Without the
        // reset in UnixPty.Spawn, `yes` here would never die and print write errors instead.
        if (!OperatingSystem.IsLinux()) return;
        var (hub, runner, run) = await Open();
        using var _r = runner;

        hub.Type("yes | head -1 >/dev/null; echo PIPE=${PIPESTATUS[0]}\r");
        Assert.True(await hub.WaitForOutput("PIPE=141", 5000), $"got: {hub.Output}");

        hub.Closing = true;
        await run.WaitAsync(TimeSpan.FromSeconds(10));
    }
}
