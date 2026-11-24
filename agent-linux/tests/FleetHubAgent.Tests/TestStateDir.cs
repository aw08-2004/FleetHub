using System.Runtime.CompilerServices;

namespace FleetHubAgent.Tests;

/// <summary>
/// Points the agent's state directory at a fresh temp folder for this whole test run.
///
/// **AgentConfig resolves its state root once, at type initialisation**, from
/// FLEETHUB_STATE_DIR_FOR_TESTS. A test that set the variable itself would lose to whichever
/// test touched AgentConfig first, and the loser would read and write the real
/// /var/lib/fleethub/agent on a box that has the agent installed -- a test rewriting the channel
/// of the machine it runs on. A module initializer runs before any test, so it always wins.
/// </summary>
internal static class TestStateDir
{
    /// <summary>The xunit collection for tests that read or write files under the state root.
    /// One collection so they never run in parallel with each other: a channel test flipping
    /// the file to beta mid-way through another test's two reads would fail that test for a
    /// reason that has nothing to do with it.</summary>
    public const string Collection = "agent state directory";

    [ModuleInitializer]
    internal static void Init()
    {
        var dir = Path.Combine(Path.GetTempPath(), "fleethub-agent-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        Environment.SetEnvironmentVariable(AgentConfig.StateDirOverrideVar, dir);
    }
}

[CollectionDefinition(TestStateDir.Collection, DisableParallelization = true)]
public sealed class TestStateDirCollection;
