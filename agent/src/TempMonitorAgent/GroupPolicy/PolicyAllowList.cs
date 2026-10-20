using System.Security.Cryptography;
using System.Text;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>
/// Where Group Policy may write, enforced on the PC (roadmap #32).
///
/// **The hub checks the same rules (gpo.registry_path_error) and this is NOT redundant.** The
/// hub is not a trust root for what LocalSystem writes into HKLM -- the same principle that
/// keeps update URLs and signing keys off the config channel (RuntimeConfig). If the hub were
/// ever wrong, or compromised, the worst a policy document could do is write under
/// <c>HKLM\SOFTWARE\Policies</c>, the tree Windows reserves for exactly this; it can never
/// reach the services key, Run keys, or the agent's own configuration.
///
/// Inside that tree, two keys are refused because something on this PC already owns them:
/// Windows Update policy, which <see cref="TempMonitorAgent.Patch.UpdateControl"/> writes and
/// would fight a second writer over, and FleetHub's own key.
/// </summary>
public static class PolicyAllowList
{
    public const string Dword = "dword";
    public const string String = "string";
    public const string ExpandString = "expand_string";
    public const string MultiString = "multi_string";

    public const string Root = @"SOFTWARE\Policies\";
    public const int MaxPathChars = 255;
    public const int MaxNameChars = 255;
    public const int MaxStringChars = 2048;
    public const int MaxLines = 64;

    private static readonly string[] Denied =
    [
        @"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
        @"SOFTWARE\Policies\FleetHub",
    ];

    private static readonly char[] BadPathChars = ['/', '*', '?', '"', '<', '>', '|'];

    /// <summary>Why this value may not be written, or null when it may.</summary>
    public static string? Refusal(PolicyValue value)
    {
        var path = value.Path;
        if (string.IsNullOrEmpty(path) || path.Length > MaxPathChars)
            return "the key path is empty or too long";
        if (!path.StartsWith(Root, StringComparison.OrdinalIgnoreCase) || path.Length <= Root.Length)
            return @"only keys under HKLM\SOFTWARE\Policies may be written";
        foreach (var part in path.Split('\\'))
        {
            if (part.Length == 0 || part is "." or ".." || part.IndexOfAny(BadPathChars) >= 0
                || part.Any(char.IsControl))
                return "the key path has an empty or invalid component";
        }
        foreach (var denied in Denied)
        {
            if (path.Equals(denied, StringComparison.OrdinalIgnoreCase)
                || path.StartsWith(denied + "\\", StringComparison.OrdinalIgnoreCase))
                return $"{denied} is owned by FleetHub itself";
        }
        if (string.IsNullOrEmpty(value.Name) || value.Name.Length > MaxNameChars
            || value.Name.Contains('\\') || value.Name.Any(char.IsControl))
            return "the value name is not valid";
        return value.Type switch
        {
            Dword => null,
            String or ExpandString => value.Text.Length > MaxStringChars || value.Text.Contains('\0')
                ? "the text is too long or contains a NUL" : null,
            MultiString => value.Lines.Count > MaxLines
                           || value.Lines.Any(l => l.Length > MaxStringChars || l.Contains('\0'))
                ? "too many lines, or a line too long or containing a NUL" : null,
            _ => "unknown value type",
        };
    }

    /// <summary>The id the hub and the agent both key one value on (gpo.setting_id): the
    /// first 16 hex characters of SHA-256 over <c>lower(path)|lower(name)</c>. Case-folded,
    /// because the registry is case-insensitive and two spellings are one value.</summary>
    public static string IdFor(string path, string name)
    {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(
            $"{path.ToLowerInvariant()}|{name.ToLowerInvariant()}"));
        return Convert.ToHexStringLower(bytes)[..16];
    }
}
