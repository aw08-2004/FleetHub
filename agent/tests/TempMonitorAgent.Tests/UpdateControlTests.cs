using TempMonitorAgent.Patch;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Issue #140: FleetHub as the only thing that installs Windows updates.
///
/// The silent failure this file exists to catch is the agent touching a Windows Update policy
/// value it did not write. These keys are exactly where GPO and Intune put theirs; an agent
/// that overwrote one would quietly undo an administrator's decision, and one that deleted
/// one on release would do it a second time, with nothing in the console to show for it.
/// Everything here runs against a dictionary standing in for HKLM.
/// </summary>
public class UpdateControlTests
{
    private const string AuKey = UpdateControl.PolicyRoot + @"\AU";

    private sealed class FakeStore : UpdateControl.IPolicyStore
    {
        public readonly Dictionary<(string, string), object> Values = new();
        public readonly List<string> Writes = [];

        public int? GetDword(string subKey, string name) =>
            Values.TryGetValue((subKey, name), out var v) ? v as int? ?? -1 : null;

        public void SetDword(string subKey, string name, int value)
        {
            Writes.Add($"{subKey}\\{name}");
            Values[(subKey, name)] = value;
        }

        public string[]? GetMultiString(string subKey, string name) =>
            Values.TryGetValue((subKey, name), out var v) ? v as string[] : null;

        public void SetMultiString(string subKey, string name, string[] value)
        {
            Writes.Add($"{subKey}\\{name}");
            Values[(subKey, name)] = value;
        }

        public void DeleteValue(string subKey, string name) => Values.Remove((subKey, name));

        public string[] Marker =>
            GetMultiString(UpdateControl.MarkerKey, UpdateControl.MarkerValue) ?? [];
    }

    [Fact]
    public void A_clean_machine_is_windows_controlled()
    {
        Assert.Equal(UpdateControl.Windows, new UpdateControl(new FakeStore()).State());
    }

    [Fact]
    public void Apply_writes_both_values_and_records_them_in_the_marker()
    {
        var store = new FakeStore();
        var state = new UpdateControl(store).Apply();

        Assert.Equal(UpdateControl.FleetHub, state);
        Assert.Equal(1, store.GetDword(AuKey, "NoAutoUpdate"));
        Assert.Equal(1, store.GetDword(UpdateControl.PolicyRoot, "SetDisableUXWUAccess"));
        Assert.Equal(["AU\\NoAutoUpdate", "SetDisableUXWUAccess"], store.Marker.Order());
    }

    [Fact]
    public void The_marker_is_written_before_any_policy_value()
    {
        // The reverse order, interrupted, leaves a value with no marker entry -- which every
        // later pass reads as somebody else's, so the PC is managed_elsewhere forever.
        var store = new FakeStore();
        new UpdateControl(store).Apply();
        Assert.Equal($"{UpdateControl.MarkerKey}\\{UpdateControl.MarkerValue}", store.Writes[0]);
    }

    [Theory]
    [InlineData(1)] // GPO also wants auto-update off -- still theirs, not ours.
    [InlineData(0)]
    public void An_unmarked_policy_value_is_managed_elsewhere_and_nothing_is_written(int existing)
    {
        var store = new FakeStore();
        store.Values[(AuKey, "NoAutoUpdate")] = existing;

        var state = new UpdateControl(store).Apply();

        Assert.Equal(UpdateControl.ManagedElsewhere, state);
        Assert.Empty(store.Writes);
        Assert.Equal(existing, store.GetDword(AuKey, "NoAutoUpdate"));
        Assert.Null(store.GetDword(UpdateControl.PolicyRoot, "SetDisableUXWUAccess"));
    }

    [Fact]
    public void A_value_of_the_wrong_type_still_counts_as_existing()
    {
        // A REG_SZ "1" somebody pushed by hand is still somebody's. Reading it as absent
        // would let Apply overwrite it.
        var store = new FakeStore();
        store.Values[(UpdateControl.PolicyRoot, "SetDisableUXWUAccess")] = "1";
        Assert.Equal(UpdateControl.ManagedElsewhere, new UpdateControl(store).Apply());
        Assert.Empty(store.Writes);
    }

    [Fact]
    public void Re_applying_is_idempotent()
    {
        var store = new FakeStore();
        var control = new UpdateControl(store);
        control.Apply();
        var marker = store.Marker;

        Assert.Equal(UpdateControl.FleetHub, control.Apply());
        Assert.Equal(marker.Order(), store.Marker.Order());
        Assert.Equal(1, store.GetDword(AuKey, "NoAutoUpdate"));
    }

    [Fact]
    public void Apply_repairs_a_marked_value_that_was_deleted()
    {
        // Somebody removed NoAutoUpdate by hand. It is on our marker, so it was ours, and
        // putting it back is the reconcile loop doing its job -- not managed_elsewhere.
        var store = new FakeStore();
        var control = new UpdateControl(store);
        control.Apply();
        store.DeleteValue(AuKey, "NoAutoUpdate");

        Assert.Equal(UpdateControl.Windows, control.State());
        Assert.Equal(UpdateControl.FleetHub, control.Apply());
    }

    [Fact]
    public void A_marked_value_taken_over_by_policy_is_managed_elsewhere()
    {
        // A GPO arrived after us and set NoAutoUpdate=0. It owns the value now.
        var store = new FakeStore();
        var control = new UpdateControl(store);
        control.Apply();
        store.Values[(AuKey, "NoAutoUpdate")] = 0;

        Assert.Equal(UpdateControl.ManagedElsewhere, control.State());
        Assert.Equal(UpdateControl.ManagedElsewhere, control.Apply());
        Assert.Equal(0, store.GetDword(AuKey, "NoAutoUpdate"));
    }

    [Fact]
    public void Release_removes_our_values_and_the_marker()
    {
        var store = new FakeStore();
        var control = new UpdateControl(store);
        control.Apply();

        Assert.Equal(UpdateControl.Windows, control.Release());
        Assert.Empty(store.Values);
    }

    [Fact]
    public void Release_never_touches_a_value_it_did_not_write()
    {
        var store = new FakeStore();
        store.Values[(AuKey, "NoAutoUpdate")] = 1;
        store.Values[(UpdateControl.PolicyRoot, "SetDisableUXWUAccess")] = 1;

        new UpdateControl(store).Release();

        Assert.Equal(1, store.GetDword(AuKey, "NoAutoUpdate"));
        Assert.Equal(1, store.GetDword(UpdateControl.PolicyRoot, "SetDisableUXWUAccess"));
    }

    [Fact]
    public void Release_leaves_a_marked_value_somebody_else_has_since_changed()
    {
        var store = new FakeStore();
        var control = new UpdateControl(store);
        control.Apply();
        store.Values[(AuKey, "NoAutoUpdate")] = 0;

        var state = control.Release();

        Assert.Equal(0, store.GetDword(AuKey, "NoAutoUpdate"));
        Assert.Null(store.GetDword(UpdateControl.PolicyRoot, "SetDisableUXWUAccess"));
        Assert.Empty(store.Marker);
        Assert.Equal(UpdateControl.ManagedElsewhere, state);
    }

    [Fact]
    public void Release_on_a_machine_never_controlled_is_a_no_op()
    {
        var store = new FakeStore();
        Assert.Equal(UpdateControl.Windows, new UpdateControl(store).Release());
        Assert.Empty(store.Values);
        Assert.Empty(store.Writes);
    }

    // ---- outside installs ---------------------------------------------------------------

    private static readonly DateTimeOffset Now = new(2026, 10, 19, 12, 0, 0, TimeSpan.Zero);

    private static UpdateHistory.Entry Installed(string title, string client, int daysAgo = 1) =>
        new(title, Now.AddDays(-daysAgo), client, Operation: 1, ResultCode: 2);

    [Fact]
    public void Outside_installs_exclude_fleethub_definitions_and_old_entries()
    {
        var rows = UpdateHistory.Select([
            Installed("2026-10 Cumulative Update for Windows 11 (KB5060842)", "UpdateOrchestrator"),
            Installed("2026-10 Cumulative Update for Windows 11 (KB5060843)", "FleetHub"),
            Installed("Security Intelligence Update for Microsoft Defender Antivirus - KB2267602",
                      "Windows Defender (77BDAF73-B396-481F-9042-AD358843EC24)"),
            Installed("Some update", "MoSetup"),
            Installed("Old update (KB5000001)", "UpdateOrchestrator", daysAgo: 31),
            new("Failed update (KB5000002)", Now.AddDays(-1), "UpdateOrchestrator", 1, 4),
            new("Uninstalled (KB5000003)", Now.AddDays(-1), "UpdateOrchestrator", 2, 2),
        ], Now);

        var row = Assert.Single(rows);
        Assert.Equal("KB5060842", row.Kb);
        Assert.Equal("UpdateOrchestrator", row.Client);
        Assert.Equal(Now.AddDays(-1).ToUnixTimeSeconds(), row.InstalledAt);
    }

    [Fact]
    public void Outside_installs_are_newest_first_and_capped()
    {
        var entries = Enumerable.Range(0, UpdateHistory.MaxRows + 10)
            .Select(i => new UpdateHistory.Entry($"Update {i}", Now.AddHours(-i), "User", 1, 2));
        var rows = UpdateHistory.Select(entries, Now);
        Assert.Equal(UpdateHistory.MaxRows, rows.Count);
        Assert.Equal("Update 0", rows[0].Title);
        Assert.Equal("", rows[0].Kb);
    }

    [Fact]
    public void Control_block_field_names_match_what_the_hub_ingests()
    {
        // The hub's reconcile loop is bound to these names and nothing else. A missing
        // `control` block reads as "agent too old", so the PC is never reconciled.
        var scan = new PatchScanner.Scan([], "", new PatchScanner.ControlReport(
            UpdateControl.ManagedElsewhere,
            [new UpdateHistory.OutsideInstall("Cumulative (KB5060842)", "KB5060842", 1760000000,
                                              "UpdateOrchestrator")]));
        var control = PatchInventoryReporter.ToPayload(scan)["control"]!.AsObject();
        Assert.Equal("managed_elsewhere", (string?)control["state"]);
        var row = control["outside_installs"]!.AsArray()[0]!.AsObject();
        Assert.Equal("Cumulative (KB5060842)", (string?)row["title"]);
        Assert.Equal("KB5060842", (string?)row["kb"]);
        Assert.Equal(1760000000, (long?)row["installed_at"]);
        Assert.Equal("UpdateOrchestrator", (string?)row["client"]);
    }
}
