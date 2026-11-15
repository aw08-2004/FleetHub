using System.Buffers.Binary;
using System.Collections.Concurrent;
using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.Files;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The live folder copy (stream_send / stream_receive, hub/folder_relay.py), run end to end
/// against an in-memory stand-in for the hub's relay.
///
/// The silent failure these exist to catch is <b>a copy that looks complete and is not</b>: a
/// file that spans chunks reassembled wrong, a stream cut short that parses as a smaller
/// folder, or a path in the stream that writes outside the destination. Each would leave a
/// target PC holding something that looks like the source and is not, with a green result.
/// tests/test_folder_relay.py pins the hub's half of the same contract.
/// </summary>
public class FolderStreamTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "fhs-test-" + Guid.NewGuid().ToString("n"));
    private const string Base = "https://hub.test/api/agent/files/stream/t1";

    public FolderStreamTests() => Directory.CreateDirectory(_root);

    public void Dispose()
    {
        try { Directory.Delete(_root, recursive: true); } catch (IOException) { }
    }

    /// <summary>The hub's relay, reduced to what the agents can observe: numbered chunks in,
    /// numbered chunks out, an explicit end, and the receiver's verdict.</summary>
    private sealed class FakeRelay : HttpMessageHandler
    {
        public readonly ConcurrentDictionary<long, byte[]> Chunks = new();
        public volatile bool Ended;
        public volatile string? SendError;
        public JsonNode? Result;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
                                                                     CancellationToken ct)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (request.Method == HttpMethod.Put && path.Contains("/chunk/"))
            {
                var seq = long.Parse(path[(path.LastIndexOf('/') + 1)..]);
                Chunks.TryAdd(seq, await request.Content!.ReadAsByteArrayAsync(ct));
                return new HttpResponseMessage(HttpStatusCode.OK);
            }
            if (request.Method == HttpMethod.Get && path.Contains("/chunk/"))
            {
                var seq = long.Parse(path[(path.LastIndexOf('/') + 1)..]);
                for (var i = 0; i < 200; i++)
                {
                    if (SendError is not null) return new HttpResponseMessage(HttpStatusCode.Gone);
                    if (Chunks.TryGetValue(seq, out var bytes))
                        return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(bytes) };
                    if (Ended)
                    {
                        var end = new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent([]) };
                        end.Headers.Add("X-Stream-End", "1");
                        return end;
                    }
                    await Task.Delay(10, ct);
                }
                return new HttpResponseMessage(HttpStatusCode.NoContent);
            }
            var body = request.Content is null ? null
                : JsonNode.Parse(await request.Content.ReadAsStringAsync(ct));
            if (path.EndsWith("/end"))
            {
                if (body?["error"] is { } error) SendError = error.ToString();
                else Ended = true;
            }
            else if (path.EndsWith("/result"))
            {
                Result = body;
            }
            return new HttpResponseMessage(HttpStatusCode.OK);
        }
    }

    private static HttpClient Client(FakeRelay relay) => new(relay, disposeHandler: false);

    private string Make(string rel, byte[] content)
    {
        var full = Path.Combine(_root, rel);
        Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        File.WriteAllBytes(full, content);
        return full;
    }

    private async Task<(CommandResult Sent, CommandResult Received)> CopyAsync(
        FakeRelay relay, string destination, params string[] roots)
    {
        var paths = new JsonArray(roots.Select(r => (JsonNode)JsonValue.Create(r)!).ToArray());
        using var sendHttp = Client(relay);
        using var recvHttp = Client(relay);
        var send = StreamSendExecutor.SendAsync(sendHttp, Base, paths, null,
                                                NullLogger.Instance, CancellationToken.None);
        var receive = StreamReceiveExecutor.ReceiveAsync(recvHttp, Base, destination, false, null,
                                                         NullLogger.Instance, CancellationToken.None);
        await Task.WhenAll(send, receive);
        return (send.Result, receive.Result);
    }

    [Fact]
    public async Task TwoFolders_ArriveByteForByte()
    {
        var big = new byte[3 * 1024 * 1024 + 123];        // spans four 1 MiB chunks
        Random.Shared.NextBytes(big);
        Make(@"src\Docs\report.bin", big);
        Make(@"src\Docs\sub\deeper\note.txt", "hello"u8.ToArray());
        Make(@"src\Docs\empty.txt", []);
        Directory.CreateDirectory(Path.Combine(_root, @"src\Docs\nothing-here"));
        Make(@"src\Pics\a.jpg", [1, 2, 3]);
        var dest = Path.Combine(_root, "dest");

        var relay = new FakeRelay();
        var (sent, received) = await CopyAsync(relay, dest,
            Path.Combine(_root, @"src\Docs"), Path.Combine(_root, @"src\Pics"));

        Assert.True(sent.Success, sent.Output);
        Assert.True(received.Success, received.Output);
        Assert.Equal(big, File.ReadAllBytes(Path.Combine(dest, @"Docs\report.bin")));
        Assert.Equal("hello", File.ReadAllText(Path.Combine(dest, @"Docs\sub\deeper\note.txt")));
        Assert.Empty(File.ReadAllBytes(Path.Combine(dest, @"Docs\empty.txt")));
        Assert.True(Directory.Exists(Path.Combine(dest, @"Docs\nothing-here")));
        Assert.Equal([1, 2, 3], File.ReadAllBytes(Path.Combine(dest, @"Pics\a.jpg")));
        Assert.Empty(Directory.GetFiles(dest, "*.fhpart", SearchOption.AllDirectories));
        Assert.Contains("Wrote 4 file(s)", relay.Result?["summary"]?.ToString());
    }

    [Fact]
    public async Task AnExistingFile_IsKeptNotReplaced()
    {
        Make(@"src\Docs\x.txt", "new"u8.ToArray());
        var dest = Path.Combine(_root, "dest");
        Make(@"dest\Docs\x.txt", "old"u8.ToArray());

        var relay = new FakeRelay();
        var (_, received) = await CopyAsync(relay, dest, Path.Combine(_root, @"src\Docs"));

        Assert.True(received.Success, received.Output);
        Assert.Equal("old", File.ReadAllText(Path.Combine(dest, @"Docs\x.txt")));
        Assert.Contains("Kept 1 existing", received.Output);
    }

    /// <summary>Builds a raw stream the way a broken or hostile sender might.</summary>
    private static byte[] Stream(Action<MemoryStream> body)
    {
        var ms = new MemoryStream();
        ms.Write("FHS1"u8);
        body(ms);
        return ms.ToArray();
    }

    private static void Path_(MemoryStream ms, string rel)
    {
        var bytes = Encoding.UTF8.GetBytes(rel);
        Span<byte> len = stackalloc byte[2];
        BinaryPrimitives.WriteUInt16BigEndian(len, (ushort)bytes.Length);
        ms.Write(len);
        ms.Write(bytes);
    }

    private static void File_(MemoryStream ms, string rel, byte[] content, byte status = 0)
    {
        ms.WriteByte((byte)'F');
        Path_(ms, rel);
        ms.Write(new byte[8]);
        Span<byte> len = stackalloc byte[4];
        if (content.Length > 0)
        {
            BinaryPrimitives.WriteUInt32BigEndian(len, (uint)content.Length);
            ms.Write(len);
            ms.Write(content);
        }
        BinaryPrimitives.WriteUInt32BigEndian(len, 0);
        ms.Write(len);
        ms.WriteByte(status);
    }

    private static void End_(MemoryStream ms)
    {
        ms.WriteByte((byte)'E');
        ms.Write(new byte[16]);
    }

    private async Task<(CommandResult Result, FakeRelay Relay)> ReceiveRawAsync(byte[] stream, string dest)
    {
        var relay = new FakeRelay();
        relay.Chunks[0] = stream;
        relay.Ended = true;
        using var http = Client(relay);
        var result = await StreamReceiveExecutor.ReceiveAsync(http, Base, dest, false, null,
                                                              NullLogger.Instance, CancellationToken.None);
        return (result, relay);
    }

    [Fact]
    public async Task AStreamCutShort_IsAFailureNotASmallerFolder()
    {
        // Two whole files and no end record: exactly what a sender that died between two
        // chunks leaves behind. It must not read as a finished two-file folder.
        var stream = Stream(ms =>
        {
            File_(ms, "Docs/a.txt", "a"u8.ToArray());
            File_(ms, "Docs/b.txt", "b"u8.ToArray());
        });
        var (result, relay) = await ReceiveRawAsync(stream, Path.Combine(_root, "dest"));
        Assert.False(result.Success);
        Assert.NotNull(relay.Result?["error"]);
    }

    [Fact]
    public async Task AFileTheSourceCouldNotFinish_IsDiscarded()
    {
        var dest = Path.Combine(_root, "dest");
        var stream = Stream(ms =>
        {
            File_(ms, "Docs/half.pst", "partial"u8.ToArray(), status: 1);
            End_(ms);
        });
        var (result, _) = await ReceiveRawAsync(stream, dest);
        Assert.True(result.Success, result.Output);
        Assert.False(File.Exists(Path.Combine(dest, @"Docs\half.pst")));
        Assert.Empty(Directory.GetFiles(dest, "*.fhpart", SearchOption.AllDirectories));
        Assert.Contains("could not be read on the source", result.Output);
    }

    [Theory]
    [InlineData("Docs/../../evil.txt")]
    [InlineData("../evil.txt")]
    [InlineData("C:/evil.txt")]
    [InlineData("Docs/a\\..\\..\\evil.txt")]
    public async Task APathThatLeavesTheDestination_IsRefused(string rel)
    {
        var dest = Path.Combine(_root, "dest");
        var stream = Stream(ms =>
        {
            File_(ms, rel, "x"u8.ToArray());
            End_(ms);
        });
        var (result, _) = await ReceiveRawAsync(stream, dest);
        Assert.True(result.Success, result.Output);
        Assert.Contains("Refused 1 unsafe path", result.Output);
        Assert.False(File.Exists(Path.Combine(_root, "evil.txt")));
        Assert.False(File.Exists(@"C:\evil.txt"));
        Assert.Empty(Directory.GetFiles(_root, "evil.txt", SearchOption.AllDirectories));
    }
}
