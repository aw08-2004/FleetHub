using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// wake_machine: send a Wake-on-LAN magic packet for a sleeping PC on this device's subnet --
/// roadmap #10, relayed from a phone (roadmap #23).
///
/// **The one command whose subject is somebody else.** The hub issues it to an awake PEER on the
/// sleeping machine's subnet (hub/wake.py's find_relay), because a broadcast the hub sends only
/// reaches the hub's own segment. A phone on the office Wi-Fi is often the only thing awake on a
/// floor at seven in the morning, which is exactly when somebody wants the PCs up -- so letting
/// it relay turns "wake requests wait for the first PC to come online" into "they go now".
///
/// **Same wire behaviour as the Windows agent's WakeMachineExecutor, deliberately duplicated
/// rather than shared** (the agents share a protocol, not code -- see ROADMAP #23). Params:
/// `macs` (every wakeable MAC the target has on this subnet), `broadcast`, `port` (9), and
/// `target` for the log line. Partial success is success, for the reason given there: one frame
/// on the wire wakes the machine, and a failure would requeue the request for a second relay.
///
/// **Only a device that reports a real MAC is ever chosen**, and on Android that is only a
/// device owner -- see AndroidNetworkReader. Nothing here checks that, because the hub never
/// sends this to a device it has no NIC row for; if one arrived anyway, sending the packet is
/// still the right thing to do with it.
///
/// Wi-Fi access points commonly drop broadcast frames between clients, or forward them only
/// to the wired side. A packet this reports as sent can therefore still wake nothing -- the
/// same caveat any wireless relay carries, and the reason hub/wake.py confirms a wake by the
/// target checking in rather than by this command's success.
/// </summary>
public sealed class WakeMachineExecutor(ILogger<WakeMachineExecutor> log) : ICommandExecutor
{
    /// <summary>Matches hub/wake.py's COMMAND_TYPE.</summary>
    public string Type => "wake_machine";

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var target = cmd.Params.GetString("target") ?? "(unnamed)";
        var broadcast = (cmd.Params.GetString("broadcast") ?? "").Trim();
        var port = cmd.Params.GetInt("port", 9);
        var macs = ReadMacs(cmd.Params);

        if (macs.Count == 0)
            return Task.FromResult(CommandResult.Fail("no MAC address to wake"));
        if (!IPAddress.TryParse(broadcast, out var destination))
            return Task.FromResult(CommandResult.Fail($"not a broadcast address: {broadcast}"));
        if (port is < 1 or > 65535)
            return Task.FromResult(CommandResult.Fail($"not a port: {port}"));

        var output = new StringBuilder();
        var sent = 0;
        var failures = new List<string>();
        try
        {
            using var socket = new UdpClient();
            // Without this the platform refuses a send to a broadcast address outright. Set
            // before the loop so a refusal fails the command instead of blaming each MAC.
            socket.EnableBroadcast = true;
            foreach (var mac in macs)
            {
                ct.ThrowIfCancellationRequested();
                try
                {
                    var frame = MagicPacket(mac);
                    socket.Send(frame, frame.Length, new IPEndPoint(destination, port));
                    sent++;
                    output.AppendLine($"sent to {mac} via {broadcast}:{port}");
                }
                catch (Exception e) when (e is FormatException or SocketException)
                {
                    failures.Add($"{mac}: {e.Message}");
                }
            }
        }
        catch (System.OperationCanceledException) { throw; }
        catch (Exception e)
        {
            return Task.FromResult(CommandResult.Fail(
                $"could not open a broadcast socket: {e.Message}"));
        }

        foreach (var failure in failures) output.AppendLine($"FAILED {failure}");
        output.AppendLine(sent > 0
            ? $"woke {target}: {sent} magic packet(s) sent"
            : $"could not wake {target}");
        log.LogInformation("wake_machine for {Target}: {Sent} sent, {Failed} failed",
            target, sent, failures.Count);

        return Task.FromResult(sent > 0
            ? CommandResult.Ok(output.ToString())
            : CommandResult.Fail(output.ToString()));
    }

    /// <summary>The 102-byte frame: six 0xFF bytes, then the target MAC sixteen times.</summary>
    internal static byte[] MagicPacket(string mac)
    {
        var bytes = ParseMac(mac);
        var frame = new byte[6 + 16 * 6];
        for (var i = 0; i < 6; i++) frame[i] = 0xFF;
        for (var repeat = 0; repeat < 16; repeat++)
            Buffer.BlockCopy(bytes, 0, frame, 6 + repeat * 6, 6);
        return frame;
    }

    /// <summary>The six bytes of a MAC in any spelling the hub sends. Throws on anything else:
    /// an unparseable MAC must not become a frame of zeroes that sends fine and wakes nothing.</summary>
    internal static byte[] ParseMac(string mac)
    {
        var hex = new string((mac ?? "").Where(Uri.IsHexDigit).ToArray());
        if (hex.Length != 12) throw new FormatException($"not a MAC address: {mac}");
        return Convert.FromHexString(hex);
    }

    private static List<string> ReadMacs(JsonNode? parameters)
    {
        var macs = new List<string>();
        if (parameters is JsonObject obj && obj.TryGetPropertyValue("macs", out var value)
            && value is JsonArray array)
        {
            foreach (var item in array)
            {
                // GetValue rather than ToString: ToString on a JsonValue of a string is its
                // JSON spelling on some paths, and a quoted MAC would fail to parse.
                var text = item is JsonValue v && v.TryGetValue<string>(out var s) ? s.Trim() : null;
                if (!string.IsNullOrEmpty(text)) macs.Add(text);
            }
        }
        return macs;
    }
}
