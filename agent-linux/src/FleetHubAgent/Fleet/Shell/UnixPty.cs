using System.Runtime.InteropServices;
using System.Text;

namespace FleetHubAgent.Fleet.Shell;

/// <summary>
/// A shell attached to a Unix pseudoterminal -- the Linux counterpart of the Windows agent's
/// ConPtyProcess, and the reason this agent can finally offer a real terminal instead of the
/// one-shot `run_script` box (where `top` printed one frame, `sudo -v` hung, and Ctrl-C did
/// nothing because there was no terminal on the other end to press it AT).
///
/// **How the child is started, and why it is not the obvious way.** The textbook recipe is
/// forkpty(): fork, make the slave the controlling terminal, exec. Calling fork() from managed
/// code is unsafe -- the child is a copy of a multithreaded runtime that runs managed code
/// (GC, JIT, locks held by threads that no longer exist) before it reaches exec -- so it is
/// not done. Instead:
///
///   1. openpty() in this process gives us the master and slave ends;
///   2. posix_spawn() starts the shell with POSIX_SPAWN_SETSID, so the child begins life as a
///      session leader with no controlling terminal;
///   3. its file actions then OPEN the slave by path onto fd 0 (no O_NOCTTY). On Linux a
///      session leader without a controlling terminal acquires the first tty it opens, so the
///      slave becomes the shell's controlling terminal -- which is what gives job control,
///      Ctrl-C as a real SIGINT to the foreground job, and SIGWINCH on resize. glibc applies
///      the setsid attribute before the file actions, which is what makes this ordering work.
///   4. fds 1 and 2 are dup2'd from 0.
///
/// posix_spawn is exactly the primitive .NET's own Process.Start avoids reimplementing in
/// managed code for the same reason, and it runs no managed code in the child at all.
///
/// Rejected alternatives, kept so nobody re-tries them:
///   * `script -qfc bash /dev/null` with redirected pipes: no fork from us, but `script`
///     owns the pty, so the window can never be resized and every full-screen program draws
///     at 80x24 forever.
///   * a `setsid --ctty` helper from util-linux: works, and remains the fallback if a libc
///     ever applies file actions before setsid -- but it adds a process and a dependency the
///     open-by-path trick does not need.
///   * Process.Start with redirected stdio: pipes, not a tty. Exactly the old terminal.
///
/// **Reaping.** The .NET runtime installs a SIGCHLD handler, but it only waitpid()s children
/// it started through Process -- it never reaps with -1 unless SIGCHLD was ignored at startup
/// -- so our own waitpid(pid, WNOHANG) is the only thing that collects this child, and it
/// will not be stolen. HasExited polls it.
///
/// Signal dispositions are RESET in the child (POSIX_SPAWN_SETSIGDEF over every signal, empty
/// mask). The runtime ignores SIGPIPE, and an ignored disposition survives exec: without the
/// reset, `yes | head -1` in the operator's shell would print "Broken pipe" errors instead of
/// stopping quietly, and other programs would misbehave in ways nobody would trace to us.
///
/// Constants below are x86_64/aarch64 Linux glibc values. This agent ships linux-x64 only.
/// </summary>
internal sealed class UnixPty : IDisposable
{
    private const string Libc = "libc.so.6";

    private const int O_RDWR = 2;
    private const int F_SETFD = 2;
    private const int FD_CLOEXEC = 1;
    private const ulong TIOCSWINSZ = 0x5414;
    private const int WNOHANG = 1;
    private const int SIGHUP = 1;
    private const int SIGKILL = 9;
    private const short POSIX_SPAWN_SETSIGDEF = 0x04;
    private const short POSIX_SPAWN_SETSIGMASK = 0x08;
    private const short POSIX_SPAWN_SETSID = 0x80;
    private const short POLLIN = 0x001;
    private const int EINTR = 4;
    private const int EAGAIN = 11;

    // Opaque glibc types are allocated generously rather than at their exact sizes (80, 336
    // and 128 bytes on x86_64): too large costs a few hundred bytes once per terminal, too
    // small is heap corruption.
    private const int SpawnFileActionsBytes = 512;
    private const int SpawnAttrBytes = 1024;
    private const int SigsetBytes = 256;

    [StructLayout(LayoutKind.Sequential)]
    private struct WinSize
    {
        public ushort Rows;
        public ushort Cols;
        public ushort XPixel;
        public ushort YPixel;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PollFd
    {
        public int Fd;
        public short Events;
        public short REvents;
    }

    // openpty moved from libutil into libc in glibc 2.34; Debian 12+ and Ubuntu 22.04+ have
    // it in libc, older ones only in libutil. Both are declared and tried in that order.
    [DllImport(Libc, EntryPoint = "openpty", SetLastError = true)]
    private static extern int openpty_libc(out int master, out int slave, IntPtr name, IntPtr termp, ref WinSize winp);

    [DllImport("libutil.so.1", EntryPoint = "openpty", SetLastError = true)]
    private static extern int openpty_libutil(out int master, out int slave, IntPtr name, IntPtr termp, ref WinSize winp);

    [DllImport(Libc, SetLastError = true)]
    private static extern int ptsname_r(int fd, byte[] buf, nuint buflen);

    [DllImport(Libc, SetLastError = true)]
    private static extern int fcntl(int fd, int cmd, int arg);

    [DllImport(Libc, SetLastError = true)]
    private static extern int ioctl(int fd, ulong request, ref WinSize ws);

    [DllImport(Libc, SetLastError = true)]
    private static extern int close(int fd);

    [DllImport(Libc, SetLastError = true)]
    private static extern nint read(int fd, byte[] buf, nint count);

    [DllImport(Libc, SetLastError = true)]
    private static extern nint write(int fd, byte[] buf, nint count);

    [DllImport(Libc, SetLastError = true)]
    private static extern int poll(ref PollFd fds, nuint nfds, int timeout);

    [DllImport(Libc, SetLastError = true)]
    private static extern int waitpid(int pid, out int status, int options);

    [DllImport(Libc, SetLastError = true)]
    private static extern int kill(int pid, int sig);

    [DllImport(Libc)]
    private static extern int posix_spawn_file_actions_init(IntPtr actions);

    [DllImport(Libc)]
    private static extern int posix_spawn_file_actions_destroy(IntPtr actions);

    [DllImport(Libc)]
    private static extern int posix_spawn_file_actions_addopen(IntPtr actions, int fd, string path, int oflag, int mode);

    [DllImport(Libc)]
    private static extern int posix_spawn_file_actions_adddup2(IntPtr actions, int fd, int newfd);

    // glibc 2.29+. Optional: without it the shell starts in the agent's own working
    // directory, which is still a usable terminal.
    [DllImport(Libc)]
    private static extern int posix_spawn_file_actions_addchdir_np(IntPtr actions, string path);

    [DllImport(Libc)]
    private static extern int posix_spawnattr_init(IntPtr attr);

    [DllImport(Libc)]
    private static extern int posix_spawnattr_destroy(IntPtr attr);

    [DllImport(Libc)]
    private static extern int posix_spawnattr_setflags(IntPtr attr, short flags);

    [DllImport(Libc)]
    private static extern int posix_spawnattr_setsigdefault(IntPtr attr, IntPtr sigset);

    [DllImport(Libc)]
    private static extern int posix_spawnattr_setsigmask(IntPtr attr, IntPtr sigset);

    [DllImport(Libc)]
    private static extern int sigfillset(IntPtr set);

    [DllImport(Libc)]
    private static extern int sigemptyset(IntPtr set);

    [DllImport(Libc)]
    private static extern int posix_spawn(out int pid, string path, IntPtr fileActions, IntPtr attr,
                                          string?[] argv, string?[] envp);

    private int _master;
    private readonly object _reapGate = new();
    private int? _exitCode;
    private bool _disposed;

    public int ProcessId { get; }

    private UnixPty(int master, int pid)
    {
        _master = master;
        ProcessId = pid;
    }

    /// <summary>Start <paramref name="argv"/>[0] (an absolute path) on a fresh pseudoterminal
    /// of the given size, in <paramref name="cwd"/>, with exactly <paramref name="env"/>.</summary>
    public static UnixPty Start(string[] argv, string cwd, IReadOnlyDictionary<string, string> env,
                                short cols, short rows)
    {
        var size = new WinSize { Cols = (ushort)cols, Rows = (ushort)rows };
        int master, slave;
        int rc;
        try { rc = openpty_libc(out master, out slave, IntPtr.Zero, IntPtr.Zero, ref size); }
        catch (EntryPointNotFoundException) { rc = openpty_libutil(out master, out slave, IntPtr.Zero, IntPtr.Zero, ref size); }
        if (rc != 0) throw new IOException($"openpty failed (errno {Marshal.GetLastPInvokeError()})");

        try
        {
            // Neither end may leak into anything else this agent spawns: a run_script child
            // holding a stray slave fd would keep this terminal "open" after its shell exits,
            // and one holding the master could read the operator's session.
            fcntl(master, F_SETFD, FD_CLOEXEC);
            fcntl(slave, F_SETFD, FD_CLOEXEC);

            var nameBuf = new byte[128];
            if (ptsname_r(master, nameBuf, (nuint)nameBuf.Length) != 0)
                throw new IOException($"ptsname_r failed (errno {Marshal.GetLastPInvokeError()})");
            var slavePath = Encoding.ASCII.GetString(nameBuf, 0, Array.IndexOf(nameBuf, (byte)0));

            var pid = Spawn(argv, cwd, env, slavePath);
            // The parent's copy of the slave is closed only AFTER the child has its own. Once
            // it is gone, the child is the last holder, so its exit shows up on the master as
            // EIO -- which is how the read pump learns the terminal is over.
            close(slave);
            slave = -1;
            return new UnixPty(master, pid);
        }
        catch
        {
            if (slave >= 0) close(slave);
            close(master);
            throw;
        }
    }

    private static int Spawn(string[] argv, string cwd, IReadOnlyDictionary<string, string> env, string slavePath)
    {
        var actions = Marshal.AllocHGlobal(SpawnFileActionsBytes);
        var attr = Marshal.AllocHGlobal(SpawnAttrBytes);
        var allSignals = Marshal.AllocHGlobal(SigsetBytes);
        var noSignals = Marshal.AllocHGlobal(SigsetBytes);
        var actionsReady = false;
        var attrReady = false;
        try
        {
            Check(posix_spawn_file_actions_init(actions), "posix_spawn_file_actions_init");
            actionsReady = true;
            Check(posix_spawnattr_init(attr), "posix_spawnattr_init");
            attrReady = true;

            Check(posix_spawn_file_actions_addopen(actions, 0, slavePath, O_RDWR, 0), "addopen");
            Check(posix_spawn_file_actions_adddup2(actions, 0, 1), "adddup2(1)");
            Check(posix_spawn_file_actions_adddup2(actions, 0, 2), "adddup2(2)");
            try { posix_spawn_file_actions_addchdir_np(actions, cwd); }
            catch (EntryPointNotFoundException) { /* older glibc: start in the agent's cwd */ }

            sigfillset(allSignals);
            sigemptyset(noSignals);
            Check(posix_spawnattr_setsigdefault(attr, allSignals), "setsigdefault");
            Check(posix_spawnattr_setsigmask(attr, noSignals), "setsigmask");
            Check(posix_spawnattr_setflags(attr,
                (short)(POSIX_SPAWN_SETSID | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK)), "setflags");

            var argvZ = new string?[argv.Length + 1];
            Array.Copy(argv, argvZ, argv.Length);
            var envZ = env.Select(kv => $"{kv.Key}={kv.Value}").Append(null).ToArray();

            Check(posix_spawn(out var pid, argv[0], actions, attr, argvZ, envZ), "posix_spawn");
            return pid;
        }
        finally
        {
            if (actionsReady) posix_spawn_file_actions_destroy(actions);
            if (attrReady) posix_spawnattr_destroy(attr);
            Marshal.FreeHGlobal(actions);
            Marshal.FreeHGlobal(attr);
            Marshal.FreeHGlobal(allSignals);
            Marshal.FreeHGlobal(noSignals);
        }
    }

    /// <summary>posix_spawn* return the error number directly rather than setting errno.</summary>
    private static void Check(int rc, string what)
    {
        if (rc != 0) throw new IOException($"{what} failed (error {rc})");
    }

    /// <summary>Wait up to <paramref name="timeoutMs"/> for output, then read what is there.
    /// Returns the byte count, 0 when nothing arrived in time, or -1 when the terminal is over
    /// (EIO once the last slave holder exits, or the fd was closed under us).
    ///
    /// Polled with a timeout rather than a bare blocking read on purpose: a process the shell
    /// started can daemonise into its own session and keep the slave open after the shell is
    /// gone, and a read thread blocked forever on such a terminal is a thread leaked per
    /// session. The timeout gives the caller a chance to notice it has been disposed.</summary>
    public int Read(byte[] buffer, int timeoutMs)
    {
        var fd = _master;
        if (fd < 0) return -1;
        var pfd = new PollFd { Fd = fd, Events = POLLIN };
        var ready = poll(ref pfd, 1, timeoutMs);
        if (ready < 0) return Marshal.GetLastPInvokeError() == EINTR ? 0 : -1;
        if (ready == 0) return 0;
        var n = read(fd, buffer, buffer.Length);
        if (n > 0) return (int)n;
        if (n < 0)
        {
            var errno = Marshal.GetLastPInvokeError();
            if (errno is EINTR or EAGAIN) return 0;
        }
        return -1;
    }

    /// <summary>Write all of <paramref name="data"/>, looping over short writes. A pty's input
    /// queue is small (a few KB), so a pasted script routinely takes several writes.</summary>
    public void Write(byte[] data)
    {
        var offset = 0;
        var chunk = data;
        while (offset < data.Length)
        {
            var fd = _master;
            if (fd < 0) throw new ObjectDisposedException(nameof(UnixPty));
            if (offset > 0) chunk = data[offset..];
            var n = write(fd, chunk, chunk.Length);
            if (n < 0)
            {
                var errno = Marshal.GetLastPInvokeError();
                if (errno is EINTR or EAGAIN) { Thread.Sleep(5); continue; }
                throw new IOException($"write to pty failed (errno {errno})");
            }
            offset += (int)n;
        }
    }

    /// <summary>TIOCSWINSZ on the master. The kernel delivers SIGWINCH to the terminal's
    /// foreground process group, so a running `top` or `vim` redraws at the new size.</summary>
    public void Resize(short cols, short rows)
    {
        var fd = _master;
        if (fd < 0) return;
        var size = new WinSize { Cols = (ushort)cols, Rows = (ushort)rows };
        ioctl(fd, TIOCSWINSZ, ref size);
    }

    /// <summary>True once the shell has been reaped. Non-blocking.</summary>
    public bool HasExited
    {
        get
        {
            lock (_reapGate)
            {
                if (_exitCode is not null) return true;
                var reaped = waitpid(ProcessId, out var status, WNOHANG);
                if (reaped == ProcessId)
                {
                    // WIFEXITED: low 7 bits zero, code in the next byte. Otherwise killed by a
                    // signal, reported shell-style as 128 + signal.
                    _exitCode = (status & 0x7f) == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f);
                    return true;
                }
                // -1 (ECHILD) means somebody else reaped it, which should not happen (see the
                // class summary) -- but treating it as "still running" would leave the session
                // looking alive forever, so it counts as exited.
                if (reaped < 0)
                {
                    _exitCode = -1;
                    return true;
                }
                return false;
            }
        }
    }

    public int? ExitCode
    {
        get { lock (_reapGate) return _exitCode; }
    }

    /// <summary>Hang up the whole session, the way closing a terminal window does: SIGHUP to
    /// the shell's process group (it is a session leader, so its pid is the group id), then
    /// SIGKILL to anything that ignored it. Then close the master and reap.</summary>
    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        if (!HasExited)
        {
            kill(-ProcessId, SIGHUP);
            for (var i = 0; i < 20 && !HasExited; i++) Thread.Sleep(50);
            if (!HasExited)
            {
                kill(-ProcessId, SIGKILL);
                for (var i = 0; i < 20 && !HasExited; i++) Thread.Sleep(50);
            }
        }
        // No signal once the shell has already been reaped: its pid may since have been
        // reused, and the kernel already sent SIGHUP to the foreground group when the
        // session's controlling process exited.

        var fd = Interlocked.Exchange(ref _master, -1);
        if (fd >= 0) close(fd);
    }
}
