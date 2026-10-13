using System.Diagnostics;
using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The H.264 encoder must not grow native commit per frame (roadmap #2).
///
/// The silent failure: every Media Foundation sample and buffer the encoder creates is a COM
/// object behind a SharpGen wrapper, and SharpGen here never releases on finalize -- so a
/// wrapper that is not disposed pins its native memory for the life of the process. Nothing in
/// the managed heap shows it, the stream keeps working, and the remote helper simply climbs
/// until the machine runs out of commit. On AIO-HOBBY that was about 19 GB and a crashed DWM.
///
/// Measured as private bytes across a few hundred frames on the in-box software MFT, the one
/// encoder every Windows box has. At 640x480 the old code leaked roughly 1 MB a frame (input
/// buffer plus two output buffers), so 300 frames is hundreds of MB against a bound of 64 MB:
/// wide enough for MF's own warm-up allocations, far too narrow for a per-frame leak. A
/// live-wrapper count was rejected as the assertion because it would pass for a leak in code
/// that bypasses whatever counter we add; commit is what actually ran out.
/// </summary>
public class H264EncoderLeakTests
{
    private const int W = 640, H = 480, Fps = 30;

    [Fact]
    public void SoftwareEncode_PrivateBytesStayBounded()
    {
        using var encoder = new H264Encoder(W, H, Fps, 1_000_000, EncoderPreference.Software);
        var nv12 = new byte[ColorConvert.Nv12Size(W, H)];
        long duration = 10_000_000 / Fps;
        int frame = 0;
        int produced = 0;

        void EncodeFrames(int count)
        {
            for (int i = 0; i < count; i++, frame++)
            {
                // Vary the picture so the encoder does real work rather than skipping frames.
                nv12[frame % nv12.Length] ^= 0xff;
                if (encoder.Encode(nv12, nv12.Length, frame * duration, duration).Length > 0)
                    produced++;
            }
        }

        // Warm-up: MF's first frames allocate its internal pools, which are not a leak.
        EncodeFrames(30);
        long before = PrivateBytesAfterGc();

        EncodeFrames(300);
        long grown = PrivateBytesAfterGc() - before;

        // Guard against the test passing because nothing was encoded at all.
        Assert.True(produced > 100, $"encoder produced only {produced} access units");
        Assert.True(grown < 64L * 1024 * 1024,
            $"private bytes grew {grown / (1024 * 1024)} MB over 300 frames -- per-frame leak");
    }

    /// <summary>Collect first so a managed-side spike is not mistaken for native growth --
    /// the leak this guards against survives any number of collections.</summary>
    private static long PrivateBytesAfterGc()
    {
        GC.Collect();
        GC.WaitForPendingFinalizers();
        GC.Collect();
        using var self = Process.GetCurrentProcess();
        return self.PrivateMemorySize64;
    }
}
