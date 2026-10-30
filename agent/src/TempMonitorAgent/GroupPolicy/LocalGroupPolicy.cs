using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>The PC's local Group Policy object, as far as the runner needs it -- the seam that
/// lets <see cref="GroupPolicyRunner"/> be tested against memory rather than the test runner's
/// own <c>C:\Windows\System32\GroupPolicy</c>.</summary>
public interface ILocalGroupPolicy
{
    /// <summary>The machine <c>Registry.pol</c> as it is now; empty when there is none.
    /// Throws <see cref="InvalidDataException"/> when it is not a file that can safely be
    /// rewritten.</summary>
    RegistryPolFile Load();

    /// <summary>Write the file back and bump the GPO version. False, with nothing written,
    /// when the file on disk is no longer the one <see cref="Load"/> read.</summary>
    bool Save(RegistryPolFile file);

    /// <summary>Ask Windows to apply policy now, rather than at its next refresh. Returns
    /// without waiting: the Group Policy service does the work.</summary>
    void Refresh(bool force);
}

/// <summary>
/// The real local GPO (roadmap #32, phase 2): <c>%SystemRoot%\System32\GroupPolicy</c>.
///
/// <para><b>Why the local GPO rather than the registry.</b> Until agent 3.50 the runner wrote
/// <c>HKLM\SOFTWARE\Policies</c> directly, which works but is invisible to every Windows tool
/// an administrator would reach for: gpedit.msc showed "Not configured" over a value that was
/// in force, and <c>gpresult</c> did not list it. Writing the local GPO puts FleetHub's settings
/// where those tools look, and lets Windows' own registry extension do the writing -- including
/// removing a value when its entry goes, the non-tattooing behaviour policy keys are meant
/// to have. A domain GPO still applies after the local one and so still wins, which is the
/// precedence an administrator expects; nothing here fights it.</para>
///
/// <para><b>Somebody else may write the same file.</b> gpedit.msc on the PC reads and rewrites
/// it. <see cref="Save"/> therefore re-reads the file and writes only when it still holds the
/// bytes <see cref="Load"/> parsed; otherwise the pass is dropped and the next one starts from
/// what is there. The window is milliseconds, not a gpedit session -- an editor left open and
/// saved later can still overwrite FleetHub's entries, which the next pass simply puts
/// back.</para>
///
/// <para><b><c>gpt.ini</c> is what makes Windows look.</b> The Group Policy service skips a GPO
/// whose version has not changed, and runs the registry extension for it only when that
/// extension is listed in <c>gPCMachineExtensionNames</c>. So every save bumps the machine half
/// (low word) of <c>Version</c> and makes sure the registry pair is listed. The rest of the
/// file, including anything gpedit or another tool put there, is left as it was.</para>
/// </summary>
public sealed class LocalGroupPolicy(string root, Action<bool> refresh) : ILocalGroupPolicy
{
    /// <summary>The registry client-side extension and the Administrative Templates snap-in
    /// that pairs with it -- exactly what gpedit.msc lists after setting one template.</summary>
    public const string RegistryExtension =
        "[{35378EAC-683F-11D2-A89A-00C04FBBCFA2}{0F6B957E-509E-11D1-A7CC-0000F87571E3}]";

    private const uint RpForce = 1;

    private readonly string _root = root;
    private readonly Action<bool> _refresh = refresh;

    public LocalGroupPolicy()
        : this(Path.Combine(Environment.SystemDirectory, "GroupPolicy"), RefreshWindows) { }

    public string PolicyPath => Path.Combine(_root, "Machine", "Registry.pol");
    public string IniPath => Path.Combine(_root, "gpt.ini");

    public RegistryPolFile Load() =>
        File.Exists(PolicyPath) ? RegistryPolFile.Parse(File.ReadAllBytes(PolicyPath))
                                : RegistryPolFile.Empty();

    public bool Save(RegistryPolFile file)
    {
        var onDisk = File.Exists(PolicyPath)
            ? Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(PolicyPath))) : null;
        if (onDisk != file.LoadedHash) return false;
        Directory.CreateDirectory(Path.GetDirectoryName(PolicyPath)!);
        WriteAtomically(PolicyPath, file.ToBytes());
        // Latin-1 both ways: one byte per char, so whatever encoding the file was written in
        // comes back out unchanged around the two lines touched here.
        var ini = File.Exists(IniPath) ? File.ReadAllText(IniPath, Encoding.Latin1) : null;
        WriteAtomically(IniPath, Encoding.Latin1.GetBytes(GptIni.Bump(ini)));
        return true;
    }

    public void Refresh(bool force) => _refresh(force);

    private static void WriteAtomically(string path, byte[] bytes)
    {
        var temp = path + ".fleethub-tmp";
        try
        {
            File.WriteAllBytes(temp, bytes);
            File.Move(temp, path, overwrite: true);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // Not left in GroupPolicy\Machine, where an administrator would find a stray file
            // next to Registry.pol and wonder what wrote it. The caller reports the failure.
            try { File.Delete(temp); }
            catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException) { }
            throw;
        }
    }

    /// <summary>RefreshPolicyEx for the computer half. Not waited on: it signals the Group
    /// Policy service and returns, and the runner judges the result on a later pass.</summary>
    private static void RefreshWindows(bool force)
    {
        if (!RefreshPolicyEx(true, force ? RpForce : 0))
            throw new IOException($"RefreshPolicyEx failed ({Marshal.GetLastPInvokeError()})");
    }

    [DllImport("userenv.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool RefreshPolicyEx([MarshalAs(UnmanagedType.Bool)] bool bMachine,
                                               uint dwOptions);
}

/// <summary>The two lines of <c>gpt.ini</c> a registry write has to touch. Pure, so it is
/// tested without a file.</summary>
public static class GptIni
{
    private static readonly Regex Guid = new(@"\{[0-9A-Fa-f-]{36}\}", RegexOptions.None,
                                             TimeSpan.FromSeconds(1));
    private static readonly Regex Group = new(@"\[([^\]]*)\]", RegexOptions.None,
                                              TimeSpan.FromSeconds(1));

    /// <summary>The file with the machine version bumped and the registry extension listed.
    /// Null (no file yet) gives the file gpedit.msc would write for one setting.</summary>
    public static string Bump(string? text)
    {
        var lines = (text ?? "").Replace("\r\n", "\n").Split('\n').ToList();
        if (lines.Count > 0 && lines[^1].Length == 0) lines.RemoveAt(lines.Count - 1);
        var general = lines.FindIndex(l => l.Trim().Equals("[General]", StringComparison.OrdinalIgnoreCase));
        if (general < 0)
        {
            lines.Insert(0, "[General]");
            general = 0;
        }
        var end = lines.FindIndex(general + 1, l => l.TrimStart().StartsWith('['));
        if (end < 0) end = lines.Count;

        var version = FindKey(lines, general, end, "Version");
        var current = version >= 0 && uint.TryParse(Value(lines[version]), out var parsed) ? parsed : 0;
        var bumped = $"Version={NextMachineVersion(current)}";
        if (version >= 0) lines[version] = bumped;
        else lines.Insert(end++, bumped);

        var names = FindKey(lines, general, end, "gPCMachineExtensionNames");
        var listed = $"gPCMachineExtensionNames={WithRegistryExtension(names >= 0 ? Value(lines[names]) : "")}";
        if (names >= 0) lines[names] = listed;
        else lines.Insert(general + 1, listed);

        return string.Join("\r\n", lines) + "\r\n";
    }

    /// <summary>The low word is the computer half's version, the high word the user half's.
    /// Only the low word moves; it skips 0 on wrapping, which Windows reads as "no
    /// settings".</summary>
    public static uint NextMachineVersion(uint version)
    {
        var machine = (version & 0xFFFF) + 1;
        if (machine > 0xFFFF) machine = 1;
        return (version & 0xFFFF0000) | machine;
    }

    /// <summary>The list with the registry extension's pair in it. Unchanged when it already
    /// is; otherwise groups are sorted by extension GUID, tools within a group likewise, as
    /// gpedit writes them.</summary>
    public static string WithRegistryExtension(string list)
    {
        var groups = Group.Matches(list)
            .Select(m => Guid.Matches(m.Groups[1].Value).Select(g => g.Value.ToUpperInvariant()).ToList())
            .Where(g => g.Count > 0)
            .ToList();
        var wanted = Guid.Matches(LocalGroupPolicy.RegistryExtension).Select(g => g.Value).ToList();
        var mine = groups.FirstOrDefault(g => g[0] == wanted[0]);
        if (mine is not null && mine.Contains(wanted[1])) return list;
        if (mine is null) groups.Add([wanted[0], wanted[1]]);
        else mine.Add(wanted[1]);
        return string.Concat(groups
            .OrderBy(g => g[0], StringComparer.Ordinal)
            .Select(g => "[" + g[0] + string.Concat(g.Skip(1).Distinct().Order(StringComparer.Ordinal)) + "]"));
    }

    private static int FindKey(List<string> lines, int general, int end, string key)
    {
        for (var i = general + 1; i < end; i++)
        {
            var eq = lines[i].IndexOf('=');
            if (eq > 0 && lines[i][..eq].Trim().Equals(key, StringComparison.OrdinalIgnoreCase)) return i;
        }
        return -1;
    }

    private static string Value(string line) => line[(line.IndexOf('=') + 1)..].Trim();
}
