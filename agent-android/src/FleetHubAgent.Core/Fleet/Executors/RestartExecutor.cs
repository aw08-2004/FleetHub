using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// restart: reboot a fully managed device -- roadmap #23.
///
/// **This used to sit in CommandDispatcher.Impossible, and for most devices it still is.** An
/// ordinary app has no reboot API at all. A Device Owner does
/// (`DevicePolicyManager.reboot`, API 24+), and that is what this agent IS on a phone
/// provisioned by QR -- so the old message, "a device-owner MDM can reboot it", was telling the
/// operator to go and find the very app they were talking to. On an unmanaged device the answer
/// is still no, and it is a FAIL rather than a success with a note, for LockDeviceExecutor's
/// reason: somebody waiting for a phone to come back must not read "done".
///
/// **The result goes out BEFORE the reboot**, which is why the reboot is scheduled rather than
/// called inline. A process that reboots the device from inside the executor never posts a
/// result, and the hub shows the command claimed and running until it expires -- the console
/// would report a restart that worked as one that hung. The Windows agent gets the same effect
/// from `shutdown /r /t`. `delay_seconds` is honoured, but floored at five here rather than
/// zero, because five seconds is what the result post needs on a slow phone connection.
///
/// `force` is accepted and ignored. On Windows it overrides an application's veto; Android has
/// no veto to override, and pretending otherwise in the output would be inventing a difference.
/// </summary>
public sealed class RestartExecutor(ILogger<RestartExecutor> log, IDeviceReboot reboot)
    : ICommandExecutor
{
    public string Type => "restart";

    /// <summary>Matches the Windows agent's default, so a rule written for PCs means the same
    /// thing when it reaches a phone.</summary>
    internal const int DefaultDelaySeconds = 60;
    internal const int MinDelaySeconds = 5;
    /// <summary>A scheduled restart lives in this process, which Android may kill; an hour is
    /// already longer than that promise is worth.</summary>
    internal const int MaxDelaySeconds = 3600;

    internal static int ResolveDelay(System.Text.Json.Nodes.JsonNode? parameters)
        => Math.Clamp(parameters.GetInt("delay_seconds", DefaultDelaySeconds),
                      MinDelaySeconds, MaxDelaySeconds);

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        if (!reboot.CanReboot)
        {
            log.LogWarning("Refused restart: this device is not a device owner");
            return Task.FromResult(CommandResult.Fail(
                "This device is not fully managed, so it cannot be restarted remotely -- " +
                "Android gives that power only to a device-owner app. Provisioning one needs " +
                "a factory reset."));
        }

        var delay = ResolveDelay(cmd.Params);
        // Not tied to `ct`: the command loop's token is cancelled when the agent stops, and an
        // agent that is stopping is exactly when a restart already promised to the hub should
        // still happen if the process lives long enough to do it.
        _ = Task.Run(async () =>
        {
            await Task.Delay(TimeSpan.FromSeconds(delay), CancellationToken.None);
            var error = reboot.Reboot();
            if (error is not null)
                // Too late to tell the hub; the next heartbeat after the expected downtime is
                // what tells it nothing happened. Logged so that is explicable on the device.
                log.LogWarning("Scheduled restart did not happen: {Error}", error);
        }, CancellationToken.None);

        log.LogInformation("Restart scheduled in {Delay}s", delay);
        return Task.FromResult(CommandResult.Ok($"restart scheduled in {delay}s"));
    }
}

/// <summary>Rebooting the device, however the platform allows it.</summary>
public interface IDeviceReboot
{
    /// <summary>Whether a reboot is possible at all -- on Android, whether this app is the
    /// device owner. Asked per command because ownership can be granted to a running agent.</summary>
    bool CanReboot { get; }

    /// <summary>Reboot now. Returns null when the platform accepted it, else why not, phrased
    /// for an operator ("a phone call is in progress"). Must not throw.</summary>
    string? Reboot();
}
