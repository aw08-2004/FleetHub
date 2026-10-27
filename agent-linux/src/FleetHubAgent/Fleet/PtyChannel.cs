namespace FleetHubAgent.Fleet;

/// <summary>One item the console queued for an open terminal: raw bytes the operator typed
/// (<c>Kind == "data"</c>) or a terminal resize (<c>Kind == "resize"</c>).</summary>
public readonly record struct PtyInputItem(int Seq, string Kind, string Data, short Cols, short Rows);

/// <summary>Result of one keystroke poll. <c>Ok</c> is false when the hub could not be reached
/// at all -- distinct from "reached it, nothing typed", because only the former should make
/// the session give up. <c>Closing</c> is the console asking us to end the terminal;
/// <c>Gone</c> is the hub no longer knowing the session (404).</summary>
public readonly record struct PtyInputBatch(
    bool Ok, IReadOnlyList<PtyInputItem> Items, int NextSeq, bool Closing, bool Gone);

/// <summary>The terminal half of the hub API -- the same three /api/agent/pty/* endpoints the
/// Windows agent uses, unchanged, because hub/terminal.py does not care which platform the
/// bytes come from. An interface for the same reason as IOutputSink: PtySessionRunner's
/// polling, cursors and backoff are worth testing without a live hub.</summary>
public interface IPtyChannel
{
    Task<PtyInputBatch> PullPtyInputAsync(string sessionId, int afterSeq, CancellationToken ct);
    Task<bool> PostPtyOutputAsync(string sessionId, int seq, string chunk, CancellationToken ct);
    Task ReportPtyClosedAsync(string sessionId, string reason, CancellationToken ct);
}
