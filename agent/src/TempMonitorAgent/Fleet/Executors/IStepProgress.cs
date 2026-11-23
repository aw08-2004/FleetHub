namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// Where an executor says which phase of its work it is in -- the console's PDQ-style
/// "step 3 of 5" view for a package deployment (roadmap #5, agent 3.53.0).
///
/// Structured, rather than the hub reading the `[deploy] step i/n` lines already in the
/// output stream, because the stream is capped and pruned and its wording belongs to this
/// build: the deploy an operator most needs to follow -- the long, noisy, failing one -- is
/// the one whose markers would fall off the end. See hub/packages.py, LIVE PROGRESS.
///
/// The hub draws the step list itself from the recipe it sent, so an event carries only
/// WHERE (phase + 1-based ordinal) and WHAT HAPPENED. Ordinals must count exactly as
/// packages.progress_skeleton does: downloads are the file payloads only, in list order.
///
/// <see cref="Report"/> must never block or throw: the install is the work, and its
/// narration is not allowed to slow it down or break it.
/// </summary>
public interface IStepProgress
{
    void Report(StepEvent e);

    /// <summary>Set when the console has asked for this run to stop. The executor is
    /// already running under a token this cancels; this is for wording the result.</summary>
    bool AbortRequested { get; }
}

/// <summary>One phase transition. <see cref="OutSeq"/> is filled in by the reporter, not
/// the executor: it is the output stream's chunk number at the moment of the event, which
/// is what lets the console show one step's log.</summary>
public readonly record struct StepEvent(
    string Phase, int Ordinal, string State, int? ExitCode = null, string? Message = null,
    int? OutSeq = null);

/// <summary>The wire words, in one place. They are the hub's PHASES and STEP_STATES; a
/// word the hub does not know is refused there with a 400, never silently stored.</summary>
public static class StepPhase
{
    public const string Download = "download";
    public const string Step = "step";
    public const string Detect = "detect";
}

public static class StepState
{
    public const string Running = "running";
    public const string Ok = "ok";
    public const string Failed = "failed";
    /// <summary>Failed, but the step's continue_on_error carried the deploy on.</summary>
    public const string Skipped = "skipped";
    public const string Aborted = "aborted";
}

/// <summary>
/// Tracks the phase in flight, so a failure that escapes as an exception still closes the
/// phase it happened in rather than leaving the console showing it "running" forever.
/// One per deploy attempt; null-safe on a null sink (an executor run from a test, or an
/// agent path that has no reporter).
/// </summary>
public sealed class PhaseTracker(IStepProgress? sink)
{
    private (string Phase, int Ordinal)? _open;

    public bool AbortRequested => sink?.AbortRequested ?? false;

    public void Begin(string phase, int ordinal)
    {
        _open = (phase, ordinal);
        sink?.Report(new StepEvent(phase, ordinal, StepState.Running));
    }

    public void End(string state, int? exitCode = null, string? message = null)
    {
        if (_open is not { } open) return;
        _open = null;
        sink?.Report(new StepEvent(open.Phase, open.Ordinal, state, exitCode, message));
    }

    /// <summary>Close whatever is still open after an exception or an abort.</summary>
    public void Fail(string? message, bool aborted) =>
        End(aborted ? StepState.Aborted : StepState.Failed, message: message);
}
