using System.Text.Json;
using TempMonitorAgent.Remote;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The rules that decide whether a live remote session follows the interactive session when it
/// moves (sign-out, switch user, RDP taking the console). The decision is pure so these can run
/// without a Windows session, a helper process, or a state directory -- the parts that need
/// those are the kill and the re-injection, which are exercised on real hardware.
/// </summary>
public class RemoteSupervisorTests
{
    private const uint None = 0xFFFFFFFF;

    [Fact]
    public void PinnedSession_NeverFollowsTheConsole()
    {
        // The operator asked for session 3. Session 5 owning the console now is not a reason to
        // abandon what they asked for -- watching one user's session while another uses the
        // machine is a legitimate thing to be doing.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Stay,
                     RemoteSessionSupervisor.DecideMove(
                         follows: false, current: 3, target: 5, pending: 5, moves: 0));
    }

    [Fact]
    public void UnchangedSelection_StaysPut()
    {
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Stay,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 2, target: 2, pending: null, moves: 0));
    }

    [Fact]
    public void NothingInteractiveAnywhere_StaysPut()
    {
        // A normal beat or two mid-sign-out. Moving "nowhere" would kill a helper that is about
        // to be perfectly fine.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Stay,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 2, target: None, pending: null, moves: 0));
    }

    [Fact]
    public void ANewTarget_IsWaitedOutOnceBeforeMoving()
    {
        // First sighting: remember it, do nothing.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Wait,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 1, target: 4, pending: null, moves: 0));

        // Same answer next pass: the switch has settled, move.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Move,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 1, target: 4, pending: 4, moves: 0));
    }

    [Fact]
    public void ASelectionStillWobbling_KeepsWaiting()
    {
        // Windows moved us from "session 3 next" to "session 4 next" between passes -- it has
        // not settled, so neither do we.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Wait,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 1, target: 4, pending: 3, moves: 0));
    }

    [Fact]
    public void RepeatedMoves_AreCappedRatherThanThrashed()
    {
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Move,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 1, target: 2, pending: 2, moves: 9));
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Capped,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 1, target: 2, pending: 2, moves: 10));
    }

    [Fact]
    public void Record_RoundTripsTheSessionItIsFollowing()
    {
        var record = new RemoteSessionSupervisor.LiveSessionRecord
        {
            SessionId = "abc123",
            Params = "{\"session_id\":\"abc123\"}",
            Pid = 4242,
            WindowsSession = 7,
            Auto = true,
            SessionMoves = 2,
            StartedAt = DateTimeOffset.UtcNow,
        };

        var back = JsonSerializer.Deserialize<RemoteSessionSupervisor.LiveSessionRecord>(
            JsonSerializer.Serialize(record))!;

        Assert.Equal(7u, back.WindowsSession);
        Assert.True(back.Auto);
        Assert.Equal(2, back.SessionMoves);
        Assert.Equal(4242, back.Pid);
    }

    [Fact]
    public void ARecordWrittenByTheOlderAgent_DoesNotFollowAnything()
    {
        // A session already in flight across an agent update has no auto flag. Defaulting to
        // "pinned" leaves it exactly as it behaved before the update rather than moving a helper
        // on the strength of a field nobody wrote.
        var back = JsonSerializer.Deserialize<RemoteSessionSupervisor.LiveSessionRecord>(
            """{"session_id":"old","params":"{}","pid":10,"started_at":"2026-07-31T00:00:00+00:00"}""")!;

        Assert.False(back.Auto);
        Assert.False(back.FollowSignIn);
        Assert.Equal(0u, back.WindowsSession);
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Stay,
                     RemoteSessionSupervisor.DecideMove(
                         back.Auto || back.FollowSignIn, back.WindowsSession,
                         target: 5, pending: 5, moves: 0));
    }

    // ---- A pinned logon screen follows the sign-in (issue #170) ----------------------------
    // The operator picked "Session 1 -- logon screen" to sign in, signed in, and the sign-in
    // reconnected their existing session to the console. A plain pinned helper stayed behind in
    // the abandoned session, streaming a desktop nobody would see again: a black screen.

    private static SessionProbe.SessionInfo Session(uint id, string user, bool console,
                                                    int state = SessionProbe.WtsActive) =>
        new() { SessionId = id, User = user, IsConsole = console, State = state,
                StationName = console ? "Console" : "RDP-Tcp#3" };

    [Fact]
    public void PinningTheConsoleLogonScreen_FollowsTheSignIn()
    {
        var sessions = new[] { Session(1, "", console: true, SessionProbe.WtsConnected) };
        Assert.True(RemoteSessionSupervisor.FollowsSignIn(pinned: 1, landed: 1, sessions));
    }

    [Fact]
    public void PinningAUsersSession_StillMeansThatSession()
    {
        // Shadowing a signed-in user while somebody else uses the console is legitimate; the
        // fix for the logon screen must not turn every pin into "follow the console".
        var sessions = new[] { Session(2, "alice", console: true) };
        Assert.False(RemoteSessionSupervisor.FollowsSignIn(pinned: 2, landed: 2, sessions));
    }

    [Fact]
    public void PinningAnRdpLogonScreen_StaysPinned()
    {
        var sessions = new[]
        {
            Session(1, "", console: true, SessionProbe.WtsConnected),
            Session(3, "", console: false, SessionProbe.WtsConnected),
        };
        Assert.False(RemoteSessionSupervisor.FollowsSignIn(pinned: 3, landed: 3, sessions));
    }

    [Fact]
    public void AnAutoSession_IsNotASignInFollower()
    {
        // Auto already follows, on auto-selection's terms; the sign-in rule is for pins only.
        var sessions = new[] { Session(1, "", console: true, SessionProbe.WtsConnected) };
        Assert.False(RemoteSessionSupervisor.FollowsSignIn(pinned: null, landed: 1, sessions));
    }

    [Fact]
    public void SignInTarget_StaysWhileTheHelperIsStillAtTheConsole()
    {
        // Still at the logon screen, with an active RDP session next door. Auto would pick the
        // RDP session -- the very reason the logon screen is not simply made "auto".
        var waiting = new[]
        {
            Session(1, "", console: true, SessionProbe.WtsConnected),
            Session(4, "bob", console: false),
        };
        Assert.Equal(1u, RemoteSessionSupervisor.SignInTarget(1, waiting));

        // Signed in right where it stood: same session, nothing to move.
        var signedInPlace = new[] { Session(1, "alice", console: true) };
        Assert.Equal(1u, RemoteSessionSupervisor.SignInTarget(1, signedInPlace));
    }

    [Fact]
    public void SignInTarget_FollowsTheConsoleToTheSignedInSession()
    {
        // The sign-in reconnected alice's session 2 to the console; session 1 lingers.
        var sessions = new[]
        {
            Session(1, "", console: false, SessionProbe.WtsDisconnected),
            Session(2, "alice", console: true),
        };
        uint target = RemoteSessionSupervisor.SignInTarget(1, sessions);
        Assert.Equal(2u, target);
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Wait,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 1, target, pending: null, moves: 0));
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Move,
                     RemoteSessionSupervisor.DecideMove(
                         follows: true, current: 1, target, pending: target, moves: 0));
    }

    [Fact]
    public void SignInTarget_IgnoresAConsoleStillBetweenSessions()
    {
        // Mid-transition: the console shows another logon screen, or there is no console at
        // all. Neither is a sign-in, and killing a working helper for it would be a black gap.
        var anotherLogon = new[]
        {
            Session(1, "", console: false, SessionProbe.WtsDisconnected),
            Session(5, "", console: true, SessionProbe.WtsConnected),
        };
        Assert.Equal(0xFFFFFFFFu, RemoteSessionSupervisor.SignInTarget(1, anotherLogon));
        Assert.Equal(0xFFFFFFFFu, RemoteSessionSupervisor.SignInTarget(
            1, new[] { Session(1, "", console: false, SessionProbe.WtsDisconnected) }));
    }

    [Fact]
    public void Record_RoundTripsTheSignInFlag()
    {
        var record = new RemoteSessionSupervisor.LiveSessionRecord
        {
            SessionId = "s", Params = "{}", WindowsSession = 1, FollowSignIn = true,
            StartedAt = DateTimeOffset.UtcNow,
        };
        var back = JsonSerializer.Deserialize<RemoteSessionSupervisor.LiveSessionRecord>(
            JsonSerializer.Serialize(record))!;
        Assert.True(back.FollowSignIn);
        Assert.False(back.Auto);
    }
}
