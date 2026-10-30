using System.Text;
using TempMonitorAgent.GroupPolicy;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Roadmap #32, phase 2: the local GPO's files -- Registry.pol and gpt.ini.
///
/// The silent failures this file exists to catch:
///   * an entry somebody wrote in gpedit.msc coming back out of a FleetHub save changed -- a
///     <c>**del.</c> instruction, a binary or QWORD value, anything this code does not model --
///     which rewrites the PC owner's own policy with nobody the wiser;
///   * a damaged file "salvaged" and written back without whatever followed the damage;
///   * a save Windows never acts on, because the GPO version did not move or the registry
///     extension is not listed in gpt.ini;
///   * a FleetHub save clobbering a gpedit save that landed in between.
/// </summary>
public class LocalGroupPolicyTests
{
    private const string Edge = @"SOFTWARE\Policies\Microsoft\Edge";

    /// <summary>One entry encoded by hand, independently of RegistryPolFile, exactly as the
    /// format describes it -- so the round trip is checked against the format, not against
    /// the class's own encoder.</summary>
    private static byte[] Entry(string key, string name, uint type, byte[] data)
    {
        var bytes = new List<byte>();
        void Char(char c) => bytes.AddRange([(byte)c, 0]);
        void Dword(uint v) => bytes.AddRange(BitConverter.GetBytes(v));
        Char('[');
        bytes.AddRange(Encoding.Unicode.GetBytes(key + "\0"));
        Char(';');
        bytes.AddRange(Encoding.Unicode.GetBytes(name + "\0"));
        Char(';');
        Dword(type);
        Char(';');
        Dword((uint)data.Length);
        Char(';');
        bytes.AddRange(data);
        Char(']');
        return [.. bytes];
    }

    private static byte[] File(params byte[][] entries) =>
        [.. "PReg"u8.ToArray(), 1, 0, 0, 0, .. entries.SelectMany(e => e)];

    private static byte[] Sz(string text) => Encoding.Unicode.GetBytes(text + "\0");

    /// <summary>What gpedit.msc leaves behind for a handful of ordinary settings.</summary>
    private static readonly byte[][] Gpedit =
    [
        Entry(@"Software\Policies\Microsoft\Windows\System", "EnableSmartScreen", 4, BitConverter.GetBytes(1u)),
        Entry(@"Software\Policies\Microsoft\Edge", "**delvals.", 1, Sz(" ")),
        Entry(@"Software\Policies\Microsoft\Edge\ExtensionInstallBlocklist", "1", 1, Sz("*")),
        Entry(@"Software\Policies\Microsoft\Windows\WindowsUpdate", "**del.WUServer", 1, Sz(" ")),
        Entry(@"Software\Policies\Vendor", "Blob", 3, [0xDE, 0xAD, 0xBE, 0xEF]),
        Entry(@"Software\Policies\Vendor", "Big", 11, BitConverter.GetBytes(0x1_0000_0001UL)),
    ];

    private static PolicyValue Dword(string path, string name, uint value) => new()
    {
        Path = path, Name = name, Type = PolicyAllowList.Dword, Dword = value,
    };

    // --- Registry.pol -------------------------------------------------------

    [Fact]
    public void A_gpedit_file_round_trips_byte_for_byte()
    {
        var original = File(Gpedit);
        Assert.Equal(original, RegistryPolFile.Parse(original).ToBytes());
    }

    [Fact]
    public void Adding_a_value_leaves_every_existing_entry_exactly_as_it_was()
    {
        var original = File(Gpedit);
        var file = RegistryPolFile.Parse(original);
        file.Set(Dword(Edge, "HideFirstRunExperience", 1));
        var written = file.ToBytes();
        Assert.True(file.Changed);
        Assert.Equal(original, written[..original.Length]);
        Assert.Equal(Entry(Edge, "HideFirstRunExperience", 4, BitConverter.GetBytes(1u)),
                     written[original.Length..]);
    }

    [Fact]
    public void Values_are_encoded_as_the_registry_holds_them()
    {
        var file = RegistryPolFile.Empty();
        file.Set(new PolicyValue { Path = Edge, Name = "S", Type = PolicyAllowList.String, Text = "abc" });
        file.Set(new PolicyValue { Path = Edge, Name = "E", Type = PolicyAllowList.ExpandString, Text = "%windir%" });
        file.Set(new PolicyValue { Path = Edge, Name = "M", Type = PolicyAllowList.MultiString, Lines = ["a", "b"] });
        Assert.Equal(File(Entry(Edge, "S", 1, Sz("abc")), Entry(Edge, "E", 2, Sz("%windir%")),
                          Entry(Edge, "M", 7, Sz("a\0b\0"))),
                     file.ToBytes());
        var parsed = RegistryPolFile.Parse(file.ToBytes());
        Assert.Equal("string:abc", parsed.Get(Edge, "s"));
        Assert.Equal("expand_string:%windir%", parsed.Get(Edge, "E"));
        Assert.Equal("multi_string:a\0b", parsed.Get(Edge, "M"));
    }

    [Fact]
    public void Get_reads_instructions_and_unmodelled_types_as_somebody_elses()
    {
        var file = RegistryPolFile.Parse(File(Gpedit));
        Assert.Equal("dword:1", file.Get(@"SOFTWARE\POLICIES\Microsoft\Windows\System", "enablesmartscreen"));
        Assert.Equal("delete:", file.Get(@"Software\Policies\Microsoft\Windows\WindowsUpdate", "WUServer"));
        Assert.Equal("other:delvals", file.Get(Edge, "Anything"));
        Assert.Equal("other:3", file.Get(@"Software\Policies\Vendor", "Blob"));
        Assert.Equal("other:11", file.Get(@"Software\Policies\Vendor", "Big"));
        Assert.Null(file.Get(@"Software\Policies\Vendor", "Absent"));
    }

    [Fact]
    public void Set_replaces_in_place_and_drops_whatever_else_the_file_said_about_the_value()
    {
        var file = RegistryPolFile.Parse(File(
            Entry(Edge, "A", 4, BitConverter.GetBytes(1u)),
            Entry(Edge, "B", 4, BitConverter.GetBytes(1u)),
            Entry(Edge, "a", 4, BitConverter.GetBytes(3u)),
            Entry(Edge, "**del.A", 1, Sz(" ")),
            Entry(Edge, "**soft.A", 4, BitConverter.GetBytes(5u))));
        file.Set(Dword(Edge, "A", 2));
        Assert.Equal(File(
            Entry(Edge, "A", 4, BitConverter.GetBytes(2u)),
            Entry(Edge, "B", 4, BitConverter.GetBytes(1u)),
            Entry(Edge, "**soft.A", 4, BitConverter.GetBytes(5u))), file.ToBytes());
        file.Delete(Edge, "A");
        Assert.Equal(2, file.Count);
        Assert.Equal("other:soft", file.Get(Edge, "A"));
    }

    [Fact]
    public void A_delete_is_written_as_gpedit_writes_it_and_swaps_with_the_value()
    {
        var file = RegistryPolFile.Parse(File(
            Entry(Edge, "A", 4, BitConverter.GetBytes(1u)),
            Entry(Edge, "B", 4, BitConverter.GetBytes(1u))));
        file.Set(new PolicyValue { Path = Edge, Name = "A", Type = PolicyAllowList.Delete });
        Assert.Equal(File(
            Entry(Edge, "**del.A", 1, Sz(" ")),
            Entry(Edge, "B", 4, BitConverter.GetBytes(1u))), file.ToBytes());
        Assert.Equal("delete:", file.Get(Edge, "A"));
        file.Set(Dword(Edge, "A", 1));
        Assert.Equal("dword:1", file.Get(Edge, "A"));
        Assert.Equal(2, file.Count);
        file.Delete(Edge, "A");
        Assert.Null(file.Get(Edge, "A"));
    }

    [Fact]
    public void Setting_the_value_it_already_holds_is_not_a_change()
    {
        var file = RegistryPolFile.Parse(File(Entry(Edge, "A", 4, BitConverter.GetBytes(1u))));
        file.Set(Dword(Edge, "A", 1));
        Assert.False(file.Changed);
    }

    [Fact]
    public void An_empty_file_is_an_empty_policy()
    {
        var file = RegistryPolFile.Parse([]);
        Assert.Equal(0, file.Count);
        Assert.NotNull(file.LoadedHash);
    }

    [Theory]
    [InlineData("not a policy file")]
    [InlineData("truncated")]
    [InlineData("data past the end")]
    [InlineData("missing bracket")]
    [InlineData("version 2")]
    public void A_damaged_file_is_refused_rather_than_salvaged(string damage)
    {
        var good = File(Gpedit);
        var bad = damage switch
        {
            "not a policy file" => Encoding.ASCII.GetBytes("Windows Registry Editor Version 5.00"),
            "truncated" => good[..^3],
            "data past the end" => File(Entry(Edge, "A", 4, BitConverter.GetBytes(1u)))[..^6],
            "missing bracket" => [.. good, (byte)'x', 0],
            _ => [.. "PReg"u8.ToArray(), 2, 0, 0, 0],
        };
        Assert.Throws<InvalidDataException>(() => RegistryPolFile.Parse(bad));
    }

    // --- gpt.ini ------------------------------------------------------------

    [Fact]
    public void A_missing_gpt_ini_is_written_as_gpedit_would_for_one_setting()
    {
        Assert.Equal("[General]\r\ngPCMachineExtensionNames=" + LocalGroupPolicy.RegistryExtension
                     + "\r\nVersion=1\r\n", GptIni.Bump(null));
    }

    [Fact]
    public void Only_the_machine_half_of_the_version_moves_and_it_never_wraps_to_zero()
    {
        Assert.Equal(0x0002_0004u, GptIni.NextMachineVersion(0x0002_0003));
        Assert.Equal(0x0001_0001u, GptIni.NextMachineVersion(0x0001_FFFF));
    }

    [Fact]
    public void The_registry_extension_is_added_in_order_and_the_rest_of_the_file_is_kept()
    {
        const string security =
            "[{827D319E-6EAC-11D2-A4EA-00C04F79F83A}{803E14A0-B4FB-11D0-A0D0-00A0C90F574B}]";
        var ini = "[General]\r\ngPCMachineExtensionNames=" + security + "\r\nVersion=131075\r\n"
                  + "gPCUserExtensionNames=[{A}]\r\n[Other]\r\nVersion=7\r\n";
        Assert.Equal("[General]\r\ngPCMachineExtensionNames=" + LocalGroupPolicy.RegistryExtension + security
                     + "\r\nVersion=131076\r\ngPCUserExtensionNames=[{A}]\r\n[Other]\r\nVersion=7\r\n",
                     GptIni.Bump(ini));
    }

    [Fact]
    public void A_listed_registry_extension_is_left_exactly_as_written()
    {
        var listed = LocalGroupPolicy.RegistryExtension.ToLowerInvariant();
        Assert.Equal(listed, GptIni.WithRegistryExtension(listed));
    }

    // --- the store ----------------------------------------------------------

    [Fact]
    public void A_save_writes_both_files_and_a_gpedit_save_in_between_wins()
    {
        var root = Path.Combine(Path.GetTempPath(), "fleethub-lgpo-" + Guid.NewGuid().ToString("N"));
        try
        {
            var refreshes = new List<bool>();
            var local = new LocalGroupPolicy(root, refreshes.Add);
            var file = local.Load();
            file.Set(Dword(Edge, "A", 1));
            Assert.True(local.Save(file));
            Assert.Equal("dword:1", local.Load().Get(Edge, "A"));
            Assert.Contains("Version=1", System.IO.File.ReadAllText(local.IniPath));

            var stale = local.Load();
            var gpedit = local.Load();
            gpedit.Set(Dword(Edge, "B", 1));
            Assert.True(local.Save(gpedit));
            stale.Set(Dword(Edge, "C", 1));
            Assert.False(local.Save(stale));
            Assert.Equal("dword:1", local.Load().Get(Edge, "B"));
            Assert.Null(local.Load().Get(Edge, "C"));
            Assert.Contains("Version=2", System.IO.File.ReadAllText(local.IniPath));

            local.Refresh(true);
            Assert.Equal([true], refreshes);
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, recursive: true);
        }
    }
}
