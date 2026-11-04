using System.Security.Cryptography;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.GroupPolicy;
using TempMonitorAgent.State;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Roadmap #32: Group Policy registry values, written into the PC's local GPO since agent 3.49.
///
/// The silent failures this file exists to catch:
///   * the agent overwriting, or later deleting, a policy value it did not set -- one somebody
///     set in gpedit.msc, or a domain GPO's or Intune's in the registry -- with nothing in the
///     console to show;
///   * the agent fighting a value somebody changed after FleetHub set it, flipping it back
///     every pass;
///   * a domain override reported as applied, or a value Windows never applied reported as in
///     force;
///   * reading the registry straight after a save, before Windows applied it, and calling the
///     old value an override;
///   * a gpedit save landing mid-pass and the agent dropping a marker entry for a value still
///     in the file -- which no later pass would ever remove;
///   * an older agent's direct registry writes stranded by the move to the local GPO;
///   * a value written outside HKLM\SOFTWARE\Policies because the hub said so;
///   * a lost heartbeat swallowing the report that a value was refused;
///   * a reboot with a lost state file stripping every value before the hub resends;
///   * a User Configuration row written into HKLM's file, or onto the machine marker, or judged
///     against HKLM -- where a machine value at the same path would read as somebody else's;
///   * the User half's file read or rewritten on a PC that sets no User Configuration at all.
/// Everything runs against memory: a dictionary for HKLM, a byte array for Registry.pol, and a
/// fake Windows that applies the file to the dictionary when asked to refresh.
/// </summary>
public class GroupPolicyTests
{
    private const string Edge = @"SOFTWARE\Policies\Microsoft\Edge";

    private sealed class FakeStore : IRegistryValueStore
    {
        public readonly Dictionary<string, string> Values = new(StringComparer.OrdinalIgnoreCase);
        public readonly Dictionary<string, string[]> Multi = new(StringComparer.OrdinalIgnoreCase);
        public readonly List<string> Writes = [];

        private static string K(string subKey, string name) => $"{subKey}|{name}";

        public string? Get(string subKey, string name) =>
            Values.TryGetValue(K(subKey, name), out var v) ? v : null;

        public void Set(PolicyValue value)
        {
            Writes.Add($"value:{value.Path}\\{value.Name}");
            Values[K(value.Path, value.Name)] = value.Canonical;
        }

        public void Delete(string subKey, string name)
        {
            Writes.Add($"delete:{subKey}\\{name}");
            Values.Remove(K(subKey, name));
            Multi.Remove(K(subKey, name));
        }

        public string[]? GetMultiString(string subKey, string name) =>
            Multi.TryGetValue(K(subKey, name), out var v) ? v : null;

        public void SetMultiString(string subKey, string name, string[] value)
        {
            Writes.Add($"marker:{value.Length}");
            Multi[K(subKey, name)] = value;
        }

        public string[] Marker =>
            GetMultiString(GroupPolicyRunner.MarkerKey, GroupPolicyRunner.MarkerValue) ?? [];
    }

    /// <summary>Registry.pol as bytes, and Windows as far as these tests need it: on a
    /// refresh, the registry extension removes what it applied last time and is no longer in
    /// the file, writes what is, and then a domain GPO's values go on top.</summary>
    private sealed class FakeLocal(FakeStore registry) : ILocalGroupPolicy
    {
        public byte[]? Bytes;
        /// <summary>User\Registry.pol. Windows applies it to signed-in users' hives, which
        /// this fake does not model -- nor does the runner judge them.</summary>
        public byte[]? UserBytes;
        public int Saves;
        public int Refreshes;
        public int Forced;
        public int UserLoads;
        /// <summary>False: local policy processing is off, and a refresh changes nothing.</summary>
        public bool WindowsApplies = true;
        /// <summary>Somebody saves in gpedit between the next Load and its Save.</summary>
        public bool Interfere;
        public readonly Dictionary<string, string> Domain = new(StringComparer.OrdinalIgnoreCase);
        private HashSet<string> _applied = new(StringComparer.OrdinalIgnoreCase);

        public RegistryPolFile Load() => Load(PolicyAllowList.Machine);

        public RegistryPolFile Load(string cls)
        {
            if (cls == PolicyAllowList.User) UserLoads++;
            var bytes = cls == PolicyAllowList.User ? UserBytes : Bytes;
            return bytes is null ? RegistryPolFile.Empty() : RegistryPolFile.Parse(bytes);
        }

        public bool Save(string cls, RegistryPolFile file)
        {
            if (Interfere)
            {
                Interfere = false;
                return false;
            }
            var user = cls == PolicyAllowList.User;
            var bytes = user ? UserBytes : Bytes;
            var onDisk = bytes is null ? null : Convert.ToHexStringLower(SHA256.HashData(bytes));
            if (onDisk != file.LoadedHash) return false;
            if (user) UserBytes = file.ToBytes();
            else Bytes = file.ToBytes();
            Saves++;
            return true;
        }

        public void Refresh(bool force)
        {
            Refreshes++;
            if (force) Forced++;
            if (WindowsApplies) ApplyPolicy();
        }

        public void ApplyPolicy()
        {
            var now = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var (key, name, value) in Load().Values()) now[$"{key}|{name}"] = value;
            foreach (var gone in _applied.Where(k => !now.ContainsKey(k))) registry.Values.Remove(gone);
            foreach (var (k, v) in now)
            {
                if (v == "delete:") registry.Values.Remove(k);
                else registry.Values[k] = v;
            }
            foreach (var (k, v) in Domain) registry.Values[k] = v;
            _applied = [.. now.Where(kv => kv.Value != "delete:").Select(kv => kv.Key)];
        }

        public string? Held(string path, string name) => Load().Get(path, name);
        public string? HeldUser(string path, string name) => Load(PolicyAllowList.User).Get(path, name);
    }

    private sealed class FakeClock : TimeProvider
    {
        public DateTimeOffset Now = new(2026, 10, 30, 12, 0, 0, TimeSpan.Zero);
        public override DateTimeOffset GetUtcNow() => Now;
        public void Advance(int seconds) => Now = Now.AddSeconds(seconds);
    }

    private sealed record Rig(GroupPolicyRunner Runner, FakeStore Store, FakeLocal Local, FakeClock Clock)
    {
        /// <summary>A pass long enough after the last refresh for the registry to count.</summary>
        public void SettledTick()
        {
            Clock.Advance(GroupPolicyRunner.SettleSeconds + 1);
            Runner.Tick();
        }

        public GroupPolicyRunner Restart() =>
            new(NullLogger<GroupPolicyRunner>.Instance, new AgentState(), Store, Local, Clock);
    }

    private static PolicyValue Dword(string path, string name, uint value) => new()
    {
        Id = PolicyAllowList.IdFor(path, name), Path = path, Name = name,
        Type = PolicyAllowList.Dword, Dword = value,
    };

    private static PolicyValue Text(string path, string name, string value) => new()
    {
        Id = PolicyAllowList.IdFor(path, name), Path = path, Name = name,
        Type = PolicyAllowList.String, Text = value,
    };

    private static PolicyValue User(PolicyValue value) => value with
    {
        Class = PolicyAllowList.User,
        Id = PolicyAllowList.IdFor(value.Path, value.Name, PolicyAllowList.User),
    };

    private string[] UserMarker(FakeStore store) =>
        store.GetMultiString(GroupPolicyRunner.MarkerKey, GroupPolicyRunner.UserMarkerValue) ?? [];

    private static PolicyValue Del(string path, string name) => new()
    {
        Id = PolicyAllowList.IdFor(path, name), Path = path, Name = name,
        Type = PolicyAllowList.Delete,
    };

    private static Rig NewRig(FakeStore? store = null)
    {
        if (File.Exists(AgentConfig.GroupPolicyStatePath)) File.Delete(AgentConfig.GroupPolicyStatePath);
        GroupPolicyReporter.Reset();
        store ??= new FakeStore();
        var local = new FakeLocal(store);
        var clock = new FakeClock();
        return new Rig(new GroupPolicyRunner(NullLogger<GroupPolicyRunner>.Instance, new AgentState(),
                                             store, local, clock),
                       store, local, clock);
    }

    /// <summary>A Registry.pol somebody wrote with gpedit, holding these values.</summary>
    private static byte[] GpeditFile(params PolicyValue[] values)
    {
        var file = RegistryPolFile.Empty();
        foreach (var value in values) file.Set(value);
        return file.ToBytes();
    }

    private static PolicyDocument Doc(params PolicyValue[] values) =>
        new() { Version = $"v{values.Length}", Values = values };

    private static Dictionary<string, (string Status, string Detail)> Reported()
    {
        var payload = GroupPolicyReporter.TakeIfChanged();
        var result = new Dictionary<string, (string, string)>();
        foreach (var node in payload?["states"]?.AsArray() ?? [])
            result[node!["id"]!.GetValue<string>()] =
                (node["status"]!.GetValue<string>(), node["detail"]!.GetValue<string>());
        if (payload is not null) GroupPolicyReporter.AckSent(payload);
        return result;
    }

    // --- the document -------------------------------------------------------

    [Fact]
    public void An_absent_document_is_nothing_to_say_and_an_empty_one_is_a_document()
    {
        Assert.Null(PolicyDocument.Parse(null, "v"));
        Assert.Null(PolicyDocument.Parse(new JsonObject(), "v"));
        var empty = PolicyDocument.Parse(new JsonObject { ["values"] = new JsonArray() }, "v1");
        Assert.NotNull(empty);
        Assert.Empty(empty!.Values);
    }

    [Fact]
    public void A_bad_entry_is_dropped_not_defaulted()
    {
        var doc = PolicyDocument.Parse(JsonNode.Parse("""
            {"values": [
              {"path": "SOFTWARE\\Policies\\X", "name": "A", "type": "dword", "value": 1},
              {"path": "SOFTWARE\\Policies\\X", "name": "B", "type": "dword", "value": "nope"},
              {"path": "SOFTWARE\\Policies\\X", "name": "C", "type": "binary", "value": "00"},
              {"path": "SOFTWARE\\Policies\\X", "name": "D", "type": "multi_string", "value": ["a", "b"]}
            ]}
            """), "v");
        Assert.Equal(["A", "D"], doc!.Values.Select(v => v.Name));
        Assert.Equal("multi_string:a\0b", doc.Values[1].Canonical);
    }

    [Fact]
    public void The_id_matches_the_hub()
    {
        // gpo.setting_id("SOFTWARE\\Policies\\Microsoft\\Edge", "HomepageLocation") on the hub.
        Assert.Equal("332aecccfafb5f3d", PolicyAllowList.IdFor(Edge, "HomepageLocation"));
        Assert.Equal(PolicyAllowList.IdFor(Edge, "X"), PolicyAllowList.IdFor(Edge.ToUpperInvariant(), "x"));
    }

    // --- the allow-list -----------------------------------------------------

    [Theory]
    [InlineData(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Run")]
    [InlineData(@"SYSTEM\CurrentControlSet\Services\X")]
    [InlineData(@"SOFTWARE\Policies")]
    [InlineData(@"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU")]
    [InlineData(@"SOFTWARE\Policies\FleetHub")]
    [InlineData(@"SOFTWARE\Policies\..\Microsoft\Run")]
    [InlineData(@"SOFTWARE\Policies\X\\Y")]
    public void Paths_outside_the_policy_tree_or_owned_by_FleetHub_are_refused(string path)
    {
        Assert.NotNull(PolicyAllowList.Refusal(Dword(path, "V", 1)));
    }

    [Fact]
    public void A_policy_path_is_allowed()
    {
        Assert.Null(PolicyAllowList.Refusal(Dword(Edge, "HideFirstRunExperience", 1)));
        Assert.NotNull(PolicyAllowList.Refusal(Dword(Edge, "", 1)));
    }

    // --- the runner ---------------------------------------------------------

    [Fact]
    public void Nothing_happens_before_a_document_has_ever_been_held()
    {
        var store = new FakeStore();
        store.Multi[$"{GroupPolicyRunner.MarkerKey}|{GroupPolicyRunner.MarkerValue}"] = [$"{Edge}|A"];
        var rig = NewRig(store);
        rig.Local.Bytes = GpeditFile(Dword(Edge, "A", 1));
        rig.Runner.Tick();
        // A lost state file must not read as "remove everything" before the hub resends.
        Assert.Equal("dword:1", rig.Local.Held(Edge, "A"));
        Assert.Single(store.Marker);
        Assert.Equal(0, rig.Local.Saves);
    }

    [Fact]
    public void A_new_value_goes_into_the_local_GPO_with_the_marker_first_and_Windows_applies_it()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "HideFirstRunExperience", 1)));
        rig.Runner.Tick();
        Assert.Equal("dword:1", rig.Local.Held(Edge, "HideFirstRunExperience"));
        Assert.Equal("marker:1", rig.Store.Writes[0]);
        // Never written to the registry by the agent: Windows did that, from the file.
        Assert.DoesNotContain(rig.Store.Writes, w => w.StartsWith("value:"));
        Assert.Equal("dword:1", rig.Store.Get(Edge, "HideFirstRunExperience"));
        Assert.Equal([$"{Edge}|HideFirstRunExperience"], rig.Store.Marker);
        Assert.Equal((1, 0), (rig.Local.Refreshes, rig.Local.Forced));
        Assert.Equal(GroupPolicyRunner.Applied,
                     Reported()[PolicyAllowList.IdFor(Edge, "HideFirstRunExperience")].Status);
    }

    [Fact]
    public void A_pass_that_changes_nothing_writes_nothing()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        rig.Runner.Tick();
        rig.SettledTick();
        Assert.Equal((1, 1), (rig.Local.Saves, rig.Local.Refreshes));
    }

    [Fact]
    public void A_value_somebody_set_in_gpedit_is_never_touched()
    {
        var rig = NewRig();
        rig.Local.Bytes = GpeditFile(Text(Edge, "HomepageLocation", "https://intranet"));
        var before = rig.Local.Bytes;
        rig.Runner.Apply(Doc(Text(Edge, "HomepageLocation", "https://fleethub")));
        rig.Runner.Tick();
        Assert.Same(before, rig.Local.Bytes);
        Assert.Empty(rig.Store.Marker);
        var (status, detail) = Reported()[PolicyAllowList.IdFor(Edge, "HomepageLocation")];
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere, status);
        Assert.Contains("local Group Policy", detail);
    }

    [Fact]
    public void A_gpedit_instruction_about_the_value_counts_as_somebody_elses()
    {
        var rig = NewRig();
        var gpedit = RegistryPolFile.Empty();
        gpedit.Set(Text(Edge, "**del.HomepageLocation", " "));
        rig.Local.Bytes = gpedit.ToBytes();
        rig.Runner.Apply(Doc(Text(Edge, "HomepageLocation", "https://fleethub")));
        rig.Runner.Tick();
        Assert.Equal(0, rig.Local.Saves);
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere,
                     Reported()[PolicyAllowList.IdFor(Edge, "HomepageLocation")].Status);
    }

    [Fact]
    public void A_value_somebody_else_wrote_to_the_registry_is_never_touched()
    {
        var rig = NewRig();
        rig.Store.Values[$"{Edge}|HomepageLocation"] = "string:https://intranet";
        rig.Runner.Apply(Doc(Text(Edge, "HomepageLocation", "https://fleethub")));
        rig.Runner.Tick();
        Assert.Null(rig.Local.Bytes);
        Assert.Equal("string:https://intranet", rig.Store.Get(Edge, "HomepageLocation"));
        Assert.Empty(rig.Store.Marker);
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere,
                     Reported()[PolicyAllowList.IdFor(Edge, "HomepageLocation")].Status);
    }

    [Fact]
    public void A_value_changed_in_gpedit_after_we_set_it_is_let_go()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        var edited = rig.Local.Load();
        edited.Set(Dword(Edge, "A", 0));             // the PC's administrator, in gpedit.msc
        rig.Local.Bytes = edited.ToBytes();
        rig.Runner.Tick();
        Assert.Equal("dword:0", rig.Local.Held(Edge, "A"));
        Assert.Empty(rig.Store.Marker);
        rig.Runner.Tick();                            // and is not fought on later passes
        Assert.Equal("dword:0", rig.Local.Held(Edge, "A"));
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
    }

    [Fact]
    public void A_value_removed_from_the_local_GPO_is_put_back()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        var edited = rig.Local.Load();
        edited.Delete(Edge, "A");                     // "Not configured" in gpedit.msc
        rig.Local.Bytes = edited.ToBytes();
        rig.Runner.Tick();
        Assert.Equal("dword:1", rig.Local.Held(Edge, "A"));
    }

    [Fact]
    public void A_changed_policy_rewrites_our_entry()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 2)));
        rig.Runner.Tick();
        Assert.Equal("dword:2", rig.Local.Held(Edge, "A"));
        Assert.Equal("dword:2", rig.Store.Get(Edge, "A"));
        Assert.Single(rig.Store.Marker);
        Assert.Single(rig.Local.Load().Values());
    }

    [Fact]
    public void An_empty_document_removes_our_entry_and_Windows_removes_the_value()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        Reported();
        rig.Runner.Apply(Doc());
        rig.Runner.Tick();
        Assert.Null(rig.Local.Held(Edge, "A"));
        Assert.Null(rig.Store.Get(Edge, "A"));
        Assert.Empty(rig.Store.Marker);
        var payload = GroupPolicyReporter.TakeIfChanged();
        Assert.Contains(PolicyAllowList.IdFor(Edge, "A"),
                        payload!["removed"]!.AsArray().Select(n => n!.GetValue<string>()));
    }

    [Fact]
    public void Removal_leaves_a_value_somebody_changed_meanwhile()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1), Dword(Edge, "B", 1)));
        rig.Runner.Tick();
        rig.Runner.Apply(Doc(Dword(Edge, "B", 1)));
        // Changed in gpedit between the document arriving and the pass that would remove it.
        var edited = rig.Local.Load();
        edited.Set(Dword(Edge, "A", 7));
        rig.Local.Bytes = edited.ToBytes();
        rig.Runner.Tick();
        Assert.Equal("dword:7", rig.Local.Held(Edge, "A"));
        Assert.Equal([$"{Edge}|B"], rig.Store.Marker);
    }

    [Fact]
    public void A_gpedit_save_mid_pass_keeps_the_released_entry_marked_until_it_is_gone()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1), Dword(Edge, "B", 1)));
        rig.Runner.Tick();
        rig.Runner.Apply(Doc(Dword(Edge, "B", 1)));
        rig.Local.Interfere = true;
        rig.Runner.Tick();
        // Not saved, so A is still in the file -- and must still be on the marker, or no
        // later pass would ever take it out.
        Assert.Equal("dword:1", rig.Local.Held(Edge, "A"));
        Assert.Contains($"{Edge}|A", rig.Store.Marker);
        rig.Runner.Tick();
        Assert.Null(rig.Local.Held(Edge, "A"));
        Assert.Equal([$"{Edge}|B"], rig.Store.Marker);
    }

    [Fact]
    public void A_domain_override_is_reported_once_Windows_has_had_time_and_stays_in_the_file()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Local.Domain[$"{Edge}|A"] = "dword:0";
        rig.Runner.Tick();
        // Straight after the save the registry is not yet believed.
        Assert.Equal(GroupPolicyRunner.Applied, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
        rig.SettledTick();
        var (status, detail) = Reported()[PolicyAllowList.IdFor(Edge, "A")];
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere, status);
        Assert.Contains("domain", detail);
        // Not fought and not let go: the local value takes over if the domain policy goes.
        Assert.Equal("dword:1", rig.Local.Held(Edge, "A"));
        Assert.Single(rig.Store.Marker);
        rig.Local.Domain.Clear();
        rig.Local.ApplyPolicy();
        rig.SettledTick();
        Assert.Equal(GroupPolicyRunner.Applied, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
    }

    [Fact]
    public void A_value_Windows_never_applies_is_an_error_and_forces_at_most_one_refresh_an_hour()
    {
        var rig = NewRig();
        rig.Local.WindowsApplies = false;            // local policy processing switched off
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        rig.SettledTick();
        Assert.Equal(GroupPolicyRunner.Error, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
        Assert.Equal(1, rig.Local.Forced);
        rig.SettledTick();
        rig.SettledTick();
        Assert.Equal(1, rig.Local.Forced);
        rig.Clock.Advance(GroupPolicyRunner.RepairSeconds);
        rig.Runner.Tick();
        Assert.Equal(2, rig.Local.Forced);
    }

    [Fact]
    public void A_delete_row_parses_and_is_allowed()
    {
        var doc = PolicyDocument.Parse(JsonNode.Parse("""
            {"values": [{"path": "SOFTWARE\\Policies\\X", "name": "A", "type": "delete", "value": null}]}
            """), "v");
        Assert.Equal("delete:", doc!.Values.Single().Canonical);
        Assert.Null(PolicyAllowList.Refusal(doc.Values[0]));
    }

    [Fact]
    public void A_delete_goes_into_the_local_GPO_and_Windows_removes_the_value()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Del(Edge, "A")));
        rig.Runner.Tick();
        Assert.Equal("delete:", rig.Local.Held(Edge, "A"));
        Assert.Equal([$"{Edge}|A"], rig.Store.Marker);
        rig.SettledTick();
        Assert.Equal(GroupPolicyRunner.Applied, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
        Assert.Equal(1, rig.Local.Saves);
    }

    [Fact]
    public void Enabled_turning_Disabled_swaps_our_value_for_a_delete_and_release_takes_both_out()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        Assert.Equal("dword:1", rig.Store.Get(Edge, "A"));
        rig.Runner.Apply(Doc(Del(Edge, "A")));
        rig.Runner.Tick();
        Assert.Equal(new[] { (Edge, "A", "delete:") }, rig.Local.Load().Values().ToArray());
        Assert.Null(rig.Store.Get(Edge, "A"));
        rig.Runner.Release();
        Assert.Empty(rig.Local.Load().Values());
        Assert.Empty(rig.Store.Marker);
    }

    [Fact]
    public void A_delete_a_domain_policy_overrides_is_reported_once_settled()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Del(Edge, "A")));
        rig.Local.Domain[$"{Edge}|A"] = "dword:1";
        rig.Runner.Tick();
        rig.SettledTick();
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
        Assert.Equal("delete:", rig.Local.Held(Edge, "A"));
    }

    [Fact]
    public void An_older_agents_direct_write_is_moved_into_the_local_GPO()
    {
        var store = new FakeStore();
        store.Multi[$"{GroupPolicyRunner.MarkerKey}|{GroupPolicyRunner.MarkerValue}"] = [$"{Edge}|A"];
        store.Values[$"{Edge}|A"] = "dword:1";
        var rig = NewRig(store);
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        Assert.Equal("dword:1", rig.Local.Held(Edge, "A"));
        Assert.Equal("dword:1", store.Get(Edge, "A"));
        Assert.Equal([$"{Edge}|A"], store.Marker);
        Assert.Equal(GroupPolicyRunner.Applied, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
    }

    [Fact]
    public void An_older_agents_direct_write_no_longer_wanted_is_deleted_from_the_registry()
    {
        var store = new FakeStore();
        store.Multi[$"{GroupPolicyRunner.MarkerKey}|{GroupPolicyRunner.MarkerValue}"] = [$"{Edge}|A"];
        store.Values[$"{Edge}|A"] = "dword:1";
        var rig = NewRig(store);
        rig.Runner.Apply(Doc());
        rig.Runner.Tick();
        // No GPO entry will ever make Windows remove it, so the agent does.
        Assert.Null(store.Get(Edge, "A"));
        Assert.Empty(store.Marker);
        Assert.Null(rig.Local.Bytes);
    }

    [Fact]
    public void An_unreadable_Registry_pol_is_refused_not_rewritten()
    {
        var rig = NewRig();
        rig.Local.Bytes = [0x50, 0x52, 0x65, 0x67, 1, 0, 0, 0, (byte)'[', 0, (byte)'S'];
        var before = rig.Local.Bytes;
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        Assert.Same(before, rig.Local.Bytes);
        Assert.Empty(rig.Store.Marker);
        Assert.Equal(GroupPolicyRunner.Error, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
    }

    [Fact]
    public void A_value_outside_the_allow_list_is_refused_and_not_written()
    {
        var rig = NewRig();
        var bad = Dword(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "Evil", 1);
        rig.Runner.Apply(Doc(bad));
        rig.Runner.Tick();
        Assert.Null(rig.Local.Bytes);
        Assert.Empty(rig.Store.Values);
        Assert.Equal(GroupPolicyRunner.Refused, Reported()[bad.Id].Status);
    }

    [Fact]
    public void Release_takes_our_values_out_of_the_local_GPO_and_leaves_gpedits()
    {
        var rig = NewRig();
        rig.Local.Bytes = GpeditFile(Dword(Edge, "Theirs", 5));
        rig.Runner.Apply(Doc(Dword(Edge, "Ours", 1), Dword(Edge, "Theirs", 1)));
        rig.Runner.Tick();
        rig.Runner.Release();
        Assert.Null(rig.Local.Held(Edge, "Ours"));
        Assert.Null(rig.Store.Get(Edge, "Ours"));
        Assert.Equal("dword:5", rig.Local.Held(Edge, "Theirs"));
        Assert.Empty(rig.Store.Marker);
    }

    [Fact]
    public void Release_retries_a_gpedit_save_rather_than_dropping_the_marker()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        rig.Local.Interfere = true;
        rig.Runner.Release();
        Assert.Null(rig.Local.Held(Edge, "A"));
        Assert.Empty(rig.Store.Marker);
    }

    [Fact]
    public void The_document_and_what_was_written_survive_a_restart()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        var restarted = rig.Restart();
        Assert.Equal("v1", restarted.Version);
        var edited = rig.Local.Load();
        edited.Set(Dword(Edge, "A", 9));
        rig.Local.Bytes = edited.ToBytes();
        restarted.Tick();
        // It still knows it wrote 1, so 9 is somebody else's and is let go rather than fought.
        Assert.Equal("dword:9", rig.Local.Held(Edge, "A"));
        Assert.Empty(rig.Store.Marker);
    }

    // --- User Configuration (phase 3) ---------------------------------------

    [Fact]
    public void A_user_row_parses_with_the_hubs_id_and_an_unknown_class_is_dropped()
    {
        var doc = PolicyDocument.Parse(JsonNode.Parse("""
            {"values": [
              {"path": "SOFTWARE\\Policies\\Microsoft\\Edge", "name": "HomepageLocation", "type": "string", "value": "x", "class": "user"},
              {"path": "SOFTWARE\\Policies\\Microsoft\\Edge", "name": "HomepageLocation", "type": "string", "value": "x"},
              {"path": "SOFTWARE\\Policies\\Microsoft\\Edge", "name": "Other", "type": "dword", "value": 1, "class": "group"}
            ]}
            """), "v");
        Assert.Equal(2, doc!.Values.Count);
        // gpo.setting_id(path, "HomepageLocation", "user") on the hub.
        Assert.Equal(("user", "8271643f7b465887"), (doc.Values[0].Class, doc.Values[0].Id));
        // And the machine id has not moved.
        Assert.Equal(("machine", "332aecccfafb5f3d"), (doc.Values[1].Class, doc.Values[1].Id));
    }

    [Fact]
    public void A_user_value_goes_into_User_Registry_pol_with_its_own_marker_and_no_refresh()
    {
        var rig = NewRig();
        var value = User(Dword(Edge, "A", 1));
        rig.Runner.Apply(Doc(value));
        rig.Runner.Tick();
        Assert.Equal("dword:1", rig.Local.HeldUser(Edge, "A"));
        Assert.Null(rig.Local.Bytes);
        Assert.Empty(rig.Store.Marker);
        Assert.Equal([$"{Edge}|A"], UserMarker(rig.Store));
        // Windows applies it at sign-in; there is nothing for a service to refresh.
        Assert.Equal(0, rig.Local.Refreshes);
        Assert.Equal(GroupPolicyRunner.Applied, Reported()[value.Id].Status);
    }

    [Fact]
    public void A_machine_and_a_user_value_at_one_path_are_two_values()
    {
        var rig = NewRig();
        var user = User(Dword(Edge, "A", 2));
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1), user));
        rig.Runner.Tick();
        Assert.Equal("dword:1", rig.Local.Held(Edge, "A"));
        Assert.Equal("dword:2", rig.Local.HeldUser(Edge, "A"));
        rig.SettledTick();
        var reported = Reported();
        Assert.Equal(GroupPolicyRunner.Applied, reported[PolicyAllowList.IdFor(Edge, "A")].Status);
        // The machine value in HKLM is not "somebody else's" to the user row.
        Assert.Equal(GroupPolicyRunner.Applied, reported[user.Id].Status);
        // Dropping the user row leaves the machine one where it is.
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        Assert.Null(rig.Local.HeldUser(Edge, "A"));
        Assert.Equal("dword:1", rig.Local.Held(Edge, "A"));
        Assert.Empty(UserMarker(rig.Store));
    }

    [Fact]
    public void A_user_value_somebody_set_in_gpedit_is_left_alone_and_one_changed_is_let_go()
    {
        var rig = NewRig();
        rig.Local.UserBytes = GpeditFile(Text(Edge, "Theirs", "gpedit"));
        var theirs = User(Text(Edge, "Theirs", "fleethub"));
        var ours = User(Dword(Edge, "Ours", 1));
        rig.Runner.Apply(Doc(theirs, ours));
        rig.Runner.Tick();
        Assert.Equal("string:gpedit", rig.Local.HeldUser(Edge, "Theirs"));
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere, Reported()[theirs.Id].Status);
        var edited = rig.Local.Load(PolicyAllowList.User);
        edited.Set(User(Dword(Edge, "Ours", 0)));
        rig.Local.UserBytes = edited.ToBytes();
        rig.Runner.Tick();
        Assert.Equal("dword:0", rig.Local.HeldUser(Edge, "Ours"));
        Assert.Empty(UserMarker(rig.Store));
    }

    [Fact]
    public void A_PC_with_no_User_Configuration_never_reads_the_user_file()
    {
        var rig = NewRig();
        rig.Local.UserBytes = [0xBA, 0xD0];          // unreadable, and none of FleetHub's business
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1)));
        rig.Runner.Tick();
        rig.SettledTick();
        Assert.Equal(0, rig.Local.UserLoads);
        Assert.Equal(GroupPolicyRunner.Applied, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
    }

    [Fact]
    public void An_unreadable_user_file_costs_only_the_user_half()
    {
        var rig = NewRig();
        rig.Local.UserBytes = [0xBA, 0xD0];
        var user = User(Dword(Edge, "U", 1));
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1), user));
        rig.Runner.Tick();
        var reported = Reported();
        Assert.Equal(GroupPolicyRunner.Error, reported[user.Id].Status);
        Assert.Equal(GroupPolicyRunner.Applied, reported[PolicyAllowList.IdFor(Edge, "A")].Status);
        Assert.Equal([0xBA, 0xD0], rig.Local.UserBytes);
    }

    [Fact]
    public void Release_takes_both_halves_out_and_drops_both_markers()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1), User(Dword(Edge, "U", 1))));
        rig.Runner.Tick();
        rig.Runner.Release();
        Assert.Null(rig.Local.Held(Edge, "A"));
        Assert.Null(rig.Local.HeldUser(Edge, "U"));
        Assert.Empty(rig.Store.Marker);
        Assert.Empty(UserMarker(rig.Store));
    }

    [Fact]
    public void User_rows_are_persisted_where_an_older_agent_never_reads_them()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(Dword(Edge, "A", 1), User(Dword(Edge, "U", 2))));
        // What an agent before 3.49 reads of the state file: Values, with no notion of class.
        var stored = new AgentState().LoadGroupPolicyState()!;
        Assert.Equal(["A"], stored.Values!.Select(v => v.Name));
        Assert.Equal(["U"], stored.UserValues!.Select(v => v.Name));
        // And this agent reads both back, each with its own class and id.
        var restarted = rig.Restart();
        restarted.Tick();
        Assert.Equal("dword:1", rig.Local.Held(Edge, "A"));
        Assert.Equal("dword:2", rig.Local.HeldUser(Edge, "U"));
    }

    [Fact]
    public void A_user_value_survives_a_restart_on_its_own_written_record()
    {
        var rig = NewRig();
        rig.Runner.Apply(Doc(User(Dword(Edge, "A", 1))));
        rig.Runner.Tick();
        var restarted = rig.Restart();
        var edited = rig.Local.Load(PolicyAllowList.User);
        edited.Set(User(Dword(Edge, "A", 9)));
        rig.Local.UserBytes = edited.ToBytes();
        restarted.Tick();
        // It still knows it wrote 1 to the user file, so 9 is somebody else's.
        Assert.Equal("dword:9", rig.Local.HeldUser(Edge, "A"));
        Assert.Empty(UserMarker(rig.Store));
    }

    // --- the reporter -------------------------------------------------------

    [Fact]
    public void A_report_is_kept_until_the_hub_acknowledges_it()
    {
        GroupPolicyReporter.Reset();
        GroupPolicyReporter.Record("abc", GroupPolicyRunner.Refused, "outside the tree");
        var first = GroupPolicyReporter.TakeIfChanged();
        Assert.NotNull(first);
        // The heartbeat failed: nothing was acknowledged, so it is offered again.
        var second = GroupPolicyReporter.TakeIfChanged();
        Assert.NotNull(second);
        GroupPolicyReporter.AckSent(second!);
        Assert.Null(GroupPolicyReporter.TakeIfChanged());
        // An unchanged state is not re-sent; a changed one is.
        GroupPolicyReporter.Record("abc", GroupPolicyRunner.Refused, "outside the tree");
        Assert.Null(GroupPolicyReporter.TakeIfChanged());
        GroupPolicyReporter.Record("abc", GroupPolicyRunner.Applied, "");
        Assert.NotNull(GroupPolicyReporter.TakeIfChanged());
    }

    [Fact]
    public void A_state_that_changed_after_it_was_taken_stays_pending()
    {
        GroupPolicyReporter.Reset();
        GroupPolicyReporter.Record("abc", GroupPolicyRunner.Error, "access denied");
        var taken = GroupPolicyReporter.TakeIfChanged()!;
        GroupPolicyReporter.Record("abc", GroupPolicyRunner.Applied, "");
        GroupPolicyReporter.AckSent(taken);
        var next = GroupPolicyReporter.TakeIfChanged();
        Assert.Equal(GroupPolicyRunner.Applied,
                     next!["states"]![0]!["status"]!.GetValue<string>());
    }
}
