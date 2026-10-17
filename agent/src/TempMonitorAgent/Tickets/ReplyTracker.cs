using System.Text.Json;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Tickets;

/// <summary>One thing the tray should raise a notification for.</summary>
public sealed record TicketNotice(long TicketId, string Subject, string Text);

/// <summary>
/// Which ticket updates the person has not been told about yet (roadmap #31 phase D), so the
/// tray raises a notification for a new reply or a ticket being resolved -- once, and not
/// again every time the list is re-sent.
///
/// **Remembered per user on disk** (<c>%LOCALAPPDATA%\FleetHub\tickets-seen.json</c>), not in
/// memory: the tray is restarted on every sign-in and after every agent update, and an
/// in-memory "seen" set would either re-announce every old reply after each restart or, if it
/// treated the first list as the baseline, swallow the one reply that arrived overnight --
/// which is exactly the one the person needs to see. The file is the person's own, written by
/// their own tray, and holds ids and counts, never the text.
///
/// The decision is <see cref="Diff"/>, pure, so the rules are tested without a tray.
/// </summary>
public static class ReplyTracker
{
    /// <summary>What the person has been told about one ticket: how many helpdesk messages,
    /// and the status it was in.</summary>
    public sealed record Seen(int OperatorMessages, string Status);

    /// <summary>
    /// Compare the hub's list with what the person was already told. Returns the notices to
    /// raise and the new "told" state to remember.
    ///
    /// <paramref name="seen"/> null means this user has never had a list on this PC: the
    /// first one is taken as read, because every ticket in it was filed from this tray and
    /// announcing them back would be noise. After that a ticket the tray has never seen is
    /// announced if the helpdesk has already answered it.
    /// </summary>
    public static (List<TicketNotice> Notices, Dictionary<long, Seen> Now) Diff(
        IReadOnlyDictionary<long, Seen>? seen, JsonArray tickets)
    {
        var notices = new List<TicketNotice>();
        var now = new Dictionary<long, Seen>();
        foreach (var node in tickets)
        {
            if (node is not JsonObject ticket || Id(ticket) is not { } id) continue;
            var subject = Str(ticket, "subject");
            var status = Str(ticket, "status");
            var operatorMessages = HelpdeskMessages(ticket);
            now[id] = new Seen(operatorMessages.Count, status);
            if (seen is null) continue;

            seen.TryGetValue(id, out var before);
            int told = before?.OperatorMessages ?? 0;
            if (operatorMessages.Count > told)
                notices.Add(new TicketNotice(id, subject, operatorMessages[^1]));
            else if (before is not null && before.Status != status && status is "resolved" or "closed")
                notices.Add(new TicketNotice(id, subject, status == "resolved"
                    ? "The helpdesk marked this as solved."
                    : "The helpdesk closed this ticket."));
        }
        return (notices, now);
    }

    /// <summary>The remembered state, or null when there is none yet (or it is unreadable --
    /// which reads as "first time", the quiet answer).</summary>
    public static Dictionary<long, Seen>? Load(string path)
    {
        try
        {
            return File.Exists(path)
                ? JsonSerializer.Deserialize<Dictionary<long, Seen>>(File.ReadAllText(path))
                : null;
        }
        catch (Exception e) when (e is IOException or JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public static void Save(string path, Dictionary<long, Seen> state)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, JsonSerializer.Serialize(state));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // Worst case the same reply is announced once more after the next restart.
        }
    }

    private static List<string> HelpdeskMessages(JsonObject ticket) =>
        ticket["messages"] is JsonArray messages
            ? messages.OfType<JsonObject>()
                      .Where(m => Str(m, "author_kind") == "operator")
                      .Select(m => Str(m, "body"))
                      .ToList()
            : [];

    private static long? Id(JsonObject o) =>
        o["id"] is JsonValue v && v.TryGetValue<long>(out var id) ? id : null;

    private static string Str(JsonObject o, string key) =>
        o[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s : "";
}
