using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;

namespace FleetHub.Shared.Pty;

// The interactive terminal's hub side, compiled into the Windows and Linux agents from this one
// file (each csproj links agent-shared/Pty/*.cs). See PtySessionRunner.cs for why the split
// between shared and per-agent code falls where it does.

/// <summary>One item the console queued for an open terminal: raw bytes the operator typed
/// (<c>Kind == "data"</c>) or a terminal resize (<c>Kind == "resize"</c>).</summary>
public readonly record struct PtyInputItem(int Seq, string Kind, string Data, short Cols, short Rows);

/// <summary>Result of one keystroke poll. <c>Ok</c> is false when the hub could not be reached
/// at all -- distinct from "reached it, nothing typed", because only the former should make
/// the session give up. <c>Closing</c> is the console asking us to end the terminal;
/// <c>Gone</c> is the hub no longer knowing the session (404).</summary>
public readonly record struct PtyInputBatch(
    bool Ok, IReadOnlyList<PtyInputItem> Items, int NextSeq, bool Closing, bool Gone);

/// <summary>The terminal half of the hub API (hub/fleet_web.py, agent_pty_*). An interface so
/// PtySessionRunner's polling, cursors and backoff can be tested without a live hub; each
/// agent's FleetClient implements it by handing its HttpClient to <see cref="PtyHttp"/>.</summary>
public interface IPtyChannel
{
    Task<PtyInputBatch> PullPtyInputAsync(string sessionId, int afterSeq, CancellationToken ct);
    Task<bool> PostPtyOutputAsync(string sessionId, int seq, string chunk, CancellationToken ct);
    Task ReportPtyClosedAsync(string sessionId, string reason, CancellationToken ct);
}

/// <summary>
/// The terminal's cadence and limits -- one set for every platform.
///
/// **These are facts about the hub protocol and about what an operator perceives, not about
/// the operating system**, which is why they live here rather than in each AgentConfig. The
/// echo an operator feels is roughly PtyInputPollMillis/2 + PtyOutputFlushMillis + the
/// console's own output poll, ~250-400ms typical: a usable terminal. Going lower buys little
/// (the browser poll dominates) and costs the hub real request volume -- waitress runs a fixed
/// thread pool, and every open session is already ~7 req/s while someone is typing.
/// </summary>
public static class PtyTimings
{
    /// <summary>Keystroke poll while the operator is typing.</summary>
    public const int InputPollMillis = 150;

    /// <summary>...backing off to this after <see cref="IdleAfterMillis"/> of no typing. A
    /// terminal left open on a second monitor should cost about what a heartbeat costs.</summary>
    public const int InputPollIdleMillis = 1000;
    public const int IdleAfterMillis = 15_000;

    /// <summary>Coalesce output for a beat before posting: a single `dir` or `ls -R` is dozens
    /// of small writes, and one POST per write would be pure overhead.</summary>
    public const int OutputFlushMillis = 40;

    /// <summary>Matches terminal.PTY_MAX_CHUNK_CHARS on the hub; split before it would refuse.</summary>
    public const int MaxChunkChars = 16_000;

    /// <summary>The agent's BACKSTOP reap. A terminal deliberately outlives the page that
    /// opened it, so the HUB decides abandonment (terminal.PTY_ABANDONED_SECONDS, an hour, on a
    /// clock only the console's polls refresh) and says so with `closing`. This covers the one
    /// case it cannot: a hub that forgot the session while still answering. It must sit well
    /// above the hub's horizon or it would reap the very absence the feature exists to support
    /// -- but it must exist, because what would leak is a SYSTEM or root shell.</summary>
    public const int IdleTimeoutSeconds = 2 * 60 * 60;

    /// <summary>Most terminals open at once on one machine. The hub caps each operator at four
    /// per machine; this bounds the machine whatever the hub does.</summary>
    public const int MaxSessions = 8;
}

/// <summary>
/// The three terminal requests, bound to one agent's HttpClient, its way of making an
/// authorised request, the hub's base URL and a logger. Each agent's FleetClient owns its own
/// client and bearer token and builds one of these over them; this holds the parts that must be
/// identical everywhere -- the URLs, the 404-means-gone rule, the input parse -- so a fix to one
/// cannot miss the other platform. An object rather than static methods so each call carries
/// only what differs per request.
///
/// **Every method fails soft and never throws**, the rule FleetClient lives by: these run on a
/// tight loop on a machine nobody is watching, and an exception escaping one ends the terminal.
/// </summary>
public sealed class PtyHttp(
    HttpClient http, Func<HttpMethod, string, HttpRequestMessage> authorized, string hubBase, ILogger log)
{
    public static string InputUrl(string hubBase, string sessionId, int afterSeq) =>
        hubBase + "/api/agent/pty/" + Uri.EscapeDataString(sessionId) +
        "/input?after_seq=" + afterSeq.ToString(System.Globalization.CultureInfo.InvariantCulture);

    public static string OutputUrl(string hubBase, string sessionId) =>
        hubBase + "/api/agent/pty/" + Uri.EscapeDataString(sessionId) + "/output";

    public static string ClosedUrl(string hubBase, string sessionId) =>
        hubBase + "/api/agent/pty/" + Uri.EscapeDataString(sessionId) + "/closed";

    /// <summary>Fetch keystrokes and resizes queued for this session.</summary>
    public async Task<PtyInputBatch> PullInputAsync(string sessionId, int afterSeq, CancellationToken ct)
    {
        var empty = Array.Empty<PtyInputItem>();
        try
        {
            using var req = authorized(HttpMethod.Get, InputUrl(hubBase, sessionId, afterSeq));
            using var resp = await http.SendAsync(req, ct);

            // The hub forgot this session (reaped, or its database was reset). Nothing will
            // ever arrive for it, so end the terminal rather than poll a 404 forever.
            if (resp.StatusCode == HttpStatusCode.NotFound)
                return new PtyInputBatch(true, empty, afterSeq, Closing: true, Gone: true);
            if (!resp.IsSuccessStatusCode)
                return new PtyInputBatch(false, empty, afterSeq, false, false);

            var json = JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct));
            return new PtyInputBatch(
                true, ParseItems(json?["items"] as JsonArray),
                json?["next_seq"]?.GetValue<int>() ?? afterSeq,
                json?["closing"]?.GetValue<bool>() ?? false,
                Gone: false);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException
                                     or JsonException or InvalidOperationException or FormatException)
        {
            // The last three are a hub answering 200 with something that is not the shape
            // above. Same answer as unreachable: try again on the next tick.
            log.LogDebug(e, "pty input poll for {Id} failed", sessionId);
            return new PtyInputBatch(false, empty, afterSeq, false, false);
        }
    }

    private static List<PtyInputItem> ParseItems(JsonArray? array)
    {
        var items = new List<PtyInputItem>();
        if (array is null) return items;
        foreach (var node in array)
        {
            if (node is null) continue;
            var seq = node["seq"]?.GetValue<int>() ?? -1;
            if ((node["kind"]?.GetValue<string>() ?? "data") == "resize")
            {
                var size = node["size"];
                items.Add(new PtyInputItem(seq, "resize", "",
                    (short)(size?["cols"]?.GetValue<int>() ?? 120),
                    (short)(size?["rows"]?.GetValue<int>() ?? 30)));
            }
            else
            {
                items.Add(new PtyInputItem(seq, "data", node["data"]?.GetValue<string>() ?? "", 0, 0));
            }
        }
        return items;
    }

    /// <summary>Post one output chunk. A retry MUST reuse the same seq -- the hub keys on it,
    /// and a fresh number would duplicate bytes into the middle of an escape sequence.</summary>
    public async Task<bool> PostOutputAsync(string sessionId, int seq, string chunk, CancellationToken ct)
    {
        var body = new JsonObject { ["seq"] = seq, ["chunk"] = chunk };
        try
        {
            using var req = authorized(HttpMethod.Post, OutputUrl(hubBase, sessionId));
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
            using var resp = await http.SendAsync(req, ct);
            return resp.IsSuccessStatusCode;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            log.LogDebug(e, "pty output post for {Id} seq {Seq} failed", sessionId, seq);
            return false;
        }
    }

    /// <summary>Tell the hub the terminal is over, so the console stops polling and can say
    /// why. Best-effort: if it does not land, the hub's own reaper closes it anyway.</summary>
    public async Task ReportClosedAsync(string sessionId, string reason, CancellationToken ct)
    {
        var body = new JsonObject { ["reason"] = reason };
        try
        {
            using var req = authorized(HttpMethod.Post, ClosedUrl(hubBase, sessionId));
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
            using var resp = await http.SendAsync(req, ct);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            log.LogDebug(e, "pty close report for {Id} failed", sessionId);
        }
    }
}
