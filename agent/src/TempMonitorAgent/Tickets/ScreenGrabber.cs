using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace TempMonitorAgent.Tickets;

/// <summary>One screen as the tray captured it: what the person sees as a preview, and the
/// JPEG that goes to the helpdesk if they keep it ticked.</summary>
public sealed class CapturedScreen(string label, bool primary, byte[] jpeg, Image preview)
    : IDisposable
{
    public string Label { get; } = label;
    public bool Primary { get; } = primary;
    public byte[] Jpeg { get; } = jpeg;
    public Image Preview { get; } = preview;
    public void Dispose() => Preview.Dispose();
}

/// <summary>
/// The tray's screenshots (roadmap #31 phase A): every screen, taken **before** the ticket
/// form opens, so the picture is of the problem and not of the form covering it.
///
/// **Not <see cref="Remote.GdiScreenCapture"/>**, which the design named: it blits only the
/// primary display (SM_CXSCREEN), and the owner's decision is that the person picks which
/// screens to send -- the error is as often on the second monitor as on the first. This is
/// the same GDI blit per <see cref="Screen.Bounds"/> (see <see cref="Blit"/>), in a process that is per-monitor DPI aware (TrayApp sets it), so bounds are real pixels and
/// a 150 % laptop screen is not captured as its top-left two thirds. DXGI duplication was not
/// considered worth it here: one still frame per screen, taken on a click, is what GDI is for.
///
/// **Downscaled and size-capped before it leaves the PC**: at most <see cref="MaxEdge"/>
/// pixels on the long side, JPEG, and re-encoded smaller until it is under
/// <see cref="TrayProtocol.MaxScreenshotBytes"/>. A 4K screen is a lot of somebody's screen to
/// keep for thirty days, and 1920 still reads every error dialog.
/// </summary>
public static class ScreenGrabber
{
    public const int MaxEdge = 1920;
    private const int PreviewEdge = 320;
    private static readonly long[] Qualities = [80L, 65L, 50L];

    /// <summary>Capture every screen. A screen that fails (a display mode change mid-blit,
    /// a disconnected monitor) is left out rather than failing the ticket.</summary>
    public static List<CapturedScreen> CaptureAll()
    {
        var result = new List<CapturedScreen>();
        var screens = Screen.AllScreens;
        for (int i = 0; i < screens.Length && result.Count < TrayProtocol.MaxScreenshots; i++)
        {
            var screen = screens[i];
            try
            {
                using var full = new Bitmap(screen.Bounds.Width, screen.Bounds.Height,
                                            PixelFormat.Format24bppRgb);
                if (!Blit(full, screen.Bounds))
                {
                    TrayLog.Write($"Could not capture screen {i + 1}: BitBlt failed (win32 {Marshal.GetLastWin32Error()}).");
                    continue;
                }
                var jpeg = Encode(full);
                if (jpeg is null)
                {
                    TrayLog.Write($"Screen {i + 1} could not be made small enough to send.");
                    continue;
                }
                var label = "Your screen";
                if (screens.Length > 1)
                    label = $"Screen {i + 1}" + (screen.Primary ? " (main)" : "");
                result.Add(new CapturedScreen(label, screen.Primary, jpeg, Scale(full, PreviewEdge)));
            }
            catch (Exception e) when (e is ArgumentException or System.ComponentModel.Win32Exception
                                          or ExternalException)
            {
                TrayLog.Write($"Could not capture screen {i + 1}: {e.Message}");
            }
        }
        return result;
    }

    /// <summary>
    /// One screen's pixels into <paramref name="target"/>: BitBlt from the desktop DC with
    /// SRCCOPY | CAPTUREBLT, the call GdiScreenCapture makes. **Not
    /// <c>Graphics.CopyFromScreen</c>**, which the first version used: .NET validates its
    /// operation as a single enum value and throws on SourceCopy | CaptureBlt, and without
    /// CAPTUREBLT layered windows -- many error pop-ups, every toast -- are missing from the
    /// picture, which is the one thing the screenshot is for. Found by TicketTrayUiTests, on
    /// the first capture ever attempted.
    /// </summary>
    private static bool Blit(Bitmap target, Rectangle bounds)
    {
        using var g = Graphics.FromImage(target);
        IntPtr destination = g.GetHdc();
        IntPtr desktop = GetDC(IntPtr.Zero);
        try
        {
            return desktop != IntPtr.Zero
                && BitBlt(destination, 0, 0, bounds.Width, bounds.Height, desktop, bounds.X, bounds.Y,
                          SrcCopy | CaptureBlt);
        }
        finally
        {
            if (desktop != IntPtr.Zero) ReleaseDC(IntPtr.Zero, desktop);
            g.ReleaseHdc(destination);
        }
    }

    private const int SrcCopy = 0x00CC0020;
    private const int CaptureBlt = 0x40000000;

    [DllImport("user32.dll")]
    private static extern IntPtr GetDC(IntPtr window);

    [DllImport("user32.dll")]
    private static extern int ReleaseDC(IntPtr window, IntPtr dc);

    [DllImport("gdi32.dll", SetLastError = true)]
    private static extern bool BitBlt(IntPtr destination, int x, int y, int width, int height,
                                      IntPtr source, int sourceX, int sourceY, int operation);

    /// <summary>The size an image of <paramref name="width"/> x <paramref name="height"/> is
    /// scaled to so its long side is at most <paramref name="maxEdge"/>. Never upscales, and
    /// never rounds a side to zero.</summary>
    public static Size FitWithin(int width, int height, int maxEdge)
    {
        int longEdge = Math.Max(width, height);
        if (longEdge <= maxEdge || longEdge <= 0) return new Size(Math.Max(1, width), Math.Max(1, height));
        double factor = (double)maxEdge / longEdge;
        return new Size(Math.Max(1, (int)Math.Round(width * factor)),
                        Math.Max(1, (int)Math.Round(height * factor)));
    }

    /// <summary>JPEG under the cap: lower quality first, then smaller, then give up -- a
    /// screen that cannot be made small enough is one the person is told was not sent.</summary>
    private static byte[]? Encode(Bitmap full)
    {
        int edge = MaxEdge;
        for (int attempt = 0; attempt < 4; attempt++, edge = edge * 3 / 4)
        {
            using var scaled = Scale(full, edge);
            foreach (var quality in Qualities)
            {
                var bytes = ToJpeg(scaled, quality);
                if (bytes.Length <= TrayProtocol.MaxScreenshotBytes) return bytes;
            }
        }
        return null;
    }

    private static Bitmap Scale(Bitmap source, int maxEdge)
    {
        var size = FitWithin(source.Width, source.Height, maxEdge);
        var scaled = new Bitmap(size.Width, size.Height, PixelFormat.Format24bppRgb);
        using var g = Graphics.FromImage(scaled);
        g.InterpolationMode = InterpolationMode.HighQualityBicubic;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;
        g.DrawImage(source, new Rectangle(Point.Empty, size));
        return scaled;
    }

    private static byte[] ToJpeg(Image image, long quality)
    {
        var codec = ImageCodecInfo.GetImageEncoders().First(c => c.FormatID == ImageFormat.Jpeg.Guid);
        using var parameters = new EncoderParameters(1);
        parameters.Param[0] = new EncoderParameter(Encoder.Quality, quality);
        using var stream = new MemoryStream();
        image.Save(stream, codec, parameters);
        return stream.ToArray();
    }
}
