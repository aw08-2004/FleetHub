using System.Text;
using System.Text.Json;
using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Remote clipboard sync (issue #148). Every failure here is silent: a chunk set reassembled
/// wrong pastes garbage or nothing, a message over the data channel's size limit closes the
/// channel (and input with it), and an offer missing its <c>clipboard</c> flag hides the
/// feature from every operator without a word in any log.
/// </summary>
public class ClipboardTests
{
    private static JsonElement Chunk(long id, int i, int n, string s) =>
        JsonDocument.Parse(JsonSerializer.Serialize(new { t = "clip", id, i, n, s })).RootElement;

    [Fact]
    public void Assembler_JoinsChunksInOrder()
    {
        var a = new ClipboardAssembler();
        Assert.Null(a.Accept(Chunk(1, 0, 3, "ab")));
        Assert.Null(a.Accept(Chunk(1, 1, 3, "cd")));
        Assert.Equal("abcdef", a.Accept(Chunk(1, 2, 3, "ef")));
    }

    [Fact]
    public void Assembler_JoinsChunksThatArriveOutOfOrder()
    {
        var a = new ClipboardAssembler();
        Assert.Null(a.Accept(Chunk(7, 2, 3, "ef")));
        Assert.Null(a.Accept(Chunk(7, 0, 3, "ab")));
        Assert.Equal("abcdef", a.Accept(Chunk(7, 1, 3, "cd")));
    }

    [Fact]
    public void Assembler_NewerPasteAbandonsAHalfAssembledOne()
    {
        var a = new ClipboardAssembler();
        Assert.Null(a.Accept(Chunk(1, 0, 2, "old")));
        Assert.Equal("new", a.Accept(Chunk(2, 0, 1, "new")));
        // The abandoned paste's last chunk must not complete anything on its own.
        Assert.Null(a.Accept(Chunk(1, 1, 2, "tail")));
    }

    [Fact]
    public void Assembler_IgnoresADuplicateChunk()
    {
        var a = new ClipboardAssembler();
        Assert.Null(a.Accept(Chunk(3, 0, 2, "ab")));
        Assert.Null(a.Accept(Chunk(3, 0, 2, "ab")));
        Assert.Equal("abcd", a.Accept(Chunk(3, 1, 2, "cd")));
    }

    [Theory]
    [InlineData(0, 0)]
    [InlineData(-1, 1)]
    [InlineData(1, 1)]
    [InlineData(0, ClipboardAssembler.MaxChunks + 1)]
    public void Assembler_RejectsImpossibleIndexes(int i, int n) =>
        Assert.Null(new ClipboardAssembler().Accept(Chunk(1, i, n, "x")));

    [Fact]
    public void Assembler_CapsTheWholePaste()
    {
        var a = new ClipboardAssembler();
        string half = new('x', ClipboardBridge.MaxChars / 2 + 10);
        Assert.Null(a.Accept(Chunk(1, 0, 2, half)));
        Assert.Equal(ClipboardBridge.MaxChars, a.Accept(Chunk(1, 1, 2, half))!.Length);
    }

    [Fact]
    public void EchoGuard_SwallowsOnlyTheEchoOfOurOwnPaste()
    {
        var g = new ClipboardEchoGuard();
        g.Set("pasted");
        Assert.False(g.ShouldSend("pasted"));
        // Consumed: the PC user copying the same text afterwards is a real copy.
        Assert.True(g.ShouldSend("pasted"));
    }

    [Fact]
    public void EchoGuard_ARecopyAfterAPasteInBetweenIsSent()
    {
        // The sequence found in review of #150: copy X, viewer pastes Y, copy X again.
        var g = new ClipboardEchoGuard();
        Assert.True(g.ShouldSend("X"));
        g.Set("Y");
        Assert.False(g.ShouldSend("Y"));
        Assert.True(g.ShouldSend("X"));
    }

    [Fact]
    public void EchoGuard_ACopyThatOvertakesTheEchoIsSent()
    {
        // The user copied something else before the debounced read saw our paste.
        var g = new ClipboardEchoGuard();
        g.Set("Y");
        Assert.True(g.ShouldSend("Z"));
        Assert.True(g.ShouldSend("Y"));
    }

    [Fact]
    public void Truncate_NeverSplitsASurrogatePair()
    {
        // "a" + one emoji (two code units): a cut at 2 would leave half the emoji.
        string text = "a\U0001F600b";
        Assert.Equal("a", ClipboardBridge.Truncate(text, 2));
        Assert.Equal("a\U0001F600", ClipboardBridge.Truncate(text, 3));
        Assert.Equal(text, ClipboardBridge.Truncate(text, 10));
    }

    [Fact]
    public void ClipMessage_StartsWithTypeAndRoundTrips()
    {
        string json = ClipboardMessages.Clip("Grüße \"quoted\"", truncated: false);
        Assert.StartsWith("{\"t\":\"clip\",", json);
        var e = JsonDocument.Parse(json).RootElement;
        Assert.Equal("Grüße \"quoted\"", e.GetProperty("s").GetString());
        Assert.False(e.GetProperty("trunc").GetBoolean());
        // Non-ASCII stays UTF-8 rather than six-byte escapes.
        Assert.Contains("Grüße", json);
    }

    [Fact]
    public void ClipMessage_StaysUnderTheByteCapAndSaysItWasCut()
    {
        // Control characters are the worst case: six bytes each once escaped.
        string text = new('\u0001', ClipboardBridge.MaxChars);
        string json = ClipboardMessages.Clip(text, truncated: false);
        Assert.True(Encoding.UTF8.GetByteCount(json) <= ClipboardMessages.MaxBytes);
        Assert.True(JsonDocument.Parse(json).RootElement.GetProperty("trunc").GetBoolean());
    }

    [Fact]
    public void Offer_CarriesBothCapabilityFlags()
    {
        var json = JsonSerializer.Serialize(RemotePeer.OfferPayload("v=0", true, true));
        var e = JsonDocument.Parse(json).RootElement;
        Assert.Equal("offer", e.GetProperty("type").GetString());
        Assert.True(e.GetProperty("relay").GetBoolean());
        Assert.True(e.GetProperty("clipboard").GetBoolean());
    }
}
