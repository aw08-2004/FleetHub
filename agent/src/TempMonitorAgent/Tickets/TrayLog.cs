namespace TempMonitorAgent.Tickets;

/// <summary>
/// The tray's own log, <c>%LOCALAPPDATA%\FleetHub\tray.log</c>.
///
/// **Not the agent's log**, and not Serilog: the tray runs as the user, who cannot write to
/// the service's %ProgramData% folder -- and must not be able to, because that folder is
/// ACL-locked against exactly this (State/StateDirectory). The supervisor's "keeps exiting"
/// warning points here, so the reason a tray died is on the PC where somebody can read it.
/// One file, rolled once at <see cref="MaxBytes"/>; it records connects, refusals and
/// failures, never what anybody typed.
/// </summary>
internal static class TrayLog
{
    private const long MaxBytes = 256 * 1024;
    private static readonly object Lock = new();

    public static string Folder =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FleetHub");

    private static string LogPath => Path.Combine(Folder, "tray.log");

    public static void Write(string line)
    {
        lock (Lock)
        {
            try
            {
                Directory.CreateDirectory(Folder);
                var info = new FileInfo(LogPath);
                if (info.Exists && info.Length > MaxBytes)
                    File.Move(LogPath, LogPath + ".1", overwrite: true);
                File.AppendAllText(LogPath, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {line}{Environment.NewLine}");
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                // Nowhere else to say it.
            }
        }
    }
}
