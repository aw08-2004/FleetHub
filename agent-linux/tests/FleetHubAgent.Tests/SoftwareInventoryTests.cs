using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.Software;

namespace FleetHubAgent.Tests;

/// <summary>
/// Installed software on Linux (0.5.0), from dpkg-query and rpm output.
///
/// **The silent failure this file exists to catch is a Software section that is wrong without
/// looking wrong.** A removed-but-not-purged package listed as installed puts software that is
/// not there on a licence audit; a failed read sent as an empty list tells the hub the box has
/// nothing installed and wipes a good inventory; and two kernels sharing one id collapse into
/// one row, so "which machines still boot the old kernel" undercounts.
/// </summary>
public class SoftwareInventoryTests
{
    private const string Dpkg =
        "openssl\t3.0.11-1~deb12u2\tDebian OpenSSL Team <pkg-openssl-devel@alioth-lists.debian.net>\tamd64\tinstall ok installed\n"
        + "libc6:i386\t2.36-9\tGNU Libc Maintainers <debian-glibc@lists.debian.org>\ti386\tinstall ok installed\n"
        + "libc6:amd64\t2.36-9\tGNU Libc Maintainers <debian-glibc@lists.debian.org>\tamd64\tinstall ok installed\n"
        + "oldtool\t1.0\tSomeone\tamd64\tdeinstall ok config-files\n"
        + "tzdata\t2024a-0\tGNU Libc Maintainers <x@y>\tall\thold ok installed\n"
        + "garbage line\n";

    [Fact]
    public void Dpkg_keeps_installed_packages_only()
    {
        var items = SoftwareReader.ParseDpkg(Dpkg, _ => "20261101");
        Assert.Equal(["openssl", "libc6", "libc6", "tzdata"], items.Select(i => i.Name));
        Assert.DoesNotContain(items, i => i.Name == "oldtool");
    }

    [Fact]
    public void Dpkg_multiarch_packages_get_distinct_ids()
    {
        var items = SoftwareReader.ParseDpkg(Dpkg, _ => "");
        Assert.Contains(items, i => i.Id == "dpkg:libc6:i386" && i.Arch == "x86");
        Assert.Contains(items, i => i.Id == "dpkg:libc6:amd64" && i.Arch == "x64");
        Assert.Equal("", items.Single(i => i.Name == "tzdata").Arch);
    }

    [Fact]
    public void Dpkg_publisher_drops_the_address()
    {
        var openssl = SoftwareReader.ParseDpkg(Dpkg, _ => "20261101").Single(i => i.Name == "openssl");
        Assert.Equal("Debian OpenSSL Team", openssl.Publisher);
        Assert.Equal("3.0.11-1~deb12u2", openssl.Version);
        Assert.Equal("20261101", openssl.InstallDate);
    }

    private const string Rpm =
        "kernel-core\t6.5.6-300.fc39\tFedora Project\tx86_64\t1700000000\t(none)\n"
        + "kernel-core\t6.6.2-201.fc39\tFedora Project\tx86_64\t1701000000\t(none)\n"
        + "gpg-pubkey\t18b8e74c-62f2920f\t(none)\t(none)\t1690000000\t(none)\n"
        + "bind-libs\t9.18.20-1.fc39\t(none)\tx86_64\t0\t32\n";

    [Fact]
    public void Rpm_keeps_every_installed_kernel()
    {
        var items = SoftwareReader.ParseRpm(Rpm);
        Assert.Equal(2, items.Count(i => i.Name == "kernel-core"));
        Assert.Equal(2, items.Where(i => i.Name == "kernel-core").Select(i => i.Id).Distinct().Count());
    }

    [Fact]
    public void Rpm_skips_signing_keys_and_reads_epoch_vendor_and_date()
    {
        var items = SoftwareReader.ParseRpm(Rpm);
        Assert.DoesNotContain(items, i => i.Name == "gpg-pubkey");
        var bind = items.Single(i => i.Name == "bind-libs");
        Assert.Equal("32:9.18.20-1.fc39", bind.Version);
        Assert.Equal("", bind.Publisher);
        Assert.Equal("", bind.InstallDate);
        Assert.Equal("20231114", items.First(i => i.Name == "kernel-core").InstallDate);
    }

    [Fact]
    public void Past_the_hubs_cap_the_report_says_it_was_truncated()
    {
        var many = Enumerable.Range(0, SoftwareReader.MaxEntries + 5)
            .Select(i => new InstalledPackage($"dpkg:p{i:D5}", $"p{i:D5}", "1", "", "", "")).ToList();
        var report = SoftwareReader.Finish(many);
        Assert.Equal(SoftwareReader.MaxEntries, report.Items.Count);
        Assert.Contains("truncated", report.Error);
    }

    // ---------------------------------------------------------------- the reporter

    private static SoftwareInventoryReporter Reporter(SoftwareReport report) =>
        new(NullLogger<SoftwareInventoryReporter>.Instance) { Read = _ => Task.FromResult(report) };

    [Fact]
    public async Task A_failed_read_sends_nothing()
    {
        var reporter = Reporter(new SoftwareReport([], "dpkg-query exited 2"));
        await reporter.RefreshIfDueAsync(CancellationToken.None);
        Assert.Null(reporter.TakeIfChanged());
    }

    [Fact]
    public async Task A_payload_stays_pending_until_marked_sent_and_then_is_not_resent()
    {
        var report = new SoftwareReport([new InstalledPackage("dpkg:x", "x", "1", "", "", "x64")], null);
        var reporter = Reporter(report);
        await reporter.RefreshIfDueAsync(CancellationToken.None);
        var payload = reporter.TakeIfChanged();
        Assert.NotNull(payload);
        Assert.Same(payload, reporter.TakeIfChanged());   // a failed heartbeat re-sends

        reporter.MarkSent(payload!);
        Assert.Null(reporter.TakeIfChanged());
        reporter.Invalidate();
        await reporter.RefreshIfDueAsync(CancellationToken.None);
        Assert.NotNull(reporter.TakeIfChanged());          // Invalidate forces a re-send
    }

    [Fact]
    public void The_payload_is_the_hubs_shape()
    {
        var payload = SoftwareInventoryReporter.ToPayload(
            new SoftwareReport([new InstalledPackage("rpm:a-1.x86_64", "a", "1", "Fedora Project", "20260101", "x64")], null));
        var row = payload["software"]![0]!;
        Assert.Equal("machine", row["scope"]!.GetValue<string>());
        Assert.Equal("x64", row["arch"]!.GetValue<string>());
        Assert.True(payload.ContainsKey("error"));
    }

    [Theory]
    [InlineData("{\"software_rejected\": true}", true)]
    [InlineData("{\"ok\": true}", false)]
    [InlineData("not json", true)]
    public void A_rejection_or_an_unreadable_reply_keeps_the_list_pending(string reply, bool rejected)
    {
        Assert.Equal(rejected, FleetClient.ReadSoftwareRejected(reply));
    }
}
