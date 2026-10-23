using Android.App.Admin;
using Android.Content;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Android.Policy;

/// <summary>
/// Locking and erasing this device -- roadmap #23 phase H.
///
/// The platform half of <see cref="IDeviceSecurity"/>, and deliberately the whole of it: every
/// decision about whether to do either of these, what to answer, and how long to wait first is
/// in the executors. This file knows two API calls and the names of the flags they take.
///
/// **`lockNow` needs the force-lock policy and `wipeData` needs wipe-data**, both declared in
/// `device_admin.xml` since phase A. That was not foresight about this phase so much as the
/// one lesson provisioning teaches: a device owner cannot be re-prompted for a policy it did
/// not declare, so a missing one is discovered on a device that has already been factory reset
/// and costs a second wipe to add.
/// </summary>
public sealed class AndroidDeviceSecurity(Context context, ILogger log)
    : IDeviceSecurity, FleetHubAgent.Fleet.Executors.IDeviceReboot
{
    /// <summary>Evaluated live rather than captured, for the same reason DeviceOwner.Features
    /// is: `adb shell dpm set-device-owner` can grant ownership to a running process, and a
    /// stale answer here would refuse a lock on a device that can perform one.</summary>
    public bool CanEnforce => DeviceOwner.IsManaged(context);

    public bool Lock()
    {
        var dpm = Manager();
        if (dpm is null) return false;
        try
        {
            // The no-argument overload: lock the screen as it is configured. The variant that
            // takes flags can also lock a work profile's challenge separately, which is a
            // work-profile concept and this is a fully managed device.
            dpm.LockNow();
            return true;
        }
        catch (Exception e)
        {
            log.LogWarning("lockNow refused: {Msg}", e.Message);
            return false;
        }
    }

    public void Wipe(bool clearResetProtection)
    {
        var dpm = Manager();
        if (dpm is null)
        {
            log.LogError("No device-policy service; this device has NOT been erased");
            return;
        }

        // WIPE_RESET_PROTECTION_DATA clears the factory-reset protection block along with the
        // data, so the device can be set up again by anybody. The hub decides this per request
        // and says which it is doing -- see wipe.py. Requesting the flag on a device whose
        // build has no FRP is harmless; the platform ignores what it cannot act on.
        var flags = clearResetProtection
            ? WipeDataFlags.WipeResetProtectionData
            : WipeDataFlags.None;

        try
        {
            // **wipeDevice from API 34, wipeData only below it.** wipeData throws
            // IllegalStateException when called on the system user by an app targeting 34 or
            // above -- and a device owner always runs on the system user, while .NET 10 builds
            // this app for target SDK 36. So up to 0.3.1 every wipe on every Android 14+
            // device answered "the wipe has started", then threw here ten seconds later into a
            // log nobody reads, and the phone carried on reporting. **Observed on an Android 16
            // Galaxy A15**, which kept heartbeating two minutes after the console said it was
            // being erased. wipeDevice is the replacement the platform added for exactly this
            // caller; it always requests a full factory reset and honours the same flags.
            //
            // Rejected: pinning targetSdkVersion to 33 to keep wipeData working. Play and the
            // platform both penalise a low target, and it would trade one wipe call for every
            // newer behaviour the rest of this app is written against.
            // Does not return. Anything after these lines runs only if the platform refused.
            if (OperatingSystem.IsAndroidVersionAtLeast(34))
            {
                dpm.WipeDevice(flags);
                log.LogError("wipeDevice returned; this device has NOT been erased");
            }
            else
            {
                dpm.WipeData(flags);
                log.LogError("wipeData returned; this device has NOT been erased");
            }
        }
        catch (Exception e)
        {
            log.LogError(e, "wipeData refused; this device has NOT been erased");
        }
    }

    public bool CanReboot => DeviceOwner.IsManaged(context);

    /// <summary>Restart the device -- RestartExecutor, 0.3.0.
    ///
    /// DevicePolicyManager.reboot exists from API 24 and this app's floor is 26, so there is no
    /// version guard. It throws IllegalStateException while a call is in progress, which is the
    /// one refusal a person can act on, so it is translated rather than passed through as a
    /// Java exception name.</summary>
    public string? Reboot()
    {
        var dpm = Manager();
        if (dpm is null) return "this device has no device-policy service";
        try
        {
            dpm.Reboot(DeviceOwner.Component(context));
            return null;
        }
        catch (Java.Lang.IllegalStateException)
        {
            return "a phone call is in progress on the device";
        }
        catch (Exception e)
        {
            log.LogWarning(e, "reboot refused");
            return e.Message;
        }
    }

    private DevicePolicyManager? Manager()
        => context.GetSystemService(Context.DevicePolicyService) as DevicePolicyManager;
}
