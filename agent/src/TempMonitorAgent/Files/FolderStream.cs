using System.Buffers.Binary;
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Files;

/// <summary>
/// The wire format of a live folder copy (hub/folder_relay.py), and the two halves that
/// move it through the hub in numbered chunks.
///
/// <b>Why a format of our own and not a zip or a tar.</b> A zip's directory is at its END, so
/// the target could not write a single file until the last byte of a 40 GB profile had
/// arrived; a tar header carries the file's length up front, and a file that shrinks while it
/// is being read (a log, a mailbox somebody has open) would leave the receiver waiting for
/// bytes that are never sent. Here every file is a sequence of length-prefixed blocks with an
/// explicit terminator and a status byte, so a file the source could not finish reading is
/// SAID to be incomplete, and the receiver discards it rather than leaving a truncated copy
/// that looks real.
///
/// <code>
/// stream  := "FHS1" record* end
/// record  := 'D' path                              -- a folder
///          | 'F' path mtime:i64 block* 0:u32 status:u8   -- a file; status 0 = complete
/// block   := length:u32 (1..MaxBlockBytes) bytes
/// end     := 'E' files:i64 folders:i64
/// path    := length:u16 utf8, '/'-separated, relative, first segment = the folder's name
/// </code>
///
/// <b>The end record is the receiver's proof that nothing was lost.</b> The hub also says when
/// the sender has finished, but a stream cut short at a chunk boundary would otherwise parse
/// as a complete, smaller folder -- the silent failure this whole format is built against.
/// </summary>
internal static class FolderStreamFormat
{
    internal static readonly byte[] Magic = "FHS1"u8.ToArray();
    internal const byte KindFolder = (byte)'D';
    internal const byte KindFile = (byte)'F';
    internal const byte KindEnd = (byte)'E';
    internal const byte StatusComplete = 0;
    internal const byte StatusIncomplete = 1;

    /// <summary>Largest block the sender writes, and therefore the largest the receiver
    /// will believe. Anything bigger is a corrupt stream, not a big file.</summary>
    internal const int MaxBlockBytes = 256 * 1024;

    /// <summary>Longest relative path, in UTF-8 bytes. Well past MAX_PATH, short enough
    /// that a garbage length cannot make the receiver allocate much.</summary>
    internal const int MaxPathBytes = 8 * 1024;

    /// <summary>Bytes per HTTP chunk. Under the hub's MAX_CHUNK_BYTES with room; large
    /// enough that a fast LAN is not dominated by request overhead.</summary>
    internal const int ChunkBytes = 1024 * 1024;

    /// <summary>Longer than the hub's long-poll wait (folder_relay.WAIT_SECONDS, 15 s) plus
    /// a 1 MiB upload on a slow uplink.</summary>
    internal static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(90);

    /// <summary>Network failures in a row before a transfer is given up. The hub keeps the
    /// window for fifteen minutes of silence, so this is about the agent's own link: two
    /// minutes of trying covers a Wi-Fi roam or a VPN reconnect, not a PC that has gone.</summary>
    internal const int MaxNetworkFailures = 12;

    internal static string BaseUrl(string transferId) =>
        $"{AgentConfig.HubBase}/api/agent/files/stream/{Uri.EscapeDataString(transferId)}";

    internal static HttpClient NewClient(string bearer)
    {
        var http = new HttpClient { Timeout = RequestTimeout };
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        return http;
    }

    internal static async Task PostJsonAsync(HttpClient http, string url, JsonNode body,
                                             CancellationToken ct)
    {
        // Best effort, retried briefly: this is the verdict, and a verdict lost to one dropped
        // connection leaves the console polling a copy that already ended.
        for (var attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                using var content = new StringContent(body.ToJsonString(), Encoding.UTF8,
                                                      "application/json");
                using var resp = await http.PostAsync(url, content, ct);
                if ((int)resp.StatusCode < 500) return;
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException
                                        && !ct.IsCancellationRequested)
            {
            }
            await Task.Delay(TimeSpan.FromSeconds(2 * (attempt + 1)), ct);
        }
    }
}

/// <summary>The copy is over on the hub's side -- cancelled, timed out, or the hub restarted.
/// Not an error in this agent; the reason is already on the console.</summary>
internal sealed class RelayGoneException(string message) : Exception(message);

/// <summary>
/// The sender's half of the relay: buffers what the executor writes and PUTs it to the hub as
/// numbered chunks, one in flight at a time.
///
/// A 503 is the hub saying its window is full -- the receiver is behind -- and is answered by
/// sending the same chunk again, with no delay of our own: the hub already waited for room
/// before answering. That is the whole of the backpressure, and it is why this side never
/// holds more than one chunk however slow the target is.
/// </summary>
internal sealed class RelayChunkWriter(HttpClient http, string baseUrl)
{
    private readonly byte[] _buffer = new byte[FolderStreamFormat.ChunkBytes];
    private int _length;
    private long _sequence;

    public long BytesSent { get; private set; }

    public async Task WriteAsync(ReadOnlyMemory<byte> data, CancellationToken ct)
    {
        while (data.Length > 0)
        {
            var take = Math.Min(data.Length, _buffer.Length - _length);
            data[..take].CopyTo(_buffer.AsMemory(_length));
            _length += take;
            data = data[take..];
            if (_length == _buffer.Length) await FlushAsync(ct);
        }
    }

    public Task WriteByteAsync(byte value, CancellationToken ct) =>
        WriteAsync(new[] { value }, ct);

    public async Task FlushAsync(CancellationToken ct)
    {
        if (_length == 0) return;
        var failures = 0;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                using var content = new ByteArrayContent(_buffer, 0, _length);
                content.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
                using var resp = await http.PutAsync($"{baseUrl}/chunk/{_sequence}", content, ct);
                switch (resp.StatusCode)
                {
                    case HttpStatusCode.OK:
                        // 200 covers "stored" and "duplicate" alike: a retried chunk whose
                        // first answer was lost is already there, which is success.
                        BytesSent += _length;
                        _sequence++;
                        _length = 0;
                        return;
                    case HttpStatusCode.ServiceUnavailable:
                        failures = 0;   // the hub answered; the receiver is just behind
                        continue;
                    case HttpStatusCode.Gone:
                        throw new RelayGoneException("The hub ended this copy.");
                    default:
                        var text = await resp.Content.ReadAsStringAsync(ct);
                        throw new IOException(
                            $"The hub refused a chunk: HTTP {(int)resp.StatusCode} {text}".Trim());
                }
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException
                                        && !ct.IsCancellationRequested)
            {
                if (++failures >= FolderStreamFormat.MaxNetworkFailures)
                    throw new IOException($"Lost the connection to the hub: {e.Message}");
                await Task.Delay(TimeSpan.FromSeconds(Math.Min(10, 2 * failures)), ct);
            }
        }
    }
}

/// <summary>
/// The receiver's half: asks the hub for chunk 0, 1, 2 ... in order and hands the bytes out
/// as a stream. Asking for N+1 is what tells the hub N arrived, so a response lost on the way
/// here is simply asked for again and is still there.
/// </summary>
internal sealed class RelayChunkReader(HttpClient http, string baseUrl)
{
    private byte[] _current = [];
    private int _position;
    private long _sequence;
    private bool _ended;

    public long BytesReceived { get; private set; }

    public async Task ReadExactlyAsync(Memory<byte> destination, CancellationToken ct)
    {
        while (destination.Length > 0)
        {
            if (_position == _current.Length && !await FillAsync(ct))
                throw new EndOfStreamException(
                    "The copy ended before the source said it was finished.");
            var take = Math.Min(destination.Length, _current.Length - _position);
            _current.AsMemory(_position, take).CopyTo(destination);
            _position += take;
            destination = destination[take..];
        }
    }

    public async Task<byte> ReadByteAsync(CancellationToken ct)
    {
        var one = new byte[1];
        await ReadExactlyAsync(one, ct);
        return one[0];
    }

    /// <summary>True when every byte has been consumed AND the hub says the sender has
    /// finished. Asking is also the acknowledgement of the last chunk.</summary>
    public async Task<bool> AtEndAsync(CancellationToken ct) =>
        _position == _current.Length && !await FillAsync(ct);

    private async Task<bool> FillAsync(CancellationToken ct)
    {
        if (_ended) return false;
        var failures = 0;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                using var resp = await http.GetAsync($"{baseUrl}/chunk/{_sequence}",
                                                     HttpCompletionOption.ResponseContentRead, ct);
                if (resp.StatusCode == HttpStatusCode.NoContent)
                {
                    failures = 0;   // nothing yet; the hub already waited, ask again
                    continue;
                }
                if (resp.StatusCode == HttpStatusCode.Gone)
                    throw new RelayGoneException("The hub ended this copy.");
                if (resp.StatusCode != HttpStatusCode.OK)
                {
                    var text = await resp.Content.ReadAsStringAsync(ct);
                    throw new IOException(
                        $"The hub refused a chunk: HTTP {(int)resp.StatusCode} {text}".Trim());
                }
                if (resp.Headers.Contains("X-Stream-End"))
                {
                    _ended = true;
                    return false;
                }
                var bytes = await resp.Content.ReadAsByteArrayAsync(ct);
                _sequence++;
                if (bytes.Length == 0) continue;
                _current = bytes;
                _position = 0;
                BytesReceived += bytes.Length;
                return true;
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException
                                        && !ct.IsCancellationRequested)
            {
                if (++failures >= FolderStreamFormat.MaxNetworkFailures)
                    throw new IOException($"Lost the connection to the hub: {e.Message}");
                await Task.Delay(TimeSpan.FromSeconds(Math.Min(10, 2 * failures)), ct);
            }
        }
    }
}

/// <summary>
/// stream_send: read one or more folders and stream them, live, to another PC through the hub.
///
/// <b>Nothing on this machine changes.</b> It is a copy; the owner rejected "move" for the
/// first version (2026-11-14) because the delete would run on the strength of a target that
/// only THINKS it finished.
///
/// <b>Files are read with FileShare.ReadWrite | Delete and no shadow copy.</b> That reads
/// almost everything a user has open; what it cannot read (a file opened exclusively, like
/// an Outlook .pst in use) is skipped and named in the summary rather than failing the whole
/// copy. A VSS snapshot, as backup_files takes, was rejected for this path: it is minutes of
/// setup and gigabytes of diff area for what is usually a copy of a few folders somebody is
/// moving to a new PC, and a snapshot held open for a long copy is held for its whole length.
///
/// <b>Reparse points are not followed</b>, for the reason MeasureTree gives in
/// FetchFileExecutor: a junction into C:\ makes the walk unbounded.
/// </summary>
public sealed class StreamSendExecutor(ILogger<StreamSendExecutor> log, FleetClient fleet)
    : ICommandExecutor
{
    public string Type => "stream_send";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var transferId = cmd.Params.GetString("transfer_id") ?? "";
        var bearer = fleet.BearerValue;
        if (transferId.Length == 0 || bearer is null)
            return CommandResult.Fail("This copy is missing its transfer details.");

        using var http = FolderStreamFormat.NewClient(bearer);
        return await SendAsync(http, FolderStreamFormat.BaseUrl(transferId),
                               cmd.Params.GetArray("paths"), onOutput, log, ct);
    }

    /// <summary>The whole send, against any HttpClient -- split out so a test can run it
    /// against an in-memory relay and a real folder (FolderStreamTests).</summary>
    internal static async Task<CommandResult> SendAsync(HttpClient http, string baseUrl,
                                                        JsonArray? paths,
                                                        Action<string>? onOutput,
                                                        ILogger log, CancellationToken ct)
    {
        var roots = new List<string>();
        foreach (var node in paths ?? [])
        {
            var raw = node?.GetValue<string>() ?? "";
            var refusal = PathRules.Reject(raw);
            if (refusal is not null) return await FailAsync(http, baseUrl, refusal, ct);
            var path = PathRules.Normalize(raw);
            if (!Directory.Exists(path))
                return await FailAsync(http, baseUrl, $"{path} is not a folder.", ct);
            roots.Add(path);
        }
        if (roots.Count == 0)
            return await FailAsync(http, baseUrl, "No folders were named.", ct);

        // Measured first, for the console's progress bar. Cheap next to the copy, and a
        // measurement that fails is only a missing percentage.
        long totalBytes = 0, totalFiles = 0;
        foreach (var root in roots) Measure(root, ref totalBytes, ref totalFiles, ct);
        await FolderStreamFormat.PostJsonAsync(http, $"{baseUrl}/meta", new JsonObject
        {
            ["total_bytes"] = totalBytes,
            ["total_files"] = totalFiles,
        }, ct);
        onOutput?.Invoke($"[files] sending {totalFiles} file(s), {Mb(totalBytes)}\n");

        var writer = new RelayChunkWriter(http, baseUrl);
        var block = new byte[FolderStreamFormat.MaxBlockBytes];
        long files = 0, folders = 0;
        var unreadable = new List<string>();
        try
        {
            await writer.WriteAsync(FolderStreamFormat.Magic, ct);
            foreach (var root in roots)
            {
                var name = Path.GetFileName(root);
                await WriteFolderAsync(writer, name, ct);
                folders++;
                var stack = new Stack<(DirectoryInfo Dir, string Rel)>();
                stack.Push((new DirectoryInfo(root), name));
                while (stack.Count > 0)
                {
                    ct.ThrowIfCancellationRequested();
                    var (dir, rel) = stack.Pop();
                    FileInfo[] dirFiles;
                    DirectoryInfo[] subs;
                    try
                    {
                        dirFiles = dir.GetFiles();
                        subs = dir.GetDirectories();
                    }
                    catch (Exception e) when (e is IOException or UnauthorizedAccessException)
                    {
                        unreadable.Add($"{dir.FullName}\\ ({e.Message})");
                        continue;
                    }
                    foreach (var file in dirFiles)
                    {
                        if (file.Attributes.HasFlag(FileAttributes.ReparsePoint)) continue;
                        var problem = await WriteFileAsync(writer, $"{rel}/{file.Name}", file,
                                                           block, ct);
                        if (problem is null) files++;
                        else unreadable.Add($"{file.FullName} ({problem})");
                    }
                    foreach (var sub in subs)
                    {
                        if (sub.Attributes.HasFlag(FileAttributes.ReparsePoint)) continue;
                        var subRel = $"{rel}/{sub.Name}";
                        await WriteFolderAsync(writer, subRel, ct);
                        folders++;
                        stack.Push((sub, subRel));
                    }
                }
            }

            var end = new byte[17];
            end[0] = FolderStreamFormat.KindEnd;
            BinaryPrimitives.WriteInt64BigEndian(end.AsSpan(1), files);
            BinaryPrimitives.WriteInt64BigEndian(end.AsSpan(9), folders);
            await writer.WriteAsync(end, ct);
            await writer.FlushAsync(ct);
            await FolderStreamFormat.PostJsonAsync(http, $"{baseUrl}/end", new JsonObject(), ct);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (RelayGoneException e)
        {
            return CommandResult.Fail($"[files] {e.Message}");
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            log.LogWarning(e, "stream_send to {Url} failed", baseUrl);
            return await FailAsync(http, baseUrl, e.Message, ct);
        }

        var summary = new StringBuilder(
            $"[files] sent {files} file(s) in {folders} folder(s), {Mb(writer.BytesSent)} on the wire");
        if (unreadable.Count > 0)
        {
            summary.Append($"; {unreadable.Count} could not be read:");
            foreach (var line in unreadable.Take(50)) summary.Append("\n  ").Append(line);
            if (unreadable.Count > 50) summary.Append($"\n  ... and {unreadable.Count - 50} more");
        }
        return CommandResult.Ok(summary.ToString());
    }

    private static async Task WriteFolderAsync(RelayChunkWriter writer, string rel,
                                               CancellationToken ct)
    {
        await writer.WriteByteAsync(FolderStreamFormat.KindFolder, ct);
        await WritePathAsync(writer, rel, ct);
    }

    /// <summary>One file record. Null when the file went across complete, otherwise why not.
    /// A file that cannot even be opened writes no record at all; one that fails part way
    /// is closed with the INCOMPLETE status, so the receiver throws its partial copy away.</summary>
    private static async Task<string?> WriteFileAsync(RelayChunkWriter writer, string rel,
                                                      FileInfo file, byte[] block,
                                                      CancellationToken ct)
    {
        FileStream source;
        try
        {
            source = new FileStream(file.FullName, FileMode.Open, FileAccess.Read,
                                    FileShare.ReadWrite | FileShare.Delete,
                                    FolderStreamFormat.MaxBlockBytes, useAsync: true);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return e.Message;
        }

        await using (source)
        {
            await writer.WriteByteAsync(FolderStreamFormat.KindFile, ct);
            await WritePathAsync(writer, rel, ct);
            var mtime = new byte[8];
            BinaryPrimitives.WriteInt64BigEndian(mtime, file.LastWriteTimeUtc.Ticks);
            await writer.WriteAsync(mtime, ct);

            var prefix = new byte[4];
            string? problem = null;
            while (true)
            {
                int read;
                try
                {
                    read = await source.ReadAsync(block, ct);
                }
                catch (Exception e) when (e is IOException or UnauthorizedAccessException)
                {
                    problem = e.Message;
                    break;
                }
                if (read == 0) break;
                BinaryPrimitives.WriteUInt32BigEndian(prefix, (uint)read);
                await writer.WriteAsync(prefix, ct);
                await writer.WriteAsync(block.AsMemory(0, read), ct);
            }
            BinaryPrimitives.WriteUInt32BigEndian(prefix, 0);
            await writer.WriteAsync(prefix, ct);
            await writer.WriteByteAsync(problem is null
                ? FolderStreamFormat.StatusComplete
                : FolderStreamFormat.StatusIncomplete, ct);
            return problem;
        }
    }

    private static async Task WritePathAsync(RelayChunkWriter writer, string rel,
                                             CancellationToken ct)
    {
        var bytes = Encoding.UTF8.GetBytes(rel);
        if (bytes.Length > FolderStreamFormat.MaxPathBytes)
            throw new IOException($"{rel} is too long a path to send.");
        var length = new byte[2];
        BinaryPrimitives.WriteUInt16BigEndian(length, (ushort)bytes.Length);
        await writer.WriteAsync(length, ct);
        await writer.WriteAsync(bytes, ct);
    }

    private static void Measure(string root, ref long bytes, ref long count, CancellationToken ct)
    {
        var stack = new Stack<DirectoryInfo>();
        stack.Push(new DirectoryInfo(root));
        while (stack.Count > 0)
        {
            ct.ThrowIfCancellationRequested();
            var dir = stack.Pop();
            try
            {
                foreach (var file in dir.EnumerateFiles())
                {
                    if (file.Attributes.HasFlag(FileAttributes.ReparsePoint)) continue;
                    bytes += file.Length;
                    count++;
                }
                foreach (var sub in dir.EnumerateDirectories())
                    if (!sub.Attributes.HasFlag(FileAttributes.ReparsePoint)) stack.Push(sub);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                // Counted as nothing; the copy reports it by name when it gets there.
            }
        }
    }

    private static async Task<CommandResult> FailAsync(HttpClient http, string baseUrl,
                                                       string reason, CancellationToken ct)
    {
        // Told to the hub as well as returned: the console polls the TRANSFER, and without
        // this it would wait out the idle timeout to learn the source had already given up.
        await FolderStreamFormat.PostJsonAsync(http, $"{baseUrl}/end",
                                               new JsonObject { ["error"] = reason }, ct);
        return CommandResult.Fail($"[files] {reason}");
    }

    private static string Mb(long bytes) => $"{bytes / 1024.0 / 1024.0:F1} MB";
}

/// <summary>
/// stream_receive: take a live folder copy from the hub and write it under a destination
/// folder on this machine.
///
/// <b>Every path in the stream is untrusted</b>, however it was produced: it came off
/// another machine. Each segment must be a valid Windows name (PathRules.RejectName, which
/// refuses "..", separators and device-ish names), the joined path must still resolve
/// inside the destination, and no folder on the way may be a reparse point -- a junction
/// planted in the destination would otherwise turn "C:\Incoming\x" into a write anywhere.
/// A path that fails is skipped and counted, never written.
///
/// <b>Existing files are kept, not replaced</b>, unless the hub says `overwrite` (the console
/// does not offer it yet). Copying a profile onto a PC that already has half of it is the
/// normal case, and silently replacing a newer document with an older one is the mistake a
/// helpdesk makes by accident.
///
/// <b>Each file lands beside its name as .fhpart and is renamed into place only when its
/// status byte says it is complete</b>, so a copy cut off mid-file leaves the destination
/// holding whole files only -- the same promise push_file's staging makes.
/// </summary>
public sealed class StreamReceiveExecutor(ILogger<StreamReceiveExecutor> log, FleetClient fleet)
    : ICommandExecutor
{
    private const string PartSuffix = ".fhpart";

    public string Type => "stream_receive";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var transferId = cmd.Params.GetString("transfer_id") ?? "";
        var bearer = fleet.BearerValue;
        if (transferId.Length == 0 || bearer is null)
            return CommandResult.Fail("This copy is missing its transfer details.");
        using var http = FolderStreamFormat.NewClient(bearer);
        return await ReceiveAsync(http, FolderStreamFormat.BaseUrl(transferId),
                                  cmd.Params.GetString("destination") ?? "",
                                  cmd.Params.GetBool("overwrite"), onOutput, log, ct);
    }

    /// <summary>The whole receive, against any HttpClient -- see SendAsync.</summary>
    internal static async Task<CommandResult> ReceiveAsync(HttpClient http, string baseUrl,
                                                           string destination, bool overwrite,
                                                           Action<string>? onOutput,
                                                           ILogger log, CancellationToken ct)
    {
        var refusal = PathRules.Reject(destination);
        if (refusal is not null) return await FailAsync(http, baseUrl, refusal, ct);
        destination = PathRules.Normalize(destination);

        string root;
        try
        {
            Directory.CreateDirectory(destination);
            if (new DirectoryInfo(destination).Attributes.HasFlag(FileAttributes.ReparsePoint))
                return await FailAsync(http, baseUrl,
                    $"{destination} is a link to somewhere else; pick a real folder.", ct);
            root = Path.GetFullPath(destination);
            if (!root.EndsWith('\\')) root += "\\";
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                    or NotSupportedException or ArgumentException)
        {
            return await FailAsync(http, baseUrl, e.Message, ct);
        }

        var reader = new RelayChunkReader(http, baseUrl);
        var block = new byte[FolderStreamFormat.MaxBlockBytes];
        var safeDirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        long written = 0, kept = 0, rejected = 0, incomplete = 0, folders = 0;
        string? partPath = null;
        try
        {
            var magic = new byte[FolderStreamFormat.Magic.Length];
            await reader.ReadExactlyAsync(magic, ct);
            if (!magic.AsSpan().SequenceEqual(FolderStreamFormat.Magic))
                throw new InvalidDataException("That is not a folder copy stream.");

            var ended = false;
            var number = new byte[8];
            while (!ended)
            {
                var kind = await reader.ReadByteAsync(ct);
                switch (kind)
                {
                    case FolderStreamFormat.KindFolder:
                    {
                        var target = Resolve(root, await ReadPathAsync(reader, ct), safeDirs);
                        if (target is null) { rejected++; break; }
                        Directory.CreateDirectory(target);
                        folders++;
                        break;
                    }
                    case FolderStreamFormat.KindFile:
                    {
                        var rel = await ReadPathAsync(reader, ct);
                        await reader.ReadExactlyAsync(number, ct);
                        var mtime = BinaryPrimitives.ReadInt64BigEndian(number);
                        var target = Resolve(root, rel, safeDirs);

                        FileStream? sink = null;
                        if (target is null)
                            rejected++;
                        else if (Directory.Exists(target) || (File.Exists(target) && !overwrite))
                            kept++;
                        else
                        {
                            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                            partPath = target + PartSuffix;
                            sink = new FileStream(partPath, FileMode.Create, FileAccess.Write,
                                                  FileShare.None, FolderStreamFormat.MaxBlockBytes,
                                                  useAsync: true);
                        }

                        // The bytes are read whether or not they are kept: the stream has to be
                        // consumed to reach the next record.
                        var prefix = new byte[4];
                        await using (sink)
                        {
                            while (true)
                            {
                                await reader.ReadExactlyAsync(prefix, ct);
                                var length = BinaryPrimitives.ReadUInt32BigEndian(prefix);
                                if (length == 0) break;
                                if (length > FolderStreamFormat.MaxBlockBytes)
                                    throw new InvalidDataException("The copy stream is corrupt.");
                                await reader.ReadExactlyAsync(block.AsMemory(0, (int)length), ct);
                                if (sink is not null)
                                    await sink.WriteAsync(block.AsMemory(0, (int)length), ct);
                            }
                        }
                        var status = await reader.ReadByteAsync(ct);
                        if (sink is null) break;
                        if (status == FolderStreamFormat.StatusComplete)
                        {
                            File.Move(partPath!, target!, overwrite);
                            try { File.SetLastWriteTimeUtc(target!, new DateTime(mtime, DateTimeKind.Utc)); }
                            catch (Exception e) when (e is IOException or ArgumentException
                                                        or UnauthorizedAccessException) { }
                            written++;
                        }
                        else
                        {
                            TryDelete(partPath!);
                            incomplete++;
                        }
                        partPath = null;
                        break;
                    }
                    case FolderStreamFormat.KindEnd:
                        await reader.ReadExactlyAsync(number, ct);
                        await reader.ReadExactlyAsync(number, ct);
                        ended = true;
                        break;
                    default:
                        throw new InvalidDataException("The copy stream is corrupt.");
                }
            }
            if (!await reader.AtEndAsync(ct))
                throw new InvalidDataException("The copy stream carried data past its end.");
        }
        catch (OperationCanceledException)
        {
            if (partPath is not null) TryDelete(partPath);
            throw;
        }
        catch (RelayGoneException e)
        {
            if (partPath is not null) TryDelete(partPath);
            return CommandResult.Fail($"[files] {e.Message} {written} file(s) had been written.");
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                    or InvalidDataException or NotSupportedException)
        {
            if (partPath is not null) TryDelete(partPath);
            log.LogWarning(e, "stream_receive from {Url} failed", baseUrl);
            return await FailAsync(http, baseUrl,
                $"{e.Message} {written} file(s) had been written to {destination}.", ct);
        }

        var summary = $"Wrote {written} file(s) in {folders} folder(s) to {destination}"
                      + $" ({reader.BytesReceived / 1024.0 / 1024.0:F1} MB received)."
                      + (kept > 0 ? $" Kept {kept} existing file(s) that were already there." : "")
                      + (incomplete > 0 ? $" {incomplete} file(s) could not be read on the source." : "")
                      + (rejected > 0 ? $" Refused {rejected} unsafe path(s)." : "");
        await FolderStreamFormat.PostJsonAsync(http, $"{baseUrl}/result",
                                               new JsonObject { ["summary"] = summary }, ct);
        onOutput?.Invoke($"[files] {summary}\n");
        return CommandResult.Ok($"[files] {summary}");
    }

    private static async Task<string> ReadPathAsync(RelayChunkReader reader, CancellationToken ct)
    {
        var length = new byte[2];
        await reader.ReadExactlyAsync(length, ct);
        var count = BinaryPrimitives.ReadUInt16BigEndian(length);
        if (count == 0 || count > FolderStreamFormat.MaxPathBytes)
            throw new InvalidDataException("The copy stream is corrupt.");
        var bytes = new byte[count];
        await reader.ReadExactlyAsync(bytes, ct);
        return Encoding.UTF8.GetString(bytes);
    }

    /// <summary>The absolute path a relative stream path lands at, or null when it must not
    /// be written. See the class remarks for the three rules.</summary>
    private static string? Resolve(string root, string rel, HashSet<string> safeDirs)
    {
        var segments = rel.Split('/');
        foreach (var segment in segments)
            if (PathRules.RejectName(segment) is not null) return null;
        string full;
        try
        {
            full = Path.GetFullPath(Path.Combine(root, string.Join('\\', segments)));
        }
        catch (Exception e) when (e is ArgumentException or NotSupportedException
                                    or PathTooLongException)
        {
            return null;
        }
        if (!full.StartsWith(root, StringComparison.OrdinalIgnoreCase)
            || full.Length > PathRules.MaxPathChars)
            return null;

        // Every existing folder between the destination and this entry must be a real folder.
        var current = root.TrimEnd('\\');
        for (var i = 0; i < segments.Length - 1 + (Directory.Exists(full) ? 1 : 0); i++)
        {
            current = current + "\\" + segments[i];
            if (safeDirs.Contains(current)) continue;
            if (Directory.Exists(current))
            {
                if (new DirectoryInfo(current).Attributes.HasFlag(FileAttributes.ReparsePoint))
                    return null;
                safeDirs.Add(current);
            }
            else
            {
                break;   // nothing below a folder that does not exist yet can be a link
            }
        }
        return full;
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
    }

    private static async Task<CommandResult> FailAsync(HttpClient http, string baseUrl,
                                                       string reason, CancellationToken ct)
    {
        await FolderStreamFormat.PostJsonAsync(http, $"{baseUrl}/result",
                                               new JsonObject { ["error"] = reason }, ct);
        return CommandResult.Fail($"[files] {reason}");
    }
}
