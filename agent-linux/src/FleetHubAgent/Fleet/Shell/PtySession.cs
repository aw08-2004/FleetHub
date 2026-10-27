using System.Text;
using Microsoft.Extensions.Logging;
using FleetHub.Shared.Pty;

namespace FleetHubAgent.Fleet.Shell;

/// <summary>
/// One live interactive terminal on this machine: a login shell on a pseudoterminal (see
/// <see cref="UnixPty"/>), its output pumped to a callback and the operator's keystrokes
/// written back in. The Windows agent's PtySession, with ConPTY swapped for a Unix pty.
///
/// This is a STREAM, not a sequence of commands -- no submission framing, no exit code to
/// report, no cwd to track. `run_script` still exists for favorites and automation, where a
/// discrete result is the point; this is for an operator driving a shell.
///
/// What we deliberately do NOT do, for the reasons the Windows copy gives:
///  * interpret the bytes. Ctrl-C is 0x03 written to the master; the tty line discipline
///    turns it into SIGINT for the foreground job, exactly as a physical terminal would.
///  * buffer lines or add newlines. Enter is the '\r' the operator typed; the line
///    discipline maps it. "Helpfully" appending '\n' would double every Enter.
///  * echo. The tty echoes, so the browser's xterm must not.
/// </summary>
public sealed class PtySession : IPtySession
{
    private readonly UnixPty _pty;
    private readonly ILogger _log;
    private readonly Thread _pump;
    private readonly CancellationTokenSource _stop = new();
    private readonly object _writeGate = new();
    private volatile bool _disposed;
    private volatile bool _streamEnded;

    /// <summary>How long one read waits before re-checking whether we were disposed. See
    /// UnixPty.Read on why the pump never blocks indefinitely.</summary>
    private const int ReadPollMillis = 200;

    public string SessionId { get; }

    /// <summary>"bash" or "sh" -- what actually runs, which may differ from what was asked
    /// for (see <see cref="ResolveShell"/>).</summary>
    public string Shell { get; }

    /// <summary>Set once the shell has been reaped or the terminal stream has ended.</summary>
    public bool Exited => _disposed || _streamEnded || _pty.HasExited;

    public DateTime LastActivityUtc { get; private set; } = DateTime.UtcNow;

    public int? ExitCode => _pty.ExitCode;

    private PtySession(string sessionId, string shell, UnixPty pty, Action<string> onOutput, ILogger log)
    {
        SessionId = sessionId;
        Shell = shell;
        _pty = pty;
        _log = log;
        // A dedicated thread rather than the pool: it spends its life in poll(), and parking a
        // pool thread per open terminal would starve the heartbeat and command loops of the
        // threads they run on.
        _pump = new Thread(() => Pump(onOutput)) { IsBackground = true, Name = $"pty-{sessionId[..Math.Min(8, sessionId.Length)]}" };
        _pump.Start();
    }

    /// <summary>Start a login shell on a fresh pseudoterminal. <paramref name="onOutput"/> is
    /// called from the read pump with decoded text -- it must not block or throw.</summary>
    public static IPtySession Start(
        string sessionId, string shell, short cols, short rows, Action<string> onOutput, ILogger log)
    {
        var (path, name) = ResolveShell(shell);
        // `-l`: a login shell, so the operator gets root's profile, PATH and prompt -- the
        // same reason the Windows agent drops -NoProfile for its interactive PowerShell.
        var pty = UnixPty.Start([path, "-l"], "/root", ShellEnvironment(path), cols, rows);
        log.LogInformation("Opened {Shell} pty session {Id} (pid {Pid})", name, sessionId, pty.ProcessId);
        return new PtySession(sessionId, name, pty, onOutput, log);
    }

    /// <summary>Which shell to run for the name the hub sent. The console offers bash and sh
    /// for a Linux machine, but a terminal opened from an older console -- or one whose shell
    /// box still said PowerShell -- arrives as "powershell", and refusing it would be a
    /// terminal that never opens over a name. Anything that is not "sh" gets bash where it
    /// exists, the same substitution RunScriptExecutor makes, and the terminal's own prompt
    /// tells the operator which one they got.</summary>
    internal static (string Path, string Name) ResolveShell(string? requested)
    {
        if ((requested ?? "").Trim().ToLowerInvariant() == "sh") return ("/bin/sh", "sh");
        var path = Executors.RunScriptExecutor.BashOrSh();
        return (path, path.EndsWith("/sh", StringComparison.Ordinal) ? "sh" : "bash");
    }

    /// <summary>The environment the shell starts with: the agent's own, minus its FLEETHUB_*
    /// settings, plus what a terminal needs.
    ///
    /// The agent's settings are dropped because this shell's output is kept: the hub holds a
    /// replay buffer of every terminal, so an `env` typed out of curiosity would copy the hub
    /// address and enrollment settings into it. root can still read them from disk; the point
    /// is not to scatter them.
    ///
    /// TERM is what makes colour, cursor movement and full-screen programs work -- systemd
    /// starts the service with no TERM at all, and most programs fall back to "dumb".</summary>
    internal static Dictionary<string, string> ShellEnvironment(string shellPath)
    {
        var env = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (System.Collections.DictionaryEntry entry in Environment.GetEnvironmentVariables())
        {
            var key = entry.Key?.ToString();
            if (string.IsNullOrEmpty(key) || key.StartsWith("FLEETHUB_", StringComparison.Ordinal)) continue;
            env[key] = entry.Value?.ToString() ?? "";
        }
        env["TERM"] = "xterm-256color";
        env["SHELL"] = shellPath;
        env.TryAdd("HOME", "/root");
        env.TryAdd("USER", "root");
        env.TryAdd("LOGNAME", "root");
        env.TryAdd("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin");
        // C.UTF-8 rather than nothing: without a UTF-8 locale, programs print anything outside
        // ASCII as '?', and the browser's terminal is UTF-8 end to end.
        env.TryAdd("LANG", "C.UTF-8");
        return env;
    }

    /// <summary>Write operator input verbatim -- no trimming, no added newline.</summary>
    public void Write(string data)
    {
        if (_disposed || string.IsNullOrEmpty(data)) return;
        var bytes = Encoding.UTF8.GetBytes(data);
        try
        {
            lock (_writeGate) _pty.Write(bytes);
            LastActivityUtc = DateTime.UtcNow;
        }
        catch (Exception e)
        {
            _log.LogDebug(e, "pty {Id}: write failed", SessionId);
        }
    }

    public void Resize(short cols, short rows)
    {
        if (_disposed) return;
        _pty.Resize(cols, rows);
    }

    private void Pump(Action<string> onOutput)
    {
        var buffer = new byte[8192];
        // Stateful, for the reason the Windows copy gives: a UTF-8 sequence can straddle two
        // reads, and decoding each read on its own would print replacement characters.
        var decoder = Encoding.UTF8.GetDecoder();
        var chars = new char[buffer.Length + 8];

        while (!_stop.IsCancellationRequested)
        {
            int read;
            try { read = _pty.Read(buffer, ReadPollMillis); }
            catch (Exception e)
            {
                _log.LogDebug(e, "pty {Id}: read ended", SessionId);
                break;
            }
            if (read < 0) break;   // EIO: the last process holding the terminal is gone
            if (read == 0) continue;

            var n = decoder.GetChars(buffer, 0, read, chars, 0);
            if (n == 0) continue;
            LastActivityUtc = DateTime.UtcNow;
            try { onOutput(new string(chars, 0, n)); }
            catch (Exception e) { _log.LogDebug(e, "pty {Id}: output sink threw", SessionId); }
        }

        _streamEnded = true;
        _log.LogInformation("pty session {Id} output stream ended", SessionId);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _stop.Cancel();
        // Stop the pump BEFORE the pty closes its master fd. The pump holds the fd NUMBER, and
        // once it is closed the kernel may hand that number to the next file anyone opens --
        // a read still in flight would then consume bytes from somebody else's file. The
        // pump re-checks the token at least every ReadPollMillis, so this wait is short.
        _pump.Join(TimeSpan.FromSeconds(2));
        try { _pty.Dispose(); } catch { /* tearing down anyway */ }
        _stop.Dispose();
    }
}
