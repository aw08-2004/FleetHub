using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text.Json.Nodes;
using TempMonitorAgent.Tickets;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The tray's windows and its screen capture, built for real on an STA thread (roadmap #31).
///
/// The silent failure: **a tray that throws while building its window** -- a control property
/// set in the wrong order, a DateTimePicker given a value outside its own range -- dies the
/// first time somebody clicks the icon, writes one line to a log in their profile, and the
/// supervisor restarts it into the same crash until its budget runs out. Nobody at the
/// helpdesk ever sees a ticket, and nothing on the hub says why. Constructing the windows here
/// catches that class before a PC does. Needs a desktop, so it is skipped without one.
/// </summary>
public class TicketTrayUiTests
{
    private static bool HasDesktop()
    {
        using var me = WindowsIdentity.GetCurrent();
        return Environment.UserInteractive && me.Groups?.Any(g => g.Value == "S-1-5-4") == true;
    }

    /// <summary>Is the person's desktop the one on screen? Not while the PC is locked: the
    /// input desktop is then Winlogon's, and BitBlt from the screen answers access denied --
    /// correctly, and a tray nobody can click is not asked to capture anything.</summary>
    private static bool DesktopUnlocked()
    {
        IntPtr desktop = OpenInputDesktop(0, false, 0x0001 /* DESKTOP_READOBJECTS */);
        if (desktop == IntPtr.Zero) return false;
        try
        {
            var name = new char[64];
            return GetUserObjectInformationW(desktop, 2 /* UOI_NAME */, name, name.Length * 2, out _)
                && new string(name).TrimEnd('\0').Equals("Default", StringComparison.OrdinalIgnoreCase);
        }
        finally { CloseDesktop(desktop); }
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool CloseDesktop(IntPtr desktop);

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool GetUserObjectInformationW(IntPtr obj, int index, char[] info, int length, out int needed);

    private static void OnSta(Action body)
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try { body(); }
            catch (Exception e) { failure = e; }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        if (failure is not null) throw new Xunit.Sdk.XunitException(failure.ToString());
    }

    [Fact]
    public void Every_screen_is_captured_as_a_jpeg_under_the_cap()
    {
        if (!HasDesktop() || !DesktopUnlocked()) return;
        OnSta(() =>
        {
            var screens = ScreenGrabber.CaptureAll();
            try
            {
                Assert.NotEmpty(screens);
                foreach (var screen in screens)
                {
                    Assert.Equal("image/jpeg", TicketRules.ImageType(screen.Jpeg));
                    Assert.True(screen.Jpeg.Length <= TrayProtocol.MaxScreenshotBytes);
                    Assert.True(screen.Preview.Width <= 320 && screen.Preview.Height <= 320);
                }
                Assert.Single(screens, s => s.Primary);
            }
            finally
            {
                foreach (var s in screens) s.Dispose();
            }
        });
    }

    [Fact]
    public void Both_windows_build_and_take_a_list()
    {
        if (!HasDesktop()) return;
        OnSta(() =>
        {
            var screens = ScreenGrabber.CaptureAll();
            using (var form = new TicketForm(screens, _ => Task.FromResult<JsonObject?>(null)))
            {
                _ = form.Handle;   // forces the native window, which CreateControl skips while hidden
                Assert.True(form.IsHandleCreated);
            }
            foreach (var s in screens) s.Dispose();
            using (var empty = new TicketForm([], _ => Task.FromResult<JsonObject?>(null)))
            {
                _ = empty.Handle;
                Assert.True(empty.IsHandleCreated);
            }

            using var mine = new MyTicketsForm(() => Task.CompletedTask,
                                               (_, _) => Task.FromResult<JsonObject?>(null));
            _ = mine.Handle;
            Assert.True(mine.IsHandleCreated);
            mine.ShowTickets([]);
            mine.ShowTickets(
            [
                new JsonObject
                {
                    ["id"] = 7, ["subject"] = "Printer jams", ["status"] = "waiting",
                    ["created_at"] = 1_760_000_000L, ["updated_at"] = 1_760_000_600L,
                    ["messages"] = new JsonArray(
                        new JsonObject { ["author_kind"] = "operator", ["body"] = "Try tray 2\nthen tell us",
                                         ["created_at"] = 1_760_000_300L },
                        new JsonObject { ["author_kind"] = "reporter", ["body"] = "Same", ["created_at"] = 1_760_000_600L }),
                },
                new JsonObject { ["id"] = 3, ["subject"] = "Old one", ["status"] = "closed" },
            ], select: 3);
        });
    }
}
