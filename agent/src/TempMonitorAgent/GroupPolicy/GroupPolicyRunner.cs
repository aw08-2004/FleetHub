using Microsoft.Extensions.Logging;
using TempMonitorAgent.State;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>
/// Holds the Group Policy registry document the hub resolved for this PC and keeps the
/// registry matching it (roadmap #32).
///
/// <para><b>A value somebody else wrote is never touched.</b> <c>HKLM\SOFTWARE\Policies</c> is
/// exactly where a domain GPO or Intune puts its settings, and a PC managed that way has an
/// administrator who decided something. So, as <see cref="TempMonitorAgent.Patch.UpdateControl"/>
/// does, the runner records every value it writes in a marker
/// (<c>HKLM\SOFTWARE\FleetHub\GroupPolicy</c>, REG_MULTI_SZ <c>Written</c>, one
/// <c>path|name</c> per entry), and a wanted value that already exists without being on the
/// marker is reported <c>managed_elsewhere</c> and left alone. Fighting a domain GPO would
/// flip the value back every refresh, which is worse for the PC than either answer.</para>
///
/// <para><b>A value the runner wrote, that has since been changed by something else, is let
/// go.</b> The last value written is kept beside the document (group_policy.json), and when the
/// registry no longer holds it the value has a new owner: it comes off the marker and is
/// reported <c>managed_elsewhere</c> from then on. Without that, a domain GPO that takes over a
/// value FleetHub set first would be fought forever too. A value that has simply been DELETED
/// is put back -- that is policy being enforced, not somebody else's decision.</para>
///
/// <para><b>Marker before value on write, value before marker on removal.</b> Dying between
/// the two then leaves a marker entry for a value that is not there, which every later pass
/// handles; the reverse order would leave a value with no marker entry, which every later pass
/// would read as somebody else's and never remove.</para>
///
/// <para><b>Removal deletes only what still holds the value written.</b> When a value leaves
/// the document (its policy was unlinked or deleted), it is deleted if the registry still holds
/// what the runner wrote; if somebody changed it meanwhile it is left, for the reason above.</para>
///
/// <para>Registry access is behind <see cref="IRegistryValueStore"/> so all of the above is
/// tested against a dictionary. Fails soft per value: one value that cannot be written costs
/// that value an <c>error</c> report, never the pass.</para>
/// </summary>
public sealed class GroupPolicyRunner(ILogger<GroupPolicyRunner> log, AgentState state,
                                      IRegistryValueStore store)
{
    public const string Applied = "applied";
    public const string ManagedElsewhere = "managed_elsewhere";
    public const string Error = "error";
    public const string Refused = "refused";

    public const string MarkerKey = @"SOFTWARE\FleetHub\GroupPolicy";
    public const string MarkerValue = "Written";

    /// <summary>A minute. Policy is not a watchdog: nothing is down while it waits, and a
    /// domain GPO refreshes every 90 minutes, so this is already far quicker than what an
    /// administrator expects from Group Policy.</summary>
    public const int TickSeconds = 60;

    private readonly ILogger<GroupPolicyRunner> _log = log;
    private readonly AgentState _state = state;
    private readonly IRegistryValueStore _store = store;
    private readonly Lock _gate = new();

    private PolicyDocument _document = PolicyDocument.Empty;
    /// <summary>marker entry (path|name, compared case-insensitively) -> the canonical value
    /// last written there.</summary>
    private Dictionary<string, string> _written = new(StringComparer.OrdinalIgnoreCase);
    private bool _loaded;
    /// <summary>Whether a document has ever been held -- restored from disk or adopted from a
    /// heartbeat. Until one has, <see cref="Tick"/> does nothing: see EnsureLoaded.</summary>
    private bool _haveDocument;

    /// <summary>The version of the document held, for the heartbeat to echo -- and its
    /// presence is what tells the hub this agent can apply registry policy at all.</summary>
    public string Version
    {
        get { lock (_gate) { EnsureLoaded(); return _document.Version; } }
    }

    /// <summary>Adopt a document the hub just sent, and persist it. Nothing is written here;
    /// the next <see cref="Tick"/> does that, on the policy loop's own thread.</summary>
    public void Apply(PolicyDocument document)
    {
        lock (_gate)
        {
            EnsureLoaded();
            _document = document;
            _haveDocument = true;
            Persist();
        }
        _log.LogInformation("Holding {Count} Group Policy registry values (document {Version})",
                            document.Values.Count, document.Version);
    }

    /// <summary>One reconcile pass: write what is wanted, let go of what is not.</summary>
    public void Tick()
    {
        lock (_gate)
        {
            EnsureLoaded();
            if (!_haveDocument) return;
            var marker = ReadMarker();
            var wanted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var changed = false;

            foreach (var value in _document.Values)
            {
                var entry = Entry(value.Path, value.Name);
                wanted.Add(entry);
                var refusal = PolicyAllowList.Refusal(value);
                if (refusal is not null)
                {
                    GroupPolicyReporter.Record(value.Id, Refused, refusal);
                    continue;
                }
                try
                {
                    changed |= Reconcile(value, entry, marker);
                }
                catch (Exception e) when (e is UnauthorizedAccessException or IOException
                                               or System.Security.SecurityException
                                               or ArgumentException)
                {
                    GroupPolicyReporter.Record(value.Id, Error, e.Message);
                }
            }

            foreach (var entry in marker.ToList())
            {
                if (wanted.Contains(entry)) continue;
                try
                {
                    ReleaseEntry(entry, marker);
                    changed = true;
                }
                catch (Exception e) when (e is UnauthorizedAccessException or IOException
                                               or System.Security.SecurityException)
                {
                    _log.LogWarning(e, "Could not remove policy value {Entry}", entry);
                }
            }

            if (changed) Persist();
        }
    }

    /// <summary>Remove every value this agent wrote, then the marker -- before an uninstall,
    /// so a deleted agent leaves no policy behind (roadmap #30). Values somebody changed since
    /// are left, as on any other removal.</summary>
    public void Release()
    {
        lock (_gate)
        {
            EnsureLoaded();
            var marker = ReadMarker();
            foreach (var entry in marker.ToList()) ReleaseEntry(entry, marker);
            _store.Delete(MarkerKey, MarkerValue);
            _document = PolicyDocument.Empty;
            Persist();
        }
    }

    /// <summary><see cref="Release"/> against the real HKLM, for SelfUninstaller, which runs
    /// outside the container's lifetime and should not have to be handed the runner.</summary>
    public static void ReleaseMachine() =>
        new GroupPolicyRunner(
            Microsoft.Extensions.Logging.Abstractions.NullLogger<GroupPolicyRunner>.Instance,
            new AgentState(), new RegistryValueStore()).Release();

    /// <summary>Bring one wanted value into line. Returns true when the marker or the
    /// written-value record changed.</summary>
    private bool Reconcile(PolicyValue value, string entry, HashSet<string> marker)
    {
        var current = _store.Get(value.Path, value.Name);
        var desired = value.Canonical;

        if (!marker.Contains(entry))
        {
            if (current is not null)
            {
                GroupPolicyReporter.Record(value.Id, ManagedElsewhere,
                    current == desired
                        ? "already set to this value by something other than FleetHub"
                        : "set by something other than FleetHub, which is left alone");
                return false;
            }
            marker.Add(entry);
            WriteMarker(marker);          // marker first -- see the class remarks
            _store.Set(value);
            _written[entry] = desired;
            GroupPolicyReporter.Record(value.Id, Applied, "");
            return true;
        }

        if (current == desired)
        {
            var changed = !_written.TryGetValue(entry, out var known) || known != desired;
            _written[entry] = desired;
            GroupPolicyReporter.Record(value.Id, Applied, "");
            return changed;
        }

        if (current is not null && _written.TryGetValue(entry, out var last) && current != last)
        {
            // Somebody changed a value we wrote: it is theirs now. See the class remarks.
            marker.Remove(entry);
            WriteMarker(marker);
            _written.Remove(entry);
            GroupPolicyReporter.Record(value.Id, ManagedElsewhere,
                "changed by something other than FleetHub after it was set, so it was let go");
            return true;
        }

        // Ours and out of date (the policy changed), deleted by somebody, or ours with no
        // record of what was written (state file lost): write the wanted value.
        _store.Set(value);
        _written[entry] = desired;
        GroupPolicyReporter.Record(value.Id, Applied, "");
        return true;
    }

    private void ReleaseEntry(string entry, HashSet<string> marker)
    {
        var (path, name) = Split(entry);
        if (path is not null && name is not null)
        {
            var current = _store.Get(path, name);
            // Deleted only while it still holds what was written. With no record of what that
            // was (state file lost), a marked value is still ours by the marker's word.
            if (current is not null
                && (!_written.TryGetValue(entry, out var last) || current == last))
                _store.Delete(path, name);
            GroupPolicyReporter.Removed(PolicyAllowList.IdFor(path, name));
        }
        marker.Remove(entry);           // value first, marker second -- see the class remarks
        WriteMarker(marker);
        _written.Remove(entry);
    }

    private static string Entry(string path, string name) => $"{path}|{name}";

    private static (string? Path, string? Name) Split(string entry)
    {
        // A path cannot contain '|' (PolicyAllowList), so the first one is the separator.
        var at = entry.IndexOf('|');
        if (at <= 0 || at == entry.Length - 1) return (null, null);
        var path = entry[..at];
        // An entry pointing outside the policy tree did not come from this runner; leave it.
        return path.StartsWith(PolicyAllowList.Root, StringComparison.OrdinalIgnoreCase)
            && !path.Contains("..") ? (path, entry[(at + 1)..]) : (null, null);
    }

    private HashSet<string> ReadMarker() =>
        new(_store.GetMultiString(MarkerKey, MarkerValue) ?? [], StringComparer.OrdinalIgnoreCase);

    private void WriteMarker(HashSet<string> marker)
    {
        if (marker.Count == 0) _store.Delete(MarkerKey, MarkerValue);
        else _store.SetMultiString(MarkerKey, MarkerValue,
                                   [.. marker.Order(StringComparer.OrdinalIgnoreCase)]);
    }

    // --- persistence -------------------------------------------------------

    /// <summary>Load the document and the written-value record, once per process.
    ///
    /// A missing or unreadable file leaves NO document, and <see cref="Tick"/> then waits for
    /// the first heartbeat to deliver one rather than reconciling against nothing -- which
    /// would delete every value this agent wrote at boot and put them all back ten seconds
    /// later. The marker in the registry still says what is ours in the meantime, and the
    /// hub resends the document because the empty version this reports cannot match.</summary>
    private void EnsureLoaded()
    {
        if (_loaded) return;
        _loaded = true;
        var stored = _state.LoadGroupPolicyState();
        if (stored is null) return;
        _haveDocument = true;
        _document = new PolicyDocument
        {
            Version = stored.Version ?? "",
            Values = [.. (stored.Values ?? []).Select(v => v with
            {
                Id = PolicyAllowList.IdFor(v.Path, v.Name),
            })],
        };
        _written = new Dictionary<string, string>(stored.Written ?? [],
                                                  StringComparer.OrdinalIgnoreCase);
    }

    private void Persist()
    {
        _state.SaveGroupPolicyState(new StoredGroupPolicyState
        {
            Version = _document.Version,
            Values = [.. _document.Values],
            Written = new Dictionary<string, string>(_written),
        });
    }
}

/// <summary>What group_policy.json holds.</summary>
public sealed class StoredGroupPolicyState
{
    public string? Version { get; set; }
    public List<PolicyValue>? Values { get; set; }
    public Dictionary<string, string>? Written { get; set; }
}
