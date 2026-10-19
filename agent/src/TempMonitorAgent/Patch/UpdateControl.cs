using Microsoft.Win32;

namespace TempMonitorAgent.Patch;

/// <summary>
/// Makes FleetHub the only thing that installs Windows updates on this PC (roadmap #14,
/// issue #140), and gives that back when asked.
///
/// <para><b>Two policy values, because either alone leaves a hole.</b>
/// <c>AU\NoAutoUpdate=1</c> stops Windows installing on its own schedule, which is what
/// makes an approved update land on a patch run instead of whenever Windows chose to.
/// <c>SetDisableUXWUAccess=1</c> removes the user's "Check for updates" / "Install now"
/// buttons -- without it a person at the PC installs an unapproved cumulative at 10:00 and
/// the maintenance window's careful approval list describes a machine that no longer
/// exists. The owner decided both; the hub decides per maintenance window whether to ask.</para>
///
/// <para><b>A value somebody else wrote is never touched.</b> These keys are exactly where
/// GPO and Intune put their own Windows Update policy, and a PC managed that way has an
/// administrator who decided something. Overwriting it would silently undo that decision
/// and, worse, Release would later DELETE their value as if it were ours. So the agent
/// records each value it writes in a marker (<c>HKLM\SOFTWARE\FleetHub\UpdateControl</c>,
/// REG_MULTI_SZ <c>Written</c>), and any target value that exists without being on the
/// marker means the whole machine is <see cref="ManagedElsewhere"/>: nothing is written, and
/// the hub shows the PC as skipped. All-or-nothing on purpose -- half-applied control (the
/// user locked out but Windows still auto-installing, or the reverse) is worse than either
/// honest state.</para>
///
/// <para><b>The marker is written BEFORE the values it lists.</b> If the agent dies between
/// the two writes, the reverse order leaves a policy value with no marker entry -- which the
/// next pass reads as somebody else's, so the PC would report managed_elsewhere forever and
/// nobody could release it. Marker-first fails the harmless way: an entry for a value that
/// does not exist, which Release ignores and the next Apply fills in.</para>
///
/// <para><b>Release deletes only a marked value that still holds what we wrote.</b> A GPO
/// that later took the same value over with its own setting owns it now; deleting it would
/// be the overwrite above, just delayed. That value is left, the marker goes, and the PC
/// then honestly reports managed_elsewhere.</para>
///
/// <para>Registry access is behind <see cref="IPolicyStore"/> so all of the above is tested
/// against a dictionary rather than this test runner's real HKLM.</para>
/// </summary>
public sealed class UpdateControl(UpdateControl.IPolicyStore store)
{
    /// <summary>The wire vocabulary of <c>control.state</c>. The hub's reconcile loop
    /// compares against these exact strings, so they are spelled once, here.</summary>
    public const string FleetHub = "fleethub";
    public const string Windows = "windows";
    public const string ManagedElsewhere = "managed_elsewhere";

    public const string PolicyRoot = @"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate";
    public const string MarkerKey = @"SOFTWARE\FleetHub\UpdateControl";
    public const string MarkerValue = "Written";

    /// <summary>One policy value this class owns when it is in control. <c>Id</c> is what the
    /// marker records -- the path below <see cref="PolicyRoot"/> -- and is also what
    /// agent-install.ps1 parses on uninstall, so it must stay this exact shape.</summary>
    public sealed record Target(string Id, string SubKey, string Name);

    public static readonly IReadOnlyList<Target> Targets =
    [
        new(@"AU\NoAutoUpdate", PolicyRoot + @"\AU", "NoAutoUpdate"),
        new("SetDisableUXWUAccess", PolicyRoot, "SetDisableUXWUAccess"),
    ];

    /// <summary>The value written to every target. Both policies are "1 = on".</summary>
    private const int On = 1;

    /// <summary>The machine's real HKLM. LocalSystem can write these keys; see
    /// Network/PrepareWakeExecutor.cs for the same pattern.</summary>
    public static UpdateControl ForMachine() => new(new RegistryPolicyStore());

    /// <summary>Who currently decides when Windows updates install here.
    ///
    /// <see cref="FleetHub"/> only when every target holds our value AND is on the marker; a
    /// marked value somebody deleted means control has lapsed, which is <see cref="Windows"/>
    /// until the hub's reconcile loop re-applies it.</summary>
    public string State()
    {
        var marked = ReadMarker();
        var ours = 0;
        foreach (var target in Targets)
        {
            var value = store.GetDword(target.SubKey, target.Name);
            if (value is null) continue;
            if (!marked.Contains(target.Id) || value != On) return ManagedElsewhere;
            ours++;
        }
        return ours == Targets.Count ? FleetHub : Windows;
    }

    /// <summary>Take control. Idempotent: re-applying on a PC already under control rewrites
    /// the same values and the same marker. Returns the resulting <see cref="State"/>.</summary>
    public string Apply()
    {
        var marked = ReadMarker();
        foreach (var target in Targets)
        {
            var value = store.GetDword(target.SubKey, target.Name);
            if (value is null) continue;
            // Checked for EVERY target before anything is written -- see the class remarks
            // on why this is all-or-nothing.
            if (!marked.Contains(target.Id) || value != On) return ManagedElsewhere;
        }

        // Marker first, values second. The order is the point; see the class remarks.
        var ids = new SortedSet<string>(marked, StringComparer.OrdinalIgnoreCase);
        foreach (var target in Targets) ids.Add(target.Id);
        store.SetMultiString(MarkerKey, MarkerValue, [.. ids]);
        foreach (var target in Targets) store.SetDword(target.SubKey, target.Name, On);
        return State();
    }

    /// <summary>Give control back to Windows: remove what we wrote, then the marker. Safe
    /// on a PC that was never under control (nothing is marked, so nothing is touched).
    /// Returns the resulting <see cref="State"/>.</summary>
    public string Release()
    {
        foreach (var id in ReadMarker())
        {
            var target = Targets.FirstOrDefault(
                t => string.Equals(t.Id, id, StringComparison.OrdinalIgnoreCase));
            // An id this build does not know came from a newer agent; it is still ours, but
            // without the target table this build cannot say where it lives. Leaving it is
            // the conservative choice -- the newer agent's own release will find it.
            if (target is null) continue;
            if (store.GetDword(target.SubKey, target.Name) == On)
                store.DeleteValue(target.SubKey, target.Name);
        }
        store.DeleteValue(MarkerKey, MarkerValue);
        return State();
    }

    private HashSet<string> ReadMarker() =>
        new(store.GetMultiString(MarkerKey, MarkerValue) ?? [], StringComparer.OrdinalIgnoreCase);

    /// <summary>The handful of HKLM operations this class needs, and nothing more.</summary>
    public interface IPolicyStore
    {
        /// <summary>The value as a DWORD, or null when it is absent. A value of another type
        /// is reported as -1 rather than null: it EXISTS, so somebody wrote it, and treating it
        /// as absent would let Apply overwrite it.</summary>
        int? GetDword(string subKey, string name);
        void SetDword(string subKey, string name, int value);
        string[]? GetMultiString(string subKey, string name);
        void SetMultiString(string subKey, string name, string[] value);
        /// <summary>No-op when the value or key is absent.</summary>
        void DeleteValue(string subKey, string name);
    }

    /// <summary>The real HKLM, 64-bit view (the agent is published win-x64, so
    /// Registry.LocalMachine already is).</summary>
    private sealed class RegistryPolicyStore : IPolicyStore
    {
        public int? GetDword(string subKey, string name)
        {
            using var key = Registry.LocalMachine.OpenSubKey(subKey);
            var value = key?.GetValue(name);
            return value switch
            {
                null => null,
                int i => i,
                _ => -1,
            };
        }

        public void SetDword(string subKey, string name, int value)
        {
            using var key = Registry.LocalMachine.CreateSubKey(subKey, writable: true);
            key.SetValue(name, value, RegistryValueKind.DWord);
        }

        public string[]? GetMultiString(string subKey, string name)
        {
            using var key = Registry.LocalMachine.OpenSubKey(subKey);
            return key?.GetValue(name) as string[];
        }

        public void SetMultiString(string subKey, string name, string[] value)
        {
            using var key = Registry.LocalMachine.CreateSubKey(subKey, writable: true);
            key.SetValue(name, value, RegistryValueKind.MultiString);
        }

        public void DeleteValue(string subKey, string name)
        {
            bool nowEmpty;
            using (var key = Registry.LocalMachine.OpenSubKey(subKey, writable: true))
            {
                if (key is null) return;
                key.DeleteValue(name, throwOnMissingValue: false);
                nowEmpty = key.ValueCount == 0 && key.SubKeyCount == 0;
            }
            // The marker key is ours alone; once its last value goes, so does it, so an
            // uninstalled PC carries no FleetHub residue. Policy keys are left even when
            // empty -- an empty WindowsUpdate key may be a GPO's, and it costs nothing. The
            // handle is closed first (the using block above) so the delete is not refused.
            if (nowEmpty && subKey.Equals(MarkerKey, StringComparison.OrdinalIgnoreCase))
                Registry.LocalMachine.DeleteSubKey(subKey, throwOnMissingSubKey: false);
        }
    }
}
