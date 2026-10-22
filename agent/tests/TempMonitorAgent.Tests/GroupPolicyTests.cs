using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.GroupPolicy;
using TempMonitorAgent.State;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Roadmap #32: Group Policy registry values.
///
/// The silent failures this file exists to catch:
///   * the agent overwriting, or later deleting, a policy value it did not write -- a domain
///     GPO's or Intune's, in exactly the tree they use -- with nothing in the console to show;
///   * the agent fighting a GPO that took over a value FleetHub set first, flipping it back
///     every pass;
///   * a value written outside HKLM\SOFTWARE\Policies because the hub said so;
///   * a lost heartbeat swallowing the report that a value was refused;
///   * a reboot with a lost state file stripping every value before the hub resends.
/// Everything runs against a dictionary standing in for HKLM.
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

    private static (GroupPolicyRunner Runner, FakeStore Store) Runner(FakeStore? store = null)
    {
        if (File.Exists(AgentConfig.GroupPolicyStatePath)) File.Delete(AgentConfig.GroupPolicyStatePath);
        GroupPolicyReporter.Reset();
        store ??= new FakeStore();
        return (new GroupPolicyRunner(NullLogger<GroupPolicyRunner>.Instance, new AgentState(), store),
                store);
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
        store.Values[$"{Edge}|A"] = "dword:1";
        var (runner, _) = Runner(store);
        runner.Tick();
        // A lost state file must not read as "remove everything" before the hub resends.
        Assert.Equal("dword:1", store.Get(Edge, "A"));
        Assert.Single(store.Marker);
    }

    [Fact]
    public void A_new_value_is_written_with_the_marker_first()
    {
        var (runner, store) = Runner();
        runner.Apply(Doc(Dword(Edge, "HideFirstRunExperience", 1)));
        runner.Tick();
        Assert.Equal("dword:1", store.Get(Edge, "HideFirstRunExperience"));
        Assert.Equal("marker:1", store.Writes[0]);
        Assert.Equal([$"{Edge}|HideFirstRunExperience"], store.Marker);
        Assert.Equal(GroupPolicyRunner.Applied,
                     Reported()[PolicyAllowList.IdFor(Edge, "HideFirstRunExperience")].Status);
    }

    [Fact]
    public void A_value_somebody_else_wrote_is_never_touched()
    {
        var (runner, store) = Runner();
        store.Values[$"{Edge}|HomepageLocation"] = "string:https://intranet";
        runner.Apply(Doc(Text(Edge, "HomepageLocation", "https://fleethub")));
        runner.Tick();
        Assert.Equal("string:https://intranet", store.Get(Edge, "HomepageLocation"));
        Assert.Empty(store.Marker);
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere,
                     Reported()[PolicyAllowList.IdFor(Edge, "HomepageLocation")].Status);
    }

    [Fact]
    public void A_value_changed_by_somebody_else_after_we_wrote_it_is_let_go()
    {
        var (runner, store) = Runner();
        runner.Apply(Doc(Dword(Edge, "A", 1)));
        runner.Tick();
        store.Values[$"{Edge}|A"] = "dword:0";        // a domain GPO took it over
        runner.Tick();
        Assert.Equal("dword:0", store.Get(Edge, "A"));
        Assert.Empty(store.Marker);
        runner.Tick();                                 // and is not fought on later passes
        Assert.Equal("dword:0", store.Get(Edge, "A"));
        Assert.Equal(GroupPolicyRunner.ManagedElsewhere, Reported()[PolicyAllowList.IdFor(Edge, "A")].Status);
    }

    [Fact]
    public void A_value_somebody_deleted_is_put_back()
    {
        var (runner, store) = Runner();
        runner.Apply(Doc(Dword(Edge, "A", 1)));
        runner.Tick();
        store.Values.Remove($"{Edge}|A");
        runner.Tick();
        Assert.Equal("dword:1", store.Get(Edge, "A"));
    }

    [Fact]
    public void A_changed_policy_rewrites_our_value()
    {
        var (runner, store) = Runner();
        runner.Apply(Doc(Dword(Edge, "A", 1)));
        runner.Tick();
        runner.Apply(Doc(Dword(Edge, "A", 2)));
        runner.Tick();
        Assert.Equal("dword:2", store.Get(Edge, "A"));
        Assert.Single(store.Marker);
    }

    [Fact]
    public void An_empty_document_removes_what_we_wrote_and_reports_it_gone()
    {
        var (runner, store) = Runner();
        runner.Apply(Doc(Dword(Edge, "A", 1)));
        runner.Tick();
        Reported();
        runner.Apply(Doc());
        runner.Tick();
        Assert.Null(store.Get(Edge, "A"));
        Assert.Empty(store.Marker);
        var payload = GroupPolicyReporter.TakeIfChanged();
        Assert.Contains(PolicyAllowList.IdFor(Edge, "A"),
                        payload!["removed"]!.AsArray().Select(n => n!.GetValue<string>()));
    }

    [Fact]
    public void Removal_leaves_a_value_somebody_changed_meanwhile()
    {
        var (runner, store) = Runner();
        runner.Apply(Doc(Dword(Edge, "A", 1), Dword(Edge, "B", 1)));
        runner.Tick();
        runner.Apply(Doc(Dword(Edge, "B", 1)));
        // Changed between the document arriving and the pass that would remove it.
        store.Values[$"{Edge}|A"] = "dword:7";
        runner.Tick();
        Assert.Equal("dword:7", store.Get(Edge, "A"));
        Assert.Equal([$"{Edge}|B"], store.Marker);
    }

    [Fact]
    public void A_value_outside_the_allow_list_is_refused_and_not_written()
    {
        var (runner, store) = Runner();
        var bad = Dword(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "Evil", 1);
        runner.Apply(Doc(bad));
        runner.Tick();
        Assert.Empty(store.Values);
        Assert.Equal(GroupPolicyRunner.Refused, Reported()[bad.Id].Status);
    }

    [Fact]
    public void Release_removes_our_values_and_the_marker_and_leaves_others()
    {
        var (runner, store) = Runner();
        store.Values[$"{Edge}|Theirs"] = "dword:5";
        runner.Apply(Doc(Dword(Edge, "Ours", 1), Dword(Edge, "Theirs", 1)));
        runner.Tick();
        runner.Release();
        Assert.Null(store.Get(Edge, "Ours"));
        Assert.Equal("dword:5", store.Get(Edge, "Theirs"));
        Assert.Empty(store.Marker);
    }

    [Fact]
    public void The_document_and_what_was_written_survive_a_restart()
    {
        var (runner, store) = Runner();
        runner.Apply(Doc(Dword(Edge, "A", 1)));
        runner.Tick();
        var restarted = new GroupPolicyRunner(NullLogger<GroupPolicyRunner>.Instance, new AgentState(), store);
        Assert.Equal("v1", restarted.Version);
        store.Values[$"{Edge}|A"] = "dword:9";
        restarted.Tick();
        // It still knows it wrote 1, so 9 is somebody else's and is let go rather than fought.
        Assert.Equal("dword:9", store.Get(Edge, "A"));
        Assert.Empty(store.Marker);
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
