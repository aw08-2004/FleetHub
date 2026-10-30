using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;

namespace TempMonitorAgent.GroupPolicy;

/// <summary>
/// One <c>Registry.pol</c> file -- the registry half of a Group Policy object, in the PReg
/// version 1 format gpedit.msc reads and writes (roadmap #32, phase 2). A header
/// (<c>PReg</c>, version 1) and then entries <c>[key;value;type;size;data]</c>: the
/// brackets and semicolons are UTF-16LE characters, key and value are NUL-terminated UTF-16LE,
/// type and size are little-endian DWORDs and data is <c>size</c> raw bytes.
///
/// <para><b>Every entry FleetHub did not write goes back out byte for byte.</b> The local GPO
/// is shared with whoever runs gpedit.msc on the PC, and gpedit writes things this class has
/// no reason to understand -- <c>**del.</c> and <c>**delvals.</c> instructions, binary and
/// QWORD values, entries for keys FleetHub never touches. Each parsed entry keeps the exact
/// bytes it was read from, and only an entry this class itself replaced is re-encoded. A
/// re-encoder that normalised what it read would quietly rewrite the owner's own policy.</para>
///
/// <para><b>A file that does not parse is refused, never rewritten.</b> <see cref="Parse"/>
/// throws rather than salvaging what it could read: writing back a salvaged file would drop
/// whatever came after the damage, which is somebody's policy.</para>
/// </summary>
public sealed class RegistryPolFile
{
    public const uint RegSz = 1;
    public const uint RegExpandSz = 2;
    public const uint RegDword = 4;
    public const uint RegMultiSz = 7;

    private static readonly byte[] Header = [(byte)'P', (byte)'R', (byte)'e', (byte)'g', 1, 0, 0, 0];
    private const char Open = '[';
    private const char Separator = ';';
    private const char Close = ']';

    /// <summary>"Delete this value": the entry gpedit writes, REG_SZ holding one space.</summary>
    private const string DeletePrefix = "**del.";
    private static readonly byte[] DeleteData = Encoding.Unicode.GetBytes(" \0");
    private const string DeleteCanonical = PolicyAllowList.Delete + ":";

    /// <summary>Type for an entry that ends after its value name -- a key-only entry, which
    /// has no type at all.</summary>
    private const uint NoType = uint.MaxValue;

    /// <summary>One entry. <c>Raw</c> is the exact bytes it was read from, or null for an
    /// entry this class created, which <see cref="Encode"/> then writes.</summary>
    private sealed record Entry(string Key, string Name, uint Type, byte[] Data, byte[]? Raw);

    private readonly List<Entry> _entries;

    private RegistryPolFile(List<Entry> entries, string? loadedHash)
    {
        _entries = entries;
        LoadedHash = loadedHash;
    }

    /// <summary>SHA-256 of the bytes this was parsed from, or null when there was no file --
    /// what <see cref="LocalGroupPolicy.Save"/> compares against to notice that somebody
    /// else wrote the file in between.</summary>
    public string? LoadedHash { get; }

    /// <summary>Whether <see cref="Set"/> or <see cref="Delete"/> changed anything, so a pass
    /// that changed nothing writes nothing and does not bump the GPO version.</summary>
    public bool Changed { get; private set; }

    public int Count => _entries.Count;

    public static RegistryPolFile Empty() => new([], null);

    /// <summary>Parse a file's bytes. An empty file is an empty policy (gpedit leaves one
    /// behind when the last setting is cleared). Throws <see cref="InvalidDataException"/>
    /// on anything else that is not exactly the format.</summary>
    public static RegistryPolFile Parse(byte[] bytes)
    {
        var hash = Convert.ToHexStringLower(SHA256.HashData(bytes));
        if (bytes.Length == 0) return new([], hash);
        if (bytes.Length < Header.Length || !bytes.AsSpan(0, Header.Length).SequenceEqual(Header))
            throw new InvalidDataException("not a version 1 Registry.pol file");
        var entries = new List<Entry>();
        var at = Header.Length;
        while (at < bytes.Length)
        {
            var start = at;
            Expect(bytes, ref at, Open);
            var key = ReadString(bytes, ref at);
            Expect(bytes, ref at, Separator);
            var name = ReadString(bytes, ref at);
            uint type = NoType;
            var data = Array.Empty<byte>();
            if (PeekChar(bytes, at) == Separator)
            {
                at += 2;
                type = ReadDword(bytes, ref at);
                Expect(bytes, ref at, Separator);
                var size = ReadDword(bytes, ref at);
                Expect(bytes, ref at, Separator);
                if (size > bytes.Length - at)
                    throw new InvalidDataException("an entry's data runs past the end of the file");
                data = bytes.AsSpan(at, (int)size).ToArray();
                at += (int)size;
            }
            Expect(bytes, ref at, Close);
            if (key.Length == 0) throw new InvalidDataException("an entry has no key");
            entries.Add(new Entry(key, name, type, data, bytes.AsSpan(start, at - start).ToArray()));
        }
        return new(entries, hash);
    }

    public byte[] ToBytes()
    {
        using var stream = new MemoryStream();
        stream.Write(Header);
        foreach (var entry in _entries) stream.Write(entry.Raw ?? Encode(entry));
        return stream.ToArray();
    }

    /// <summary>The value this file sets, in <see cref="PolicyValue.Canonical"/> form, or null
    /// when it says nothing about it. Last entry wins, as when Windows applies the file.
    ///
    /// An instruction about the value is not "nothing". <c>**del.Name</c> (gpedit's
    /// "Disabled" for some policies) reads as <c>delete:</c>, the canonical form of a
    /// <see cref="PolicyAllowList.Delete"/> row, so a delete FleetHub wrote compares equal to
    /// the one it wants. <c>**soft.Name</c> and a <c>**delvals.</c> on the key read as
    /// <c>other:...</c>: somebody's policy decided this value, and the runner leaves it alone
    /// exactly as it would a value.</summary>
    public string? Get(string key, string name)
    {
        string? found = null;
        foreach (var entry in _entries)
        {
            if (!entry.Key.Equals(key, StringComparison.OrdinalIgnoreCase)) continue;
            if (entry.Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                found = Canonical(entry);
            else if (entry.Name.Equals("**del." + name, StringComparison.OrdinalIgnoreCase))
                found = DeleteCanonical;
            else if (entry.Name.Equals("**soft." + name, StringComparison.OrdinalIgnoreCase))
                found = "other:soft";
            else if (entry.Name.Equals("**delvals.", StringComparison.OrdinalIgnoreCase))
                found = "other:delvals";
        }
        return found;
    }

    /// <summary>Every value the file sets or deletes, in file order, as
    /// <c>(key, name, canonical)</c> -- a delete as <c>delete:</c>. What Windows' registry
    /// extension would do with it, other instructions aside. The tests use it to stand in for
    /// that extension.</summary>
    public IEnumerable<(string Key, string Name, string Value)> Values()
    {
        foreach (var e in _entries)
        {
            if (e.Type == NoType) continue;
            if (e.Name.StartsWith(DeletePrefix, StringComparison.OrdinalIgnoreCase))
                yield return (e.Key, e.Name[DeletePrefix.Length..], DeleteCanonical);
            else if (!e.Name.StartsWith("**", StringComparison.Ordinal))
                yield return (e.Key, e.Name, Canonical(e));
        }
    }

    /// <summary>Set one value -- or, for a <see cref="PolicyAllowList.Delete"/> row, say
    /// "delete it" -- replacing whatever the file said about it before: the first entry for it
    /// is replaced where it stands and any later one dropped, or a new entry goes on the end,
    /// after any <c>**delvals.</c> for the key, which would otherwise delete it. A value and a
    /// delete of it are the same thing to replace, so Enabled turning into Disabled swaps one
    /// for the other rather than leaving both.</summary>
    public void Set(PolicyValue value)
    {
        var replacement = value.Type == PolicyAllowList.Delete
            ? new Entry(value.Path, DeletePrefix + value.Name, RegSz, DeleteData, null)
            : new Entry(value.Path, value.Name, TypeOf(value), DataOf(value), null);
        var first = _entries.FindIndex(e => Matches(e, value.Path, value.Name));
        if (first < 0)
        {
            _entries.Add(replacement);
            Changed = true;
            return;
        }
        if (!SameValue(_entries[first], replacement))
        {
            _entries[first] = replacement;
            Changed = true;
        }
        for (var i = _entries.Count - 1; i > first; i--)
        {
            if (!Matches(_entries[i], value.Path, value.Name)) continue;
            _entries.RemoveAt(i);
            Changed = true;
        }
    }

    /// <summary>Remove every entry for this value, a <c>**del.</c> for it included. Other
    /// instructions (<c>**soft.</c>, <c>**delvals.</c>) are not FleetHub's to remove and
    /// stay.</summary>
    public void Delete(string key, string name)
    {
        if (_entries.RemoveAll(e => Matches(e, key, name)) > 0) Changed = true;
    }

    private static bool Matches(Entry entry, string key, string name) =>
        entry.Type != NoType
        && entry.Key.Equals(key, StringComparison.OrdinalIgnoreCase)
        && (entry.Name.Equals(name, StringComparison.OrdinalIgnoreCase)
            || entry.Name.Equals(DeletePrefix + name, StringComparison.OrdinalIgnoreCase));

    private static bool SameValue(Entry a, Entry b) =>
        a.Key == b.Key && a.Name == b.Name && a.Type == b.Type && a.Data.AsSpan().SequenceEqual(b.Data);

    // --- values -------------------------------------------------------------

    /// <summary>The same strings <see cref="RegistryValueStore.Get"/> produces for the same
    /// value in the registry, so the runner can compare the file, the registry and the
    /// document as plain strings.</summary>
    private static string Canonical(Entry entry) => entry.Type switch
    {
        RegDword when entry.Data.Length == 4 =>
            $"{PolicyAllowList.Dword}:{BinaryPrimitives.ReadUInt32LittleEndian(entry.Data)}",
        RegSz => $"{PolicyAllowList.String}:{Text(entry.Data)}",
        RegExpandSz => $"{PolicyAllowList.ExpandString}:{Text(entry.Data)}",
        RegMultiSz => $"{PolicyAllowList.MultiString}:{string.Join('\0', Lines(entry.Data))}",
        NoType => "other:key",
        var other => $"other:{other}",
    };

    /// <summary>Up to the first NUL, as the registry API reads a string.</summary>
    private static string Text(byte[] data)
    {
        var text = Encoding.Unicode.GetString(data);
        var nul = text.IndexOf('\0');
        return nul < 0 ? text : text[..nul];
    }

    private static string[] Lines(byte[] data)
    {
        var text = Encoding.Unicode.GetString(data).TrimEnd('\0');
        return text.Length == 0 ? [] : text.Split('\0');
    }

    private static uint TypeOf(PolicyValue value) => value.Type switch
    {
        PolicyAllowList.Dword => RegDword,
        PolicyAllowList.ExpandString => RegExpandSz,
        PolicyAllowList.MultiString => RegMultiSz,
        _ => RegSz,
    };

    private static byte[] DataOf(PolicyValue value)
    {
        switch (value.Type)
        {
            case PolicyAllowList.Dword:
                var dword = new byte[4];
                BinaryPrimitives.WriteUInt32LittleEndian(dword, value.Dword);
                return dword;
            case PolicyAllowList.MultiString:
                // Each line NUL-terminated, then the empty line that ends the list.
                return Encoding.Unicode.GetBytes(string.Concat(value.Lines.Select(l => l + "\0")) + "\0");
            default:
                return Encoding.Unicode.GetBytes(value.Text + "\0");
        }
    }

    // --- the encoding -------------------------------------------------------

    private static byte[] Encode(Entry entry)
    {
        using var stream = new MemoryStream();
        WriteChar(stream, Open);
        stream.Write(Encoding.Unicode.GetBytes(entry.Key + "\0"));
        WriteChar(stream, Separator);
        stream.Write(Encoding.Unicode.GetBytes(entry.Name + "\0"));
        WriteChar(stream, Separator);
        WriteDword(stream, entry.Type);
        WriteChar(stream, Separator);
        WriteDword(stream, (uint)entry.Data.Length);
        WriteChar(stream, Separator);
        stream.Write(entry.Data);
        WriteChar(stream, Close);
        return stream.ToArray();
    }

    private static void WriteChar(Stream stream, char c) => stream.Write([(byte)c, 0]);

    private static void WriteDword(Stream stream, uint value)
    {
        Span<byte> bytes = stackalloc byte[4];
        BinaryPrimitives.WriteUInt32LittleEndian(bytes, value);
        stream.Write(bytes);
    }

    private static char? PeekChar(byte[] bytes, int at) =>
        at + 1 < bytes.Length ? (char)BinaryPrimitives.ReadUInt16LittleEndian(bytes.AsSpan(at)) : null;

    private static void Expect(byte[] bytes, ref int at, char c)
    {
        if (PeekChar(bytes, at) != c)
            throw new InvalidDataException($"expected '{c}' at byte {at}");
        at += 2;
    }

    private static uint ReadDword(byte[] bytes, ref int at)
    {
        if (at + 4 > bytes.Length) throw new InvalidDataException("the file ends inside an entry");
        var value = BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(at));
        at += 4;
        return value;
    }

    /// <summary>A NUL-terminated UTF-16LE string; <paramref name="at"/> ends past the NUL.</summary>
    private static string ReadString(byte[] bytes, ref int at)
    {
        var start = at;
        while (true)
        {
            var c = PeekChar(bytes, at) ?? throw new InvalidDataException("the file ends inside a name");
            at += 2;
            if (c == '\0') return Encoding.Unicode.GetString(bytes, start, at - start - 2);
        }
    }
}
