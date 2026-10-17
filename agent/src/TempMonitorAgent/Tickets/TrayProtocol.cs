using System.Buffers.Binary;
using System.Text;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Tickets;

/// <summary>
/// The wire format between the ticket tray (running as the user) and the agent service
/// (LocalSystem), over <see cref="AgentConfig.TicketPipeName"/> -- roadmap #31 phase A.
///
/// **Length-prefixed JSON frames, not lines.** A frame is a 4-byte little-endian length and
/// that many bytes of UTF-8 JSON object. Lines were the obvious choice (SecureAttentionRelay
/// uses one) and are wrong here for a reason that only shows up under attack: the service
/// reads whatever the user's side sends, and <c>StreamReader.ReadLineAsync</c> has no upper
/// bound -- one process that never sends a newline grows a SYSTEM process's memory until
/// something gives. A length prefix is checked against <see cref="MaxFrameBytes"/> BEFORE a
/// byte of the body is buffered.
///
/// **Screenshots ride inside the submit frame as base64**, which costs a third more bytes over
/// a local pipe and buys one message per ticket: the service either has the whole ticket or
/// none of it, so a tray killed half way through never leaves a ticket the person believes
/// they sent with half its screens missing.
///
/// Every message is a JSON object with a <c>type</c>. Requests carry a <c>req</c> number the
/// answer echoes, so a push (new replies, the policy) can arrive between a request and its
/// answer without being mistaken for it.
/// </summary>
public static class TrayProtocol
{
    // --- tray -> service ---------------------------------------------------
    /// <summary>First frame from a tray: its build, so the service can log a stale one.</summary>
    public const string Hello = "hello";
    /// <summary>A new ticket, with its screenshots.</summary>
    public const string Submit = "submit";
    /// <summary>"Show me my tickets" -- answered with <see cref="Tickets"/>.</summary>
    public const string Mine = "mine";
    /// <summary>The person answering one of their own tickets (phase D).</summary>
    public const string Reply = "reply";

    // --- service -> tray ---------------------------------------------------
    /// <summary>What the tray may offer this person right now: <c>can_submit</c>.</summary>
    public const string Policy = "policy";
    /// <summary>The answer to a submit or a reply.</summary>
    public const string Result = "result";
    /// <summary>The person's tickets, as the hub's <c>GET /api/agent/tickets</c> serves them.
    /// Sent in answer to <see cref="Mine"/> and, unasked, when the hub's marker moves.</summary>
    public const string Tickets = "tickets";
    /// <summary>Close: the hub switched the tray off.</summary>
    public const string Exit = "exit";

    /// <summary>The largest frame either side will read. Six screenshots at
    /// <see cref="MaxScreenshotBytes"/> as base64 is 32 MiB; the cap sits just above it so an
    /// honest tray at its limits always fits and nothing else can make the service buffer
    /// more.</summary>
    public const int MaxFrameBytes = 34 * 1024 * 1024;

    /// <summary>One screenshot after the tray has downscaled it. Under the hub's 5 MB
    /// (<c>tickets.MAX_ATTACHMENT_BYTES</c>), so a picture the service accepts is never one the
    /// hub then refuses with a 413 after the person was told it was sent.</summary>
    public const int MaxScreenshotBytes = 4 * 1024 * 1024;

    /// <summary>Screens per ticket, the hub's <c>tickets.MAX_ATTACHMENTS</c>.</summary>
    public const int MaxScreenshots = 6;

    /// <summary>Write one frame. The caller serialises writers: two frames interleaved on one
    /// pipe are garbage to the reader.</summary>
    public static async Task WriteAsync(Stream stream, JsonObject message, CancellationToken ct)
    {
        var body = Encoding.UTF8.GetBytes(message.ToJsonString());
        if (body.Length > MaxFrameBytes)
            throw new InvalidDataException($"a {body.Length}-byte frame is over the pipe's limit");
        var header = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(header, body.Length);
        await stream.WriteAsync(header, ct);
        await stream.WriteAsync(body, ct);
        await stream.FlushAsync(ct);
    }

    /// <summary>Read one frame. Null when the other side closed cleanly between frames;
    /// <see cref="InvalidDataException"/> for an oversized, truncated or non-object frame --
    /// after which the connection is not worth keeping, because the reader no longer knows
    /// where the next frame starts.</summary>
    public static async Task<JsonObject?> ReadAsync(Stream stream, CancellationToken ct)
    {
        var header = new byte[4];
        int got = await ReadFullyAsync(stream, header, ct);
        if (got == 0) return null;
        if (got < header.Length) throw new InvalidDataException("the pipe closed inside a frame header");
        int length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length <= 0 || length > MaxFrameBytes)
            throw new InvalidDataException($"refusing a {length}-byte frame");
        var body = new byte[length];
        if (await ReadFullyAsync(stream, body, ct) < length)
            throw new InvalidDataException("the pipe closed inside a frame");
        try
        {
            return JsonNode.Parse(body) as JsonObject
                   ?? throw new InvalidDataException("a frame that is not a JSON object");
        }
        catch (System.Text.Json.JsonException e)
        {
            throw new InvalidDataException("a frame that is not JSON", e);
        }
    }

    private static async Task<int> ReadFullyAsync(Stream stream, byte[] buffer, CancellationToken ct)
    {
        int total = 0;
        while (total < buffer.Length)
        {
            int n = await stream.ReadAsync(buffer.AsMemory(total), ct);
            if (n == 0) break;
            total += n;
        }
        return total;
    }

    /// <summary>The message's type, or "" -- never throws on a hostile frame.</summary>
    public static string TypeOf(JsonObject message) =>
        message["type"] is JsonValue v && v.TryGetValue<string>(out var s) ? s : "";

    /// <summary>The request number, or 0 for a push.</summary>
    public static long ReqOf(JsonObject message) =>
        message["req"] is JsonValue v && v.TryGetValue<long>(out var n) ? n : 0;
}
