using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Patch;

/// <summary>
/// The <c>set_update_control</c> command (roadmap #14, issue #140): take Windows Update's
/// install decisions away from Windows and the user, or give them back.
///
/// <para><b>The hub sends this from a reconcile loop, not from a button.</b> It compares the
/// maintenance window's <c>own_windows_update</c> switch with the <c>control.state</c> this
/// machine last reported, and queues the command when they differ. So the command must be
/// idempotent (a retry or a stale duplicate re-applies harmlessly), and the outcome that
/// matters is the state the NEXT inventory report carries -- which is why this invalidates
/// the patch inventory rather than leaving the hub to wait out the six-hour refresh, and
/// keep re-queueing in the meantime.</para>
///
/// <para><b>managed_elsewhere is a successful result, not a failure.</b> A PC whose Windows
/// Update policy belongs to GPO or Intune was correctly left alone (see UpdateControl); a
/// failed command would spend the hub's retries on a machine with nothing wrong with it.
/// The output is the bare resulting state, one of UpdateControl's three wire strings, so the
/// hub can read it without parsing prose.</para>
/// </summary>
public sealed class SetUpdateControlExecutor : ICommandExecutor
{
    /// <summary>Matches the hub's command type for update control.</summary>
    public string Type => "set_update_control";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                            CancellationToken ct)
    {
        // Required, not defaulted: a params object that lost its flag must not be read as
        // "release" (or "own") and flip a whole window's PCs the wrong way.
        if (cmd.Params is not JsonObject obj || !obj.ContainsKey("own"))
        {
            return Task.FromResult(CommandResult.Fail("set_update_control needs \"own\": true|false."));
        }
        var own = cmd.Params.GetBool("own");

        string state;
        try
        {
            var control = UpdateControl.ForMachine();
            state = own ? control.Apply() : control.Release();
        }
        catch (Exception e) when (e is UnauthorizedAccessException or IOException
                                       or System.Security.SecurityException)
        {
            return Task.FromResult(CommandResult.Fail(
                $"Windows Update policy could not be {(own ? "written" : "removed")}: {e.Message}"));
        }

        PatchInventoryReporter.Invalidate();
        onOutput?.Invoke(state + "\n");
        return Task.FromResult(CommandResult.Ok(state));
    }
}
