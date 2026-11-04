using Microsoft.Extensions.Logging;
using TempMonitorAgent.State;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>
/// Holds the Group Policy registry document the hub resolved for this PC and keeps the PC's
/// local Group Policy object matching it (roadmap #32).
///
/// <para><b>Since agent 3.49 the values go into the local GPO, not the registry</b>
/// (<see cref="LocalGroupPolicy"/> says why). The runner edits
/// <c>Machine\Registry.pol</c>, and Windows' registry extension writes
/// <c>HKLM\SOFTWARE\Policies</c> from it. So there are two places to read: the file, which is
/// what this PC's local policy SAYS, and the registry, which is what is IN FORCE -- the local
/// GPO's value, or a domain GPO's, which Windows applies later and so wins.</para>
///
/// <para><b>Since 3.49 there are two halves, as in gpedit</b> (phase 3). Computer
/// Configuration is everything above. User Configuration goes into <c>User\Registry.pol</c>,
/// which Windows applies to <c>HKCU</c> of every user who signs in to the PC -- the agent never
/// loads a hive, which is what kept HKCU out before. Each half has its own file, its own marker
/// value and its own entries in the written-value record, and every rule below holds in each,
/// with one difference: a user value is in force in whoever's hive is loaded, not in one place
/// this service can read, so for the User half the file is taken at its word -- there is no
/// "somebody else wrote the registry" check, no override report and no repair.</para>
///
/// <para><b>A value somebody else set is never touched.</b> As
/// <see cref="TempMonitorAgent.Patch.UpdateControl"/> does, the runner records every value it
/// owns in a marker (<c>HKLM\SOFTWARE\FleetHub\GroupPolicy</c>, REG_MULTI_SZ <c>Written</c> for
/// the Computer half and <c>WrittenUser</c> for the User half, one <c>path|name</c> per entry).
/// A wanted value that is not on the marker but is already in the local GPO (somebody used
/// gpedit.msc) or, for the Computer half, in the registry (a domain GPO, Intune, anything) is
/// reported <c>managed_elsewhere</c> and left alone. Fighting either would flip the value back
/// every refresh, which is worse for the PC than either answer.</para>
///
/// <para><b>A value the runner set, that has since been changed in the local GPO, is let
/// go.</b> The last value written is kept beside the document (group_policy.json); when the file
/// no longer holds it, the value has a new owner, comes off the marker and is reported
/// <c>managed_elsewhere</c> from then on. A value simply REMOVED from the file is put back --
/// that is policy being enforced, not somebody else's decision. A value a domain GPO overrides
/// is neither: it stays in the local GPO, where it takes effect again if the domain policy
/// goes, and is reported <c>managed_elsewhere</c> while it is overridden.</para>
///
/// <para><b>A delete is a row like any other.</b> A Disabled Administrative Template often
/// asks for a value to be deleted rather than set; the hub sends that as a
/// <see cref="PolicyAllowList.Delete"/> row, which goes into the file as gpedit's own
/// <c>**del.Name</c> entry. Every rule above holds for it unchanged, and it is in force when the
/// registry does NOT hold the value. Releasing it removes the entry, which does not bring a
/// deleted value back -- nor should it: nothing remembers what the value was.</para>
///
/// <para><b>The registry is only judged once Windows has had time to apply.</b> Refreshing
/// policy is asynchronous, so straight after a save the registry still holds the old value, and
/// reading that as "overridden" would be wrong every time. Until <see cref="SettleSeconds"/>
/// after the last refresh the file is taken at its word.</para>
///
/// <para><b>Marker before value on write, value before marker on removal.</b> The file is
/// written once per pass, so the marker gains new entries before the save and loses released
/// ones only after it succeeded. Dying in between then leaves a marker entry for a value that
/// is not there, which every later pass handles; the reverse order would leave a value with no
/// marker entry, which every later pass would read as somebody else's and never remove.</para>
///
/// <para><b>Values written straight to the registry by an older agent are taken over, not
/// stranded.</b> A marked value missing from the file but present in the registry, still
/// holding what was written, is moved into the file if it is wanted -- Windows then rewrites
/// the same data -- and deleted from the registry directly if it is not, since no GPO entry
/// will ever make Windows remove it. Computer half only: no agent ever wrote HKCU.</para>
///
/// <para>Both places are behind interfaces (<see cref="IRegistryValueStore"/>,
/// <see cref="ILocalGroupPolicy"/>), so all of the above is tested against memory. Fails soft
/// per value where it can: a value that cannot be judged costs that value an <c>error</c>
/// report, never the pass. A file that cannot be written costs that half's pass, and the next
/// one tries again.</para>
/// </summary>
public sealed class GroupPolicyRunner(ILogger<GroupPolicyRunner> log, AgentState state,
                                      IRegistryValueStore store, ILocalGroupPolicy local,
                                      TimeProvider clock)
{
    public const string Applied = "applied";
    public const string ManagedElsewhere = "managed_elsewhere";
    public const string Error = "error";
    public const string Refused = "refused";

    public const string MarkerKey = @"SOFTWARE\FleetHub\GroupPolicy";
    public const string MarkerValue = "Written";
    /// <summary>The User half's marker: a value of its own beside <see cref="MarkerValue"/>,
    /// not prefixed entries in it, so an agent before 3.49 -- which would read any entry it
    /// cannot place as not its own and drop it -- never sees one.</summary>
    public const string UserMarkerValue = "WrittenUser";

    /// <summary>A minute. Policy is not a watchdog: nothing is down while it waits, and a
    /// domain GPO refreshes every 90 minutes, so this is already far quicker than what an
    /// administrator expects from Group Policy.</summary>
    public const int TickSeconds = 60;

    /// <summary>How long after asking Windows to refresh policy the registry is believed. A
    /// refresh normally lands in seconds; five minutes leaves room for a slow one without
    /// leaving a domain override unreported for long.</summary>
    public const int SettleSeconds = 300;

    /// <summary>How often a value that is in the file but not in force may force a full
    /// refresh. A forced refresh re-runs every extension of every GPO, so a PC where local
    /// policy processing is switched off must not get one a minute.</summary>
    public const int RepairSeconds = 3600;

    /// <summary>What this agent can do with a policy document, sent beside <c>gpo_version</c>
    /// so the console can say how a PC applies it. <c>lgpo</c>: values go into the local GPO;
    /// <c>user</c>: User Configuration rows are applied, not dropped.</summary>
    public static IReadOnlyList<string> Features { get; } = ["lgpo", "user"];

    /// <summary>One half of the local GPO: its class, its marker value, and how its entries are
    /// keyed in the written-value record -- a machine entry exactly as before 3.49, so the
    /// record a 3.48 agent persisted still reads.</summary>
    private sealed record Scope(string Class, string MarkerName)
    {
        public bool IsMachine => Class == PolicyAllowList.Machine;
        public string WrittenKey(string entry) => IsMachine ? entry : $"{PolicyAllowList.User}:{entry}";
    }

    private static readonly Scope[] Scopes =
    [
        new(PolicyAllowList.Machine, MarkerValue),
        new(PolicyAllowList.User, UserMarkerValue),
    ];

    private readonly ILogger<GroupPolicyRunner> _log = log;
    private readonly AgentState _state = state;
    private readonly IRegistryValueStore _store = store;
    private readonly ILocalGroupPolicy _local = local;
    private readonly TimeProvider _clock = clock;
    private readonly Lock _gate = new();

    private PolicyDocument _document = PolicyDocument.Empty;
    /// <summary>written key (<see cref="Scope.WrittenKey"/>, compared case-insensitively) ->
    /// the canonical value last written there.</summary>
    private Dictionary<string, string> _written = new(StringComparer.OrdinalIgnoreCase);
    private bool _loaded;
    /// <summary>Whether a document has ever been held -- restored from disk or adopted from a
    /// heartbeat. Until one has, <see cref="Tick"/> does nothing: see EnsureLoaded.</summary>
    private bool _haveDocument;
    /// <summary>When Windows was last asked to apply policy. Starts at construction: what
    /// happened before this process started is unknown, so the first pass after a start
    /// waits as if it had just asked.</summary>
    private DateTimeOffset _refreshedAt = clock.GetUtcNow();
    private DateTimeOffset _repairedAt = DateTimeOffset.MinValue;

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

    /// <summary>One reconcile pass over both halves: write what is wanted, let go of what is
    /// not.</summary>
    public void Tick()
    {
        lock (_gate)
        {
            EnsureLoaded();
            if (!_haveDocument) return;
            var settled = _clock.GetUtcNow() - _refreshedAt >= TimeSpan.FromSeconds(SettleSeconds);
            var changed = false;
            var missing = false;
            foreach (var scope in Scopes)
            {
                if (TickScope(scope, settled) is not { } pass) continue;
                changed |= pass.Changed;
                missing |= pass.Missing;
            }
            if (missing) Repair();
            if (changed) Persist();
        }
    }

    /// <summary>One half's pass, or null when there was nothing to do or its file could not be
    /// read.</summary>
    private Pass? TickScope(Scope scope, bool settled)
    {
        var values = _document.Values.Where(v => v.Class == scope.Class).ToList();
        var marker = ReadMarker(scope);
        // Nothing wanted and nothing ours: the file is not even read. Most PCs carry no User
        // Configuration, and a User\Registry.pol this agent never wrote is none of its business.
        if (values.Count == 0 && marker.Count == 0) return null;
        RegistryPolFile file;
        try
        {
            file = _local.Load(scope.Class);
        }
        catch (Exception e) when (e is InvalidDataException or IOException
                                       or UnauthorizedAccessException)
        {
            // Refused, not rewritten: see RegistryPolFile. Every value of this half says why.
            foreach (var value in values)
                GroupPolicyReporter.Record(value.Id, Error,
                    $"the PC's local Group Policy cannot be read: {e.Message}");
            return null;
        }

        var pass = new Pass(scope, file, marker, settled);
        pass.Changed = ReconcileAll(values, pass, out var wanted);
        var released = ReleaseUnwanted(wanted, pass);
        // Value first, marker second -- see the class remarks: an entry leaves the marker
        // only once the file without it has been written.
        if (Save(pass)) pass.Changed |= Unmark(pass, released);
        return pass;
    }

    /// <summary>Reconcile one half's values. Returns true when the marker or the written-value
    /// record changed; <paramref name="wanted"/> is every entry those values name, refused ones
    /// included, so none of them is released.</summary>
    private bool ReconcileAll(List<PolicyValue> values, Pass pass, out HashSet<string> wanted)
    {
        wanted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var changed = false;
        foreach (var value in values)
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
                changed |= Reconcile(value, entry, pass);
            }
            catch (Exception e) when (e is UnauthorizedAccessException or IOException
                                           or System.Security.SecurityException
                                           or ArgumentException)
            {
                GroupPolicyReporter.Record(value.Id, Error, e.Message);
            }
        }
        return changed;
    }

    /// <summary>Take every marked entry the document no longer names out of the file. Returns
    /// the entries handled, which stay on the marker until the file is saved.</summary>
    private List<string> ReleaseUnwanted(HashSet<string> wanted, Pass pass)
    {
        var released = new List<string>();
        foreach (var entry in pass.Marker.Where(e => !wanted.Contains(e)))
        {
            try
            {
                ReleaseEntry(entry, pass);
                released.Add(entry);
            }
            catch (Exception e) when (e is UnauthorizedAccessException or IOException
                                           or System.Security.SecurityException)
            {
                _log.LogWarning(e, "Could not remove policy value {Entry}", entry);
            }
        }
        return released;
    }

    /// <summary>Drop released entries from the marker and the written-value record. Returns
    /// true when there were any.</summary>
    private bool Unmark(Pass pass, List<string> released)
    {
        if (released.Count == 0) return false;
        foreach (var entry in released)
        {
            pass.Marker.Remove(entry);
            _written.Remove(pass.Scope.WrittenKey(entry));
        }
        WriteMarker(pass.Scope, pass.Marker);
        return true;
    }

    /// <summary>Remove every value this agent set, in both halves, then the markers -- before
    /// an uninstall, so a deleted agent leaves no policy behind (roadmap #30). Values somebody
    /// changed since are left, as on any other removal. Retried when somebody writes a file at
    /// the same moment; throws rather than dropping a marker over values still in its file.</summary>
    public void Release()
    {
        lock (_gate)
        {
            EnsureLoaded();
            foreach (var scope in Scopes) ReleaseScope(scope);
            _document = PolicyDocument.Empty;
            _written.Clear();
            Persist();
        }
    }

    private void ReleaseScope(Scope scope)
    {
        var marker = ReadMarker(scope);
        if (marker.Count == 0) return;
        var done = false;
        for (var attempt = 0; attempt < 3 && !done; attempt++)
        {
            var pass = new Pass(scope, _local.Load(scope.Class), marker, settled: true);
            foreach (var entry in marker) ReleaseEntry(entry, pass);
            done = !pass.File.Changed || _local.Save(scope.Class, pass.File);
            if (done && pass.File.Changed && scope.IsMachine) _local.Refresh(false);
        }
        if (!done)
            throw new IOException("the local Group Policy kept changing while FleetHub removed its values");
        _store.Delete(MarkerKey, scope.MarkerName);
    }

    /// <summary><see cref="Release"/> against the real PC, for SelfUninstaller, which runs
    /// outside the container's lifetime and should not have to be handed the runner.</summary>
    public static void ReleaseMachine() =>
        new GroupPolicyRunner(
            Microsoft.Extensions.Logging.Abstractions.NullLogger<GroupPolicyRunner>.Instance,
            new AgentState(), new RegistryValueStore(), new LocalGroupPolicy(),
            TimeProvider.System).Release();

    /// <summary>What one pass has done to one half's file, and what to report once it is
    /// saved.</summary>
    private sealed class Pass(Scope scope, RegistryPolFile file, HashSet<string> marker, bool settled)
    {
        public Scope Scope { get; } = scope;
        public RegistryPolFile File { get; } = file;
        public HashSet<string> Marker { get; } = marker;
        public bool Settled { get; } = settled;
        /// <summary>Values whose entry this pass wrote: Applied once the save lands.</summary>
        public List<PolicyValue> Written { get; } = [];
        /// <summary>A value in the file that Windows has not put in force.</summary>
        public bool Missing { get; set; }
        /// <summary>The marker or the written-value record changed.</summary>
        public bool Changed { get; set; }
    }

    /// <summary>Bring one wanted value into line. Returns true when the marker or the
    /// written-value record changed.</summary>
    private bool Reconcile(PolicyValue value, string entry, Pass pass)
    {
        var held = pass.File.Get(value.Path, value.Name);
        var desired = value.Canonical;
        var key = pass.Scope.WrittenKey(entry);
        _written.TryGetValue(key, out var last);

        if (!pass.Marker.Contains(entry)) return Adopt(value, entry, pass, held);

        if (held == desired)
        {
            var changed = last != desired;
            _written[key] = desired;
            ReportInForce(value, desired, pass);
            return changed;
        }

        if (ChangedBySomebodyElse(value, held, last, pass.Scope))
        {
            pass.Marker.Remove(entry);
            WriteMarker(pass.Scope, pass.Marker);
            _written.Remove(key);
            GroupPolicyReporter.Record(value.Id, ManagedElsewhere,
                "changed by something other than FleetHub after it was set, so it was let go");
            return true;
        }

        // Ours and out of date (the policy changed), removed from the file by somebody, an
        // older agent's direct write being moved into the file, or ours with no record of what
        // was written (state file lost): write the wanted value.
        return Write(value, entry, pass);
    }

    /// <summary>A wanted value not on the marker: take it on only when nobody else has said
    /// anything about it -- in the file, or for the Computer half in the registry.</summary>
    private bool Adopt(PolicyValue value, string entry, Pass pass, string? held)
    {
        var desired = value.Canonical;
        if (held is not null)
        {
            GroupPolicyReporter.Record(value.Id, ManagedElsewhere,
                held == desired
                    ? "already set to this value in the PC's local Group Policy by someone other than FleetHub"
                    : "set in the PC's local Group Policy by someone other than FleetHub, which is left alone");
            return false;
        }
        if (pass.Scope.IsMachine && _store.Get(value.Path, value.Name) is { } current)
        {
            GroupPolicyReporter.Record(value.Id, ManagedElsewhere,
                current == desired
                    ? "already set to this value by something other than FleetHub"
                    : "set by something other than FleetHub, which is left alone");
            return false;
        }
        pass.Marker.Add(entry);
        WriteMarker(pass.Scope, pass.Marker);          // marker first -- see the class remarks
        return Write(value, entry, pass);
    }

    /// <summary>Whether somebody changed a value we set, making it theirs. In the file, that is
    /// gpedit. Missing from the file but changed in the registry, it is an older agent's direct
    /// write that a domain GPO (or anyone) took over before this agent arrived -- Computer half
    /// only. With no record of what was written (state file lost), nothing can be told apart,
    /// and the marker's word stands.</summary>
    private bool ChangedBySomebodyElse(PolicyValue value, string? held, string? last, Scope scope)
    {
        if (last is null) return false;
        if (held is not null) return held != last;
        return scope.IsMachine && _store.Get(value.Path, value.Name) is { } inForce && inForce != last;
    }

    private bool Write(PolicyValue value, string entry, Pass pass)
    {
        pass.File.Set(value);
        _written[pass.Scope.WrittenKey(entry)] = value.Canonical;
        pass.Written.Add(value);
        return true;
    }

    /// <summary>Report a value the file already holds, judged against the registry once
    /// Windows has had time to apply it. A User Configuration value is in force in whichever
    /// user's hive is loaded, nowhere this service can look, so it is taken at the file's
    /// word.</summary>
    private void ReportInForce(PolicyValue value, string desired, Pass pass)
    {
        if (!pass.Settled || !pass.Scope.IsMachine)
        {
            GroupPolicyReporter.Record(value.Id, Applied, "");
            return;
        }
        var current = _store.Get(value.Path, value.Name);
        // A delete is in force when the value is gone; one that is still there was set by
        // something applied after the local GPO -- a domain or MDM policy.
        if (current == desired || (current is null && value.Type == PolicyAllowList.Delete))
        {
            GroupPolicyReporter.Record(value.Id, Applied, "");
        }
        else if (current is not null)
        {
            GroupPolicyReporter.Record(value.Id, ManagedElsewhere,
                "a domain or MDM policy sets this value, and it wins over the local Group Policy");
        }
        else
        {
            pass.Missing = true;
            GroupPolicyReporter.Record(value.Id, Error,
                "in the PC's local Group Policy, but Windows has not applied it -- local policy processing may be turned off");
        }
    }

    /// <summary>Remove one marked value from the file -- or, for an older agent's direct write
    /// that never made it into the file, from the registry. Only while it still holds what was
    /// written; with no record of that (state file lost), a marked value is ours by the
    /// marker's word.</summary>
    private void ReleaseEntry(string entry, Pass pass)
    {
        var (path, name) = Split(entry);
        if (path is null || name is null) return;
        _written.TryGetValue(pass.Scope.WrittenKey(entry), out var last);
        var held = pass.File.Get(path, name);
        if (held is not null)
        {
            if (last is null || held == last) pass.File.Delete(path, name);
        }
        else if (pass.Scope.IsMachine && _store.Get(path, name) is { } current
                 && (last is null || current == last))
        {
            _store.Delete(path, name);
        }
        GroupPolicyReporter.Removed(PolicyAllowList.IdFor(path, name, pass.Scope.Class));
    }

    /// <summary>Write the file if this pass changed it, and for the Computer half ask Windows
    /// to apply it. The User half is applied by Windows at each user's next sign-in, or at the
    /// background refresh of whoever is signed in; there is no refresh a service can ask for on
    /// their behalf. False when it was not written -- somebody else wrote it first, or it could
    /// not be -- so nothing that depends on the write (released marker entries) happens this
    /// pass.</summary>
    private bool Save(Pass pass)
    {
        if (!pass.File.Changed) return true;
        try
        {
            if (!_local.Save(pass.Scope.Class, pass.File))
            {
                _log.LogInformation("The local Group Policy changed while it was being updated; trying again next pass");
                return false;
            }
        }
        catch (Exception e) when (e is UnauthorizedAccessException or IOException)
        {
            _log.LogWarning(e, "Could not write the local Group Policy");
            foreach (var value in pass.Written) GroupPolicyReporter.Record(value.Id, Error, e.Message);
            return false;
        }
        foreach (var value in pass.Written) GroupPolicyReporter.Record(value.Id, Applied, "");
        if (pass.Scope.IsMachine) RequestRefresh(force: false);
        return true;
    }

    /// <summary>A value is in the file and not in force. A plain refresh skips a GPO whose
    /// version did not change, so this one is forced -- at most once per
    /// <see cref="RepairSeconds"/>.</summary>
    private void Repair()
    {
        var now = _clock.GetUtcNow();
        if (now - _repairedAt < TimeSpan.FromSeconds(RepairSeconds)) return;
        _repairedAt = now;
        RequestRefresh(force: true);
    }

    private void RequestRefresh(bool force)
    {
        _refreshedAt = _clock.GetUtcNow();
        try
        {
            _local.Refresh(force);
        }
        catch (IOException e)
        {
            // Windows applies the file at its next background refresh anyway.
            _log.LogWarning(e, "Could not ask Windows to refresh Group Policy");
        }
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

    private HashSet<string> ReadMarker(Scope scope) =>
        new(_store.GetMultiString(MarkerKey, scope.MarkerName) ?? [], StringComparer.OrdinalIgnoreCase);

    private void WriteMarker(Scope scope, HashSet<string> marker)
    {
        if (marker.Count == 0) _store.Delete(MarkerKey, scope.MarkerName);
        else _store.SetMultiString(MarkerKey, scope.MarkerName,
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
            // Each list says its class; a stray class inside a row is not trusted over it.
            Values = [.. (stored.Values ?? []).Select(v => Restored(v, PolicyAllowList.Machine)),
                      .. (stored.UserValues ?? []).Select(v => Restored(v, PolicyAllowList.User))],
        };
        _written = new Dictionary<string, string>(stored.Written ?? [],
                                                  StringComparer.OrdinalIgnoreCase);
    }

    private static PolicyValue Restored(PolicyValue value, string cls) => value with
    {
        Class = cls,
        Id = PolicyAllowList.IdFor(value.Path, value.Name, cls),
    };

    /// <summary>Save the document and the written-value record. **User Configuration rows go in
    /// a list of their own**, <c>UserValues</c>: an agent before 3.49 reads <c>Values</c>,
    /// ignores the class inside a row, and applies every row it finds there to the Computer half
    /// on its first pass after a start -- before any heartbeat could tell it otherwise. That is
    /// exactly what a pin lowered back from 3.49 would do on every PC with User Configuration.</summary>
    private void Persist()
    {
        _state.SaveGroupPolicyState(new StoredGroupPolicyState
        {
            Version = _document.Version,
            Values = [.. _document.Values.Where(v => v.Class != PolicyAllowList.User)],
            UserValues = [.. _document.Values.Where(v => v.Class == PolicyAllowList.User)],
            Written = new Dictionary<string, string>(_written),
        });
    }
}

/// <summary>What group_policy.json holds.</summary>
public sealed class StoredGroupPolicyState
{
    public string? Version { get; set; }
    /// <summary>Computer Configuration rows -- the only list an agent before 3.49 reads.</summary>
    public List<PolicyValue>? Values { get; set; }
    /// <summary>User Configuration rows, kept apart so an older agent never sees them.</summary>
    public List<PolicyValue>? UserValues { get; set; }
    public Dictionary<string, string>? Written { get; set; }
}
