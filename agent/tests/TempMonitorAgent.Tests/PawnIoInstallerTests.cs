using System.Security.Cryptography;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.Telemetry;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The agent installing a missing PawnIO driver itself (roadmap #33).
///
/// The silent failures this file exists for: a setup run whose bytes do not match the pin, which
/// would start an unchecked driver installer as SYSTEM; a PC that cannot reach GitHub never
/// trying the hub's copy, and so never getting temperatures; a failing download retried on every
/// inventory tick; a PC that already has the driver downloading it anyway; and a driver installed
/// but never seen, because LibreHardwareMonitor was not reopened.
/// </summary>
public sealed class PawnIoInstallerTests : IDisposable
{
    private const string Hub = "https://hub.example.com";
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "pawnio-test-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        try { Directory.Delete(_dir, recursive: true); } catch (IOException) { }
        PawnIoInstaller.TakeSensorReopen();
    }

    // The real PawnIO file is not in the repository, so no fake source can serve bytes that
    // match the pin. The tests therefore assert on what was fetched and that nothing was RUN.
    private PawnIoInstaller Make(Func<string, byte[]> serve, List<string> fetched, List<string> ran,
                                 Func<bool> installed, int exit = 0, Func<DateTime>? now = null) =>
        new(NullLogger<PawnIoInstaller>.Instance)
        {
            IsInstalled = installed,
            GetBytes = (url, _) => { fetched.Add(url); return Task.FromResult(serve(url)); },
            RunSetup = (path, _) => { ran.Add(Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant()); return Task.FromResult(exit); },
            UtcNow = now ?? (() => DateTime.UtcNow),
            HubBase = Hub,
            StagingDir = _dir,
            // The real hardening would lock an unelevated test run out of its own temp folder.
            PrepareStaging = p => { Directory.CreateDirectory(p); return null; },
        };

    private static byte[] Mismatch(string _) => [1, 2, 3];

    [Fact]
    public async Task A_PC_that_has_the_driver_downloads_nothing()
    {
        var fetched = new List<string>(); var ran = new List<string>();
        var sut = Make(Mismatch, fetched, ran, installed: () => true);
        Assert.True(await sut.EnsureAsync(default));
        Assert.Empty(fetched);
        Assert.Empty(ran);
    }

    [Fact]
    public async Task Bytes_that_do_not_match_the_pin_are_never_run_and_the_hub_copy_is_tried_by_hash()
    {
        var fetched = new List<string>(); var ran = new List<string>();
        var sut = Make(Mismatch, fetched, ran, installed: () => false);
        Assert.False(await sut.EnsureAsync(default));
        Assert.Empty(ran);
        Assert.Equal(new[] { PawnIoInstaller.Url, $"{Hub}/api/agent/dist/bin/{PawnIoInstaller.Sha256}" }, fetched);
    }

    [Fact]
    public async Task A_GitHub_failure_falls_back_to_the_hub()
    {
        var fetched = new List<string>(); var ran = new List<string>();
        var sut = Make(url => url == PawnIoInstaller.Url ? throw new HttpRequestException("blocked") : [9], fetched, ran,
                       installed: () => false);
        await sut.EnsureAsync(default);
        Assert.Equal(2, fetched.Count);
        Assert.Empty(ran);   // [9] is not the pinned file either
    }

    [Fact]
    public async Task A_failed_attempt_is_not_repeated_before_the_retry_interval()
    {
        var fetched = new List<string>(); var ran = new List<string>();
        var clock = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        var sut = Make(Mismatch, fetched, ran, installed: () => false, now: () => clock);
        await sut.EnsureAsync(default);
        var first = fetched.Count;

        clock += PawnIoInstaller.RetryInterval - TimeSpan.FromMinutes(1);
        await sut.EnsureAsync(default);
        Assert.Equal(first, fetched.Count);

        clock += TimeSpan.FromMinutes(2);
        await sut.EnsureAsync(default);
        Assert.Equal(first * 2, fetched.Count);
    }

    [Fact]
    public void The_pin_is_a_lowercase_sha256()
    {
        Assert.Matches("^[0-9a-f]{64}$", PawnIoInstaller.Sha256);
        Assert.False(PawnIoInstaller.TakeSensorReopen());
    }
}
