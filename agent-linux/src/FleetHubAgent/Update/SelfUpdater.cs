using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using FleetHubAgent.State;
using FleetHub.Shared;

namespace FleetHubAgent.Update;

/// <summary>
/// Signed self-update for the Linux agent (roadmap #22).
///
/// Fetches a small Ed25519-signed manifest (version + sha256 + url), verifies it fail-closed,
/// downloads the newer binary, verifies its hash against the SIGNED value, swaps it in, and
/// exits so systemd starts the new one. A restart-count guard stops a bad update from looping.
///
/// **This is a port of the Windows agent's SelfUpdater, and the differences are exactly three.**
/// Everything else is deliberately identical, because the two are installing artifacts signed
/// by the same offline key and a subtly different verification would mean an update one fleet
/// accepts and the other does not.
///
///   1. **The restart is systemd's.** The Windows agent exits with a distinct code the SCM is
///      configured to treat as "restart me". This unit is `Restart=always`, so ANY exit brings
///      the agent back ten seconds later -- the exit code is a marker in the journal rather
///      than a contract. That also means this class must never exit for a reason it has not
///      thought about, since there is no "stopped" state to land in.
///   2. **The swap is a rename either way.** On Windows a running exe cannot be deleted but can
///      be renamed aside, which is what that code does. On Linux the running binary can be
///      renamed OR unlinked while executing -- the kernel keeps the inode open -- so the same
///      rename-aside sequence works, and is kept rather than simplified because the recovery
///      path in the middle of it is the valuable part.
///   3. **The new binary needs its mode set.** File.Move preserves the staged file's
///      permissions, and a downloaded file is 0600 by default. A 0600 binary at the unit's
///      ExecStart is a service that cannot start, on a machine that has just deleted the
///      version that worked.
///
/// **The .old binary survives boot and is cleared only once the new build has reached the
/// hub.** That is the Windows agent's hardest-won lesson, restated here because it is easy to
/// undo: treating "the process started" as "the update succeeded" means a build that starts,
/// deletes its predecessor, and only then turns out to be unable to reach the hub -- on every
/// machine at once, because they all take the same manifest. Nothing here starts the old
/// binary again; rolling back needs something still running when this process cannot start,
/// which on Linux is systemd or a person. What this promises is that the artifact is still on
/// disk when somebody goes looking.
/// </summary>
public sealed class SelfUpdater
{
    private readonly ILogger<SelfUpdater> _log;
    private readonly AgentState _state;
    private readonly HttpClient _http;

    /// <summary>The binary download's own client. A ~70 MB file does not fit the manifest's
    /// 30-second budget on a slow link, and sharing one client meant every check on such a
    /// machine cancelled the download and started again fifteen minutes later.</summary>
    private readonly HttpClient _download;

    /// <summary>The last failure reported at Warning, so a check that keeps failing the same
    /// way says so once in the journal rather than every fifteen minutes -- and a check that
    /// fails a NEW way says so at once. Cleared by any check that gets as far as a decision.
    ///
    /// **Until 0.4.1 a network failure here logged at Debug**, under a journal that runs at
    /// Information, so a box that could not reach the manifest (a 404, a proxy, a timeout)
    /// looked exactly like a box with nothing to install.</summary>
    private string? _lastFailure;

    /// <summary>1 while an applied update is still waiting to be confirmed by this build doing
    /// real work. Claimed rather than merely read by <see cref="ConfirmRunningBuild"/>, so the
    /// retirement happens exactly once even though two loops call it.</summary>
    private int _awaitingConfirmation;

    /// <summary>Whether this process has already said that the env override is in force.
    ///
    /// **The 0.4.1 bootstrap for 0.3.0 boxes is that override**, and it outranks both the
    /// console's channel and a pin. Left in agent.env after the box reaches 0.4.1, it would keep
    /// the box on whatever train it names while the console showed something else, and nothing
    /// would say why -- the exact failure this release exists to end. Once per process, so the
    /// journal says it at every start without repeating it every fifteen minutes.</summary>
    private bool _overrideAnnounced;

    public SelfUpdater(ILogger<SelfUpdater> log, AgentState state)
    {
        _log = log;
        _state = state;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
        _download = new HttpClient { Timeout = AgentConfig.DownloadTimeout };
    }

    /// <summary>Whether the restart-loop guard should stop an attempt at <paramref name="target"/>
    /// right now. Pure, so the expiry can be tested without a clock or a state file.
    ///
    /// It blocks only while the same target has used up its restarts AND the last attempt was
    /// less than <see cref="AgentConfig.GiveUpRetryAfter"/> ago. A missing timestamp is a file
    /// from before 0.4.1 and reads as long ago.</summary>
    public static bool GuardBlocks(RestartState? rs, string target, DateTimeOffset now)
    {
        if (rs is null || rs.Target != target || rs.Count < AgentConfig.MaxChainRestarts)
            return false;
        return rs.LastAttemptUtc is { } at && now - at < AgentConfig.GiveUpRetryAfter;
    }

    /// <summary>Report one failed check: Warning the first time this failure is seen, Debug
    /// while it repeats. See <see cref="_lastFailure"/>.</summary>
    private void NoteFailure(string message, Exception? e = null)
    {
        if (message == _lastFailure)
        {
            _log.LogDebug("[update] {Msg} (unchanged)", message);
            return;
        }
        _lastFailure = message;
        if (e is null) _log.LogWarning("[update] {Msg}", message);
        else _log.LogWarning(e, "[update] {Msg}", message);
    }

    /// <summary>On boot, notice that we came back on the version an update was aiming for --
    /// and then do nothing about it until the build has actually worked.</summary>
    public void ReconcileAfterBoot()
    {
        var rs = _state.LoadRestartState();
        if (rs is null) return;
        // EQUAL to the target, not "at least": a pinned target can be older than the build
        // that went looking for it, and "at least" read a downgrade whose swap did not take as
        // a success, retiring the guard that stops it retrying forever.
        if (VersionUtil.Compare(AgentConfig.Version, rs.Target) == 0)
        {
            Volatile.Write(ref _awaitingConfirmation, 1);
            _log.LogInformation(
                "Update to {Target} booted (now {Version}); holding the previous binary until "
                + "this build reaches the hub", rs.Target, AgentConfig.Version);
        }
        // Anything else means the swap did not take and systemd started the old binary. Leave the state alone: its Count is what stops CheckAndApplyAsync retrying
        // the same broken target forever.
    }

    /// <summary>Called once the updated build has completed a round trip the hub accepted,
    /// which is the first evidence it can do its job rather than merely start.
    ///
    /// **Two loops call this, and that is deliberate.** Gating confirmation on the telemetry
    /// report alone would tie "the update worked" to "the sensor also works", so a machine
    /// with no readable thermal zone would keep its previous binary forever while being
    /// perfectly healthy. The heartbeat has the opposite blind spot, since it needs enrollment
    /// and /api/report does not. Either landing is proof.</summary>
    public void ConfirmRunningBuild()
    {
        if (Interlocked.Exchange(ref _awaitingConfirmation, 0) == 0) return;
        _state.ClearRestartState();
        TryDeleteOldBinary();
        _log.LogInformation("Update to {Version} confirmed -- the hub accepted this build",
            AgentConfig.Version);
    }

    /// <summary>Check for and apply an update. Returns true if one was applied, in which case
    /// the caller is expected to stop the host so systemd can start the new binary.</summary>
    public async Task<bool> CheckAndApplyAsync(CancellationToken ct)
    {
        if (AgentConfig.UpdatesDisabled) return false;

        if (AgentConfig.UpdateManifestOverridden && !_overrideAnnounced)
        {
            _overrideAnnounced = true;
            _log.LogWarning(
                "[update] FLEETHUB_UPDATE_MANIFEST_URL is set ({Url}); it overrides the console's "
                + "release channel and any version pin. Remove it from /etc/fleethub/agent.env "
                + "once this box is on the version you wanted", AgentConfig.UpdateManifestUrl);
        }

        try
        {
            // 1. Manifest + detached signature.
            // A pin reads that version's own manifest; the env override (a test server) wins
            // over both and is read the automatic way.
            var pin = AgentConfig.UpdateManifestOverridden ? null : AgentState.LoadUpdatePin();
            var manifestUrl = AgentConfig.ManifestUrlForPin(pin) ?? AgentConfig.UpdateManifestUrl;
            var manifestBytes = await _http.GetByteArrayAsync(manifestUrl, ct);
            var sigHex = (await _http.GetStringAsync(manifestUrl + ".sig", ct)).Trim();

            // 2. Verify the signature over the EXACT manifest bytes, fail-closed. Everything
            // after this point trusts the contents; nothing before it does.
            if (!SignatureVerifier.VerifyRaw(AgentConfig.UpdatePublicKeyHex, manifestBytes, sigHex))
            {
                NoteFailure($"manifest signature invalid at {manifestUrl} -- refusing");
                return false;
            }

            // 3. Parse.
            var manifest = JsonSerializer.Deserialize<UpdateManifest>(manifestBytes);
            if (manifest is null || string.IsNullOrEmpty(manifest.Version)
                || string.IsNullOrEmpty(manifest.Sha256) || string.IsNullOrEmpty(manifest.Url))
            {
                NoteFailure($"manifest at {manifestUrl} is incomplete");
                return false;
            }

            // 4. Install it? Strictly newer on automatic, which is what makes a replayed or
            // rolled-back manifest a no-op; exactly the pin, forward or back, when pinned.
            var decision = UpdatePins.Decide(AgentConfig.Version, manifest.Version, pin,
                                              canDowngrade: true, VersionUtil.Compare);
            if (decision == UpdateDecision.PinMismatch)
            {
                NoteFailure($"manifest for pinned {pin} names {manifest.Version} -- refusing");
                return false;
            }
            if (decision != UpdateDecision.Install)
            {
                _lastFailure = null;   // reached a decision: the next failure is news again
                return false;
            }

            // 5. Restart-loop guard, which now expires (AgentConfig.GiveUpRetryAfter).
            var rs = _state.LoadRestartState();
            if (GuardBlocks(rs, manifest.Version, DateTimeOffset.UtcNow))
            {
                NoteFailure($"giving up on {rs!.Target} after {rs.Count} restarts; "
                            + $"retrying after {AgentConfig.GiveUpRetryAfter.TotalHours:0} h");
                return false;
            }
            // A target the guard gave up on and has now let through starts its count again,
            // so one more failure does not immediately put it back to sleep for a day.
            if (rs is not null && rs.Target == manifest.Version
                && rs.Count >= AgentConfig.MaxChainRestarts)
            {
                _log.LogInformation("[update] retrying {Target}, last given up on at {At}",
                    rs.Target, rs.LastAttemptUtc?.ToString("u") ?? "an older build");
                rs = null;
            }
            _lastFailure = null;

            _log.LogInformation("[update] {Cur} -> {New}", AgentConfig.Version, manifest.Version);

            // 6. Download to staging and verify the hash against the SIGNED sha256.
            Directory.CreateDirectory(AgentConfig.UpdateStagingDir);
            var stagedPath = Path.Combine(AgentConfig.UpdateStagingDir,
                $"fleethub-agent-{manifest.Version}");
            var bytes = await _download.GetByteArrayAsync(manifest.Url, ct);
            var actualSha = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
            if (!string.Equals(actualSha, manifest.Sha256.Trim().ToLowerInvariant(),
                               StringComparison.Ordinal))
            {
                NoteFailure($"sha256 mismatch for {manifest.Version} (got {actualSha}, "
                            + $"want {manifest.Sha256}) -- aborting");
                return false;
            }
            await File.WriteAllBytesAsync(stagedPath, bytes, ct);

            // 6b. Re-verify FROM DISK, because what step 7 moves into place is the file rather
            // than the byte array checked above. The staging directory is 0700 under
            // /var/lib (see StateDirectory), but this path ends at "systemd runs these bytes
            // as root", so it does not rest on the mode alone: a pre-created file at
            // stagedPath survives our write with its owner's rights intact.
            var stagedSha = Convert.ToHexString(
                SHA256.HashData(await File.ReadAllBytesAsync(stagedPath, ct))).ToLowerInvariant();
            if (!string.Equals(stagedSha, actualSha, StringComparison.Ordinal))
            {
                _log.LogWarning("[update] staged file changed after verification -- aborting");
                TryDelete(stagedPath);
                return false;
            }

            // 6c. Make it executable BEFORE it is moved into place. File.Move preserves the
            // staged file's mode, and a file written by this process is 0600 -- which at the
            // unit's ExecStart is a service that cannot start, on a machine that has just
            // renamed away the binary that worked.
            File.SetUnixFileMode(stagedPath,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute
                | UnixFileMode.GroupRead | UnixFileMode.GroupExecute
                | UnixFileMode.OtherRead | UnixFileMode.OtherExecute);

            // 7. Swap: rename the running binary aside, move the new one into place. Renaming
            // a running executable is allowed on Linux -- the kernel holds the inode open --
            // so the process carries on running from the file it started with.
            var currentPath = Environment.ProcessPath;
            if (string.IsNullOrEmpty(currentPath))
            {
                _log.LogWarning("[update] cannot resolve the current binary's path");
                return false;
            }
            var oldPath = currentPath + ".old";
            TryDelete(oldPath);
            File.Move(currentPath, oldPath);
            try
            {
                File.Move(stagedPath, currentPath);
            }
            catch (Exception move)
            {
                // **The gap between those two moves is the only place this method can leave a
                // machine with no agent binary at all.** The second move throws (a full disk,
                // an interrupted cross-filesystem copy -- /var/lib and /opt are frequently
                // different mounts, which makes this MORE likely here than on Windows), and
                // the running process survives because it is already in memory. Nothing looks
                // wrong until the next start, which is a reboot, and by then the agent that
                // would have carried a fix is the thing that is missing. Recovery is a visit.
                _log.LogWarning(move,
                    "[update] could not move the new binary into place -- restoring the previous one");
                try
                {
                    // overwrite:true because a cross-filesystem move degrades to copy-then-
                    // delete, so a full disk can leave a partial file at currentPath. The
                    // two-argument overload would then refuse for a reason unrelated to the
                    // original failure, and so would an operator following the message below.
                    File.Move(oldPath, currentPath, overwrite: true);
                }
                catch (Exception restore)
                {
                    _log.LogError(restore,
                        "[update] RESTORE FAILED -- mv {Old} {Cur} by hand (replacing it if it "
                        + "exists); this unit cannot start until you do", oldPath, currentPath);
                }
                TryDelete(stagedPath);
                return false;
            }

            // 8. Record the attempt. systemd brings the agent back on any exit, so the count is
            // the only thing standing between a broken build and a ten-second restart loop.
            _state.SaveRestartState(new RestartState
            {
                Target = manifest.Version,
                Count = (rs?.Target == manifest.Version ? rs!.Count : 0) + 1,
                LastAttemptUtc = DateTimeOffset.UtcNow,
            });
            _log.LogInformation("[update] staged {New}; exiting {Code} so systemd starts it",
                manifest.Version, AgentConfig.RestartExitCode);
            return true;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            return false;   // the host is stopping; not a failure worth a line
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            // Warning, once per distinct failure: a 404 (wrong branch, unpushed release), a
            // proxy refusal or a timeout is the difference between "nothing to install" and
            // "cannot look", and only the journal can tell an operator which one this box is.
            NoteFailure($"check failed: {e.Message}");
            return false;
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "[update] unexpected error");
            return false;
        }
    }

    private void TryDeleteOldBinary()
    {
        if (Environment.ProcessPath is { Length: > 0 } path) TryDelete(path + ".old");
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); }
        catch { /* nothing here is worth failing an update that otherwise worked */ }
    }
}
