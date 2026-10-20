using Android.App;
using Android.Content;
using Android.Content.PM;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet.Executors;
using FleetHubAgent.Android.Policy;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// Installs another app as the device owner, and waits to be told how it went -- the platform
/// half of DeployPackageExecutor (0.3.0).
///
/// **Not AndroidPackageInstaller, though both drive the same PackageInstaller session API.**
/// That one installs THIS app over itself, so it commits and is killed, and nothing listening
/// for the verdict would live to hear it. Here the process survives, so the verdict is the
/// whole point: the platform answers asynchronously, by broadcasting to the IntentSender the
/// session was committed with, and a deploy that reported success on commit would report every
/// refused install -- a downgrade, a signature clash, a corrupt file -- as installed.
///
/// **Then it checks for itself.** "The installer said success" is evidence, the same as an
/// exit code on Windows. The package the APK declares is looked up afterwards, at the version
/// it declares, and only that is reported as installed.
///
/// **This app's own package is refused.** Installing the agent over itself through a deploy
/// would kill the process mid-command and bypass the signed self-update channel, whose
/// manifest signature is the only thing that says a build is the fleet's.
/// </summary>
public sealed class AndroidAppInstaller(Context context, ILogger log) : IAppInstaller
{
    public bool CanInstallSilently => DeviceOwner.IsManaged(context);

    public async Task<AppInstallResult> InstallAsync(string path, TimeSpan timeout, CancellationToken ct)
    {
        if (!CanInstallSilently) return AppInstallResult.Refused("this device is not fully managed");
        var pm = context.PackageManager;
        var installer = pm?.PackageInstaller;
        if (pm is null || installer is null)
            return AppInstallResult.Refused("no package installer on this device");

        // What the file claims to be, read before anything is installed.
        var (packageName, wantedVersion, refusal) = ReadArchive(pm, path);
        if (refusal is not null) return AppInstallResult.Refused(refusal);

        var action = $"{context.PackageName}.INSTALL_RESULT.{Guid.NewGuid():N}";
        var verdict = new TaskCompletionSource<(int Status, string? Message)>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var receiver = Listen(action, verdict);

        var sessionId = -1;
        try
        {
            // Created here rather than inside CommitAsync, so the id is in hand before any of
            // the work that can fail -- a copy that runs out of disk, an Fsync or Commit that
            // throws, an agent stopping mid-copy. Returned from CommitAsync, it reached the
            // handlers below as -1 on exactly those paths, and the open session sat in the
            // platform's store until its own expiry (raised in review of PR #161).
            sessionId = CreateSession(installer, packageName!);
            await CommitAsync(installer, sessionId, path, action, ct);
            var refused = await VerdictAsync(verdict.Task, packageName!, timeout, ct);
            if (refused is { SessionStillOpen: true })
                return AppInstallResult.Refused(refused.Error + Abandon(installer, sessionId));
            return refused?.Result ?? Confirm(pm, packageName!, wantedVersion);
        }
        catch (System.OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // The agent is stopping. The session must not go on to install an app after the
            // command that asked for it has been abandoned too.
            Abandon(installer, sessionId);
            throw;
        }
        catch (Exception e)
        {
            log.LogWarning(e, "Install session for {Package} failed", packageName);
            Abandon(installer, sessionId);
            return AppInstallResult.Refused(e.Message);
        }
        finally
        {
            try { context.UnregisterReceiver(receiver); } catch { /* already gone */ }
        }
    }

    /// <summary>Register for one session's verdict, not exported: only the platform's
    /// installer, writing into this app's own PendingIntent, may deliver it.</summary>
    private ResultReceiver Listen(string action, TaskCompletionSource<(int, string?)> verdict)
    {
        var receiver = new ResultReceiver(verdict);
        var filter = new IntentFilter(action);
        if (OperatingSystem.IsAndroidVersionAtLeast(33))
            context.RegisterReceiver(receiver, filter, ReceiverFlags.NotExported);
        else
            context.RegisterReceiver(receiver, filter);
        return receiver;
    }

    /// <summary>A verdict that is not success. `SessionStillOpen` marks the two cases where
    /// the session is alive and has to be abandoned: no answer in time, and PENDING_USER_ACTION,
    /// which is the platform waiting for a tap rather than refusing -- left open, somebody
    /// tapping it later installs an app the console was told failed (raised in review of
    /// PR #161). Every other refusal is the platform's final word.</summary>
    private sealed record Refusal(string Error, bool SessionStillOpen)
    {
        public AppInstallResult Result => AppInstallResult.Refused(Error);
    }

    /// <summary>Wait for the platform's answer. Null when it said success; otherwise the
    /// refusal, in the platform's own words where it gave any.</summary>
    private async Task<Refusal?> VerdictAsync(Task<(int Status, string? Message)> verdict,
        string packageName, TimeSpan timeout, CancellationToken ct)
    {
        (int Status, string? Message) answer;
        try
        {
            answer = await verdict.WaitAsync(timeout, ct);
        }
        catch (TimeoutException)
        {
            return new Refusal(
                $"the installer gave no answer within {(int)timeout.TotalSeconds} seconds", true);
        }
        if (answer.Status == (int)PackageInstallStatus.Success) return null;

        log.LogWarning("Install of {Package} refused: {Status} {Msg}",
            packageName, answer.Status, answer.Message);
        return new Refusal(answer.Message is { Length: > 0 } m
                ? m : $"the installer refused it (status {answer.Status})",
            SessionStillOpen: answer.Status == (int)PackageInstallStatus.PendingUserAction);
    }

    /// <summary>Abandon a committed session that this deploy has given up on, so the app does
    /// not appear on the device after the console was told the install failed -- and a retry
    /// does not race a second session for the same package (raised in review of PR #161).
    ///
    /// Best effort, and the result says which way it went: the platform refuses to abandon a
    /// session that has already finished installing, and an operator reading "timed out" has
    /// to know whether the app may turn up anyway.</summary>
    private string Abandon(PackageInstaller installer, int sessionId)
    {
        if (sessionId < 0) return "";
        try
        {
            installer.AbandonSession(sessionId);
            return "; the install was cancelled";
        }
        catch (Exception e)
        {
            log.LogWarning(e, "Could not abandon install session {Id}", sessionId);
            return "; the install could not be cancelled, so the app may still appear";
        }
    }

    /// <summary>The package and version the APK declares, or why it will not be installed.</summary>
    private (string? Package, long Version, string? Refusal) ReadArchive(PackageManager pm, string path)
    {
        var archive = pm.GetPackageArchiveInfo(path, 0);
        if (archive?.PackageName is not { Length: > 0 } packageName)
            return (null, 0, "the file is not an Android app (no package in it)");
        if (packageName == context.PackageName)
            return (null, 0, "this is the FleetHub agent itself, which updates only through its " +
                             "own signed release channel");
        return (packageName, VersionCode(archive), null);
    }

    private static int CreateSession(PackageInstaller installer, string packageName)
    {
        var parameters = new PackageInstaller.SessionParams(PackageInstallMode.FullInstall);
        parameters.SetAppPackageName(packageName);
        return installer.CreateSession(parameters);
    }

    /// <summary>Stream the APK into the session and commit it. The verdict arrives later, as
    /// the broadcast `action` names.</summary>
    private async Task CommitAsync(PackageInstaller installer, int sessionId,
        string path, string action, CancellationToken ct)
    {
        using var session = installer.OpenSession(sessionId);
        await using (var source = File.OpenRead(path))
        await using (var destination = session.OpenWrite("app.apk", 0, source.Length))
        {
            await source.CopyToAsync(destination, ct);
            session.Fsync(destination);
        }

        // Explicit (setPackage) and mutable: the platform writes the status extras into it,
        // and from API 34 a mutable PendingIntent must name its target.
        var intent = new Intent(action);
        intent.SetPackage(context.PackageName);
        var flags = PendingIntentFlags.UpdateCurrent
                    | (OperatingSystem.IsAndroidVersionAtLeast(31) ? PendingIntentFlags.Mutable : 0);
        var pending = PendingIntent.GetBroadcast(context, sessionId, intent, flags);
        session.Commit(pending!.IntentSender!);
    }

    /// <summary>Detection: is the package really installed, at least at the version the file
    /// declared? "The installer said success" is evidence, not proof.</summary>
    private static AppInstallResult Confirm(PackageManager pm, string packageName, long wantedVersion)
    {
        PackageInfo? installed;
        try { installed = pm.GetPackageInfo(packageName, 0); }
        catch (PackageManager.NameNotFoundException) { installed = null; }
        if (installed is null)
            return AppInstallResult.Refused(
                $"the installer reported success but {packageName} is not installed");
        if (VersionCode(installed) < wantedVersion)
            return AppInstallResult.Refused(
                $"the installer reported success but {packageName} is still at " +
                $"{installed.VersionName}");
        return new AppInstallResult(true, packageName, installed.VersionName, null);
    }

    private static long VersionCode(PackageInfo info)
        => OperatingSystem.IsAndroidVersionAtLeast(28)
            ? info.LongVersionCode
#pragma warning disable CA1422 // the API-28 replacement is used above; this is the fallback
            : info.VersionCode;
#pragma warning restore CA1422

    /// <summary>Hears one session's verdict. A class rather than a lambda because a
    /// BroadcastReceiver is a Java object the platform calls back into.</summary>
    private sealed class ResultReceiver(TaskCompletionSource<(int, string?)> verdict)
        : BroadcastReceiver
    {
        public override void OnReceive(Context? context, Intent? intent)
        {
            // One verdict per session, so the receiver is done as soon as it has one. The
            // installer's finally unregisters it too; whichever runs second finds it gone.
            try
            {
                context?.UnregisterReceiver(this);
            }
            catch (Java.Lang.IllegalArgumentException)
            {
                // Already unregistered by the installer's finally, which got there first. The
                // receiver is gone either way, and that was the point of the call.
            }
            var status = intent?.GetIntExtra(PackageInstaller.ExtraStatus, int.MinValue)
                         ?? int.MinValue;
            // PENDING_USER_ACTION would mean the platform wants a tap. A device owner is never
            // asked, so this is not expected; if it happens the install is not going to finish
            // on its own, and saying so beats waiting out the timeout.
            if (status == (int)PackageInstallStatus.PendingUserAction)
            {
                verdict.TrySetResult((status, "the device asked for somebody to confirm the install"));
                return;
            }
            verdict.TrySetResult((status, intent?.GetStringExtra(PackageInstaller.ExtraStatusMessage)));
        }
    }
}
