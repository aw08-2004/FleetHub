using System.Runtime.InteropServices;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Windows.Forms;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Text clipboard sync between the operator's browser and the remote PC (issue #148, roadmap #2).
///
/// <b>Why it exists.</b> Remote control used to carry video one way and input the other, and
/// nothing else -- so a line copied on the PC could not be pasted on the operator's own machine,
/// and the reverse. Ctrl+C / Ctrl+V between two programs ON the PC always worked (Windows does
/// that itself; we only deliver the keys), which is what made the gap look like a bug.
///
/// <b>Its own thread, and never the input thread.</b> The clipboard wants an owner window
/// (<c>OpenClipboard(NULL)</c> followed by <c>EmptyClipboard</c> makes the next
/// <c>SetClipboardData</c> fail) and an STA for the OLE clipboard WinForms uses. A thread that
/// owns a window can never call <c>SetThreadDesktop</c> again (see <see cref="RemoteHelper"/>),
/// so putting it on <c>remote-input</c> would break input after the first lock screen. This
/// thread never binds to a desktop and does not need to: the clipboard belongs to the window
/// station (WinSta0), which the Default, Winlogon and UAC desktops all share.
///
/// <b>Text only, capped.</b> Files and images are out of scope; text covers the case that was
/// reported. Both directions stop at <see cref="MaxChars"/> and say so rather than failing,
/// because a message the transport refuses is a paste that silently never arrives.
///
/// <b>Who may use it is whoever may drive the PC.</b> It rides on <c>remote_control</c> by the
/// owner's decision (SECURITY.MD): someone who can type into the session can already open
/// Notepad and read the clipboard off the screen, so a separate capability would gate nothing.
/// </summary>
internal sealed class ClipboardBridge : IDisposable
{
    /// <summary>The most text either direction carries, in UTF-16 code units.</summary>
    public const int MaxChars = 65_536;

    /// <summary>How long <see cref="SetText"/> waits for the clipboard thread. Long enough to
    /// cover WinForms' own retry when another program holds the clipboard open, short enough
    /// that a stuck clipboard delays the operator's keystrokes rather than freezing them.</summary>
    private const int SetTimeoutMs = 1500;

    /// <summary>A copy on the PC raises several updates in a row (one per format some programs
    /// add). Reading after the burst sends one message instead of three.</summary>
    private const int DebounceMs = 150;

    private const int WM_CLIPBOARDUPDATE = 0x031D;
    private static readonly IntPtr HwndMessage = new(-3);

    private readonly Action<string> _log;
    private readonly Thread _thread;
    private readonly ManualResetEventSlim _ready = new();
    private SynchronizationContext? _context;

    // Touched only on the clipboard thread.
    private readonly ClipboardEchoGuard _echo = new();

    /// <summary>Text someone copied on the PC, after the session started. Raised on the
    /// clipboard thread; the bool is true when the text was cut to <see cref="MaxChars"/>.</summary>
    public event Action<string, bool>? TextCopied;

    /// <summary>False when the thread could not start or create its window; the session then
    /// goes on without clipboard sync and the offer says so.</summary>
    public bool IsReady { get; private set; }

    public ClipboardBridge(Action<string> log)
    {
        _log = log;
        _thread = new Thread(Run) { Name = "remote-clipboard", IsBackground = true };
        _thread.SetApartmentState(ApartmentState.STA);
        _thread.Start();
        _ready.Wait(5000);
    }

    /// <summary>Put <paramref name="text"/> on the PC's clipboard and wait until it is there, so
    /// the Ctrl+V queued right behind it pastes the new text rather than the old. False if the
    /// clipboard could not be set in time.</summary>
    public bool SetText(string text)
    {
        var context = _context;
        if (!IsReady || context is null || text.Length == 0) return false;
        var done = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        context.Post(_ =>
        {
            try
            {
                Clipboard.SetText(text, TextDataFormat.UnicodeText);
                // So the update this causes is not echoed straight back to the viewer that
                // sent it -- that one update, and nothing after it.
                _echo.Set(text);
                done.TrySetResult(true);
            }
            catch (Exception e)
            {
                _log($"clipboard: could not set text ({e.Message})");
                done.TrySetResult(false);
            }
        }, null);
        return done.Task.Wait(SetTimeoutMs) && done.Task.Result;
    }

    private void Run()
    {
        ListenerWindow? window = null;
        try
        {
            // Before this process's first window, always: SetCompatibleTextRenderingDefault
            // throws once any window exists, and the recording badge calls it later. Skipping
            // this would leave the badge -- not this class -- failing, on the first recording.
            ConsentBanner.InitialiseUi();
            var context = new WindowsFormsSynchronizationContext();
            SynchronizationContext.SetSynchronizationContext(context);

            var debounce = new System.Windows.Forms.Timer { Interval = DebounceMs };
            debounce.Tick += (_, _) =>
            {
                debounce.Stop();
                ReadAndRaise();
            };
            window = new ListenerWindow(() =>
            {
                debounce.Stop();
                debounce.Start();
            });
            window.CreateHandle(new CreateParams { Parent = HwndMessage });
            if (!AddClipboardFormatListener(window.Handle))
                _log($"clipboard: listener refused (win32 {Marshal.GetLastWin32Error()}); " +
                     "copies on the PC will not reach the viewer");

            _context = context;
            IsReady = true;
            _ready.Set();
            Application.Run();
        }
        catch (Exception e)
        {
            _log($"clipboard: thread failed ({e.Message}); session continues without it");
        }
        finally
        {
            IsReady = false;
            _ready.Set();
            if (window is not null && window.Handle != IntPtr.Zero)
            {
                RemoveClipboardFormatListener(window.Handle);
                window.DestroyHandle();
            }
        }
    }

    private void ReadAndRaise()
    {
        string text;
        try
        {
            if (!Clipboard.ContainsText(TextDataFormat.UnicodeText)) return;
            text = Clipboard.GetText(TextDataFormat.UnicodeText);
        }
        catch (Exception e)
        {
            // Another program has it open; the next copy tries again.
            _log($"clipboard: could not read ({e.Message})");
            return;
        }
        if (text.Length == 0 || !_echo.ShouldSend(text)) return;
        string sent = Truncate(text, MaxChars);
        try { TextCopied?.Invoke(sent, sent.Length < text.Length); }
        catch (Exception e) { _log($"clipboard: send failed ({e.Message})"); }
    }

    /// <summary>Cut to at most <paramref name="max"/> code units without splitting a surrogate
    /// pair -- half an emoji is not valid UTF-16, and the JSON on the other side rejects it.</summary>
    internal static string Truncate(string text, int max)
    {
        if (text.Length <= max) return text;
        int cut = max;
        if (cut > 0 && char.IsHighSurrogate(text[cut - 1])) cut--;
        return text[..cut];
    }

    public void Dispose()
    {
        var context = _context;
        if (context is not null)
        {
            _context = null;
            context.Post(_ => Application.ExitThread(), null);
            _thread.Join(2000);
        }
        _ready.Dispose();
    }

    /// <summary>A message-only window: no taskbar entry, nothing on any desktop, just the
    /// owner the clipboard insists on and the target for <c>WM_CLIPBOARDUPDATE</c>.</summary>
    private sealed class ListenerWindow(Action onUpdate) : NativeWindow
    {
        protected override void WndProc(ref Message m)
        {
            if (m.Msg == WM_CLIPBOARDUPDATE) onUpdate();
            base.WndProc(ref m);
        }
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool AddClipboardFormatListener(IntPtr hwnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool RemoveClipboardFormatListener(IntPtr hwnd);
}

/// <summary>
/// Decides whether a clipboard update on the PC is someone's copy, to send to the viewer, or
/// the echo of a paste the viewer just sent.
///
/// <b>Only the one update our own paste causes is suppressed, and then forgotten.</b> The first
/// version remembered the last text set and the last text sent, and dropped anything equal to
/// either -- which also dropped a genuine re-copy: the PC user copies X (sent), the operator
/// pastes Y, the user copies X again, and the viewer never hears of it, because X is still "the
/// last text sent". Found in review of #150. Bursts of updates from one copy do not need a
/// dedupe here; <see cref="ClipboardBridge"/> already debounces them into one read.
///
/// Pure on purpose: the bridge needs a real clipboard, this does not, so this is what the
/// tests pin.
/// </summary>
internal sealed class ClipboardEchoGuard
{
    private string? _pending;

    /// <summary>We just put <paramref name="text"/> on the clipboard for the viewer.</summary>
    public void Set(string text) => _pending = text;

    /// <summary>The clipboard now holds <paramref name="text"/>. True if it should go to the
    /// viewer; false only for the echo of our own <see cref="Set"/>, which is consumed.</summary>
    public bool ShouldSend(string text)
    {
        string? pending = _pending;
        _pending = null;
        return !string.Equals(text, pending, StringComparison.Ordinal);
    }
}

/// <summary>
/// Reassembles the viewer's <c>{"t":"clip","id","i","n","s"}</c> chunks into one paste.
///
/// <b>Why chunks at all.</b> The hub relay refuses an upload batch holding any message over
/// <c>MAX_UP_MESSAGE_BYTES</c> (4096, hub/remote_relay.py) -- the whole batch, keystrokes in it
/// included. A paste sent as one message would work on WebRTC and vanish on the relay with no
/// error on either side, so the viewer always splits, and this puts it back together.
///
/// Pure on purpose, so it is tested without a clipboard. A new <c>id</c> abandons whatever was
/// half-assembled: the operator pasted again, and the newer paste is the one they want.
/// </summary>
internal sealed class ClipboardAssembler
{
    /// <summary>Upper bound on chunks per paste. Generous: the viewer's worst case (text that is
    /// all escaped control characters) needs about 110 for <see cref="ClipboardBridge.MaxChars"/>.</summary>
    public const int MaxChunks = 1024;

    private long _id = -1;
    private string?[]? _parts;
    private int _have;

    /// <summary>Feed one chunk. Returns the whole text once its last chunk arrives, else null.</summary>
    public string? Accept(JsonElement e)
    {
        if (!TryLong(e, "id", out long id) || !TryLong(e, "i", out long i) ||
            !TryLong(e, "n", out long n))
            return null;
        if (!e.TryGetProperty("s", out var sEl) || sEl.ValueKind != JsonValueKind.String) return null;
        if (n < 1 || n > MaxChunks || i < 0 || i >= n) return null;

        if (id != _id || _parts is null || _parts.Length != n)
        {
            _id = id;
            _parts = new string?[n];
            _have = 0;
        }
        if (_parts[i] is null)
        {
            _parts[i] = sEl.GetString() ?? "";
            _have++;
        }
        if (_have < n) return null;

        string text = string.Concat(_parts);
        _parts = null;
        _have = 0;
        return ClipboardBridge.Truncate(text, ClipboardBridge.MaxChars);
    }

    private static bool TryLong(JsonElement e, string key, out long value)
    {
        value = 0;
        return e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number &&
               v.TryGetInt64(out value);
    }
}

/// <summary>The PC-to-viewer half of the wire format.</summary>
internal static class ClipboardMessages
{
    /// <summary>Cap on one encoded message. A browser's data channel refuses anything past
    /// its advertised max-message-size (256 KB in Chromium) and closes the channel over it, which
    /// would take input down with it; this stays well inside that and the relay's record cap.</summary>
    public const int MaxBytes = 64 * 1024;

    // Non-ASCII stays UTF-8 rather than \uXXXX, so a page of accented text costs two bytes a
    // character instead of six. It is parsed with JSON.parse, never put into HTML, which is the
    // only thing the default encoder's extra escaping protects.
    private static readonly JsonSerializerOptions Options =
        new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    /// <summary><c>{"t":"clip","s":text,"trunc":bool}</c>, cut further if the encoded message
    /// would pass <see cref="MaxBytes"/>. "t" stays first, like every other status message.</summary>
    public static string Clip(string text, bool truncated)
    {
        while (true)
        {
            string json = JsonSerializer.Serialize(new { t = "clip", s = text, trunc = truncated }, Options);
            int bytes = Encoding.UTF8.GetByteCount(json);
            if (bytes <= MaxBytes || text.Length == 0) return json;
            // Shrink by the overshoot's share of the text, plus a margin so this converges in a
            // pass or two even when escapes make bytes-per-char uneven.
            int keep = (int)((long)text.Length * MaxBytes / bytes) - 64;
            text = ClipboardBridge.Truncate(text, Math.Max(0, keep));
            truncated = true;
        }
    }
}
