using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>
/// The registry half of Group Policy as the hub resolved it for this PC (roadmap #32): the
/// values every policy linked above this PC sets, already merged by the hub's precedence
/// rules, so the agent never has to know what an OU or a link order is.
///
/// Parsed by hand from the heartbeat's <c>gpo_document</c>, like
/// <see cref="TempMonitorAgent.Watchdog.WatchdogDocument"/>. An entry that does not parse is
/// DROPPED rather than defaulted: a registry value with a guessed type or a missing name is a
/// write nobody asked for.
/// </summary>
public sealed record PolicyDocument
{
    public string Version { get; init; } = "";
    public IReadOnlyList<PolicyValue> Values { get; init; } = [];

    public static PolicyDocument Empty { get; } = new();

    /// <summary>The document a heartbeat carried, or null when the block is absent or not an
    /// object -- "the hub had nothing to say". An empty <c>values</c> list is a real document,
    /// and it is the one that makes the agent remove what it wrote.</summary>
    public static PolicyDocument? Parse(JsonNode? root, string version)
    {
        if (root is not JsonObject obj) return null;
        if (!obj.TryGetPropertyValue("values", out var list) || list is not JsonArray array)
            return null;
        var values = new List<PolicyValue>();
        foreach (var node in array)
        {
            var value = PolicyValue.Parse(node);
            if (value is not null) values.Add(value);
        }
        return new PolicyDocument { Version = version, Values = values };
    }
}

/// <summary>One registry value. <c>Value</c> is the canonical text form
/// (<see cref="PolicyValue.Canonical"/>), so the runner compares what is in the registry with
/// what is wanted as two strings.</summary>
public sealed record PolicyValue
{
    public string Id { get; init; } = "";
    /// <summary><see cref="PolicyAllowList.Machine"/> or <see cref="PolicyAllowList.User"/>.
    /// Absent on the wire (every row an older hub sends) and in a state file from 3.48, both of
    /// which mean machine.</summary>
    public string Class { get; init; } = PolicyAllowList.Machine;
    public string Path { get; init; } = "";
    public string Name { get; init; } = "";
    public string Type { get; init; } = "";
    public uint Dword { get; init; }
    public string Text { get; init; } = "";
    public IReadOnlyList<string> Lines { get; init; } = [];

    /// <summary>The value as <see cref="IRegistryValueStore.Get"/> would report it once
    /// written. Two values are the same exactly when these are equal. Computed, so it is
    /// not stored in group_policy.json.</summary>
    [JsonIgnore]
    public string Canonical => Type switch
    {
        PolicyAllowList.Dword => $"{Type}:{Dword}",
        PolicyAllowList.MultiString => $"{Type}:{string.Join('\0', Lines)}",
        _ => $"{Type}:{Text}",
    };

    public static PolicyValue? Parse(JsonNode? node)
    {
        if (node is not JsonObject obj) return null;
        try
        {
            var path = obj["path"]?.GetValue<string>() ?? "";
            var name = obj["name"]?.GetValue<string>() ?? "";
            var type = obj["type"]?.GetValue<string>() ?? "";
            // A class this agent does not know is dropped, not read as machine: a row meant
            // for some other half written into HKLM is exactly the write nobody asked for.
            var cls = obj["class"]?.GetValue<string>() ?? PolicyAllowList.Machine;
            if (cls is not (PolicyAllowList.Machine or PolicyAllowList.User)) return null;
            var value = new PolicyValue
            {
                // Recomputed here rather than trusted from the wire: the id is what the marker
                // and the hub's state rows are keyed on, and it has to be the same function of
                // path and name at both ends.
                Id = PolicyAllowList.IdFor(path, name, cls),
                Class = cls,
                Path = path,
                Name = name,
                Type = type,
            };
            var raw = obj["value"];
            return type switch
            {
                PolicyAllowList.Dword => value with { Dword = raw!.GetValue<uint>() },
                PolicyAllowList.MultiString => value with
                {
                    Lines = raw is JsonArray lines
                        ? [.. lines.Select(l => l!.GetValue<string>())]
                        : throw new FormatException("multi_string needs a list"),
                },
                PolicyAllowList.String or PolicyAllowList.ExpandString =>
                    value with { Text = raw?.GetValue<string>() ?? "" },
                PolicyAllowList.Delete => value,
                _ => null,
            };
        }
        catch (Exception e) when (e is InvalidOperationException or FormatException
                                       or OverflowException or NullReferenceException)
        {
            return null;
        }
    }
}
