using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>
/// This PC's Administrative Templates, for the hub's Group Policy editor (roadmap #32).
///
/// The hub shows gpedit's tree from the templates the PCs themselves report, so a setting is
/// offered from exactly the files the PC that applies it reads. This class answers two things:
/// the set's HASH, sent on every heartbeat (cheap, and the hub's whole question), and on
/// request a ZIP of the set, uploaded once per Windows build fleet-wide -- the hub asks one PC
/// per unknown hash (admx.note_machine) and never the rest.
///
/// **The hash is the hub's <c>admx.set_hash</c>, byte for byte**: SHA-256 over, for each file
/// sorted by its lower-cased relative path with forward slashes, <c>path\n&lt;sha256 hex&gt;\n</c>.
/// The hub recomputes it from the upload and refuses a mismatch, so a disagreement here is an
/// upload refused every time, never a wrong set.
///
/// Only the <c>.admx</c> files and the <c>en-US</c>, <c>de-DE</c> and <c>es-ES</c> <c>.adml</c>
/// are read: the console speaks those three languages, and the other forty folders on a
/// multilingual image would be most of the upload for nothing.
///
/// Read-only and cheap when nothing changed: the set is re-hashed only when the folder's
/// file count or newest write time moves, which is what a cumulative update or an Edge
/// template install does.
/// </summary>
public sealed class PolicyDefinitionsReader(string root)
{
    public static readonly string[] Languages = ["en-US", "de-DE", "es-ES"];

    /// <summary>A template larger than this is not one; the hub refuses the same size.</summary>
    public const long MaxFileBytes = 8 * 1024 * 1024;

    /// <summary>The file names the hub accepts (admx._ADMX_NAME / _ADML_NAME). The hub refuses a
    /// whole upload carrying any other name, so one odd vendor file is left out here instead of
    /// costing the PC its whole set.</summary>
    private static readonly Regex Name = new(@"^[a-z0-9._ -]{1,128}\.(admx|adml)$",
                                             RegexOptions.IgnoreCase | RegexOptions.CultureInvariant,
                                             TimeSpan.FromSeconds(1));

    private readonly Lock _gate = new();
    private string? _hash;
    private (int Count, DateTime Newest)? _signature;
    private bool _uploadRequested;

    public PolicyDefinitionsReader() : this(Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.Windows), "PolicyDefinitions")) { }

    /// <summary>The set's hash, or null before the first <see cref="Refresh"/> or on a PC
    /// with no templates. Read by the heartbeat thread.</summary>
    public string? Hash
    {
        get { lock (_gate) { return _hash; } }
    }

    /// <summary>The hub answered <c>admx_wanted</c>: upload on the next policy pass.</summary>
    public void RequestUpload()
    {
        lock (_gate) { _uploadRequested = true; }
    }

    /// <summary>Take a pending upload request, with the hash it is for.</summary>
    public bool TakeUploadRequest(out string hash)
    {
        lock (_gate)
        {
            hash = _hash ?? "";
            var take = _uploadRequested && _hash is not null;
            _uploadRequested = false;
            return take;
        }
    }

    /// <summary>Re-hash the set if the folder changed since the last pass.</summary>
    public void Refresh()
    {
        var files = Files();
        var signature = (files.Count,
                         files.Count == 0 ? DateTime.MinValue
                                          : files.Max(f => File.GetLastWriteTimeUtc(f.Full)));
        lock (_gate)
        {
            if (_signature == signature) return;
        }
        var hash = files.Count == 0 ? null : SetHash(files.Select(f => (f.Relative, Read(f.Full))));
        lock (_gate)
        {
            _hash = hash;
            _signature = signature;
        }
    }

    /// <summary>Write the set as a zip to <paramref name="path"/>, entry names being the same
    /// relative paths the hash covers. Returns the hash of what was written, which the caller
    /// sends -- so a template changing between Refresh and here uploads under its own hash,
    /// not a stale one.</summary>
    public string WriteZip(string path)
    {
        var files = Files();
        var read = files.Select(f => (f.Relative, Bytes: Read(f.Full))).ToList();
        using (var stream = new FileStream(path, FileMode.Create, FileAccess.Write))
        using (var archive = new ZipArchive(stream, ZipArchiveMode.Create))
        {
            foreach (var (relative, bytes) in read)
            {
                var entry = archive.CreateEntry(relative, CompressionLevel.Optimal);
                using var output = entry.Open();
                output.Write(bytes);
            }
        }
        return SetHash(read);
    }

    /// <summary>The hub's set hash over (relative path, bytes) pairs.</summary>
    public static string SetHash(IEnumerable<(string Relative, byte[] Bytes)> files)
    {
        var lines = files
            .Select(f => (Path: f.Relative.Replace('\\', '/').ToLowerInvariant(),
                          Digest: Convert.ToHexStringLower(SHA256.HashData(f.Bytes))))
            .OrderBy(f => f.Path, StringComparer.Ordinal);
        var text = new StringBuilder();
        foreach (var (path, digest) in lines) text.Append(path).Append('\n').Append(digest).Append('\n');
        return Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(text.ToString())));
    }

    private static byte[] Read(string path) => File.ReadAllBytes(path);

    private List<(string Relative, string Full)> Files()
    {
        var found = new List<(string Relative, string Full)>();
        if (!Directory.Exists(root)) return found;
        found.AddRange(Directory.EnumerateFiles(root, "*.admx").Where(Acceptable)
                                .Select(file => (Path.GetFileName(file), file)));
        foreach (var language in Languages)
        {
            var folder = Path.Combine(root, language);
            if (!Directory.Exists(folder)) continue;
            found.AddRange(Directory.EnumerateFiles(folder, "*.adml").Where(Acceptable)
                                    .Select(file => ($"{language}/{Path.GetFileName(file)}", file)));
        }
        return found;
    }

    private static bool Acceptable(string file) =>
        Name.IsMatch(Path.GetFileName(file)) && new FileInfo(file).Length <= MaxFileBytes;
}
