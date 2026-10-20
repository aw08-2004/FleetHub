using System.Net;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// restart, wake_machine and deploy_package on Android (0.3.0).
///
/// **Each of these can report success for something that did not happen**, which is the
/// failure these exist to catch: a restart that "worked" on a phone that cannot reboot, a
/// magic packet of zeroes that sends fine and wakes nothing, an APK installed from a file
/// nobody pinned. The platform halves are behind interfaces precisely so these rules can be
/// held here, with nothing installed but the SDK.
/// </summary>
[Collection(nameof(HubUrlTests))]
public sealed class NewExecutorTests : IDisposable
{
    public NewExecutorTests() => AgentConfig.Configure("https://hub.example.com");
    public void Dispose() => AgentConfig.Configure(AgentConfig.DefaultHubBase);

    // ------------------------------------------------------------------ restart

    [Fact]
    public async Task Restart_on_an_unmanaged_device_fails_and_says_why()
    {
        var reboot = new FakeReboot(canReboot: false);
        var result = await new RestartExecutor(NullLogger<RestartExecutor>.Instance, reboot)
            .ExecuteAsync(Cmd("restart", new JsonObject()), null, default);

        Assert.False(result.Success);
        Assert.Contains("not fully managed", result.Output);
        Assert.Equal(0, reboot.Calls);
    }

    [Fact]
    public async Task Restart_answers_before_it_reboots()
    {
        // The result has to be posted BEFORE the device goes down, or the console shows a
        // restart that worked as a command that hung.
        var reboot = new FakeReboot(canReboot: true);
        var result = await new RestartExecutor(NullLogger<RestartExecutor>.Instance, reboot)
            .ExecuteAsync(Cmd("restart", new JsonObject { ["delay_seconds"] = 0 }), null, default);

        Assert.True(result.Success);
        Assert.Equal(0, reboot.Calls);
        Assert.Contains("5s", result.Output);   // floored, so the post has time to leave
    }

    [Theory]
    [InlineData(0, 5)]
    [InlineData(30, 30)]
    [InlineData(999999, 3600)]
    public void Restart_delay_is_bounded(int asked, int expected)
        => Assert.Equal(expected,
            RestartExecutor.ResolveDelay(new JsonObject { ["delay_seconds"] = asked }));

    [Fact]
    public void Restart_without_a_delay_matches_the_windows_default()
        => Assert.Equal(60, RestartExecutor.ResolveDelay(new JsonObject()));

    // ------------------------------------------------------------------ wake

    [Fact]
    public void The_magic_packet_is_six_ff_then_the_mac_sixteen_times()
    {
        var frame = WakeMachineExecutor.MagicPacket("00-1A-2b:3C.4d5E");
        Assert.Equal(102, frame.Length);
        Assert.All(frame[..6], b => Assert.Equal(0xFF, b));
        var mac = new byte[] { 0x00, 0x1A, 0x2B, 0x3C, 0x4D, 0x5E };
        for (var i = 0; i < 16; i++)
            Assert.Equal(mac, frame[(6 + i * 6)..(12 + i * 6)]);
    }

    [Fact]
    public void A_mac_that_cannot_be_parsed_never_becomes_a_frame()
        => Assert.Throws<FormatException>(() => WakeMachineExecutor.MagicPacket("00:1A:2B"));

    [Theory]
    [InlineData("""{"macs":[],"broadcast":"10.0.0.255"}""", "no MAC")]
    [InlineData("""{"macs":["00:1a:2b:3c:4d:5e"],"broadcast":"nope"}""", "broadcast")]
    [InlineData("""{"macs":["00:1a:2b:3c:4d:5e"],"broadcast":"10.0.0.255","port":0}""", "port")]
    public async Task Wake_refuses_params_that_could_only_send_nothing(string json, string why)
    {
        var result = await new WakeMachineExecutor(NullLogger<WakeMachineExecutor>.Instance)
            .ExecuteAsync(Cmd("wake_machine", JsonNode.Parse(json)!), null, default);
        Assert.False(result.Success);
        Assert.Contains(why, result.Output);
    }

    [Fact]
    public async Task Wake_sends_to_loopback_and_reports_success()
    {
        // 127.255.255.255 keeps the frame on this machine; what is under test is that a
        // well-formed request goes out and is called a success.
        var json = JsonNode.Parse(
            """{"macs":["00:1a:2b:3c:4d:5e"],"broadcast":"127.0.0.1","port":40009,"target":"PC-1"}""")!;
        var result = await new WakeMachineExecutor(NullLogger<WakeMachineExecutor>.Instance)
            .ExecuteAsync(Cmd("wake_machine", json), null, default);
        Assert.True(result.Success, result.Output);
        Assert.Contains("woke PC-1", result.Output);
    }

    // ------------------------------------------------------------------ deploy

    [Fact]
    public void A_windows_step_is_refused_before_anything_is_downloaded()
    {
        var why = DeployPackageExecutor.Refuse(
            [new JsonObject { ["kind"] = "run", ["command"] = "msiexec" }], null);
        Assert.NotNull(why);
        Assert.Contains("cannot run on Android", why);
    }

    [Fact]
    public void A_windows_detection_rule_is_refused()
        => Assert.Contains("Windows check", DeployPackageExecutor.Refuse(
            [new JsonObject { ["kind"] = "apk", ["apk"] = "{payload}" }],
            new JsonObject { ["kind"] = "registry_value" }));

    [Fact]
    public void A_package_with_no_steps_is_refused()
        => Assert.NotNull(DeployPackageExecutor.Refuse([], null));

    [Theory]
    [InlineData("{payload}", "/tmp/a.apk")]
    [InlineData(" {payload} ", "/tmp/a.apk")]
    [InlineData("{other}", null)]
    [InlineData("payload", null)]
    [InlineData("{payload}.apk", null)]
    public void An_apk_step_names_exactly_one_payload(string reference, string? expected)
        => Assert.Equal(expected, DeployPackageExecutor.Resolve(reference,
            new Dictionary<string, string> { ["payload"] = "/tmp/a.apk" }));

    [Fact]
    public async Task An_unmanaged_device_installs_nothing()
    {
        var installer = new FakeInstaller(canInstall: false);
        var downloader = new FakeDownloader([1, 2, 3]);
        var result = await Deploy(installer, downloader).ExecuteAsync(DeployCmd(Sha([1, 2, 3])),
            null, default);

        Assert.False(result.Success);
        Assert.Contains("not fully managed", result.Output);
        Assert.Equal(0, downloader.Calls);
    }

    [Fact]
    public async Task A_pinned_payload_is_verified_and_installed()
    {
        var installer = new FakeInstaller(canInstall: true);
        var result = await Deploy(installer, new FakeDownloader([1, 2, 3]))
            .ExecuteAsync(DeployCmd(Sha([1, 2, 3])), null, default);

        Assert.True(result.Success, result.Output);
        Assert.Single(installer.Installed);
        Assert.Contains("com.example.app 1.2 installed", result.Output);
    }

    [Fact]
    public async Task A_payload_that_does_not_match_its_pin_is_never_installed()
    {
        var installer = new FakeInstaller(canInstall: true);
        var result = await Deploy(installer, new FakeDownloader([9, 9, 9]))
            .ExecuteAsync(DeployCmd(Sha([1, 2, 3])), null, default);

        Assert.False(result.Success);
        Assert.Contains("sha256 mismatch", result.Output);
        Assert.Empty(installer.Installed);
    }

    [Fact]
    public async Task An_unpinned_payload_is_refused_on_a_phone()
    {
        // The Windows agent installs an unpinned URL payload; a device owner installing
        // silently must not -- see DeployPackageExecutor's phase 1.
        var installer = new FakeInstaller(canInstall: true);
        var result = await Deploy(installer, new FakeDownloader([1, 2, 3]))
            .ExecuteAsync(DeployCmd(sha: ""), null, default);

        Assert.False(result.Success);
        Assert.Contains("sha256 pin", result.Output);
        Assert.Empty(installer.Installed);
    }

    [Fact]
    public async Task A_refused_install_is_a_failed_deploy()
    {
        var installer = new FakeInstaller(canInstall: true) { Refusal = "INSTALL_FAILED_VERSION_DOWNGRADE" };
        var result = await Deploy(installer, new FakeDownloader([1, 2, 3]))
            .ExecuteAsync(DeployCmd(Sha([1, 2, 3])), null, default);

        Assert.False(result.Success);
        Assert.Contains("INSTALL_FAILED_VERSION_DOWNGRADE", result.Output);
    }

    [Theory]
    [InlineData("https://hub.example.com/api/agent/packages/abc", true)]
    [InlineData("https://cdn.vendor.example/app.apk", false)]
    [InlineData("http://hub.example.com/api/agent/packages/abc", false)]
    public void The_bearer_only_goes_to_the_hub(string url, bool isHub)
        => Assert.Equal(isHub, HubPayloadDownloader.IsHub(new Uri(url)));

    [Fact]
    public void A_relative_download_url_resolves_against_the_hub()
        => Assert.Equal("https://hub.example.com/api/agent/packages/abc",
            HubPayloadDownloader.ResolveUrl("/api/agent/packages/abc")?.ToString());

    [Fact]
    public async Task The_downloader_signs_a_hub_request_and_not_a_cdn_one()
    {
        var handler = new AuthRecorder();
        using var downloader = new HubPayloadDownloader(() => "agent-1:tok", handler);
        var dir = Directory.CreateTempSubdirectory();
        try
        {
            await downloader.DownloadAsync("/api/agent/packages/abc", Path.Combine(dir.FullName, "a"), default);
            Assert.Equal("agent-1:tok", handler.Last);
            await downloader.DownloadAsync("https://cdn.vendor.example/x.apk", Path.Combine(dir.FullName, "b"), default);
            Assert.Null(handler.Last);
        }
        finally { dir.Delete(recursive: true); }
    }

    // ------------------------------------------------------------------ helpers

    private static FleetCommand Cmd(string type, JsonNode parameters)
        => new() { Id = "c1", Type = type, Params = parameters };

    private static string Sha(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();

    private static FleetCommand DeployCmd(string sha) => Cmd("deploy_package", new JsonObject
    {
        ["package_name"] = "Example",
        ["sources"] = new JsonArray(new JsonObject
        {
            ["name"] = "payload", ["kind"] = "upload", ["sha256"] = sha,
            ["file_name"] = "../../evil.apk", ["download_url"] = "/api/agent/packages/x",
        }),
        ["steps"] = new JsonArray(new JsonObject { ["kind"] = "apk", ["apk"] = "{payload}" }),
        ["detection"] = new JsonObject { ["kind"] = "none" },
        ["success_exit_codes"] = new JsonArray(0),
    });

    private static DeployPackageExecutor Deploy(IAppInstaller installer, IPayloadDownloader downloader)
    {
        AgentConfig.ConfigureStaging(Path.Combine(Path.GetTempPath(), "fleethub-tests-" + Guid.NewGuid().ToString("N")));
        return new DeployPackageExecutor(NullLogger<DeployPackageExecutor>.Instance, installer, downloader);
    }

    private sealed class FakeReboot(bool canReboot) : IDeviceReboot
    {
        public int Calls;
        public bool CanReboot => canReboot;
        public string? Reboot() { Interlocked.Increment(ref Calls); return null; }
    }

    private sealed class FakeInstaller(bool canInstall) : IAppInstaller
    {
        public string? Refusal { get; init; }
        public List<string> Installed { get; } = [];
        public bool CanInstallSilently => canInstall;

        public Task<AppInstallResult> InstallAsync(string path, TimeSpan timeout, CancellationToken ct)
        {
            // The params named the file "../../evil.apk"; only its last segment may survive.
            Assert.DoesNotContain("..", path);
            Assert.EndsWith("-evil.apk", path);
            if (Refusal is not null) return Task.FromResult(AppInstallResult.Refused(Refusal));
            Installed.Add(path);
            return Task.FromResult(new AppInstallResult(true, "com.example.app", "1.2", null));
        }
    }

    private sealed class FakeDownloader(byte[] bytes) : IPayloadDownloader
    {
        public int Calls;
        public async Task<string?> DownloadAsync(string url, string destination, CancellationToken ct)
        {
            Calls++;
            await File.WriteAllBytesAsync(destination, bytes, ct);
            return null;
        }
    }

    private sealed class AuthRecorder : HttpMessageHandler
    {
        public string? Last { get; private set; }
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Last = request.Headers.Authorization?.Parameter;
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new ByteArrayContent([1]),
            });
        }
    }
}
