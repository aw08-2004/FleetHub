using System.Runtime.InteropServices;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;

namespace TempMonitorAgent.Remote;

/// <summary>Produces BGRA frames of one monitor for the remote-view pipeline (roadmap #2).
/// The frame buffer is reused between calls, so a caller consumes <see cref="Frame"/> before
/// the next <see cref="TryCapture"/>.</summary>
public interface IScreenCapture : IDisposable
{
    int Width { get; }
    int Height { get; }
    /// <summary>Row pitch of <see cref="Frame"/> in bytes (>= Width*4).</summary>
    int Stride { get; }
    /// <summary>BGRA pixels; valid only after <see cref="TryCapture"/> returns true.</summary>
    byte[] Frame { get; }
    /// <summary>True once this capture has produced at least one frame, so <see cref="Frame"/>,
    /// <see cref="Width"/> and <see cref="Height"/> describe a real picture. A caller must not
    /// read "TryCapture returned false" as "there is nothing here" -- on Desktop Duplication
    /// false is overwhelmingly "the screen has not changed", and the screen we most need to
    /// capture (the logon screen) never changes on its own.</summary>
    bool HasFrame { get; }
    /// <summary>GDI device name (<c>\\.\DISPLAY2</c>) of the monitor this capture is actually
    /// showing, or null for "the primary". <b>Input is mapped onto this monitor and no other</b>
    /// (issue #138): the viewer's clicks are fractions of the picture, so the only rectangle
    /// they can be applied to is the one the picture came from -- not whichever monitor some
    /// other enumeration happens to list at the same index.</summary>
    string? OutputDevice => null;
    /// <summary>Capture the next frame. Returns false on a timeout with no screen change
    /// (nothing new to send) so the caller can hold the previous frame.</summary>
    bool TryCapture(int timeoutMs);
}

/// <summary>
/// DXGI Desktop Duplication capture. This is the fast path: the compositor hands us the
/// desktop surface on the GPU, we copy it to a CPU-readable staging texture, and expose the
/// BGRA bytes.
///
/// Two events must be handled or the stream dies silently:
///   * <b>Timeout</b> (no pixels changed) -- normal and frequent; return false, keep the last
///     frame.
///   * <b>Access lost</b> -- the duplication is invalidated whenever the desktop switches
///     (lock screen, UAC secure desktop, resolution change, a full-screen exclusive app).
///     We tear down and rebuild the duplication; the caller just retries next tick. Because the
///     helper runs as SYSTEM-in-session it has rights to the secure (Winlogon) desktop, so a
///     rebuild after the switch should re-duplicate it -- but this is the single piece that most
///     needs on-hardware validation (some setups need the capture thread attached to the input
///     desktop, and SendInput to the secure desktop is a separate concern in InputInjector).
///
/// Not thread-safe: one capture loop owns one instance.
/// </summary>
public sealed class DxgiScreenCapture : IScreenCapture
{
    private readonly int _monitorIndex;
    private ID3D11Device? _device;
    private ID3D11DeviceContext? _context;
    private IDXGIOutputDuplication? _duplication;
    private ID3D11Texture2D? _staging;
    private uint _stagingW, _stagingH;

    public int Width { get; private set; }
    public int Height { get; private set; }
    public int Stride { get; private set; }
    public byte[] Frame { get; private set; } = Array.Empty<byte>();
    public bool HasFrame { get; private set; }

    /// <summary>The duplication object exists, i.e. this adapter/output pair let us duplicate it.
    /// Distinct from <see cref="HasFrame"/>: a duplication of a perfectly static desktop is
    /// working and will simply time out until something moves.</summary>
    public bool IsDuplicating => _duplication is not null;

    /// <summary>Set when the duplication is built, from the output it was built on -- the
    /// fallback included, which is the case an index lookup elsewhere would get wrong.</summary>
    public string? OutputDevice { get; private set; }

    public DxgiScreenCapture(int monitorIndex) => _monitorIndex = monitorIndex;

    public bool TryCapture(int timeoutMs)
    {
        if (_duplication is null && !EnsureDuplication())
            return false;

        IDXGIResource? desktopResource = null;
        try
        {
            var result = _duplication!.AcquireNextFrame((uint)timeoutMs, out _, out desktopResource);
            if (result == Vortice.DXGI.ResultCode.WaitTimeout)
                return false; // no change
            if (result.Failure)
            {
                // Access lost (desktop switch) or anything else: rebuild next tick.
                Teardown();
                return false;
            }

            using var texture = desktopResource!.QueryInterface<ID3D11Texture2D>();
            var desc = texture.Description;
            EnsureStaging(desc.Width, desc.Height, desc.Format);

            _context!.CopyResource(_staging!, texture);
            var mapped = _context.Map(_staging!, 0, MapMode.Read, Vortice.Direct3D11.MapFlags.None);
            try
            {
                Width = (int)desc.Width;
                Height = (int)desc.Height;
                Stride = (int)mapped.RowPitch;
                int needed = Stride * Height;
                if (Frame.Length < needed) Frame = new byte[needed];
                Marshal.Copy(mapped.DataPointer, Frame, 0, needed);
                HasFrame = true;
                return true;
            }
            finally
            {
                _context.Unmap(_staging!, 0);
            }
        }
        catch
        {
            Teardown();
            return false;
        }
        finally
        {
            desktopResource?.Dispose();
            try { _duplication?.ReleaseFrame(); } catch { /* already released on teardown */ }
        }
    }

    private bool EnsureDuplication()
    {
        try
        {
            using var factory = DXGI.CreateDXGIFactory1<IDXGIFactory1>();
            // Pick the requested monitor, falling back to the first if it's gone. The index is
            // into DxgiOutputs' list, the same list the viewer's dropdown was sized from.
            var outputs = DxgiOutputs.List(factory);
            if (outputs.Count == 0) return false;
            var pick = _monitorIndex >= 0 && _monitorIndex < outputs.Count
                ? outputs[_monitorIndex] : outputs[0];

            // The device must live on the adapter that owns the output: DuplicateOutput refuses
            // a device from any other one, which is why a second GPU's monitors used to be
            // offered and then silently replaced by output 0.
            if (factory.EnumAdapters1(pick.Adapter, out var adapter).Failure || adapter is null)
                return false;

            using (adapter)
            {
                var levels = new[] { FeatureLevel.Level_11_0, FeatureLevel.Level_10_1, FeatureLevel.Level_10_0 };
                var hr = D3D11.D3D11CreateDevice(
                    adapter, DriverType.Unknown, DeviceCreationFlags.BgraSupport, levels,
                    out _device, out _context);
                if (hr.Failure || _device is null)
                    return false;

                // Gone between the listing and now (unplugged, desktop switch): drop the device
                // so the retry next tick does not leak it.
                if (adapter.EnumOutputs(pick.Output, out var output).Failure || output is null)
                {
                    Teardown();
                    return false;
                }

                using (output)
                using (var output1 = output.QueryInterface<IDXGIOutput1>())
                {
                    _duplication = output1.DuplicateOutput(_device);
                }
            }
            OutputDevice = pick.DeviceName;
            return _duplication is not null;
        }
        catch
        {
            Teardown();
            return false;
        }
    }

    private void EnsureStaging(uint width, uint height, Format format)
    {
        if (_staging is not null && _stagingW == width && _stagingH == height) return;
        _staging?.Dispose();
        _staging = _device!.CreateTexture2D(new Texture2DDescription
        {
            Width = width,
            Height = height,
            MipLevels = 1,
            ArraySize = 1,
            Format = format,
            SampleDescription = new SampleDescription(1, 0),
            Usage = ResourceUsage.Staging,
            BindFlags = BindFlags.None,
            CPUAccessFlags = CpuAccessFlags.Read,
            MiscFlags = ResourceOptionFlags.None,
        });
        _stagingW = width;
        _stagingH = height;
    }

    private void Teardown()
    {
        try { _duplication?.ReleaseFrame(); } catch { }
        _staging?.Dispose(); _staging = null; _stagingW = _stagingH = 0;
        _duplication?.Dispose(); _duplication = null;
        _context?.Dispose(); _context = null;
        _device?.Dispose(); _device = null;
    }

    public void Dispose() => Teardown();
}

/// <summary>
/// The one numbering of monitors the remote view uses (issue #138). The viewer's monitor index,
/// the count its dropdown offers and the screen the capture opens all come from this list, and
/// the capture hands input the chosen entry's <see cref="Entry.DeviceName"/> rather than the
/// index -- so nothing downstream has to re-enumerate and hope for the same order.
///
/// That hope is the bug this exists to close. Input used to resolve the index through
/// <c>EnumDisplayMonitors</c> and the dropdown through <c>EnumDisplayDevices</c>, while the
/// picture came from DXGI. Nothing makes those three orders agree, and on a 2 - 1 - 3 desk they
/// did not: the operator watched one screen and clicked on its neighbour, from a phone and a
/// desktop browser alike.
///
/// Order is adapter by adapter, outputs in DXGI order within each, desktop-attached outputs only.
/// On a single-GPU machine that is exactly the old <c>EnumOutputs(index)</c> numbering, so a
/// monitor choice an operator already made still means the same screen.
///
/// Session-scoped like every display API (see <see cref="DisplayProbe"/>): call it on a thread
/// attached to the input desktop.
/// </summary>
internal static class DxgiOutputs
{
    internal readonly record struct Entry(uint Adapter, uint Output, string DeviceName);

    /// <summary>Every capturable output, or an empty list when DXGI is unavailable.</summary>
    internal static List<Entry> List()
    {
        try
        {
            using var factory = DXGI.CreateDXGIFactory1<IDXGIFactory1>();
            return List(factory);
        }
        catch { return new List<Entry>(); }
    }

    internal static List<Entry> List(IDXGIFactory1 factory)
    {
        var entries = new List<Entry>();
        for (uint a = 0; factory.EnumAdapters1(a, out var adapter).Success && adapter is not null; a++)
        {
            using (adapter)
            {
                for (uint o = 0; adapter.EnumOutputs(o, out var output).Success && output is not null; o++)
                {
                    using (output)
                    {
                        var desc = output.Description;
                        if (desc.AttachedToDesktop)
                            entries.Add(new Entry(a, o, desc.DeviceName));
                    }
                }
            }
        }
        return entries;
    }
}

/// <summary>
/// GDI BitBlt fallback for when Desktop Duplication is unavailable (some VMs, older RDP
/// sessions). Slower and with no change detection -- it blits the whole primary display every
/// call -- but it always produces a frame. It cannot see the secure desktop either; that is a
/// phase-5 concern for both capture paths.
/// </summary>
public sealed class GdiScreenCapture : IScreenCapture
{
    public int Width { get; private set; }
    public int Height { get; private set; }
    public int Stride { get; private set; }
    public byte[] Frame { get; private set; } = Array.Empty<byte>();
    public bool HasFrame { get; private set; }

    public bool TryCapture(int timeoutMs)
    {
        IntPtr screenDc = GetDC(IntPtr.Zero);
        if (screenDc == IntPtr.Zero) return false;
        IntPtr memDc = IntPtr.Zero, bmp = IntPtr.Zero, old = IntPtr.Zero;
        try
        {
            int w = GetSystemMetrics(SM_CXSCREEN);
            int h = GetSystemMetrics(SM_CYSCREEN);
            if (w <= 0 || h <= 0) return false;

            memDc = CreateCompatibleDC(screenDc);
            if (memDc == IntPtr.Zero) return false;
            bmp = CreateCompatibleBitmap(screenDc, w, h);
            if (bmp == IntPtr.Zero) return false;
            old = SelectObject(memDc, bmp);
            // Every one of these can fail for a reason that is invisible from here: a
            // desktop switch, the secure desktop coming up, or a display mode change
            // mid-blit. Unchecked, the bitmap keeps whatever it held before and we hand
            // the encoder a stale or black frame while reporting a fresh capture -- so a
            // failure has to come back as one, and let the caller retry on the next tick.
            if (!BitBlt(memDc, 0, 0, w, h, screenDc, 0, 0, SRCCOPY | CAPTUREBLT)) return false;

            Width = w; Height = h; Stride = w * 4;
            int needed = Stride * h;
            if (Frame.Length < needed) Frame = new byte[needed];

            var bmi = new BITMAPINFOHEADER
            {
                biSize = Marshal.SizeOf<BITMAPINFOHEADER>(),
                biWidth = w,
                biHeight = -h,          // top-down
                biPlanes = 1,
                biBitCount = 32,
                biCompression = 0,      // BI_RGB
            };
            int copied;
            var handle = GCHandle.Alloc(Frame, GCHandleType.Pinned);
            try { copied = GetDIBits(memDc, bmp, 0, (uint)h, handle.AddrOfPinnedObject(), ref bmi, 0); }
            finally { handle.Free(); }
            // GetDIBits returns the number of scan lines it actually copied, 0 on failure.
            // A partial copy leaves the tail of Frame holding the previous frame, which
            // reads as a torn image rather than a dropped one.
            if (copied != h) return false;

            HasFrame = true;
            return true;
        }
        catch { return false; }
        finally
        {
            // Restore before deleting: DeleteObject refuses a bitmap still selected into
            // a DC, which on the early-return paths would leak it.
            if (memDc != IntPtr.Zero && old != IntPtr.Zero) SelectObject(memDc, old);
            if (bmp != IntPtr.Zero) DeleteObject(bmp);
            if (memDc != IntPtr.Zero) DeleteDC(memDc);
            ReleaseDC(IntPtr.Zero, screenDc);
        }
    }

    public void Dispose() { }

    private const int SM_CXSCREEN = 0, SM_CYSCREEN = 1;
    private const int SRCCOPY = 0x00CC0020;
    private const int CAPTUREBLT = 0x40000000;

    [StructLayout(LayoutKind.Sequential)]
    private struct BITMAPINFOHEADER
    {
        public int biSize, biWidth, biHeight;
        public short biPlanes, biBitCount;
        public int biCompression, biSizeImage, biXPelsPerMeter, biYPelsPerMeter, biClrUsed, biClrImportant;
    }

    [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr hWnd, IntPtr hDc);
    [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleDC(IntPtr hDc);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(IntPtr hDc);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleBitmap(IntPtr hDc, int w, int h);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr obj);
    [DllImport("gdi32.dll")] private static extern IntPtr SelectObject(IntPtr hDc, IntPtr obj);
    [DllImport("gdi32.dll")] private static extern bool BitBlt(
        IntPtr hDc, int x, int y, int w, int h, IntPtr hSrcDc, int xSrc, int ySrc, int rop);
    [DllImport("gdi32.dll")] private static extern int GetDIBits(
        IntPtr hDc, IntPtr bmp, uint start, uint lines, IntPtr bits, ref BITMAPINFOHEADER bmi, uint usage);
}
