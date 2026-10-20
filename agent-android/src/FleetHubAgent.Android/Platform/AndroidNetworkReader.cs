using System.Text.Json.Nodes;
using Android.App.Admin;
using Android.Content;
using Android.Net;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;
using FleetHubAgent.Android.Policy;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// The device's network adapters, as the heartbeat's `network` block (hub/wake.py
/// record_network) -- 0.3.0.
///
/// Two things read it. The console's Network tab, which until now showed nothing for a phone,
/// and Wake-on-LAN's relay picker (wake.find_relay), which chooses an awake machine that shares
/// a subnet with the sleeping one. A phone on the office Wi-Fi is that machine more often than
/// a PC is at seven in the morning.
///
/// **The MAC is the catch, and it decides whether a phone can relay at all.** Since Android 10
/// an app reads 02:00:00:00:00:00 for every adapter, and wake.py keys adapters by MAC -- it
/// drops one without a real address, and a device with no adapter row is never picked. A
/// device owner may ask DevicePolicyManager.getWifiMacAddress, which returns the real one. So
/// a fully managed phone reports its Wi-Fi adapter with its MAC and can relay; any other phone
/// reports nothing here, rather than a placeholder MAC that every phone in the fleet would
/// share and the hub would then treat as one adapter.
///
/// That MAC is the device's FACTORY address. With per-network MAC randomisation (the default
/// since Android 10) the address on the wire is a different one, so the console's adapter row
/// is an identity rather than what a switch would see. Nothing reads it as the latter: a phone
/// is never a wake TARGET (`wake_enabled` is always false), only a relay, and relaying needs
/// the subnet, not the MAC.
/// </summary>
public sealed class AndroidNetworkReader(Context context, ILogger log) : IInventorySource
{
    /// <summary>hub/fleet_web.py's heartbeat key.</summary>
    public string Key => "network";

    /// <summary>A phone changes network several times a day; the relay picker is only right
    /// about a subnet the device is still on. Change-only on the wire, so this is cheap.</summary>
    public TimeSpan RefreshInterval => TimeSpan.FromMinutes(5);

    public JsonObject? Read()
    {
        try
        {
            var nics = new JsonArray();
            var mac = DeviceOwner.IsManaged(context) ? WifiMac() : null;
            if (mac is not null
                && context.GetSystemService(Context.ConnectivityService) is ConnectivityManager cm
                && cm.ActiveNetwork is { } network
                && cm.GetNetworkCapabilities(network) is { } caps
                && caps.HasTransport(TransportType.Wifi)
                && cm.GetLinkProperties(network) is { } link)
            {
                var (ipv4, prefix) = FirstIpv4(link);
                // Cast to JsonNode: the generic Add<T> resolves T by reflection and throws on
                // the trimmed APK -- the 0.2.1 failure (AgentCapabilities.ToJson says more).
                nics.Add((JsonNode)new JsonObject
                {
                    ["mac"] = mac,
                    ["name"] = link.InterfaceName ?? "wlan0",
                    ["description"] = "Wi-Fi",
                    ["ipv4"] = ipv4 ?? "",
                    ["prefix"] = prefix,
                    ["kind"] = "wireless",
                    ["link_up"] = true,
                    ["wake_enabled"] = false,
                });
            }
            return new JsonObject { ["nics"] = nics };
        }
        catch (Exception e)
        {
            // Null is "could not look", which leaves the hub's last reading alone -- see
            // IInventorySource. An empty list would erase it.
            log.LogDebug("Network read failed: {Msg}", e.Message);
            return null;
        }
    }

    private string? WifiMac()
    {
        try
        {
            var dpm = context.GetSystemService(Context.DevicePolicyService) as DevicePolicyManager;
            var mac = dpm?.GetWifiMacAddress(DeviceOwner.Component(context));
            // Null when Wi-Fi has never been switched on since boot on some builds; the next
            // read, five minutes on, will have it.
            return string.IsNullOrWhiteSpace(mac) || mac == "02:00:00:00:00:00" ? null : mac;
        }
        catch (Exception e)
        {
            log.LogDebug("getWifiMacAddress refused: {Msg}", e.Message);
            return null;
        }
    }

    private static (string? Address, int? Prefix) FirstIpv4(LinkProperties link)
    {
        foreach (var address in link.LinkAddresses)
        {
            if (address.Address is Java.Net.Inet4Address v4)
                return (v4.HostAddress, address.PrefixLength);
        }
        return (null, null);
    }
}

/// <summary>
/// The security facts a phone can report, as the heartbeat's `posture` block (hub/posture.py)
/// -- 0.3.0.
///
/// **Only what Android lets an app see, in the hub's existing vocabulary.** The screen lock
/// goes into `session_lock`, the area the Windows agent fills from screen-saver settings, so a
/// phone is judged by the same CIS 4.3 threshold as a PC. Storage encryption and the security
/// patch level go into `mobile`, whose presence is also what tells the hub to judge this
/// machine on the phone checks rather than seven Windows ones it could never answer.
///
/// **The lock timeout is the screen-off timeout**, which is a slight under-statement: Android
/// may wait a further few seconds after the screen goes dark before it demands the PIN, and
/// that delay is not readable by an app. When the device owner has set a maximum time to lock,
/// that policy is reported instead, because it is the one that is enforced.
/// </summary>
public sealed class AndroidPostureReader(Context context, ILogger log) : IInventorySource
{
    public string Key => "posture";

    /// <summary>Hourly, like the Windows agent's posture read. None of this changes often.</summary>
    public TimeSpan RefreshInterval => TimeSpan.FromHours(1);

    public JsonObject? Read()
    {
        try
        {
            var keyguard = context.GetSystemService(Context.KeyguardService) as global::Android.App.KeyguardManager;
            var dpm = context.GetSystemService(Context.DevicePolicyService) as DevicePolicyManager;

            long? policyMs = null;
            try { policyMs = dpm?.GetMaximumTimeToLock(null); }
            catch (Exception) { /* not readable on this build */ }

            int? screenOffSeconds = null;
            try
            {
                var ms = global::Android.Provider.Settings.System.GetInt(
                    context.ContentResolver, global::Android.Provider.Settings.System.ScreenOffTimeout);
                if (ms > 0) screenOffSeconds = ms / 1000;
            }
            catch (Exception) { /* the setting is absent on some builds */ }

            return new JsonObject
            {
                ["session_lock"] = new JsonObject
                {
                    ["machine_inactivity_seconds"] = policyMs is > 0 ? (int?)(int)(policyMs.Value / 1000) : null,
                    ["error"] = "",
                    ["users"] = new JsonArray(new JsonObject
                    {
                        // posture.py keeps a user only when it has an id. A phone has one
                        // person on it as far as an app can tell, and this names them.
                        ["sid"] = "android-user-0",
                        ["active"] = true,
                        ["secure"] = keyguard?.IsDeviceSecure,
                        ["timeout_seconds"] = screenOffSeconds,
                    }),
                },
                ["mobile"] = new JsonObject
                {
                    ["security_patch"] = global::Android.OS.Build.VERSION.SecurityPatch ?? "",
                    ["encryption"] = Encryption(dpm),
                    ["error"] = "",
                },
            };
        }
        catch (Exception e)
        {
            log.LogDebug("Posture read failed: {Msg}", e.Message);
            return null;
        }
    }

    private static string Encryption(DevicePolicyManager? dpm)
    {
        if (dpm is null) return "unknown";
        try
        {
            return dpm.StorageEncryptionStatus switch
            {
                // ACTIVE_DEFAULT_KEY is encrypted with a key that does not depend on the
                // person's PIN -- encrypted at rest, weaker than it could be, and still the
                // answer CIS 3.6 asks for. ACTIVE_PER_USER is what every file-based-encryption
                // device reports.
                EncryptionStatus.Active or EncryptionStatus.ActivePerUser
                    or EncryptionStatus.ActiveDefaultKey => "active",
                EncryptionStatus.Inactive or EncryptionStatus.Activating => "inactive",
                EncryptionStatus.Unsupported => "unsupported",
                _ => "unknown",
            };
        }
        catch (Exception)
        {
            return "unknown";
        }
    }
}
