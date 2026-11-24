using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Software;

/// <summary>
/// Carries the installed-package list to the hub on the heartbeat, change-only (roadmap #25 B's
/// Linux half). The wire shape is the Windows agent's -- <c>{"software": [...], "error": ...}</c>
/// -- because hub/software.py does not ask which operating system it came from.
///
/// <para><b>Two steps, like PatchInventoryReporter, and for its reason.</b>
/// <see cref="TakeIfChanged"/> hands a payload over; <see cref="MarkSent"/> records it only
/// after the hub has accepted it. The hub answers <c>software_rejected</c> when its write
/// failed, and a payload marked sent on hand-over would then be suppressed by hash until the
/// box's packages next changed -- possibly weeks of a stale Software section.</para>
///
/// <para><b>A failed read sends nothing.</b> SoftwareReader reports an error with no items when
/// it could not read the database, and an empty list in its place would tell the hub this box
/// has nothing installed. A truncated read, which DOES have items, is sent with its error.</para>
/// </summary>
public sealed class SoftwareInventoryReporter(ILogger<SoftwareInventoryReporter> log)
{
    /// <summary>The Windows reporter's hour. A package query is cheap; the interval is about
    /// not re-hashing two thousand rows every minute for a list that changes weekly.
    /// install_patches calls <see cref="Invalidate"/> for the moment it does change.</summary>
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromHours(1);

    private readonly object _gate = new();
    private DateTimeOffset _lastScan = DateTimeOffset.MinValue;
    private string _lastSentHash = "";
    private JsonObject? _pending;

    /// <summary>Test seam for the reader; production reads dpkg or rpm.</summary>
    internal Func<CancellationToken, Task<SoftwareReport>> Read { get; init; } = SoftwareReader.ReadAsync;

    public async Task RefreshIfDueAsync(CancellationToken ct)
    {
        lock (_gate)
        {
            if (DateTimeOffset.UtcNow - _lastScan < RefreshInterval) return;
            _lastScan = DateTimeOffset.UtcNow;
        }

        SoftwareReport report;
        try { report = await Read(ct); }
        catch (OperationCanceledException) { return; }
        catch (Exception e)
        {
            log.LogWarning(e, "Software inventory read threw");
            return;
        }
        if (report.Items.Count == 0 && report.Error is { Length: > 0 } error)
        {
            log.LogWarning("Software inventory not sent: {Error}", error);
            return;
        }

        var payload = ToPayload(report);
        var hash = Hash(payload.ToJsonString());
        lock (_gate)
        {
            if (hash == _lastSentHash)
            {
                _pending = null;
                return;
            }
            _pending = payload;
        }
        log.LogInformation("Software inventory: {Count} package(s) to report", report.Items.Count);
    }

    /// <summary>Re-read at the next inventory pass and re-send even if unchanged.</summary>
    public void Invalidate()
    {
        lock (_gate)
        {
            _lastScan = DateTimeOffset.MinValue;
            _lastSentHash = "";
        }
    }

    public JsonObject? TakeIfChanged()
    {
        lock (_gate) return _pending;
    }

    /// <summary>The hub accepted <paramref name="payload"/>. Compared by reference, so a read
    /// that replaced the pending payload while the heartbeat was in flight is not dropped.</summary>
    public void MarkSent(JsonObject payload)
    {
        lock (_gate)
        {
            if (!ReferenceEquals(_pending, payload)) return;
            _pending = null;
            _lastSentHash = Hash(payload.ToJsonString());
        }
    }

    public static JsonObject ToPayload(SoftwareReport report)
    {
        var items = new JsonArray();
        foreach (var p in report.Items)
        {
            items.Add(new JsonObject
            {
                ["id"] = p.Id,
                ["name"] = p.Name,
                ["version"] = p.Version,
                ["publisher"] = p.Publisher,
                ["install_date"] = p.InstallDate,
                ["install_location"] = "",
                ["uninstall_string"] = "",
                ["scope"] = "machine",
                ["user_sid"] = "",
                ["arch"] = p.Arch,
            });
        }
        return new JsonObject { ["software"] = items, ["error"] = report.Error };
    }

    private static string Hash(string text) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
}
