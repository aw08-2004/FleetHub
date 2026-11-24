using System.Globalization;
using System.Management;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Win32;
using TempMonitorAgent.Telemetry;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>Three-valued: a condition can be true, false, or something this machine could
/// not answer. Kept as its own type rather than bool? so a missing case in a switch is a
/// compile error, not a null that reads as false.</summary>
public enum Tri { False, True, Unknown }

/// <summary>
/// What a package condition can ask about this machine (agent 3.54.0, roadmap #5). The
/// hub's catalog is hub/package_conditions.py STATIC_VARIABLES; every name there is read
/// here under the same spelling, and a name this build does not know reads null -- UNKNOWN
/// -- which fails an If closed rather than open.
/// </summary>
public interface IMachineFacts
{
    /// <summary>The value as text, or null when it is unknown on this machine.</summary>
    string? Get(string name);
}

/// <summary>
/// The live answers, read when a deploy asks and cached for that deploy only.
///
/// **Live, not the startup <see cref="SystemInfo"/> snapshot.** That snapshot is what the hub
/// already has; a condition exists to match the hardware in the chassis NOW, after a
/// motherboard swap or a re-image the agent has not restarted since. Read lazily, per fact:
/// a package that only asks for the model costs one WMI round trip, not twelve.
///
/// `hw.model_name` is Win32_ComputerSystemProduct.Version, which is where Lenovo keeps the
/// name an operator thinks in ("ThinkPad X1 Carbon Gen 11") -- its Model is the machine-type
/// code. Dell and HP leave Version empty or put a placeholder there, which reads unknown.
/// </summary>
public sealed class WindowsMachineFacts(IReadOnlyDictionary<string, string?>? hubFacts = null)
    : IMachineFacts
{
    private readonly Dictionary<string, string?> _cache = new(StringComparer.Ordinal);
    private readonly Lock _gate = new();

    private const string ComputerSystem = "Win32_ComputerSystem";
    private const string OperatingSystem = "Win32_OperatingSystem";

    /// <summary>Values an OEM ships in unset SMBIOS strings. Same idea as SystemInfo's
    /// placeholder list -- a "System Version" model name identifies nothing.</summary>
    private static readonly string[] Placeholders =
        { "default string", "to be filled", "system version", "system product name",
          "not applicable", "none", "invalid", "o.e.m." };

    public string? Get(string name)
    {
        if (name.StartsWith("field.", StringComparison.Ordinal)
            || name.StartsWith("ad.", StringComparison.Ordinal))
        {
            // Only the hub knows these; it resolved them at dispatch (params.facts).
            return hubFacts is not null && hubFacts.TryGetValue(name, out var v) ? Clean(v) : null;
        }
        lock (_gate)
        {
            if (_cache.TryGetValue(name, out var cached)) return cached;
            string? value;
            try { value = Clean(Read(name)); }
            catch { value = null; }   // a WMI class that throws is an unknown, not a crash
            _cache[name] = value;
            return value;
        }
    }

    private static string? Read(string name) => name switch
    {
        "hw.manufacturer" => RealManufacturer(Wmi(ComputerSystem, "Manufacturer")),
        "hw.model" => Wmi(ComputerSystem, "Model"),
        "hw.model_name" => NotPlaceholder(Wmi("Win32_ComputerSystemProduct", "Version")),
        "hw.sku" => NotPlaceholder(Wmi(ComputerSystem, "SystemSKUNumber")),
        "hw.serial_number" => NotPlaceholder(Wmi("Win32_BIOS", "SerialNumber")),
        "hw.chassis" => Chassis(),
        "hw.memory_gb" => MemoryGb(),
        "hw.cpu" => Wmi("Win32_Processor", "Name"),
        "os.name" => Wmi(OperatingSystem, "Caption"),
        "os.version" => Wmi(OperatingSystem, "Version"),
        "os.build" => Wmi(OperatingSystem, "BuildNumber"),
        "os.display_version" => DisplayVersion(),
        "os.arch" => Arch(),
        "bios.version" => Wmi("Win32_BIOS", "SMBIOSBIOSVersion"),
        "sys.hostname" => Environment.MachineName,
        "sys.domain" => Wmi(ComputerSystem, "Domain"),
        "sys.domain_joined" => BoolWord(Wmi(ComputerSystem, "PartOfDomain")),
        "sys.agent_version" => AgentConfig.Version,
        _ => null,
    };

    private static string? RealManufacturer(string? value) =>
        value is not null && SystemInfo.IsRealManufacturer(value) ? value : null;

    /// <summary>"True"/"False" from WMI as the lowercase words a condition compares.</summary>
    private static string? BoolWord(string? value)
    {
        if (!bool.TryParse(value, out var b)) return null;
        return b ? "true" : "false";
    }

    /// <summary>The first instance of a WMI class, or null. Every class read here has one
    /// instance per machine.</summary>
    private static ManagementObject? FirstInstance(string query)
    {
        using var searcher = new ManagementObjectSearcher(query);
        using var results = searcher.Get();
        return results.OfType<ManagementObject>().FirstOrDefault();
    }

    private static string? Wmi(string wmiClass, string property)
    {
        using var obj = FirstInstance($"SELECT {property} FROM {wmiClass}");
        return obj?[property]?.ToString();
    }

    private static string? NotPlaceholder(string? value)
    {
        var text = (value ?? "").Trim();
        if (text.Length == 0) return null;
        var lowered = text.ToLowerInvariant();
        return Placeholders.Any(p => lowered == p || lowered.StartsWith(p + " ", StringComparison.Ordinal))
            ? null : text;
    }

    /// <summary>SMBIOS chassis types folded to the five words a condition offers. The table
    /// is DMTF's; anything not listed (docking station, expansion chassis ...) is "other".</summary>
    internal static string ChassisWord(int type) => type switch
    {
        8 or 9 or 10 or 14 or 31 or 32 => "laptop",
        11 or 30 => "tablet",
        3 or 4 or 5 or 6 or 7 or 13 or 15 or 16 or 24 or 35 or 36 => "desktop",
        17 or 23 or 25 or 28 => "server",
        _ => "other",
    };

    private static string? Chassis()
    {
        using var obj = FirstInstance("SELECT ChassisTypes FROM Win32_SystemEnclosure");
        return obj?["ChassisTypes"] is ushort[] { Length: > 0 } types ? ChassisWord(types[0]) : null;
    }

    private static string? MemoryGb()
    {
        // Rounded to the nearest whole GB: a "16 GB" machine reports 15.7 after the firmware
        // reserves its share, and `hw.memory_gb >= 16` has to mean what the operator meant.
        var raw = Wmi(ComputerSystem, "TotalPhysicalMemory");
        return ulong.TryParse(raw, out var bytes)
            ? Math.Round(bytes / (1024.0 * 1024 * 1024)).ToString(CultureInfo.InvariantCulture)
            : null;
    }

    private static string? DisplayVersion()
    {
        using var key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion");
        return (key?.GetValue("DisplayVersion") ?? key?.GetValue("ReleaseId"))?.ToString();
    }

    private static string? Arch() => RuntimeInformation.OSArchitecture switch
    {
        Architecture.X64 => "x64",
        Architecture.X86 => "x86",
        Architecture.Arm64 => "arm64",
        _ => null,
    };

    private static string? Clean(string? value)
    {
        var text = (value ?? "").Trim();
        return text.Length == 0 ? null : text;
    }
}

/// <summary>
/// Evaluates a package condition -- the rules engine's AST, hub/rules.py, plus the probes in
/// hub/package_conditions.py -- against this machine.
///
/// **The semantics are the hub's, copied deliberately and pinned by tests on both sides**
/// (PackageConditionTests here, tests/test_package_conditions.py there): case-insensitive
/// text equality, wildcard-not-regex `matches`, Kleene and/or/not, and UNKNOWN for anything
/// that could not be read. A model that compares differently here than in the console's
/// preview is the bug this class most needs not to have.
///
/// One deliberate difference: `and` stops at the first FALSE and `or` at the first TRUE.
/// The result is the same under Kleene logic, and it means a PowerShell probe later in an
/// `and` never runs on a machine an earlier clause already ruled out.
/// </summary>
public sealed partial class ConditionEvaluator(
    IMachineFacts facts, PackageVariables vars, Func<int?> lastExitCode,
    Action<string> say, Action<string> emit, CancellationToken ct)
{
    /// <summary>The kind of each variable, as the hub's catalog declares it. A name absent
    /// here (a custom field) takes its kind from the value the hub sent.</summary>
    private const string KindNumber = "number";
    private const string KindBool = "bool";
    private const string KindVersion = "version";
    private const string KindText = "text";

    private static readonly Dictionary<string, string> Kinds = new(StringComparer.Ordinal)
    {
        ["hw.memory_gb"] = KindNumber,
        ["os.build"] = KindNumber,
        ["step.last_exit_code"] = KindNumber,
        ["sys.domain_joined"] = KindBool,
        ["os.version"] = KindVersion,
        ["sys.agent_version"] = KindVersion,
    };

    /// <summary>Short "what was looked at" notes, for the If's message in the console.</summary>
    public List<string> Notes { get; } = new();

    public async Task<Tri> EvaluateAsync(JsonNode? node)
    {
        if (node is not JsonObject obj) return Tri.Unknown;
        var op = obj.GetString("op");
        var children = obj["nodes"] as JsonArray;
        return op switch
        {
            "and" or "or" => await GroupAsync(op, children),
            "not" => children is { Count: 1 } ? Not(await EvaluateAsync(children[0])) : Tri.Unknown,
            null when obj.GetString("probe") is { } probe => await ProbeAsync(probe, obj),
            null => Leaf(obj),
            _ => Tri.Unknown,
        };
    }

    /// <summary>Kleene and/or, stopping at the first decisive answer: FALSE settles an and,
    /// TRUE settles an or.</summary>
    private async Task<Tri> GroupAsync(string op, JsonArray? children)
    {
        if (children is null || children.Count == 0) return Tri.Unknown;
        var decisive = op == "or" ? Tri.True : Tri.False;
        var sawUnknown = false;
        foreach (var child in children)
        {
            var r = await EvaluateAsync(child);
            if (r == decisive) return decisive;
            sawUnknown |= r == Tri.Unknown;
        }
        if (sawUnknown) return Tri.Unknown;
        return decisive == Tri.True ? Tri.False : Tri.True;
    }

    // ---------------------------------------------------------------- variables

    private Tri Leaf(JsonObject leaf)
    {
        var name = leaf.GetString("var") ?? "";
        var cmp = leaf.GetString("cmp") ?? "";
        var value = name == "step.last_exit_code"
            ? lastExitCode()?.ToString(CultureInfo.InvariantCulture)
            : facts.Get(name);
        var result = Compare(Kind(name, leaf["value"]), cmp, value, leaf["value"]);
        Notes.Add($"{name} = {(value is null ? "unknown" : $"'{value}'")}");
        return result;
    }

    private static string Kind(string name, JsonNode? right)
    {
        if (Kinds.TryGetValue(name, out var kind)) return kind;
        // A custom field: the hub coerced the literal to the field's kind at save time, so
        // the literal's JSON type says what the field is.
        var sample = right is JsonArray list && list.Count > 0 ? list[0] : right;
        if (sample is JsonValue v)
        {
            if (v.TryGetValue<bool>(out _)) return KindBool;
            if (v.TryGetValue<double>(out _)) return KindNumber;
        }
        return KindText;
    }

    /// <summary>One comparison. Public for the tests, which pin it against the hub's.</summary>
    public static Tri Compare(string kind, string cmp, string? left, JsonNode? right)
    {
        if (cmp == "is_known") return Of(left is not null);
        if (cmp == "is_unknown") return Of(left is null);
        if (left is null) return Tri.Unknown;
        if (cmp is "in" or "not_in") return Membership(kind, cmp, left, right);

        var wanted = Text(right);
        if (wanted is null) return Tri.Unknown;
        return cmp switch
        {
            "==" => Equal(kind, left, wanted),
            "!=" => Not(Equal(kind, left, wanted)),
            ">" or ">=" or "<" or "<=" => Ordered(kind, cmp, left, wanted),
            "contains" => Of(left.Contains(wanted, StringComparison.OrdinalIgnoreCase)),
            "not_contains" => Of(!left.Contains(wanted, StringComparison.OrdinalIgnoreCase)),
            "starts_with" => Of(left.StartsWith(wanted, StringComparison.OrdinalIgnoreCase)),
            "ends_with" => Of(left.EndsWith(wanted, StringComparison.OrdinalIgnoreCase)),
            "matches" => Of(WildcardMatch(wanted, left)),
            _ => Tri.Unknown,   // an operator this build does not know
        };
    }

    private static Tri Membership(string kind, string cmp, string left, JsonNode? right)
    {
        if (right is not JsonArray items) return Tri.Unknown;
        var hit = items.Any(item => Equal(kind, left, Text(item)) == Tri.True);
        return Of(hit == (cmp == "in"));
    }

    private static Tri Ordered(string kind, string cmp, string left, string wanted)
    {
        int? order = kind switch
        {
            KindVersion => CompareVersions(left, wanted),
            KindNumber when TryNumber(left, out var a) && TryNumber(wanted, out var b) => a.CompareTo(b),
            _ => null,
        };
        if (order is not { } o) return Tri.Unknown;
        return Of(cmp switch { ">" => o > 0, ">=" => o >= 0, "<" => o < 0, _ => o <= 0 });
    }

    private static Tri Equal(string kind, string left, string? right)
    {
        if (right is null) return Tri.Unknown;
        switch (kind)
        {
            case KindNumber:
                // A tolerance, not ==: "16" parsed twice is the same double, but a value that
                // went through arithmetic (a rounded memory size) need not be bit-identical.
                return TryNumber(left, out var a) && TryNumber(right, out var b)
                    ? Of(Math.Abs(a - b) < 1e-9) : Tri.Unknown;
            case KindBool:
                return bool.TryParse(left, out var x) && bool.TryParse(right, out var y)
                    ? Of(x == y) : Tri.Unknown;
            case KindVersion:
                return CompareVersions(left, right) is { } order ? Of(order == 0) : Tri.Unknown;
            default:
                // Case-insensitive and trimmed, as rules._loose_equal: "LENOVO" is "Lenovo".
                return Of(string.Equals(left.Trim(), right.Trim(), StringComparison.OrdinalIgnoreCase));
        }
    }

    private static string? Text(JsonNode? node)
    {
        if (node is null) return null;
        if (node is JsonValue v)
        {
            if (v.TryGetValue<string>(out var s)) return s;
            if (v.TryGetValue<bool>(out var b)) return b ? "true" : "false";
            if (v.TryGetValue<double>(out var d)) return d.ToString(CultureInfo.InvariantCulture);
        }
        return node.ToString();
    }

    private static bool TryNumber(string text, out double value) =>
        double.TryParse(text.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out value);

    [GeneratedRegex(@"^\s*(\d+(?:\.\d+)*)")]
    private static partial Regex VersionPrefix();

    /// <summary>-1/0/1, or null when either side does not START with a dotted number.
    /// Stricter than VersionUtil.Compare, which reads "N3KET60W" as version 0 -- fine for an
    /// updater's "is this newer", wrong for "bios.version &gt;= 1.40", which must be unknown.</summary>
    public static int? CompareVersions(string left, string right)
    {
        var a = VersionPrefix().Match(left);
        var b = VersionPrefix().Match(right);
        if (!a.Success || !b.Success) return null;
        var pa = a.Groups[1].Value.Split('.');
        var pb = b.Groups[1].Value.Split('.');
        for (var i = 0; i < Math.Max(pa.Length, pb.Length); i++)
        {
            var x = i < pa.Length && long.TryParse(pa[i], out var px) ? px : 0;
            var y = i < pb.Length && long.TryParse(pb[i], out var py) ? py : 0;
            if (x != y) return x > y ? 1 : -1;
        }
        return 0;
    }

    /// <summary>`*` and `?`, case-insensitive -- hub/rules.py wildcard_match, line for line,
    /// including testing the star BEFORE the literal so a literal '*' in the text cannot eat
    /// the pattern's one backtrack point.</summary>
    public static bool WildcardMatch(string pattern, string text)
    {
        pattern = pattern.ToLowerInvariant();
        text = text.ToLowerInvariant();
        int p = 0, t = 0, star = -1, resume = 0;
        while (t < text.Length)
        {
            if (p < pattern.Length && pattern[p] == '*')
            {
                star = p; resume = t; p++;
            }
            else if (p < pattern.Length && (pattern[p] == '?' || pattern[p] == text[t]))
            {
                p++; t++;
            }
            else if (star >= 0)
            {
                p = star + 1; resume++; t = resume;
            }
            else return false;
        }
        while (p < pattern.Length && pattern[p] == '*') p++;
        return p == pattern.Length;
    }

    private static Tri Of(bool value) => value ? Tri.True : Tri.False;

    private static Tri Not(Tri value) => value switch
    {
        Tri.True => Tri.False,
        Tri.False => Tri.True,
        _ => Tri.Unknown,
    };

    // ---------------------------------------------------------------- probes

    private async Task<Tri> ProbeAsync(string probe, JsonObject leaf)
    {
        try
        {
            switch (probe)
            {
                case "file_exists" or "registry_value" or "installed_version":
                {
                    // The same code that judges a package's detection rule: "is it
                    // installed" and "should I install it" must not disagree.
                    var rule = new JsonObject { ["kind"] = probe };
                    foreach (var (key, value) in leaf)
                    {
                        if (key == "probe") continue;
                        rule[key] = key is "path" or "key"
                            ? vars.Resolve(value?.ToString())
                            : value?.DeepClone();
                    }
                    var (passed, detail, errored) = DeployPackageExecutor.Detect(rule);
                    Notes.Add(detail);
                    return errored ? Tri.Unknown : Of(passed);
                }
                case "powershell":
                    return await PowerShellAsync(leaf);
                default:
                    Notes.Add($"unsupported check '{probe}' -- update the agent");
                    return Tri.Unknown;
            }
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception e)
        {
            // Includes a withheld variable (a payload this machine did not download):
            // the check cannot be made, which is unknown, not false.
            Notes.Add($"{probe}: {e.Message}");
            return Tri.Unknown;
        }
    }

    private async Task<Tri> PowerShellAsync(JsonObject leaf)
    {
        var script = vars.Resolve(leaf.GetString("script"));
        if (string.IsNullOrWhiteSpace(script)) return Tri.Unknown;
        var timeout = Math.Clamp(leaf.GetInt("timeout_seconds", 60), 5, 600);
        var id = Guid.NewGuid().ToString("N");
        var path = vars.PathIn($"check-{id}.ps1");
        await File.WriteAllTextAsync(path, script, ct);
        // The operator's script runs INSIDE a wrapper, because `powershell.exe -File` exits 1
        // for a script that throws, fails to parse, or calls a cmdlet that is not there -- the
        // same 1 that means "no". Run bare, a typo in a driver package's check would read as
        // false and send every machine down Else, which is the false-when-it-should-be-unknown
        // case three-valued logic exists to prevent. The wrapper turns any terminating error
        // into exit 2 (unknown) and passes an explicit `exit N` through. A script that ends
        // without one answers 0, as it would on its own.
        var wrapper = vars.PathIn($"check-{id}-run.ps1");
        await File.WriteAllTextAsync(wrapper,
            "$global:LASTEXITCODE = 0\n" +
            $"try {{ & '{path.Replace("'", "''")}' }} catch {{ [Console]::Error.WriteLine($_); exit 2 }}\n" +
            "exit $LASTEXITCODE\n", ct);
        say("[deploy] running a PowerShell check");
        var outcome = await ProcessRunner.RunAsync(
            "powershell.exe",
            $"-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"{wrapper}\"",
            ct, timeoutSeconds: timeout, workingDir: vars.WorkDir, onLine: emit);
        if (outcome.TimedOut)
        {
            if (ct.IsCancellationRequested) throw new OperationCanceledException(ct);
            Notes.Add($"PowerShell check timed out after {timeout}s");
            return Tri.Unknown;
        }
        Notes.Add($"PowerShell check exited {outcome.ExitCode}");
        // 0 and 1 are the two answers; anything else -- including the wrapper's 2 for a script
        // that threw -- is a script that broke, which is not the same thing as one that said no.
        return outcome.ExitCode switch { 0 => Tri.True, 1 => Tri.False, _ => Tri.Unknown };
    }
}
