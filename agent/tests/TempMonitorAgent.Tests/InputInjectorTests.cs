using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>The pure, testable seam of input injection (roadmap #2): mapping the browser's
/// KeyboardEvent.code to a Windows virtual-key. The SendInput calls and monitor geometry need a
/// real desktop and are validated on-device; this pins the keymap that decides whether typing
/// works.</summary>
public class InputInjectorTests
{
    [Theory]
    [InlineData("KeyA", 0x41)]
    [InlineData("KeyZ", 0x5A)]
    [InlineData("Digit0", 0x30)]
    [InlineData("Digit9", 0x39)]
    [InlineData("Enter", 0x0D)]
    [InlineData("NumpadEnter", 0x0D)]
    [InlineData("Escape", 0x1B)]
    [InlineData("Backspace", 0x08)]
    [InlineData("Tab", 0x09)]
    [InlineData("Space", 0x20)]
    [InlineData("ArrowUp", 0x26)]
    [InlineData("Delete", 0x2E)]
    [InlineData("ControlLeft", 0xA2)]
    [InlineData("AltRight", 0xA5)]
    [InlineData("F1", 0x70)]
    [InlineData("F12", 0x7B)]
    public void MapCodeToVk_KnownKeys(string code, int expected)
    {
        Assert.Equal((ushort)expected, InputInjector.MapCodeToVk(code));
    }

    [Theory]
    [InlineData("")]
    [InlineData(null)]
    [InlineData("SomeUnknownKey")]
    [InlineData("IntlBackslash")]   // deliberately unmapped -> Unicode fallback handles it
    public void MapCodeToVk_UnknownKeysReturnZero(string? code)
    {
        // 0 signals "no VK" so the injector falls back to Unicode injection for printable keys.
        Assert.Equal((ushort)0, InputInjector.MapCodeToVk(code));
    }

    [Fact]
    public void MapCodeToVk_FunctionKeyOutOfRangeIsUnmapped()
    {
        Assert.Equal((ushort)0, InputInjector.MapCodeToVk("F25"));
    }

    // Issue #138: a 2 - 1 - 3 desk, listed by EnumDisplayMonitors in an order that is neither
    // the physical layout nor DXGI's. Input used to pick the monitor by its position in this
    // list, so the operator watched one screen and clicked on its neighbour.
    private static readonly InputInjector.Monitor[] TwoOneThree =
    {
        new(@"\\.\DISPLAY2", false, -1920, 0, 1920, 1080),
        new(@"\\.\DISPLAY1", true, 0, 0, 2560, 1440),
        new(@"\\.\DISPLAY3", false, 2560, 0, 1920, 1080),
    };

    [Theory]
    [InlineData(@"\\.\DISPLAY1", 0)]
    [InlineData(@"\\.\DISPLAY2", -1920)]
    [InlineData(@"\\.\DISPLAY3", 2560)]
    public void Pick_FindsTheCapturedMonitorByNameWhateverItsPositionInTheList(string device, int left)
    {
        Assert.Equal(left, InputInjector.Pick(TwoOneThree, device)!.Value.Left);
    }

    [Fact]
    public void Pick_ComparesDeviceNamesCaseInsensitively()
    {
        Assert.Equal(2560, InputInjector.Pick(TwoOneThree, @"\\.\display3")!.Value.Left);
    }

    [Theory]
    [InlineData(null)]               // GDI fallback: it only ever captures the primary
    [InlineData(@"\\.\DISPLAY9")]    // unplugged since the capture opened
    public void Pick_FallsBackToThePrimaryNotTheFirstEntry(string? device)
    {
        var m = InputInjector.Pick(TwoOneThree, device)!.Value;
        Assert.True(m.Primary);
        Assert.Equal(@"\\.\DISPLAY1", m.Device);
    }

    [Fact]
    public void Pick_ReturnsNullWithNoMonitorsSoTheCallerUsesTheScreenMetrics()
    {
        Assert.Null(InputInjector.Pick(Array.Empty<InputInjector.Monitor>(), @"\\.\DISPLAY1"));
    }
}
