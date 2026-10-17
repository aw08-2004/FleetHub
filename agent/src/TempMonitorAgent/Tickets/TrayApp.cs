using System.Drawing;
using System.Text.Json.Nodes;
using System.Windows.Forms;

namespace TempMonitorAgent.Tickets;

/// <summary>
/// The ticket tray (roadmap #31 phases A and D): <c>FleetHubAgent.exe --tray</c>, started by
/// the service AS THE SIGNED-IN USER in each session (<see cref="TraySupervisor"/>), living in
/// the notification area for as long as the session does.
///
/// **It holds no secret and talks to nobody but the service.** No agent token, no hub URL:
/// it hands a ticket to the service over the pipe, and the service decides who sent it from
/// the connection. So a person who attaches a debugger to it, or a script that drives it, can
/// do what the person could do anyway -- file a ticket as themselves -- and nothing else.
///
/// **It lives exactly as long as its pipe.** When the service goes away -- stopped, or
/// restarting into a new build after a self-update -- the tray closes, and the supervisor
/// starts the new build's tray once the service is back. That one rule is what keeps a stale
/// tray from holding the old exe open across updates (SelfUpdater renames the running binary
/// aside, and a tray still running from it would block the next rename). The exception is a
/// window the person is typing in: losing a half-written ticket to an agent update would be
/// the worst possible first impression, so the tray waits for the last window to close and
/// a Send in the meantime reconnects first.
///
/// One per session, by a <c>Local\</c> mutex: a second tray started by mistake leaves at once.
/// </summary>
public static class TrayApp
{
    /// <summary>Is this process the tray? Checked by Program.cs before anything that needs
    /// the service's %ProgramData% (which the user cannot write) or its logger.</summary>
    public static bool IsTrayLaunch(string[] args) =>
        args.Any(a => string.Equals(a, AgentConfig.TrayArg, StringComparison.Ordinal));

    public static int Run()
    {
        using var single = new Mutex(true, @"Local\FleetHub.TicketTray", out bool first);
        if (!first) return 0;
        TrayLog.Write($"Tray {AgentConfig.Version} starting.");
        int exit = 0;
        var thread = new Thread(() =>
        {
            try
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                // Per-monitor: screen bounds in real pixels, or a scaled laptop screen would be
                // captured as a corner of itself (see ScreenGrabber).
                try { Application.SetHighDpiMode(HighDpiMode.PerMonitorV2); }
                catch (InvalidOperationException)
                {
                    // Already set (a window exists): the tray still works, only a scaled screen
                    // may be captured at its scaled size.
                }
                using var context = new TrayContext();
                Application.Run(context);
            }
            catch (Exception e)
            {
                TrayLog.Write($"Tray failed: {e}");
                exit = 1;
            }
        })
        { Name = "ticket-tray", IsBackground = false };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        TrayLog.Write("Tray exiting.");
        return exit;
    }

    /// <summary>The icon, its menu, and the windows it opens. Everything here runs on the UI
    /// thread; pipe events are marshalled onto it through <see cref="_ui"/>.</summary>
    private sealed class TrayContext : ApplicationContext
    {
        private static readonly TimeSpan SubmitTimeout = TimeSpan.FromMinutes(3);
        private static readonly TimeSpan QuickTimeout = TimeSpan.FromSeconds(30);

        private readonly Control _ui = new();
        private readonly NotifyIcon _icon;
        private readonly ToolStripMenuItem _report;
        private readonly TrayPipeClient _pipe = new();
        private readonly string _seenPath = Path.Combine(TrayLog.Folder, "tickets-seen.json");
        private TicketForm? _ticketForm;
        private MyTicketsForm? _ticketsForm;
        private JsonArray _tickets = [];
        private bool _canSubmit = true;
        private bool _closing;
        private long _noticeTicket;

        public TrayContext()
        {
            _ui.CreateControl();
            _ = _ui.Handle;
            _report = new ToolStripMenuItem("Report a problem...", null, (_, _) => OpenReport());
            var menu = new ContextMenuStrip();
            menu.Items.Add(_report);
            menu.Items.Add(new ToolStripMenuItem("My tickets", null, (_, _) => OpenTickets()));
            _icon = new NotifyIcon
            {
                Icon = LoadIcon(),
                Text = "IT helpdesk -- report a problem",
                ContextMenuStrip = menu,
                Visible = false,
            };
            _icon.MouseClick += (_, e) => { if (e.Button == MouseButtons.Left) OpenDefault(); };
            _icon.BalloonTipClicked += (_, _) => OpenTickets(_noticeTicket);

            _pipe.Pushed += frame => OnUi(() => OnPush(frame));
            _pipe.Disconnected += () => OnUi(OnDisconnected);
            _ = ConnectAsync();
        }

        private async Task ConnectAsync()
        {
            // The service may still be starting (a reboot, a self-update): give it a minute.
            if (await _pipe.ConnectAsync(TimeSpan.FromMinutes(1), CancellationToken.None))
            {
                _icon.Visible = true;
                TrayLog.Write("Connected to the agent service.");
            }
            else
            {
                TrayLog.Write("The agent service did not answer; closing.");
                ExitThread();
            }
        }

        private void OnUi(Action action)
        {
            if (_closing || _ui.IsDisposed) return;
            try { _ui.BeginInvoke(action); }
            catch (InvalidOperationException)
            {
                // The UI thread is already shutting down; there is nobody left to tell.
            }
        }

        private void OnPush(JsonObject frame)
        {
            switch (TrayProtocol.TypeOf(frame))
            {
                case TrayProtocol.Policy:
                    _canSubmit = frame["can_submit"]?.GetValue<bool>() ?? true;
                    _report.Visible = _canSubmit;
                    _icon.Text = _canSubmit ? "IT helpdesk -- report a problem" : "IT helpdesk -- my tickets";
                    break;
                case TrayProtocol.Tickets:
                    if (frame["tickets"] is JsonArray list) ApplyTickets(list);
                    break;
                case TrayProtocol.Exit:
                    TrayLog.Write("The helpdesk switched the tray off.");
                    Shutdown();
                    break;
            }
        }

        private void ApplyTickets(JsonArray list)
        {
            _tickets = (JsonArray)list.DeepClone();
            _ticketsForm?.ShowTickets(_tickets);
            var (notices, now) = ReplyTracker.Diff(ReplyTracker.Load(_seenPath), _tickets);
            ReplyTracker.Save(_seenPath, now);
            if (notices.Count == 0) return;
            var latest = notices[^1];
            _noticeTicket = latest.TicketId;
            var title = notices.Count == 1 ? $"IT replied: {latest.Subject}"
                                           : $"IT replied to {notices.Count} of your tickets";
            _icon.ShowBalloonTip(10_000, Truncate(title, 60), Truncate(latest.Text, 200), ToolTipIcon.Info);
        }

        private void OnDisconnected()
        {
            // See the type docs: close now unless the person is in the middle of something.
            if (_ticketForm is null && _ticketsForm is null) Shutdown();
        }

        private void OpenDefault()
        {
            if (_canSubmit) OpenReport();
            else OpenTickets();
        }

        private void OpenReport()
        {
            if (_ticketForm is { IsDisposed: false })
            {
                _ticketForm.Activate();
                return;
            }
            // Screens FIRST, before our own window can cover the problem.
            var screens = ScreenGrabber.CaptureAll();
            _ticketForm = new TicketForm(screens, SendTicketAsync);
            _ticketForm.FormClosed += (_, _) =>
            {
                foreach (var s in screens) s.Dispose();
                _ticketForm = null;
                if (!_pipe.IsConnected) Shutdown();
            };
            _ticketForm.Show();
            _ticketForm.Activate();
        }

        private void OpenTickets(long select = 0)
        {
            if (_ticketsForm is not { IsDisposed: false })
            {
                _ticketsForm = new MyTicketsForm(RefreshTicketsAsync, SendReplyAsync);
                _ticketsForm.FormClosed += (_, _) =>
                {
                    _ticketsForm = null;
                    if (!_pipe.IsConnected) Shutdown();
                };
                _ticketsForm.Show();
            }
            _ticketsForm.ShowTickets(_tickets, select);
            _ticketsForm.Activate();
            _ = RefreshTicketsAsync();
        }

        private async Task<JsonObject?> SendTicketAsync(JsonObject frame)
        {
            if (!await EnsureConnectedAsync()) return null;
            frame["type"] = TrayProtocol.Submit;
            return await _pipe.RequestAsync(frame, SubmitTimeout);
        }

        private async Task<JsonObject?> SendReplyAsync(long ticketId, string body)
        {
            if (!await EnsureConnectedAsync()) return null;
            return await _pipe.RequestAsync(new JsonObject
            {
                ["type"] = TrayProtocol.Reply, ["ticket_id"] = ticketId, ["body"] = body,
            }, QuickTimeout);
        }

        private async Task RefreshTicketsAsync()
        {
            if (!_pipe.IsConnected) return;
            var answer = await _pipe.RequestAsync(new JsonObject { ["type"] = TrayProtocol.Mine }, QuickTimeout);
            if (answer?["tickets"] is JsonArray list) ApplyTickets(list);
        }

        /// <summary>A Send after the service restarted under an open window: reconnect once,
        /// briefly, rather than tell the person their ticket failed.</summary>
        private async Task<bool> EnsureConnectedAsync() =>
            _pipe.IsConnected || await _pipe.ConnectAsync(TimeSpan.FromSeconds(15), CancellationToken.None);

        private void Shutdown()
        {
            if (_closing) return;
            _closing = true;
            _ticketForm?.Close();
            _ticketsForm?.Close();
            _icon.Visible = false;
            ExitThread();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _icon.Visible = false;
                _icon.Dispose();
                _pipe.Dispose();
                _ui.Dispose();
            }
            base.Dispose(disposing);
        }

        private static Icon LoadIcon()
        {
            try
            {
                if (Environment.ProcessPath is { } exe && Icon.ExtractAssociatedIcon(exe) is { } icon)
                    return icon;
            }
            catch (ArgumentException)
            {
                // No icon in the exe: fall through to the system one below.
            }
            return SystemIcons.Information;
        }

        private static string Truncate(string text, int max) =>
            text.Length <= max ? text : text[..(max - 3)] + "...";
    }
}
