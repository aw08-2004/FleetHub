using TempMonitorAgent.Backup;

namespace TempMonitorAgent.Tests;

/// <summary>
/// What one included path contributes to a backup (BackupFilesExecutor.RootEntries).
///
/// The silent failure these exist to catch: before 3.52.0 a picked FILE as an included root
/// read as "does not exist; skipping", and the run went green with exactly what the operator
/// asked for left out. The other three cases pin that a file root is held to the same rules
/// as a file found by the walk -- excludes apply, links are never followed -- and that a path
/// which is neither still says so instead of contributing nothing quietly.
/// </summary>
public class BackupRootEntriesTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "fhb-roots-" + Guid.NewGuid().ToString("n"));
    private static readonly MachineProfiles NoProfiles =
        new(@"C:\Users", new Dictionary<string, string>(), []);

    public BackupRootEntriesTests() => Directory.CreateDirectory(_root);

    public void Dispose()
    {
        try { Directory.Delete(_root, recursive: true); } catch (IOException) { }
    }

    private static List<(string Original, string ReadPath)> Entries(
        string root, IEnumerable<string> excludes, List<string>? said = null) =>
        BackupFilesExecutor.RootEntries(root, root, new PathExpander.ExcludeMatcher(excludes, NoProfiles),
                                        line => said?.Add(line), CancellationToken.None).ToList();

    [Fact]
    public void AFileRoot_IsBackedUpItself()
    {
        var file = Path.Combine(_root, "budget.xlsx");
        File.WriteAllText(file, "x");
        Assert.Equal([(file, file)], Entries(file, []));
    }

    [Fact]
    public void AFileRoot_StillAnswersToTheExcludes()
    {
        var file = Path.Combine(_root, "scratch.tmp");
        File.WriteAllText(file, "x");
        Assert.Empty(Entries(file, ["*.tmp"]));
    }

    [Fact]
    public void AFolderRoot_IsWalkedAsBefore()
    {
        var folder = Path.Combine(_root, "Docs");
        Directory.CreateDirectory(Path.Combine(folder, "sub"));
        File.WriteAllText(Path.Combine(folder, "a.txt"), "a");
        File.WriteAllText(Path.Combine(folder, "sub", "b.txt"), "b");
        File.WriteAllText(Path.Combine(folder, "c.tmp"), "c");

        var names = Entries(folder, ["*.tmp"]).Select(e => Path.GetFileName(e.Original)).Order();
        Assert.Equal(["a.txt", "b.txt"], names);
    }

    [Fact]
    public void APathThatIsNeither_IsSaidAloudAndContributesNothing()
    {
        var said = new List<string>();
        Assert.Empty(Entries(Path.Combine(_root, "gone.txt"), [], said));
        Assert.Contains(said, line => line.Contains("does not exist"));
    }

    [Fact]
    public void AFileRootThatIsALink_IsNotFollowed()
    {
        var target = Path.Combine(_root, "real.txt");
        File.WriteAllText(target, "x");
        var link = Path.Combine(_root, "link.txt");
        try
        {
            File.CreateSymbolicLink(link, target);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // Creating a symlink needs Developer Mode or an elevated token on Windows. Without
            // either there is no link to test with, which is not a failure of the code.
            return;
        }
        Assert.Empty(Entries(link, []));
    }
}
