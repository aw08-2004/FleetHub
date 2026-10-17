using System.Text.Json.Nodes;

namespace TempMonitorAgent.Tickets;

/// <summary>Who is on the other end of a tray connection, as the SERVICE established it from
/// the pipe -- never as the tray described itself. <see cref="Name"/> is DOMAIN\user.</summary>
public sealed record TrayCaller(string Sid, string Name, string Kind, uint Session);

/// <summary>A ticket the service has checked and is ready to hand the hub: the JSON for
/// <c>POST /api/agent/tickets</c> and the screenshots to upload after it, one each.</summary>
public sealed record CheckedTicket(JsonObject HubPayload, IReadOnlyList<byte[]> Screenshots);

/// <summary>
/// The decisions the service makes about a ticket from a tray (roadmap #31 phase A), kept
/// pure so they are tested without a pipe, a session or a hub.
///
/// **The reporter comes from the connection, the words come from the tray.** Everything here
/// that names a person -- SID, account, domain-or-local -- is an argument the pipe server
/// filled in from the caller's token; the tray's frame is read for subject, body, urgency, a
/// due time and pictures, and nothing else. A tray that put <c>reporter_sid</c> in its frame
/// would simply be ignored, which is what makes "the user attached a debugger to the tray"
/// worth nothing: the most they can do is file a ticket as themselves.
///
/// **The checks repeat the hub's on purpose.** The hub validates every submit too, but the
/// service is where a bad screenshot can still be refused with the person watching -- after
/// the ticket is filed, a picture the hub rejects is a ticket missing the one thing that made
/// it better than a phone call, and nobody is told.
/// </summary>
public static class TicketRules
{
    /// <summary>The hub's <c>tickets.URGENCIES</c>.</summary>
    public static readonly IReadOnlyList<string> Urgencies = ["low", "normal", "high", "critical"];

    /// <summary>The hub's <c>tickets.MAX_SUBJECT_CHARS</c> / <c>MAX_BODY_CHARS</c>. Trimmed
    /// here rather than refused, as the hub does: a long description is not a reason to lose
    /// the ticket.</summary>
    public const int MaxSubjectChars = 200;
    public const int MaxBodyChars = 8000;
    public const int MaxReplyChars = 8000;

    /// <summary>
    /// Domain or local, for <c>tickets.submitters</c>. An account whose domain part is this
    /// computer's own name is a local account; anything else -- an AD domain, <c>AzureAD</c>
    /// for an Entra-joined PC -- is an organisation's account. Entra counts as domain because
    /// the policy exists to refuse the shared kiosk login nobody can be phoned back on, and an
    /// Entra account is a named person.
    /// </summary>
    public static string ReporterKind(string accountDomain, string machineName) =>
        string.Equals(accountDomain?.Trim(), machineName?.Trim(), StringComparison.OrdinalIgnoreCase)
            ? "local" : "domain";

    /// <summary>Whether to offer "Report a problem" to this person, mirroring the hub's
    /// <c>tickets.submitter_allowed</c>. Display only -- the hub decides.</summary>
    public static bool CanSubmit(string submitters, string reporterKind) =>
        submitters != "domain" || reporterKind == "domain";

    /// <summary>Check one submit frame. Returns a sentence for the person, or the ticket.</summary>
    public static (string? Error, CheckedTicket? Ticket) Check(JsonObject frame, TrayCaller caller)
    {
        var subject = Text(frame, "subject", MaxSubjectChars);
        if (subject.Length == 0) return ("Please say in a few words what is wrong.", null);
        var urgency = Text(frame, "urgency", 16).ToLowerInvariant();
        if (urgency.Length == 0) urgency = "normal";
        if (!Urgencies.Contains(urgency)) return ("That urgency is not one the helpdesk uses.", null);

        var screenshots = new List<byte[]>();
        if (frame["screenshots"] is JsonArray shots)
        {
            if (shots.Count > TrayProtocol.MaxScreenshots)
                return ($"At most {TrayProtocol.MaxScreenshots} screenshots can be sent.", null);
            foreach (var shot in shots)
            {
                byte[] bytes;
                try { bytes = Convert.FromBase64String(shot?.GetValue<string>() ?? ""); }
                catch (Exception e) when (e is FormatException or InvalidOperationException)
                {
                    return ("One of the screenshots could not be read.", null);
                }
                if (bytes.Length == 0 || bytes.Length > TrayProtocol.MaxScreenshotBytes)
                    return ("One of the screenshots is too large to send.", null);
                if (ImageType(bytes) is null)
                    return ("One of the screenshots is not a picture.", null);
                screenshots.Add(bytes);
            }
        }

        var payload = new JsonObject
        {
            ["subject"] = subject,
            ["body"] = Text(frame, "body", MaxBodyChars),
            ["urgency"] = urgency,
            ["reporter_name"] = caller.Name,
            ["reporter_sid"] = caller.Sid,
            ["reporter_kind"] = caller.Kind,
        };
        if (DueAt(frame) is { } due) payload["due_at"] = due;
        return (null, new CheckedTicket(payload, screenshots));
    }

    /// <summary>The content type of a screenshot by its first bytes -- the hub's
    /// <c>tickets.sniff_image</c> -- or null for anything that is not a JPEG or a PNG.</summary>
    public static string? ImageType(ReadOnlySpan<byte> head)
    {
        if (head.Length >= 3 && head[0] == 0xFF && head[1] == 0xD8 && head[2] == 0xFF)
            return "image/jpeg";
        ReadOnlySpan<byte> png = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A];
        return head.Length >= png.Length && head[..png.Length].SequenceEqual(png)
            ? "image/png" : null;
    }

    /// <summary>One reply frame's text, trimmed, or "" -- for <see cref="TrayProtocol.Reply"/>.</summary>
    public static string ReplyText(JsonObject frame) => Text(frame, "body", MaxReplyChars);

    /// <summary>The ticket id a reply frame names, or 0.</summary>
    public static long TicketId(JsonObject frame) =>
        frame["ticket_id"] is JsonValue v && v.TryGetValue<long>(out var id) && id > 0 ? id : 0;

    private static string Text(JsonObject frame, string key, int limit)
    {
        var text = frame[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s.Trim() : "";
        return text.Length > limit ? text[..limit] : text;
    }

    private static long? DueAt(JsonObject frame) =>
        frame["due_at"] is JsonValue v && v.TryGetValue<long>(out var due) && due > 0 ? due : null;
}

/// <summary>
/// At most a handful of tickets per person per window, counted in the service.
///
/// The hub has no per-person limit, and the tray is a button any process running as the
/// user can press without the person -- a script in a loop is a queue nobody can read and a
/// <c>ticket_new</c> push to every phone on the helpdesk for each one. Counted per SID rather
/// than per session, so signing out and back in does not reset it.
/// </summary>
public sealed class SubmitLimiter(int perWindow = 5, TimeSpan? window = null)
{
    private readonly TimeSpan _window = window ?? TimeSpan.FromMinutes(10);
    private readonly Dictionary<string, Queue<DateTimeOffset>> _seen = new(StringComparer.OrdinalIgnoreCase);
    private readonly object _lock = new();

    /// <summary>Count one submit for <paramref name="sid"/>, or refuse it. A refused attempt
    /// is not counted, so a person who waits the window out is never punished for retrying.</summary>
    public bool TryTake(string sid, DateTimeOffset now)
    {
        lock (_lock)
        {
            if (!_seen.TryGetValue(sid, out var times)) _seen[sid] = times = new Queue<DateTimeOffset>();
            while (times.Count > 0 && now - times.Peek() >= _window) times.Dequeue();
            if (times.Count >= perWindow) return false;
            times.Enqueue(now);
            return true;
        }
    }
}
