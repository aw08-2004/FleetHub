using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.Remote;

/// <summary>
/// The helper's side of WebRTC signaling (roadmap #2): posts this peer's SDP offer and ICE
/// candidates to the hub, and polls for the console's answer + ICE. Authenticated with the
/// agent's enrollment bearer token (loaded from agent.json), talking to the same hub the
/// service reports to. Plain HTTP polling -- the agent has no listening port and needs none;
/// signaling is a short burst at setup.
/// </summary>
public sealed class RemoteSignalingClient : IDisposable
{
    private readonly HttpClient _http;
    private readonly string _signalUrl;
    private readonly string _pollUrl;
    private readonly string _endedUrl;
    private readonly string _iceUrl;

    public RemoteSignalingClient(string sessionId, string bearer)
        : this(sessionId, bearer, new HttpClientHandler()) { }

    /// <summary>The handler is injectable so tests can answer as the hub would without one.</summary>
    internal RemoteSignalingClient(string sessionId, string bearer, HttpMessageHandler handler)
    {
        _http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(30) };
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        var baseUrl = AgentConfig.HubBase;
        var enc = Uri.EscapeDataString(sessionId);
        _signalUrl = $"{baseUrl}/api/agent/remote/{enc}/signal";
        _pollUrl = $"{baseUrl}/api/agent/remote/{enc}/poll";
        _endedUrl = $"{baseUrl}/api/agent/remote/{enc}/ended";
        _iceUrl = $"{baseUrl}/api/agent/remote/{enc}/ice";
    }

    /// <summary>Fetch the ICE servers the hub picks for THIS machine, or null if the hub could
    /// not answer.
    ///
    /// The copy in the start command was minted when the operator pressed Start, from the
    /// CONSOLE's source address -- and the relay is published under several URLs precisely
    /// because the two peers are often not on the same side of the hub's network (the hub's LAN
    /// address means nothing to a machine on the internet, and its public hostname means a
    /// hairpin off the router to a relay one switch away). Asking here lets the hub choose from
    /// the address it sees US arriving from.
    ///
    /// Null -- an older hub with no such route, or any transport failure -- means "keep what the
    /// command carried", so this can never be the reason a session fails to start.
    /// </summary>
    public async Task<List<IceServerConfig>?> GetIceServersAsync(CancellationToken ct)
    {
        try
        {
            using var resp = await _http.GetAsync(_iceUrl, ct);
            if (!resp.IsSuccessStatusCode) return null;
            var body = await resp.Content.ReadFromJsonAsync<IceResult>(cancellationToken: ct);
            return body?.IceServers;
        }
        catch (OperationCanceledException) { throw; }
        catch { return null; }
    }

    /// <summary>Send one signal (offer/ice/bye) to the console side. Returns the hub's sequence
    /// number for it, or 0 if the hub did not report one -- see <see cref="RemoteHelper"/> for
    /// why the offer's seq matters.
    ///
    /// A 409 throws <see cref="SessionSupersededException"/> rather than the generic
    /// HttpRequestException: it is the hub's answer, not a transport failure, and it means
    /// this session is over (see that exception for why that matters).</summary>
    public async Task<int> PostSignalAsync(string kind, object payload, CancellationToken ct)
    {
        using var resp = await _http.PostAsJsonAsync(
            _signalUrl, new { kind, payload }, ct);
        if (resp.StatusCode == HttpStatusCode.Conflict)
            throw new SessionSupersededException(kind, await ErrorTextAsync(resp, ct));
        resp.EnsureSuccessStatusCode();
        try
        {
            var body = await resp.Content.ReadFromJsonAsync<PostResult>(cancellationToken: ct);
            return body?.Seq ?? 0;
        }
        catch
        {
            // A hub too old to report the seq, or a body we cannot parse. 0 means "start from
            // the beginning", which is the behaviour we had before this returned anything.
            return 0;
        }
    }

    /// <summary>Fetch console signals newer than <paramref name="afterSeq"/>, plus the session
    /// status so the helper knows when to tear down.</summary>
    public async Task<PollResult> PollAsync(int afterSeq, CancellationToken ct)
    {
        using var resp = await _http.GetAsync($"{_pollUrl}?after_seq={afterSeq}", ct);
        resp.EnsureSuccessStatusCode();
        var result = await resp.Content.ReadFromJsonAsync<PollResult>(cancellationToken: ct);
        return result ?? new PollResult();
    }

    /// <summary>Tell the hub this session has ended (consent denied, capture failed, or normal
    /// teardown), so it doesn't sit alive until the TTL sweep. Best-effort.</summary>
    public async Task ReportEndedAsync(string reason, CancellationToken ct)
    {
        using var resp = await _http.PostAsJsonAsync(_endedUrl, new { reason }, ct);
        resp.EnsureSuccessStatusCode();
    }

    public void Dispose() => _http.Dispose();

    /// <summary>The hub's <c>{"error": ...}</c> sentence, or empty if the body is not that.
    /// Only ever logged.</summary>
    private static async Task<string> ErrorTextAsync(HttpResponseMessage resp, CancellationToken ct)
    {
        try
        {
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
            return doc.RootElement.ValueKind == JsonValueKind.Object &&
                   doc.RootElement.TryGetProperty("error", out var e) &&
                   e.ValueKind == JsonValueKind.String ? e.GetString() ?? "" : "";
        }
        catch (OperationCanceledException) { throw; }
        catch { return ""; }
    }

    private sealed class PostResult
    {
        [JsonPropertyName("seq")] public int Seq { get; set; }
    }

    private sealed class IceResult
    {
        [JsonPropertyName("ice_servers")] public List<IceServerConfig>? IceServers { get; set; }
    }

    public sealed class PollResult
    {
        [JsonPropertyName("signals")] public List<SignalMessage> Signals { get; set; } = new();
        [JsonPropertyName("next_seq")] public int NextSeq { get; set; }
        [JsonPropertyName("status")] public string Status { get; set; } = "";
    }

    public sealed class SignalMessage
    {
        [JsonPropertyName("seq")] public int Seq { get; set; }
        [JsonPropertyName("kind")] public string Kind { get; set; } = "";
        // Left as a JsonElement: an answer carries {sdp}, an ice candidate carries the WebRTC
        // candidate init shape -- the peer decodes each based on Kind.
        [JsonPropertyName("payload")] public JsonElement Payload { get; set; }
    }
}

/// <summary>
/// The hub refused a signal with 409: it will take no more signaling for this session --
/// almost always because the session is no longer live (ended, expired, or replaced by a
/// newer one for this machine), occasionally because it hit the per-session signal cap.
/// Either way nothing this helper sends will be accepted again.
///
/// A type of its own because the generic HttpRequestException from EnsureSuccessStatusCode
/// escaped RunSessionAsync as "Remote helper terminated unexpectedly". That is an exit WITHOUT
/// clearing the supervision record, which the service reads as a helper that died
/// involuntarily -- so it re-injects a fresh one, whose offer gets the same 409, and so on:
/// OPENCLAW logged seven helpers injected in thirteen seconds on 2026-10-12, for a session
/// that had already ended. A superseded session is a normal ending, and is handled as one.
/// </summary>
public sealed class SessionSupersededException(string kind, string hubMessage)
    : Exception($"the hub refused the '{kind}' signal with 409" +
                (hubMessage.Length > 0 ? $" ({hubMessage})" : ""))
{
    /// <summary>The signal kind that was refused.</summary>
    public string Kind { get; } = kind;

    /// <summary>The hub's own sentence for the refusal, or empty.</summary>
    public string HubMessage { get; } = hubMessage;
}
