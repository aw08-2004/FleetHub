using Microsoft.Win32;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>The handful of HKLM operations Group Policy needs, and nothing more -- the seam
/// that lets <see cref="GroupPolicyRunner"/> be tested against a dictionary rather than the
/// test runner's real registry, as <see cref="TempMonitorAgent.Patch.UpdateControl.IPolicyStore"/>
/// does for update control.</summary>
public interface IRegistryValueStore
{
    /// <summary>The value in canonical form (<see cref="PolicyValue.Canonical"/>), or null
    /// when it is absent. A value of a kind policy never writes reads as <c>other:KIND</c>
    /// rather than null: it EXISTS, so somebody wrote it, and treating it as absent would let
    /// the runner overwrite it.</summary>
    string? Get(string subKey, string name);
    void Set(PolicyValue value);
    /// <summary>No-op when the value or key is absent.</summary>
    void Delete(string subKey, string name);
    string[]? GetMultiString(string subKey, string name);
    void SetMultiString(string subKey, string name, string[] value);
}

/// <summary>The real HKLM, 64-bit view (the agent is published win-x64, so
/// Registry.LocalMachine already is).</summary>
public sealed class RegistryValueStore : IRegistryValueStore
{
    public string? Get(string subKey, string name)
    {
        using var key = Registry.LocalMachine.OpenSubKey(subKey);
        if (key is null) return null;
        var raw = key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
        if (raw is null) return null;
        return key.GetValueKind(name) switch
        {
            RegistryValueKind.DWord => $"{PolicyAllowList.Dword}:{unchecked((uint)(int)raw)}",
            RegistryValueKind.String => $"{PolicyAllowList.String}:{raw}",
            RegistryValueKind.ExpandString => $"{PolicyAllowList.ExpandString}:{raw}",
            RegistryValueKind.MultiString =>
                $"{PolicyAllowList.MultiString}:{string.Join('\0', (string[])raw)}",
            var other => $"other:{other}",
        };
    }

    public void Set(PolicyValue value)
    {
        using var key = Registry.LocalMachine.CreateSubKey(value.Path, writable: true);
        switch (value.Type)
        {
            case PolicyAllowList.Dword:
                key.SetValue(value.Name, unchecked((int)value.Dword), RegistryValueKind.DWord);
                break;
            case PolicyAllowList.ExpandString:
                key.SetValue(value.Name, value.Text, RegistryValueKind.ExpandString);
                break;
            case PolicyAllowList.MultiString:
                key.SetValue(value.Name, value.Lines.ToArray(), RegistryValueKind.MultiString);
                break;
            default:
                key.SetValue(value.Name, value.Text, RegistryValueKind.String);
                break;
        }
    }

    public void Delete(string subKey, string name)
    {
        bool nowEmpty;
        using (var key = Registry.LocalMachine.OpenSubKey(subKey, writable: true))
        {
            if (key is null) return;
            key.DeleteValue(name, throwOnMissingValue: false);
            nowEmpty = key.ValueCount == 0 && key.SubKeyCount == 0;
        }
        // Only the marker key is ours to remove once empty. A policy key is left even when
        // empty, as UpdateControl leaves WindowsUpdate: it may be a domain GPO's.
        if (nowEmpty && subKey.Equals(GroupPolicyRunner.MarkerKey, StringComparison.OrdinalIgnoreCase))
            Registry.LocalMachine.DeleteSubKey(subKey, throwOnMissingSubKey: false);
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
}
