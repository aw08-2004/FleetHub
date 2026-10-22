using System.Text.Json.Nodes;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>
/// Carries what Group Policy did on this PC to the hub on the heartbeat (roadmap #32):
/// <c>{"states": [{id, status, detail}], "removed": [id, ...]}</c>.
///
/// **Change-only, and acknowledged only after a heartbeat succeeds** -- the
/// SoftwareInventoryReporter discipline rather than the watchdog reporter's, which marks a
/// state as sent when it is TAKEN and so loses it if that heartbeat fails. Here a lost
/// <c>managed_elsewhere</c> would leave the console showing a value as pending forever on a PC
/// that has in fact refused it, which is the one thing an operator needs to know. So
/// <see cref="TakeIfChanged"/> hands out a copy and keeps the pending entries; only
/// <see cref="AckSent"/> retires them.
///
/// Static, like the other reporters, because the policy loop writes it and the heartbeat loop
/// reads it, on different threads with no object in common.
/// </summary>
public static class GroupPolicyReporter
{
    private static readonly Lock Gate = new();
    private static readonly Dictionary<string, (string Status, string Detail)> Pending = [];
    private static readonly Dictionary<string, string> LastSent = [];
    private static readonly HashSet<string> PendingRemoved = [];

    /// <summary>Note one value's current state. Cheap to call every tick: a state matching what
    /// the hub was last told is dropped here.</summary>
    public static void Record(string id, string status, string detail)
    {
        lock (Gate)
        {
            PendingRemoved.Remove(id);
            if (LastSent.TryGetValue(id, out var sent) && sent == Signature(status, detail))
            {
                Pending.Remove(id);
                return;
            }
            Pending[id] = (status, detail);
        }
    }

    /// <summary>Note that a value is no longer held, so the hub drops its state row rather
    /// than showing a value that was unlinked as still applied.</summary>
    public static void Removed(string id)
    {
        lock (Gate)
        {
            Pending.Remove(id);
            LastSent.Remove(id);
            PendingRemoved.Add(id);
        }
    }

    /// <summary>Forget what was reported, so every value is reported again. Called when a new
    /// document is adopted, as WatchdogReporter.Invalidate is and for the same reason.</summary>
    public static void Invalidate()
    {
        lock (Gate) { LastSent.Clear(); }
    }

    /// <summary>A copy of everything pending, or null when nothing is. Nothing is retired
    /// here -- see <see cref="AckSent"/>.</summary>
    public static JsonObject? TakeIfChanged()
    {
        lock (Gate)
        {
            if (Pending.Count == 0 && PendingRemoved.Count == 0) return null;
            var states = new JsonArray();
            foreach (var (id, (status, detail)) in Pending)
                states.Add(new JsonObject { ["id"] = id, ["status"] = status, ["detail"] = detail });
            var removed = new JsonArray();
            foreach (var id in PendingRemoved) removed.Add(id);
            return new JsonObject { ["states"] = states, ["removed"] = removed };
        }
    }

    /// <summary>The hub stored <paramref name="sent"/>: retire exactly what it carried. A
    /// state that changed again since it was taken stays pending, so the newer one is sent.</summary>
    public static void AckSent(JsonObject sent)
    {
        lock (Gate)
        {
            foreach (var node in sent["states"]?.AsArray() ?? [])
            {
                var id = node?["id"]?.GetValue<string>();
                var status = node?["status"]?.GetValue<string>() ?? "";
                var detail = node?["detail"]?.GetValue<string>() ?? "";
                if (id is null) continue;
                LastSent[id] = Signature(status, detail);
                if (Pending.TryGetValue(id, out var now) && now == (status, detail))
                    Pending.Remove(id);
            }
            foreach (var node in sent["removed"]?.AsArray() ?? [])
            {
                var id = node?.GetValue<string>();
                if (id is not null) PendingRemoved.Remove(id);
            }
        }
    }

    /// <summary>Test hook: empty every table.</summary>
    internal static void Reset()
    {
        lock (Gate)
        {
            Pending.Clear();
            LastSent.Clear();
            PendingRemoved.Clear();
        }
    }

    private static string Signature(string status, string detail) => $"{status}|{detail}";
}
