using System.Globalization;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Software;

/// <summary>One installed package, in the shape hub/software.py stores.</summary>
public sealed record InstalledPackage(
    string Id, string Name, string Version, string Publisher, string InstallDate, string Arch);

/// <summary>What the package database says is installed, or why it could not be read.</summary>
public readonly record struct SoftwareReport(IReadOnlyList<InstalledPackage> Items, string? Error);

/// <summary>
/// Installed software on a Linux box (roadmap #25 phase B's Linux half, listed under #22): what
/// dpkg or rpm says is installed, for the same Software section and report sheet a Windows PC
/// fills from its uninstall keys.
///
/// **The package database is the whole answer, libraries included.** Windows' list is what
/// installers chose to register, which is roughly "applications"; a Linux box's is every
/// package, so a server reports a few hundred rows and a desktop a couple of thousand. Filtering
/// to `apt-mark showmanual` was considered and rejected: it is the one list that answers "who
/// still has OpenSSL 3.0.2", and that is a library nobody installs by hand. The hub's
/// MAX_ENTRIES (2000) caps it; past that the report says it was truncated rather than
/// pretending to be complete.
///
/// **A failure is not an empty list.** hub/software.py stores an empty report as "this machine
/// has nothing installed", so a dpkg-query that could not run reports an error and NO items --
/// the reporter then sends nothing, and the hub keeps its last good list.
///
/// Snap and Flatpak apps are not read yet; they live outside dpkg/rpm and each has its own
/// CLI. Recorded in ROADMAP #22 as the next step if anyone asks for them.
/// </summary>
public static class SoftwareReader
{
    /// <summary>hub/software.py MAX_ENTRIES -- the two sides agree on what "too many" means.</summary>
    public const int MaxEntries = 2000;

    private const int TimeoutSeconds = 120;

    // ${Status} rather than ${db:Status-Status}: the latter needs dpkg 1.17.
    private const string DpkgFormat =
        "${binary:Package}\\t${Version}\\t${Maintainer}\\t${Architecture}\\t${Status}\\n";

    private const string RpmFormat =
        "%{NAME}\\t%{VERSION}-%{RELEASE}\\t%{VENDOR}\\t%{ARCH}\\t%{INSTALLTIME}\\t%{EPOCH}\\n";

    /// <summary>Read the package database. Never throws.</summary>
    public static async Task<SoftwareReport> ReadAsync(CancellationToken ct)
    {
        try
        {
            if (File.Exists("/usr/bin/dpkg-query") || File.Exists("/bin/dpkg-query"))
            {
                var run = await ProcessRunner.RunAsync(
                    ProcessRunner.ProgramPath("/usr/bin/dpkg-query", "/bin/dpkg-query"),
                    ["-W", "-f=" + DpkgFormat], TimeoutSeconds, null, ct);
                if (!run.Succeeded) return Failed("dpkg-query", run);
                return Finish(ParseDpkg(run.Output, DpkgListDate));
            }
            if (File.Exists("/usr/bin/rpm") || File.Exists("/bin/rpm"))
            {
                var run = await ProcessRunner.RunAsync(
                    ProcessRunner.ProgramPath("/usr/bin/rpm", "/bin/rpm"),
                    ["-qa", "--qf", RpmFormat], TimeoutSeconds, null, ct);
                if (!run.Succeeded) return Failed("rpm", run);
                return Finish(ParseRpm(run.Output));
            }
            // A container or an immutable image: no package database is a true, empty answer.
            return new SoftwareReport([], null);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception e)
        {
            return new SoftwareReport([], $"could not read the package database: {e.Message}");
        }
    }

    private static SoftwareReport Failed(string tool, ProcessRun run) =>
        new([], run.TimedOut ? $"{tool} did not finish in {TimeoutSeconds}s"
                             : $"{tool} exited {run.ExitCode}");

    /// <summary>Sort, dedupe and cap. Sorted so the content hash is stable between scans that
    /// found the same set -- dpkg-query's order is the database's, which is stable, but rpm's
    /// is not guaranteed.</summary>
    internal static SoftwareReport Finish(List<InstalledPackage> items)
    {
        var unique = items.GroupBy(i => i.Id, StringComparer.Ordinal).Select(g => g.First())
            .OrderBy(i => i.Name, StringComparer.Ordinal).ThenBy(i => i.Id, StringComparer.Ordinal)
            .ToList();
        string? error = null;
        if (unique.Count > MaxEntries)
        {
            error = $"truncated: {unique.Count} packages, kept {MaxEntries}";
            unique.RemoveRange(MaxEntries, unique.Count - MaxEntries);
        }
        return new SoftwareReport(unique, error);
    }

    /// <summary>Parse dpkg-query's tab-separated output, keeping only installed packages.
    ///
    /// A package removed but not purged stays in the database as "deinstall ok config-files";
    /// listing it would put software that is not there on a licence audit.</summary>
    internal static List<InstalledPackage> ParseDpkg(string output, Func<string, string> installDate)
    {
        var items = new List<InstalledPackage>();
        foreach (var line in Lines(output))
        {
            var f = line.Split('\t');
            if (f.Length < 5 || f[0].Length == 0) continue;
            if (!f[4].Trim().EndsWith(" installed", StringComparison.Ordinal)) continue;
            var name = f[0].Split(':')[0];
            items.Add(new InstalledPackage(
                Id: "dpkg:" + f[0],
                Name: name,
                Version: f[1],
                Publisher: Publisher(f[2]),
                InstallDate: installDate(f[0]),
                Arch: Arch(f[3])));
        }
        return items;
    }

    /// <summary>Parse `rpm -qa --qf` output. The id carries version and arch because rpm
    /// installs several versions of one name side by side (every kernel the box has kept),
    /// and the hub keys rows on id.</summary>
    internal static List<InstalledPackage> ParseRpm(string output)
    {
        var items = new List<InstalledPackage>();
        foreach (var line in Lines(output))
        {
            var f = line.Split('\t');
            if (f.Length < 5 || f[0].Length == 0) continue;
            // gpg-pubkey entries are imported signing keys, not software.
            if (f[0] == "gpg-pubkey") continue;
            var epoch = f.Length > 5 && f[5] != "(none)" && f[5].Length > 0 ? f[5] + ":" : "";
            items.Add(new InstalledPackage(
                Id: $"rpm:{f[0]}-{f[1]}.{f[3]}",
                Name: f[0],
                Version: epoch + f[1],
                Publisher: f[2] == "(none)" ? "" : f[2].Trim(),
                InstallDate: long.TryParse(f[4], out var epochSeconds) && epochSeconds > 0
                    ? DateTimeOffset.FromUnixTimeSeconds(epochSeconds).UtcDateTime
                        .ToString("yyyyMMdd", CultureInfo.InvariantCulture)
                    : "",
                Arch: Arch(f[3])));
        }
        return items;
    }

    /// <summary>The hub's arch vocabulary is Windows' -- x64, x86 or blank -- so the two common
    /// Linux names map onto it and everything else (all, noarch, arm64) is blank rather than
    /// rejected.</summary>
    internal static string Arch(string raw) => raw.Trim() switch
    {
        "amd64" or "x86_64" => "x64",
        "i386" or "i486" or "i586" or "i686" => "x86",
        _ => "",
    };

    /// <summary>A Debian Maintainer field is "Name &lt;email&gt;"; the name is the publisher,
    /// and an address does not belong on a licence report.</summary>
    internal static string Publisher(string maintainer)
    {
        var lt = maintainer.IndexOf('<');
        return (lt >= 0 ? maintainer[..lt] : maintainer).Trim();
    }

    /// <summary>When dpkg last unpacked a package, from its file list's mtime -- dpkg keeps no
    /// install date, and this is the file it rewrites on every install or upgrade. yyyyMMdd,
    /// the Windows registry's InstallDate format, so the two read the same on one sheet.</summary>
    private static string DpkgListDate(string package)
    {
        foreach (var candidate in new[] { package, package.Split(':')[0] })
        {
            var path = $"/var/lib/dpkg/info/{candidate}.list";
            try
            {
                if (File.Exists(path))
                    return File.GetLastWriteTimeUtc(path).ToString("yyyyMMdd", CultureInfo.InvariantCulture);
            }
            catch { /* unreadable reads as unknown */ }
        }
        return "";
    }

    private static IEnumerable<string> Lines(string text) =>
        (text ?? "").Split('\n').Select(l => l.TrimEnd('\r')).Where(l => l.Length > 0);
}
