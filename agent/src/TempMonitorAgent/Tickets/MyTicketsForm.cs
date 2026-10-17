using System.Drawing;
using System.Text;
using System.Text.Json.Nodes;
using System.Windows.Forms;

namespace TempMonitorAgent.Tickets;

/// <summary>
/// "My tickets" (roadmap #31 phase D): this person's tickets on this PC, the helpdesk's
/// replies, and a box to answer in. The list is whatever the hub's
/// <c>GET /api/agent/tickets</c> served for the SID the service read off this tray's
/// connection -- internal notes never reach it, and neither do other people's tickets.
///
/// The hub's text arrives here (subjects, replies), and lands only in a read-only TextBox
/// and ListView cells, which render it as plain text. Nothing is interpreted.
/// </summary>
internal sealed class MyTicketsForm : Form
{
    private const string StatusKey = "status";
    private readonly Func<Task> _refresh;
    private readonly Func<long, string, Task<JsonObject?>> _reply;
    private readonly ListView _list;
    private readonly TextBox _thread;
    private readonly TextBox _answer;
    private readonly Button _send;
    private readonly Label _status;
    private JsonArray _tickets = [];

    public MyTicketsForm(Func<Task> refresh, Func<long, string, Task<JsonObject?>> reply)
    {
        _refresh = refresh;
        _reply = reply;
        Text = "My IT tickets";
        Font = new Font("Segoe UI", 9.5f, FontStyle.Regular, GraphicsUnit.Point);
        BackColor = Color.White;
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(720, 560);
        MinimumSize = new Size(560, 420);
        Padding = new Padding(12);

        _list = new ListView
        {
            View = View.Details, FullRowSelect = true, MultiSelect = false, HideSelection = false,
            Dock = DockStyle.Top, Height = 180,
        };
        _list.Columns.Add("Ticket", 70);
        _list.Columns.Add("What is wrong", 380);
        _list.Columns.Add("Status", 100);
        _list.Columns.Add("Last change", 130);
        _list.SelectedIndexChanged += (_, _) => ShowSelected();

        _thread = new TextBox
        {
            Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill,
            BackColor = Color.FromArgb(248, 250, 252), BorderStyle = BorderStyle.FixedSingle,
        };

        var bottom = new TableLayoutPanel { Dock = DockStyle.Bottom, Height = 120, ColumnCount = 2, RowCount = 2 };
        bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        bottom.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        _answer = new TextBox
        {
            Multiline = true, Dock = DockStyle.Fill, ScrollBars = ScrollBars.Vertical,
            MaxLength = TicketRules.MaxReplyChars, PlaceholderText = "Answer the helpdesk...",
        };
        _send = new Button { Text = "Send reply", AutoSize = true, Anchor = AnchorStyles.Top | AnchorStyles.Right };
        _send.Click += async (_, _) => await SendReplyAsync();
        var refreshButton = new Button { Text = "Refresh", AutoSize = true, Anchor = AnchorStyles.Top | AnchorStyles.Right };
        refreshButton.Click += async (_, _) => await _refresh();
        var buttons = new FlowLayoutPanel { FlowDirection = FlowDirection.TopDown, AutoSize = true };
        buttons.Controls.Add(_send);
        buttons.Controls.Add(refreshButton);
        _status = new Label { AutoSize = true, ForeColor = Color.FromArgb(100, 116, 139) };
        bottom.Controls.Add(_answer, 0, 0);
        bottom.Controls.Add(buttons, 1, 0);
        bottom.Controls.Add(_status, 0, 1);
        bottom.SetColumnSpan(_status, 2);

        var spacer = new Panel { Dock = DockStyle.Top, Height = 8 };
        Controls.Add(_thread);
        Controls.Add(spacer);
        Controls.Add(_list);
        Controls.Add(bottom);
    }

    /// <summary>Replace the list, keeping (or moving to) a selection.</summary>
    public void ShowTickets(JsonArray tickets, long select = 0)
    {
        _tickets = tickets;
        long keep = select != 0 ? select : SelectedId();
        _list.BeginUpdate();
        _list.Items.Clear();
        foreach (var ticket in tickets.OfType<JsonObject>())
        {
            var id = Long(ticket, "id");
            var item = new ListViewItem([
                "#" + id, Str(ticket, "subject"), StatusLabel(Str(ticket, StatusKey)),
                When(Long(ticket, "updated_at")),
            ]) { Tag = id };
            _list.Items.Add(item);
            if (id == keep) item.Selected = true;
        }
        if (_list.SelectedItems.Count == 0 && _list.Items.Count > 0) _list.Items[0].Selected = true;
        _list.EndUpdate();
        if (_list.Items.Count == 0)
            _thread.Text = "You have no tickets from this PC. Use \"Report a problem\" in this icon's menu.";
        ShowSelected();
    }

    private void ShowSelected()
    {
        var ticket = Selected();
        _send.Enabled = ticket is not null && Str(ticket, StatusKey) != "closed";
        _answer.Enabled = _send.Enabled;
        if (ticket is null) return;
        var text = new StringBuilder();
        text.Append(Str(ticket, "subject")).Append("\r\n");
        text.Append("Opened ").Append(When(Long(ticket, "created_at")))
            .Append(" -- ").Append(StatusLabel(Str(ticket, StatusKey))).Append("\r\n\r\n");
        var messages = ticket["messages"] as JsonArray ?? [];
        if (messages.Count == 0) text.Append("No answer from IT yet.");
        foreach (var message in messages.OfType<JsonObject>())
        {
            var who = Str(message, "author_kind") == "operator" ? "IT" : "You";
            text.Append(who).Append(", ").Append(When(Long(message, "created_at"))).Append(":\r\n")
                .Append(Str(message, "body").Replace("\r\n", "\n").Replace("\n", "\r\n"))
                .Append("\r\n\r\n");
        }
        if (Str(ticket, StatusKey) == "closed")
            text.Append("This ticket is closed. If the problem is back, please report it again.");
        _thread.Text = text.ToString();
        _thread.SelectionStart = _thread.TextLength;
        _thread.ScrollToCaret();
    }

    private async Task SendReplyAsync()
    {
        var id = SelectedId();
        var body = _answer.Text.Trim();
        if (id == 0 || body.Length == 0) return;
        _send.Enabled = false;
        _status.Text = "Sending...";
        var answer = await _reply(id, body);
        if (IsDisposed) return;
        if (answer?["ok"]?.GetValue<bool>() ?? false)
        {
            _answer.Clear();
            _status.Text = "Sent.";
        }
        else
        {
            _status.Text = answer?["error"]?.ToString() ?? "Your reply could not be sent. Please try again.";
        }
        _send.Enabled = true;
    }

    private JsonObject? Selected()
    {
        var id = SelectedId();
        return _tickets.OfType<JsonObject>().FirstOrDefault(t => Long(t, "id") == id);
    }

    private long SelectedId() =>
        _list.SelectedItems.Count > 0 && _list.SelectedItems[0].Tag is long id ? id : 0;

    private static string StatusLabel(string status) => status switch
    {
        "new" => "Received",
        "open" => "Being looked at",
        "waiting" => "Waiting for you",
        "resolved" => "Solved",
        "closed" => "Closed",
        _ => status,
    };

    private static string When(long epoch) =>
        epoch <= 0 ? "" : DateTimeOffset.FromUnixTimeSeconds(epoch).ToLocalTime().ToString("ddd d MMM HH:mm");

    private static long Long(JsonObject o, string key) =>
        o[key] is JsonValue v && v.TryGetValue<long>(out var n) ? n : 0;

    private static string Str(JsonObject o, string key) =>
        o[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s : "";
}
