using System.IO.Compression;
using System.Text;
using TempMonitorAgent.GroupPolicy;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Roadmap #32: Administrative Templates sent to the hub.
///
/// The silent failure this file exists to catch is **the agent's set hash drifting from the
/// hub's** (admx.set_hash). The hub recomputes it from every upload and refuses a mismatch, so
/// a drift would not corrupt anything -- it would make every upload from every PC fail, and
/// the editor would stay empty with nothing in the console saying why. The expected value
/// below was computed by the hub's own function over the same three files.
/// </summary>
public class PolicyDefinitionsTests
{
    // python -c "import admx; print(admx.set_hash({'a.admx': b'<a/>', 'en-US/a.adml': b'<b/>',
    //            'de-DE/A.adml': b'<c/>'}))"
    private const string HubHash = "60b9e0c428c73eb7a48ffd9956ea9f1383d09c3c056ae35350f02ce74b288fca";

    [Fact]
    public void SetHashMatchesTheHub()
    {
        var hash = PolicyDefinitionsReader.SetHash([
            ("a.admx", Encoding.UTF8.GetBytes("<a/>")),
            ("en-US/a.adml", Encoding.UTF8.GetBytes("<b/>")),
            (@"de-DE\A.adml", Encoding.UTF8.GetBytes("<c/>")),
        ]);
        Assert.Equal(HubHash, hash);
    }

    [Fact]
    public void ReadsOnlyTheConsoleLanguagesAndZipsWhatItHashed()
    {
        var root = Directory.CreateTempSubdirectory("fleethub-admx-").FullName;
        try
        {
            File.WriteAllText(Path.Combine(root, "a.admx"), "<a/>");
            File.WriteAllText(Path.Combine(root, "odd+name.admx"), "<x/>");
            foreach (var language in new[] { "en-US", "de-DE", "fr-FR" })
            {
                Directory.CreateDirectory(Path.Combine(root, language));
                File.WriteAllText(Path.Combine(root, language, "a.adml"), $"<{language}/>");
            }
            var reader = new PolicyDefinitionsReader(root);
            Assert.Null(reader.Hash);
            reader.Refresh();
            Assert.NotNull(reader.Hash);

            var zip = Path.Combine(root, "out.zip");
            var written = reader.WriteZip(zip);
            Assert.Equal(reader.Hash, written);
            using var archive = ZipFile.OpenRead(zip);
            Assert.Equal(["a.admx", "de-DE/a.adml", "en-US/a.adml"],
                         archive.Entries.Select(e => e.FullName).Order(StringComparer.Ordinal));

            Assert.False(reader.TakeUploadRequest(out _));
            reader.RequestUpload();
            Assert.True(reader.TakeUploadRequest(out var hash));
            Assert.Equal(reader.Hash, hash);
            Assert.False(reader.TakeUploadRequest(out _));
        }
        finally
        {
            Directory.Delete(root, recursive: true);
        }
    }

    [Fact]
    public void NoTemplateFolderMeansNoHash()
    {
        var reader = new PolicyDefinitionsReader(Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString()));
        reader.Refresh();
        Assert.Null(reader.Hash);
        reader.RequestUpload();
        Assert.False(reader.TakeUploadRequest(out _));
    }
}
