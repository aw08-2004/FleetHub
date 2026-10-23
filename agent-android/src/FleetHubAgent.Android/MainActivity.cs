using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.OS;
using Android.Text;
using Android.Views;
using Android.Widget;
using FleetHubAgent;
using FleetHubAgent.Fleet;
using FleetHubAgent.State;
using FleetHubAgent.Android.Platform;
using FleetHubAgent.Android.Policy;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Android;

/// <summary>
/// The device's only screen: whether this device is managed, what it is called in the console,
/// and a checklist of the switches only somebody holding it can flip.
///
/// **This is a setup and diagnostics screen, not an app.** Nobody uses it after the first
/// minute; its job is to make the states that look identical from outside distinguishable --
/// "reporting and taking commands" versus "reporting, and silently ignoring every command
/// because it never enrolled", "can be located" versus "will answer every locate with
/// unavailable". That is why the status card leads, and why every checklist row says what
/// goes wrong without it rather than only what it is.
///
/// **Status first, and no text field until somebody asks for one** (0.3.0). The screen used to
/// open on the hub URL and the fleet's enrollment secret, with the keyboard up for the first,
/// so the first thing anybody saw on a phone was a prompt for the one credential that enrols
/// ANY device. It now opens on the status card. Enrolling is a button that asks for a
/// six-character one-time code from Administration -> Onboarding (FleetClient.
/// RedeemOnboardingCodeAsync), which admits this device once and is worthless afterwards. The
/// hub URL and the shared secret are still here, behind Advanced, for a lab or a device whose
/// MDM cannot push managed configuration.
///
/// **A layout resource now, where it used to be built in code.** The old docstring argued six
/// unstyled widgets did not justify one, and said nothing here would ever be redesigned; this
/// is that redesign, and a card layout with a dark variant is exactly what resources are for.
/// The platform Material theme rather than AndroidX/Material Components: those libraries would
/// add megabytes to an APK every managed phone downloads on every release, for widgets the
/// platform already draws. **The words stay here, in English**, as values/strings.xml records
/// -- an agent's text is not the hub's catalogue.
/// </summary>
[Activity(
    Label = "FleetHub Agent",
    Icon = "@mipmap/ic_launcher",
    RoundIcon = "@mipmap/ic_launcher_round",
    Theme = "@style/FleetHubTheme",
    MainLauncher = true,
    Exported = true,
    LaunchMode = LaunchMode.SingleTask,
    // Belt and braces with the layout having no field on it: even a future one must not pull
    // the keyboard up on launch, which is the complaint this screen was redesigned around.
    WindowSoftInputMode = SoftInput.StateAlwaysHidden)]
public sealed class MainActivity : Activity
{
    private const int RequestPostNotifications = 1001;
    private const int RequestLocation = 1002;

    /// <summary>How often the screen re-reads the agent while it is visible. Enrolment, the
    /// device-owner grant and the link state all change underneath it, and the person holding
    /// the device is waiting to see one of them flip.</summary>
    private static readonly TimeSpan Refresh = TimeSpan.FromSeconds(3);

    private AgentState _state = null!;
    private Handler _handler = null!;
    private bool _visible;

    private TextView _statusChip = null!;
    private TextView _statusHeadline = null!;
    private TextView _statusDetail = null!;
    private TextView _statusFacts = null!;
    private Button _enrollButton = null!;
    private LinearLayout _checklist = null!;
    private TextView _advancedToggle = null!;
    private View _advancedPanel = null!;
    private TextView _hubLabel = null!;
    private EditText _hubField = null!;
    private EditText _secretField = null!;

    protected override void OnCreate(Bundle? savedInstanceState)
    {
        base.OnCreate(savedInstanceState);

        _state = new AgentState(new AndroidStateStore(this));
        _handler = new Handler(Looper.MainLooper!);
        ManagedConfig.ApplyHubUrl(this, _state);

        SetContentView(Resource.Layout.activity_main);
        Bind();

        // Asked here rather than from the service, because a runtime permission can only be
        // requested from an activity. On Android 13+ the foreground notification does not
        // appear without it -- the service still RUNS, so this is not fatal, but the only
        // status display the device has outside this screen would be invisible.
        RequestNotificationPermissionIfNeeded();

        // Starting on every launch, not only the first: this is also the recovery path when an
        // OEM battery manager has killed the service, and tapping the icon is what someone
        // will try first.
        AgentService.Start(this);
    }

    protected override void OnResume()
    {
        base.OnResume();
        _visible = true;
        RefreshStatus();
        ScheduleRefresh();
    }

    protected override void OnPause()
    {
        _visible = false;
        base.OnPause();
    }

    private void ScheduleRefresh()
    {
        _handler.PostDelayed(() =>
        {
            if (!_visible) return;
            RefreshStatus();
            ScheduleRefresh();
        }, (long)Refresh.TotalMilliseconds);
    }

    // ------------------------------------------------------------------ layout

    private void Bind()
    {
        FindViewById<TextView>(Resource.Id.title)!.Text = "FleetHub Agent";
        FindViewById<TextView>(Resource.Id.version)!.Text = $"Version {AgentConfig.Version}";

        _statusChip = FindViewById<TextView>(Resource.Id.status_chip)!;
        _statusHeadline = FindViewById<TextView>(Resource.Id.status_headline)!;
        _statusDetail = FindViewById<TextView>(Resource.Id.status_detail)!;
        _statusFacts = FindViewById<TextView>(Resource.Id.status_facts)!;
        _enrollButton = FindViewById<Button>(Resource.Id.enroll_button)!;
        _enrollButton.Text = "Enroll this device";
        _enrollButton.Click += (_, _) => ShowEnrollDialog();

        FindViewById<TextView>(Resource.Id.checklist_title)!.Text = "SETUP";
        _checklist = FindViewById<LinearLayout>(Resource.Id.checklist)!;

        _advancedToggle = FindViewById<TextView>(Resource.Id.advanced_toggle)!;
        _advancedPanel = FindViewById(Resource.Id.advanced_panel)!;
        _advancedToggle.Click += (_, _) => ToggleAdvanced();
        SetAdvancedOpen(false);

        _hubLabel = FindViewById<TextView>(Resource.Id.hub_label)!;
        _hubLabel.Text = "Hub URL";
        _hubField = FindViewById<EditText>(Resource.Id.hub_field)!;
        _hubField.Hint = AgentConfig.DefaultHubBase;
        _hubField.Text = _state.LoadHubBaseOverride() ?? "";

        FindViewById<TextView>(Resource.Id.secret_label)!.Text = "Fleet enrollment secret";
        _secretField = FindViewById<EditText>(Resource.Id.secret_field)!;
        // The hub's AGENT_ENROLLMENT_SECRET, one shared value for the whole fleet. Masked
        // because it is a fleet credential typed on a device somebody else may be looking at,
        // and never re-displayed once stored -- the checklist shows whether one is held, not
        // what it is.
        _secretField.Hint = "Shared AGENT_ENROLLMENT_SECRET";
        FindViewById<TextView>(Resource.Id.advanced_hint)!.Text =
            "Prefer a one-time code: the shared secret enrols any device and stays on this " +
            "one. Use it only where an MDM pushes it, or on a lab hub with no onboarding codes.";

        var save = FindViewById<Button>(Resource.Id.save_button)!;
        save.Text = "Save";
        save.Click += (_, _) => Save();

        FindViewById<TextView>(Resource.Id.footer)!.Text =
            "Battery optimisation is what stops a device reporting overnight: Android and " +
            "several manufacturers' power managers freeze a background app after a few hours " +
            "idle, and the device then reads offline until somebody picks it up. Location is " +
            "what the console's Locate button needs; it is read only when an operator asks, " +
            "and this device shows a notice naming them every time.";
    }

    private void ToggleAdvanced() => SetAdvancedOpen(_advancedPanel.Visibility != ViewStates.Visible);

    private void SetAdvancedOpen(bool open)
    {
        _advancedPanel.Visibility = open ? ViewStates.Visible : ViewStates.Gone;
        _advancedToggle.Text = open ? "Advanced  ▾" : "Advanced  ▸";
    }

    // ------------------------------------------------------------------ status

    private void RefreshStatus()
    {
        var identity = _state.LoadIdentity();
        var snapshot = AgentService.Snapshot();
        var enrolled = snapshot?.Enrolled ?? identity.IsEnrolled;
        var name = snapshot?.MachineName ?? _state.LoadMachineNameOverride() ?? "(derived from this device)";

        RenderCard(snapshot, enrolled);
        ApplyHubLock();
        _statusFacts.Text = string.Join("\n", new[]
        {
            $"Name in the console:  {name}",
            $"Hub:  {(AgentConfig.IsHubConfigured ? AgentConfig.HubBase : "none")}",
            enrolled && identity.AgentId is { Length: > 0 } id ? $"Agent id:  {id}" : null,
        }.Where(l => l is not null));
        _enrollButton.Visibility = AgentConfig.IsHubConfigured && !enrolled
            ? ViewStates.Visible : ViewStates.Gone;

        var rows = Checklist(enrolled);
        _checklist.RemoveAllViews();
        for (var i = 0; i < rows.Count; i++)
            _checklist.AddView(Render(rows[i], last: i == rows.Count - 1));
    }

    /// <summary>The status card: one chip, one headline, one sentence of what it means.</summary>
    private void RenderCard(AgentSnapshot? snapshot, bool enrolled)
    {
        var (chip, tone, headline, detail) = CardState(snapshot, enrolled);
        Chip(_statusChip, chip, tone);
        _statusHeadline.Text = headline;
        _statusDetail.Text = detail;
    }

    private (string Chip, Tone Tone, string Headline, string Detail) CardState(
        AgentSnapshot? snapshot, bool enrolled)
    {
        if (!AgentConfig.IsHubConfigured)
            // Not "Hub: https://your.hub.url". A placeholder URL formatted exactly like a real
            // one reads as configured at a glance, and this is the state a QR-provisioned
            // device was silently left in -- so it is the loudest thing on the screen.
            return ("NOT SET UP", Tone.Warn, "No hub configured",
                "This device is reporting nowhere. Provision it with a QR code minted by the " +
                "console, or set the hub URL under Advanced.");
        if (snapshot is null)
            return ("STARTING", Tone.Info, "The agent is starting",
                "If this does not change, open the app again -- that restarts it.");
        if (enrolled)
            return snapshot.Buffered > 0
                ? ("MANAGED", Tone.Ok, "Managed, but offline",
                   $"The hub cannot be reached. {snapshot.Buffered} report(s) are waiting and " +
                   "will be sent when it can.")
                : ("MANAGED", Tone.Ok, "Managed by FleetHub",
                   "Reporting to the hub and accepting commands from the console.");

        // The state that looks fine and is not. Said plainly, because on a phone there is no
        // installer output to say it in.
        var hasSecret = new AndroidEnrollmentSecretSource(this, _state).Read() is not null;
        return ("NOT ENROLLED", Tone.Warn, "Not enrolled", hasSecret
            ? "A fleet secret is set and enrolment is retried every 30 seconds."
            : "This device reports to the console but will never accept a command. Enroll it " +
              "with a one-time code from Administration → Onboarding.");
    }

    /// <summary>The setup checklist, in the order somebody setting up a device works through it.</summary>
    private List<CheckRow> Checklist(bool enrolled)
    {
        var located = AndroidLocationReader.IsGranted(this);
        var notifications = NotificationsAllowed();
        var battery = IgnoringBatteryOptimisation();
        var usage = UsageStatsReader.IsGranted(this);
        var managed = DeviceOwner.IsManaged(this);
        return
        [
            Row("Enrollment", enrolled,
                ("Done", "This device has its own credential and takes commands."),
                ("Needed", Tone.Warn,
                 "Without it the console can see this device but cannot manage it."),
                ("Enter code", ShowEnrollDialog)),
            Row("Location", located,
                ("Granted",
                 "The console can locate this device. Nothing is tracked in the background."),
                ("Not granted", Tone.Warn,
                 "A locate from the console will answer “unavailable” and the device " +
                 "will not appear on the map."),
                ("Grant location", GrantLocation)),
            Row("Notifications", notifications,
                ("On", "Messages from the helpdesk and locate notices can be shown."),
                ("Off", Tone.Warn,
                 "Messages from the helpdesk come back unanswered, and the status notification " +
                 "is hidden."),
                ("Turn on", RequestNotificationsOrSettings)),
            Row("Battery optimisation", battery,
                ("Excluded", "Android will not freeze the agent while the device is idle."),
                ("Active", Tone.Warn,
                 "The device may stop reporting overnight and read offline in the console."),
                ("Open battery settings", OpenBatterySettings)),
            Row("Usage access", usage,
                ("Granted", "Screen-time budgets can be enforced."),
                ("Not granted", Tone.Info,
                 "Only needed for screen-time budgets. Blocked hours work without it."),
                ("Grant usage access", OpenUsageAccessSettings)),
            // Information only: device ownership is taken at a factory reset, by QR, and no
            // button on this screen can grant it.
            Row("Device owner", managed,
                ("Fully managed",
                 "Lock, wipe, restart and app installs from the console work on this device."),
                ("Not managed", Tone.Info,
                 "Installed as an ordinary app. Lock, wipe, restart and app installs need the " +
                 "device provisioned by QR code, which starts with a factory reset."),
                null),
        ];
    }

    /// <summary>One row, in its done or its not-done state. The fix button only appears on the
    /// second: a button that does nothing because the thing is already done reads as broken.</summary>
    private static CheckRow Row(string title, bool done, (string State, string Detail) whenDone,
        (string State, Tone Tone, string Detail) whenNot, (string Label, Action Run)? fix)
        => done
            ? new CheckRow(title, whenDone.State, Tone.Ok, whenDone.Detail, null)
            : new CheckRow(title, whenNot.State, whenNot.Tone, whenNot.Detail, fix);

    private enum Tone { Ok, Warn, Info }

    private sealed record CheckRow(string Title, string State, Tone Tone, string Detail,
        (string Label, Action Run)? Action);

    private View Render(CheckRow row, bool last)
    {
        var view = LayoutInflater.Inflate(Resource.Layout.item_check, _checklist, false)!;
        view.FindViewById<TextView>(Resource.Id.check_title)!.Text = row.Title;
        Chip(view.FindViewById<TextView>(Resource.Id.check_chip)!, row.State, row.Tone);
        view.FindViewById<TextView>(Resource.Id.check_detail)!.Text = row.Detail;
        var button = view.FindViewById<Button>(Resource.Id.check_action)!;
        if (row.Action is { } action)
        {
            button.Visibility = ViewStates.Visible;
            button.Text = action.Label;
            button.Click += (_, _) => action.Run();
        }
        view.FindViewById(Resource.Id.check_divider)!.Visibility =
            last ? ViewStates.Invisible : ViewStates.Visible;
        return view;
    }

    private void Chip(TextView chip, string text, Tone tone)
    {
        chip.Text = text;
        var (background, colour) = tone switch
        {
            Tone.Ok => (Resource.Drawable.chip_ok, Resource.Color.ok),
            Tone.Warn => (Resource.Drawable.chip_warn, Resource.Color.warn),
            _ => (Resource.Drawable.chip_info, Resource.Color.info),
        };
        chip.SetBackgroundResource(background);
        chip.SetTextColor(Resources!.GetColor(colour, Theme));
    }

    private bool NotificationsAllowed()
        => GetSystemService(NotificationService) is NotificationManager manager
           && manager.AreNotificationsEnabled();

    private bool IgnoringBatteryOptimisation()
        => GetSystemService(PowerService) is PowerManager power
           && power.IsIgnoringBatteryOptimizations(PackageName);

    // ------------------------------------------------------------------ enrolling

    /// <summary>Ask for a one-time onboarding code and spend it.
    ///
    /// A dialog rather than a field on the screen, so nothing prompts for a credential until
    /// somebody has decided to enrol. The field is upper-case and six characters because that
    /// is what the hub mints (onboarding.ALPHABET); a typo is answered here without spending
    /// one of the hub's rate-limited attempts.</summary>
    private void ShowEnrollDialog()
    {
        if (!AgentConfig.IsHubConfigured)
        {
            Toast.MakeText(this, "Set the hub URL under Advanced first.", ToastLength.Long)?.Show();
            SetAdvancedOpen(true);
            return;
        }

        var field = new EditText(this)
        {
            Hint = "e.g. AB3K7Q",
            InputType = InputTypes.ClassText | InputTypes.TextFlagCapCharacters
                        | InputTypes.TextFlagNoSuggestions,
            TextAlignment = TextAlignment.Center,
            TextSize = 24,
        };
        field.SetFilters([new InputFilterLengthFilter(9), new InputFilterAllCaps()]);
        var frame = new FrameLayout(this);
        var pad = (int)(24 * Resources!.DisplayMetrics!.Density);
        frame.SetPadding(pad, pad / 2, pad, 0);
        frame.AddView(field);

        var dialog = new AlertDialog.Builder(this)
            .SetTitle("Enroll this device")!
            .SetMessage("Enter the six-character code from Administration → Onboarding " +
                        "in the FleetHub console. A code works once, for one device.")!
            .SetView(frame)!
            // Empty handlers: the positive button is rewired after Show, below.
            .SetPositiveButton("Enroll", (_, _) => { })!
            .SetNegativeButton("Cancel", (_, _) => { })!
            .Create()!;
        // The keyboard here IS wanted: somebody pressed Enroll and is about to type.
        dialog.Window?.SetSoftInputMode(SoftInput.StateVisible);
        dialog.Show();

        // Wired after Show so a refusal keeps the dialog open with the code still in it --
        // the default positive button dismisses first and asks questions afterwards.
        var enroll = dialog.GetButton((int)DialogButtonType.Positive)!;
        enroll.Click += async (_, _) =>
        {
            var code = field.Text ?? "";
            if (FleetClient.NormalizeOnboardingCode(code) is null)
            {
                field.Error = "A code is six letters and digits (no 0, O, 1, I or L).";
                return;
            }
            enroll.Enabled = false;
            var outcome = await AgentService.RedeemCodeAsync(code, CancellationToken.None);
            enroll.Enabled = true;
            var message = Describe(outcome);
            if (outcome is CodeRedeemOutcome.Enrolled or CodeRedeemOutcome.AlreadyEnrolled)
            {
                dialog.Dismiss();
                Toast.MakeText(this, message, ToastLength.Long)?.Show();
            }
            else
            {
                field.Error = message;
            }
            RefreshStatus();
        };
    }

    /// <summary>What to tell the person holding the device, per outcome. Each one names the
    /// next thing to do, which is why the outcome is not a bool.</summary>
    private static string Describe(CodeRedeemOutcome outcome) => outcome switch
    {
        CodeRedeemOutcome.Enrolled => "Enrolled. This device now takes commands from the console.",
        CodeRedeemOutcome.AlreadyEnrolled => "This device is already enrolled.",
        CodeRedeemOutcome.Malformed => "A code is six letters and digits (no 0, O, 1, I or L).",
        CodeRedeemOutcome.Invalid => "That code is not valid. It may have been used already or " +
                                     "have expired -- ask for a new one.",
        CodeRedeemOutcome.NameTaken => "Another device is already enrolled under this name. The " +
                                       "code was not used; rename this device in the console first.",
        CodeRedeemOutcome.Rejected => "The hub refused this device's name. The code was not used.",
        CodeRedeemOutcome.NotStored => "Enrolled, but the device could not save its credential. " +
                                       "Try again with a new code.",
        _ => "The hub could not be reached. Check the network and try again; the code was not used.",
    };

    // ------------------------------------------------------------------ advanced

    /// <summary>Grey out the hub URL on a device owner that already has one -- see
    /// AgentConfig.IsHubUrlLocked for why it is not a setting there.
    ///
    /// Re-evaluated on every refresh rather than once in Bind, for the reason DeviceOwner.
    /// IsManaged is live: `adb shell dpm set-device-owner` can grant ownership while this screen
    /// is open. The field shows the hub actually in use, so a locked device says where it
    /// reports rather than showing an empty box that looks editable in spirit.</summary>
    private void ApplyHubLock()
    {
        var locked = AgentConfig.IsHubUrlLocked(DeviceOwner.IsManaged(this));
        if (_hubField.Enabled == !locked) return;
        _hubField.Enabled = !locked;
        if (locked) _hubField.Text = AgentConfig.HubBase;
        _hubLabel.Text = locked
            ? "Hub URL (set by provisioning, cannot be changed on this device)"
            : "Hub URL";
    }

    private void Save()
    {
        var hub = _hubField.Text?.Trim() ?? "";
        // Checked here as well as by the disabled field. The field is what a person sees; this
        // is what stops the value being written, whatever reached Save.
        if (hub.Length > 0 && AgentConfig.IsHubUrlLocked(DeviceOwner.IsManaged(this))
            && hub.TrimEnd('/') != AgentConfig.HubBase)
        {
            Toast.MakeText(this, "This device is managed: its hub URL was set when it was " +
                                 "provisioned and cannot be changed here.",
                ToastLength.Long)?.Show();
            _hubField.Text = AgentConfig.HubBase;
            return;
        }
        if (hub.Length > 0)
        {
            // Validated the same way the agent validates it, so a typo is refused here rather
            // than leaving a device that looks configured and reports nowhere.
            var before = AgentConfig.HubBase;
            AgentConfig.Configure(hub);
            if (AgentConfig.HubBase == before && hub.TrimEnd('/') != before)
            {
                Toast.MakeText(this, "That hub URL is not usable -- it needs to start with https://",
                    ToastLength.Long)?.Show();
                return;
            }
            _state.SaveHubBaseOverride(AgentConfig.HubBase);
        }

        var secret = _secretField.Text?.Trim() ?? "";
        if (secret.Length > 0)
        {
            if (!_state.SaveEnrollmentSecret(secret))
            {
                Toast.MakeText(this, "The secret could not be saved on this device",
                    ToastLength.Long)?.Show();
                return;
            }
            // Cleared from the field once stored. It is a fleet-wide credential and leaving it
            // on screen behind an unlocked phone is the easiest way for it to travel.
            _secretField.Text = "";
        }

        // The service re-reads both on its next enrollment attempt -- see
        // AndroidEnrollmentSecretSource, which is asked afresh every time for exactly this
        // reason. Restarting the service is still the fastest way to pick up a changed HUB,
        // which is read once at composition.
        AgentService.Start(this);

        Toast.MakeText(this, "Saved.", ToastLength.Short)?.Show();
        RefreshStatus();
    }

    // ------------------------------------------------------------------ the switches

    /// <summary>Open the system's battery optimisation screen.
    ///
    /// The LIST, not the "allow this app to ignore optimisation?" dialog. That dialog needs the
    /// REQUEST_IGNORE_BATTERY_OPTIMIZATIONS permission, which Play policy treats as a
    /// violation for most apps and which several OEMs ignore anyway; the list is available on
    /// every device and puts the person in the screen their own manufacturer respects. Falls
    /// back to the app's settings page on a device that has neither.</summary>
    private void OpenBatterySettings()
        => OpenFirst("No battery settings screen on this device",
            global::Android.Provider.Settings.ActionIgnoreBatteryOptimizationSettings,
            global::Android.Provider.Settings.ActionApplicationDetailsSettings);

    /// <summary>Open the usage-access screen -- roadmap #23 phase E.
    ///
    /// **The one permission a Device Owner cannot grant for you.** Usage access is an appop,
    /// not a dangerous runtime permission, so setPermissionGrantState does not reach it and no
    /// provisioning flow turns it on. Without it every screen-time BUDGET reads zero minutes
    /// used and therefore never fires -- which looks, from the console, exactly like a device
    /// nobody has been using. Curfews are unaffected; they need a clock, not a ledger.
    ///
    /// The list screen is opened rather than this app's row in it, because the per-app deep
    /// link is not public API and several OEMs do not honour it.</summary>
    private void OpenUsageAccessSettings()
        => OpenFirst("No usage access screen on this device",
            global::Android.Provider.Settings.ActionUsageAccessSettings,
            global::Android.Provider.Settings.ActionApplicationDetailsSettings);

    private void OpenFirst(string none, params string[] actions)
    {
        foreach (var action in actions)
        {
            try
            {
                var intent = new Intent(action);
                if (action == global::Android.Provider.Settings.ActionApplicationDetailsSettings)
                    intent.SetData(global::Android.Net.Uri.Parse("package:" + PackageName));
                StartActivity(intent);
                return;
            }
            catch { /* try the next one */ }
        }
        Toast.MakeText(this, none, ToastLength.Long)?.Show();
    }

    /// <summary>Get this device the location permission -- roadmap #23 phase B.
    ///
    /// **Not shaped like the battery and usage buttons, because location is not that kind of
    /// permission.** Those are special-access switches an app can only point somebody at;
    /// location is an ordinary runtime permission, so on an unmanaged device the platform puts
    /// the question directly in front of the person holding it, and on a managed one the
    /// Device Owner answers it without a dialog at all. Both paths end in the same place and
    /// this button takes whichever one this device has.
    ///
    /// **Both permissions are asked for together, because asking for FINE alone is a request
    /// Android 12 and later may ignore.** From API 31 approximate location is its own grant
    /// with its own button in the system dialog, and the pair is the documented way to reach
    /// either. AndroidLocationReader accepts whichever comes back.
    ///
    /// **The second refusal is final, so there is a fallback.** Once somebody has said no
    /// twice, requestPermissions returns immediately with no dialog -- a button that silently
    /// does nothing, on the one screen whose whole job is to make invisible states visible. See
    /// OnRequestPermissionsResult, which sends them to the app's own settings page instead.
    ///
    /// **Not asked automatically on launch, unlike the notification permission**, and the
    /// difference is what a refusal costs. A reflexive "Don't allow" on notifications loses a
    /// status display; on location it loses the feature, and it is close to irreversible
    /// without a walk through Settings. A technician who presses this button has read the row
    /// above it and decided.</summary>
    private void GrantLocation()
    {
        if (AndroidLocationReader.IsGranted(this))
        {
            Toast.MakeText(this, "Location is already granted on this device", ToastLength.Short)?.Show();
            return;
        }

        // A fully managed device does not need the dialog and must not be sent to Settings:
        // it can simply take the permission. Tried first so a technician provisioning a phone
        // by QR sees the row flip to granted while they are still holding it, rather than
        // finding out at the first locate weeks later.
        if (DeviceOwner.GrantLocation(this, Logger()) && AndroidLocationReader.IsGranted(this))
        {
            Toast.MakeText(this, "Location granted -- this device can be located",
                ToastLength.Long)?.Show();
            RefreshStatus();
            return;
        }

        RequestPermissions(
            [
                global::Android.Manifest.Permission.AccessFineLocation,
                global::Android.Manifest.Permission.AccessCoarseLocation,
            ],
            RequestLocation);
    }

    /// <summary>What the system said to a runtime permission request.</summary>
    public override void OnRequestPermissionsResult(
        int requestCode, string[] permissions, Permission[] grantResults)
    {
        base.OnRequestPermissionsResult(requestCode, permissions, grantResults);
        RefreshStatus();
        if (requestCode != RequestLocation) return;
        if (AndroidLocationReader.IsGranted(this)) return;

        // ShouldShowRequestPermissionRationale is false both before the first ask and after the
        // last one, which is normally the trap in this API. Here it cannot be: the request that
        // just returned WAS an ask, so false at this point means the only one it can mean --
        // the platform will not raise the dialog again, and the switch has moved to Settings.
        var permanent = !ShouldShowRequestPermissionRationale(
            global::Android.Manifest.Permission.AccessFineLocation);
        if (!permanent)
        {
            Toast.MakeText(this, "Location was not granted. This device cannot be located.",
                ToastLength.Long)?.Show();
            return;
        }

        Toast.MakeText(this, "Android will not ask again. Turn Location on for FleetHub Agent "
                             + "under Permissions.", ToastLength.Long)?.Show();
        OpenAppSettings();
    }

    /// <summary>The Notifications row's button: the dialog where Android will still show it,
    /// the app's notification settings where it will not (below API 33 there is no dialog,
    /// and after two refusals there is none either).</summary>
    private void RequestNotificationsOrSettings()
    {
        if (OperatingSystem.IsAndroidVersionAtLeast(33)
            && ShouldShowRequestPermissionRationale(global::Android.Manifest.Permission.PostNotifications))
        {
            RequestPermissions([global::Android.Manifest.Permission.PostNotifications],
                RequestPostNotifications);
            return;
        }
        try
        {
            var intent = new Intent(global::Android.Provider.Settings.ActionAppNotificationSettings);
            intent.PutExtra(global::Android.Provider.Settings.ExtraAppPackage, PackageName);
            StartActivity(intent);
        }
        catch (Exception)
        {
            OpenAppSettings();
        }
    }

    /// <summary>This app's own row in Settings. Shared by the paths above, each of which has
    /// already tried the thing that would have been better and been refused.</summary>
    private void OpenAppSettings()
    {
        try
        {
            var intent = new Intent(
                global::Android.Provider.Settings.ActionApplicationDetailsSettings);
            intent.SetData(global::Android.Net.Uri.Parse("package:" + PackageName));
            StartActivity(intent);
        }
        catch (Exception)
        {
            Toast.MakeText(this, "No app settings screen on this device", ToastLength.Long)?.Show();
        }
    }

    /// <summary>A logger for the one call on this screen that takes one. The service builds its
    /// own factory and this activity has no business sharing it -- a logger held across the two
    /// would outlive whichever of them stopped first.</summary>
    private static ILogger Logger() =>
        new LogcatLoggerProvider(LogLevel.Information).CreateLogger("DeviceOwner");

    private void RequestNotificationPermissionIfNeeded()
    {
        // OperatingSystem.IsAndroidVersionAtLeast rather than a Build.VERSION.SdkInt
        // comparison: both are correct at runtime, but only this one is understood by the
        // platform-compatibility analyzer, which otherwise reports CA1416 on the
        // API-33-only constant below. See the note on AgentService.StartInForeground.
        if (!OperatingSystem.IsAndroidVersionAtLeast(33)) return;
        if (CheckSelfPermission(global::Android.Manifest.Permission.PostNotifications) == Permission.Granted)
            return;

        RequestPermissions([global::Android.Manifest.Permission.PostNotifications], RequestPostNotifications);
    }
}
