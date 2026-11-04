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
    /// <summary>One half's <c>Registry.pol</c> (<see cref="PolicyAllowList.Machine"/> or
    /// <see cref="PolicyAllowList.User"/>) as it is now; empty when there is none. Throws
    /// <see cref="InvalidDataException"/> when it is not a file that can safely be
    /// rewritten.</summary>
    RegistryPolFile Load(string cls);

    /// <summary>Write the file back and bump that half's GPO version. False, with nothing
    /// written, when the file on disk is no longer the one <see cref="Load"/> read.</summary>
    bool Save(string cls, RegistryPolFile file);

    /// <summary>Ask Windows to apply the computer half now, rather than at its next refresh.
    /// Returns without waiting: the Group Policy service does the work.</summary>
    void Refresh(bool force);
}

/// <summary>
/// The real local GPO (roadmap #32, phase 2): <c>%SystemRoot%\System32\GroupPolicy</c>.
///
/// <para><b>Why the local GPO rather than the registry.</b> Until agent 3.49 the runner wrote
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
/// extension is listed in <c>gPCMachineExtensionNames</c> (<c>gPCUserExtensionNames</c> for the
/// User half). So every save bumps the half's word of <c>Version</c> -- low for Computer, high
/// for User -- and makes sure the registry pair is listed on its line. The rest of the file,
/// including anything gpedit or another tool put there, is left as it was.</para>
/// </summary>
public sealed class LocalGroupPolicy(string root, Action<bool> refresh) : ILocalGroupPolicy
{
    /// <summary>The registry client-side extension and the Administrative Templates tool that
    /// pairs with it on each half -- exactly what gpedit.msc writes after setting one template
    /// (read off a real gpt.ini). The phase 2 draft of 3.49 listed <c>{0F6B957E-...}</c> on the computer
    /// line, which is a USER-side tool GUID; Windows runs the extension by its first GUID, so it
    /// would have worked, but gpresult and GPMC read the tool GUID, and that version never
    /// shipped.</summary>
    public const string MachineRegistryExtension =
        "[{35378EAC-683F-11D2-A89A-00C04FBBCFA2}{D02B1F72-3407-48AE-BA88-E8213C6761F1}]";
    public const string UserRegistryExtension =
        "[{35378EAC-683F-11D2-A89A-00C04FBBCFA2}{D02B1F73-3407-48AE-BA88-E8213C6761F1}]";

    private const uint RpForce = 1;

    private readonly string _root = root;
    private readonly Action<bool> _refresh = refresh;

    public LocalGroupPolicy()
        : this(Path.Combine(Environment.SystemDirectory, "GroupPolicy"), RefreshWindows) { }

    public string PolicyPath(string cls) =>
        Path.Combine(_root, cls == PolicyAllowList.User ? "User" : "Machine", "Registry.pol");
    public string IniPath => Path.Combine(_root, "gpt.ini");

    public RegistryPolFile Load(string cls) =>
        File.Exists(PolicyPath(cls)) ? RegistryPolFile.Parse(File.ReadAllBytes(PolicyPath(cls)))
                                     : RegistryPolFile.Empty();

    public bool Save(string cls, RegistryPolFile file)
    {
        var path = PolicyPath(cls);
        var onDisk = File.Exists(path)
            ? Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(path))) : null;
        if (onDisk != file.LoadedHash) return false;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        // gpt.ini FIRST. The other order fails badly: a gpt.ini that cannot be
        // written (gpedit or a scanner holding it) after Registry.pol was replaced leaves the
        // new values on disk under the old version, and the next pass finds nothing to change,
        // so nothing ever bumps it and Windows skips the GPO until the runner's forced repair.
        // This way round a failure leaves Registry.pol as it was and the next pass redoes both;
        // the worst case is a version bumped for a file that then failed to write, which
        // Windows reads as one harmless extra pass. Latin-1 both ways: one byte per char, so
        // whatever encoding gpt.ini was written in comes back out unchanged around the two
        // lines touched here.
        var ini = File.Exists(IniPath) ? File.ReadAllText(IniPath, Encoding.Latin1) : null;
        WriteAtomically(IniPath, Encoding.Latin1.GetBytes(GptIni.Bump(ini, cls)));
        WriteAtomically(path, file.ToBytes());
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
            // next to Registry.pol and wonder what wrote it. The caller reports the failure --
            // the original one, which is why a failed cleanup is folded into it, not thrown.
            try
            {
                File.Delete(temp);
            }
            catch (Exception cleanup) when (cleanup is IOException or UnauthorizedAccessException)
            {
                throw new IOException($"{e.Message} (and {temp} could not be removed: {cleanup.Message})", e);
            }
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

    /// <summary>The file with one half's version bumped and the registry extension listed on
    /// that half's line. Null (no file yet) gives the file gpedit.msc would write for one
    /// setting.</summary>
    public static string Bump(string? text, string cls = PolicyAllowList.Machine)
    {
        var user = cls == PolicyAllowList.User;
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
        var bumped = $"Version={(user ? NextUserVersion(current) : NextMachineVersion(current))}";
        if (version >= 0) lines[version] = bumped;
        else lines.Insert(end++, bumped);

        var key = user ? "gPCUserExtensionNames" : "gPCMachineExtensionNames";
        var pair = user ? LocalGroupPolicy.UserRegistryExtension : LocalGroupPolicy.MachineRegistryExtension;
        var names = FindKey(lines, general, end, key);
        var listed = $"{key}={WithRegistryExtension(names >= 0 ? Value(lines[names]) : "", pair)}";
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

    /// <summary>The same for the high word, the user half's version.</summary>
    public static uint NextUserVersion(uint version)
    {
        var user = (version >> 16) + 1;
        if (user > 0xFFFF) user = 1;
        return (user << 16) | (version & 0xFFFF);
    }

    /// <summary>The list with the registry extension's pair in it. Unchanged when it already
    /// is; otherwise groups are sorted by extension GUID, tools within a group likewise, as
    /// gpedit writes them.</summary>
    public static string WithRegistryExtension(string list, string pair = LocalGroupPolicy.MachineRegistryExtension)
    {
        var groups = Group.Matches(list)
            .Select(m => Guid.Matches(m.Groups[1].Value).Select(g => g.Value.ToUpperInvariant()).ToList())
            .Where(g => g.Count > 0)
            .ToList();
        var wanted = Guid.Matches(pair).Select(g => g.Value).ToList();
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
