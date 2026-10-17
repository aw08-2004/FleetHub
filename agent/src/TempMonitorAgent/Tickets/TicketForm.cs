using System.Drawing;
using System.Text.Json.Nodes;
using System.Windows.Forms;

namespace TempMonitorAgent.Tickets;

/// <summary>
/// "Report a problem" (roadmap #31 phase A): what is wrong, how urgent, an optional "needed
/// by", and the screens captured a moment before this window opened -- each with a tick box,
/// because the person decides what leaves their PC (owner, 2026-10-16), and a screen with
/// their bank open on it is theirs to untick.
///
/// Plain WinForms in the house style of UserMessage/MessageDialog: Segoe UI, a white card, one
/// accent button. No hub text reaches this window, so there is nothing here to escape.
/// </summary>
internal sealed class TicketForm : Form
{
    private static readonly Color Accent = Color.FromArgb(37, 99, 235);
    private static readonly Color Muted = Color.FromArgb(100, 116, 139);
    private static readonly (string Value, string Label)[] UrgencyChoices =
    [
        ("low", "Low -- whenever you get to it"),
        ("normal", "Normal -- it is getting in my way"),
        ("high", "High -- I cannot do part of my job"),
        ("critical", "Critical -- I cannot work at all"),
    ];

    private readonly Func<JsonObject, Task<JsonObject?>> _send;
    private readonly IReadOnlyList<CapturedScreen> _screens;
    private readonly List<CheckBox> _include = [];
    private readonly TextBox _subject;
    private readonly TextBox _body;
    private readonly ComboBox _urgency;
    private readonly CheckBox _hasDue;
    private readonly DateTimePicker _due;
    private readonly Button _sendButton;
    private readonly Label _status;

    public TicketForm(IReadOnlyList<CapturedScreen> screens, Func<JsonObject, Task<JsonObject?>> send)
    {
        _screens = screens;
        _send = send;
        Text = "Report a problem to IT";
        Font = new Font("Segoe UI", 9.5f, FontStyle.Regular, GraphicsUnit.Point);
        BackColor = Color.White;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = true;
        ShowInTaskbar = true;
        AutoScaleMode = AutoScaleMode.Dpi;
        AutoSize = true;
        AutoSizeMode = AutoSizeMode.GrowAndShrink;
        Padding = new Padding(16);

        var layout = new TableLayoutPanel
        {
            ColumnCount = 1, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink,
            Dock = DockStyle.Fill,
        };
        Controls.Add(layout);

        layout.Controls.Add(Caption("What is wrong?"));
        _subject = new TextBox { Width = 560, MaxLength = TicketRules.MaxSubjectChars,
                                 PlaceholderText = "For example: Outlook will not open" };
        layout.Controls.Add(_subject);

        layout.Controls.Add(Caption("Tell us more (optional)"));
        _body = new TextBox
        {
            Width = 560, Height = 120, Multiline = true, ScrollBars = ScrollBars.Vertical,
            MaxLength = TicketRules.MaxBodyChars, AcceptsReturn = true,
            PlaceholderText = "What were you doing when it happened? Does it happen every time?",
        };
        layout.Controls.Add(_body);

        layout.Controls.Add(Caption("How urgent is it?"));
        _urgency = new ComboBox { Width = 320, DropDownStyle = ComboBoxStyle.DropDownList };
        foreach (var (_, label) in UrgencyChoices) _urgency.Items.Add(label);
        _urgency.SelectedIndex = 1;
        layout.Controls.Add(_urgency);

        var dueRow = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new Padding(0, 10, 0, 0) };
        _hasDue = new CheckBox { Text = "I need it fixed by", AutoSize = true, Margin = new Padding(0, 4, 8, 0) };
        _due = new DateTimePicker
        {
            Format = DateTimePickerFormat.Custom, CustomFormat = "ddd d MMM yyyy  HH:mm",
            Width = 220, Enabled = false, Value = DateTime.Now.AddHours(4),
            MinDate = DateTime.Now.AddMinutes(-1),
        };
        _hasDue.CheckedChanged += (_, _) => _due.Enabled = _hasDue.Checked;
        dueRow.Controls.Add(_hasDue);
        dueRow.Controls.Add(_due);
        layout.Controls.Add(dueRow);

        layout.Controls.Add(Caption(screens.Count == 0
            ? "No screenshot could be taken -- describe what you see instead."
            : "Screenshots taken just now -- untick any you do not want to send"));
        var shots = new FlowLayoutPanel { AutoSize = true, MaximumSize = new Size(580, 0) };
        foreach (var screen in screens)
        {
            var cell = new FlowLayoutPanel
            {
                FlowDirection = FlowDirection.TopDown, AutoSize = true, Margin = new Padding(0, 0, 12, 8),
            };
            var preview = new PictureBox
            {
                Image = screen.Preview, SizeMode = PictureBoxSizeMode.Zoom,
                Size = new Size(170, 110), BorderStyle = BorderStyle.FixedSingle, Cursor = Cursors.Hand,
            };
            var include = new CheckBox { Text = "Send " + screen.Label.ToLowerInvariant(), Checked = true, AutoSize = true };
            preview.Click += (_, _) => include.Checked = !include.Checked;
            include.CheckedChanged += (_, _) => preview.Enabled = include.Checked;
            _include.Add(include);
            cell.Controls.Add(preview);
            cell.Controls.Add(include);
            shots.Controls.Add(cell);
        }
        layout.Controls.Add(shots);

        _status = new Label { AutoSize = true, ForeColor = Muted, MaximumSize = new Size(560, 0), Margin = new Padding(0, 8, 0, 0) };
        layout.Controls.Add(_status);

        var buttons = new FlowLayoutPanel
        {
            FlowDirection = FlowDirection.RightToLeft, AutoSize = true, Dock = DockStyle.Fill,
            Margin = new Padding(0, 12, 0, 0),
        };
        _sendButton = new Button
        {
            Text = "Send to IT", AutoSize = true, BackColor = Accent, ForeColor = Color.White,
            FlatStyle = FlatStyle.Flat, Padding = new Padding(12, 4, 12, 4),
        };
        _sendButton.FlatAppearance.BorderSize = 0;
        _sendButton.Click += async (_, _) => await SendAsync();
        var cancel = new Button { Text = "Cancel", AutoSize = true, Padding = new Padding(12, 4, 12, 4) };
        cancel.Click += (_, _) => Close();
        buttons.Controls.Add(_sendButton);
        buttons.Controls.Add(cancel);
        layout.Controls.Add(buttons);

        AcceptButton = null;   // Enter in the description is a new line, not a send.
        CancelButton = cancel;
        Shown += (_, _) => _subject.Focus();
    }

    private async Task SendAsync()
    {
        if (_subject.Text.Trim().Length == 0)
        {
            _status.ForeColor = Color.Firebrick;
            _status.Text = "Please say in a few words what is wrong.";
            _subject.Focus();
            return;
        }
        var shots = new JsonArray();
        for (int i = 0; i < _screens.Count; i++)
            if (_include[i].Checked) shots.Add(Convert.ToBase64String(_screens[i].Jpeg));
        var frame = new JsonObject
        {
            ["subject"] = _subject.Text.Trim(),
            ["body"] = _body.Text.Trim(),
            ["urgency"] = UrgencyChoices[Math.Max(0, _urgency.SelectedIndex)].Value,
            ["screenshots"] = shots,
        };
        if (_hasDue.Checked)
            frame["due_at"] = new DateTimeOffset(_due.Value).ToUnixTimeSeconds();

        SetBusy(true, "Sending...");
        var answer = await _send(frame);
        if (IsDisposed) return;
        if (answer?["ok"]?.GetValue<bool>() == true)
        {
            int sent = answer["screenshots_sent"]?.GetValue<int>() ?? 0;
            int total = answer["screenshots_total"]?.GetValue<int>() ?? 0;
            var id = answer["ticket_id"]?.ToString() ?? "";
            var note = sent < total
                ? $"\n\n{total - sent} of your screenshots could not be sent. The ticket itself arrived."
                : "";
            MessageBox.Show(this,
                $"Thank you -- IT has your ticket (number {id}). You will see their answer under "
                + $"\"My tickets\" in this icon's menu.{note}",
                "Ticket sent", MessageBoxButtons.OK, MessageBoxIcon.Information);
            Close();
            return;
        }
        SetBusy(false, answer?["error"]?.ToString()
            ?? "The ticket could not be handed to the agent on this PC. Your ticket was NOT sent -- "
               + "please try again, or call IT.");
        _status.ForeColor = Color.Firebrick;
    }

    private void SetBusy(bool busy, string status)
    {
        _sendButton.Enabled = !busy;
        UseWaitCursor = busy;
        _status.ForeColor = Muted;
        _status.Text = status;
    }

    private static Label Caption(string text) => new()
    {
        Text = text, AutoSize = true, ForeColor = Color.FromArgb(15, 23, 42),
        Font = new Font("Segoe UI Semibold", 9.5f, FontStyle.Regular, GraphicsUnit.Point),
        Margin = new Padding(0, 10, 0, 4),
    };
}
