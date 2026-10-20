using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.State;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Enrolling a phone by spending a one-time onboarding code (roadmap #29).
///
/// **The silent failure this exists to catch is a code spent and a credential lost.** The hub
/// mints the token once and burns the code in the same transaction, so a device that sent the
/// right body and then failed to keep the answer is a code nobody can use again and a phone
/// that is still not enrolled -- with the hub's onboarding list saying it is. These pin the
/// request the hub reads, the persist-before-adopt order, and the mapping from the hub's
/// refusals to what the person holding the phone is told to do next.
/// </summary>
[Collection(nameof(HubUrlTests))]
public sealed class OnboardingCodeTests : IDisposable
{
    public OnboardingCodeTests() => AgentConfig.Configure("https://hub.example.com");
    public void Dispose() => AgentConfig.Configure(AgentConfig.DefaultHubBase);

    [Theory]
    [InlineData("AB3K7Q", "AB3K7Q")]
    [InlineData("ab3k7q", "AB3K7Q")]
    [InlineData(" ab3-k7q ", "AB3K7Q")]
    [InlineData("AB3 K7Q", "AB3K7Q")]
    public void A_code_is_normalised_the_way_the_hub_stores_it(string typed, string expected)
        => Assert.Equal(expected, FleetClient.NormalizeOnboardingCode(typed));

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("AB3K7")]      // too short
    [InlineData("AB3K7QQ")]    // too long
    [InlineData("AB0K7Q")]     // 0 is not in the alphabet -- it reads as O
    [InlineData("ABIK7Q")]     // nor is I, which reads as 1
    [InlineData("AB3K7!")]
    public void A_code_the_hub_could_never_have_minted_is_refused_locally(string? typed)
        => Assert.Null(FleetClient.NormalizeOnboardingCode(typed));

    [Fact]
    public async Task Spending_a_code_sends_the_code_the_name_and_the_serial_and_keeps_the_identity()
    {
        var handler = new RecordingHandler(HttpStatusCode.OK,
            """{"agent_id":"agent-7","token":"tok-7"}""");
        var store = new FakeStateStore();
        var client = Client(store, handler);

        var outcome = await client.RedeemOnboardingCodeAsync("ab3-k7q", "ssaid-1", default);

        Assert.Equal(CodeRedeemOutcome.Enrolled, outcome);
        Assert.True(client.IsEnrolled);
        Assert.Equal("https://hub.example.com/api/onboarding/redeem", handler.Uri?.ToString());
        var body = JsonNode.Parse(handler.Body!)!;
        Assert.Equal("AB3K7Q", body["code"]!.GetValue<string>());
        Assert.Equal("Pixel-9-abcd1234", body["machine"]!.GetValue<string>());
        Assert.Equal("ssaid-1", body["serial"]!.GetValue<string>());
        // Persisted, not merely held: a process kill must not cost the device its credential.
        Assert.True(new AgentState(store).LoadIdentity().IsEnrolled);
        // And never the fleet secret: the whole point of a code is that it is not that.
        Assert.Null(body["enrollment_secret"]);
    }

    [Fact]
    public async Task A_credential_that_cannot_be_stored_is_not_adopted()
    {
        var store = new FakeStateStore();
        var client = Client(store, new RecordingHandler(HttpStatusCode.OK,
            """{"agent_id":"agent-7","token":"tok-7"}"""));
        store.WritesFail = true;

        var outcome = await client.RedeemOnboardingCodeAsync("AB3K7Q", "s", default);

        Assert.Equal(CodeRedeemOutcome.NotStored, outcome);
        Assert.False(client.IsEnrolled);
    }

    [Theory]
    [InlineData(HttpStatusCode.NotFound, CodeRedeemOutcome.Invalid)]
    [InlineData(HttpStatusCode.Conflict, CodeRedeemOutcome.NameTaken)]
    [InlineData(HttpStatusCode.BadRequest, CodeRedeemOutcome.Rejected)]
    [InlineData(HttpStatusCode.TooManyRequests, CodeRedeemOutcome.Unreachable)]
    [InlineData(HttpStatusCode.InternalServerError, CodeRedeemOutcome.Unreachable)]
    public async Task Each_refusal_maps_to_its_own_next_step(HttpStatusCode status,
        CodeRedeemOutcome expected)
    {
        var client = Client(new FakeStateStore(),
            new RecordingHandler(status, """{"error":"nope"}"""));

        Assert.Equal(expected, await client.RedeemOnboardingCodeAsync("AB3K7Q", "s", default));
        Assert.False(client.IsEnrolled);
    }

    [Fact]
    public async Task A_malformed_code_never_reaches_the_hub()
    {
        // The redeem route is rate limited per client; a typo must not spend one of the
        // attempts somebody standing at the device will need for the real code.
        var handler = new RecordingHandler(HttpStatusCode.OK, "{}");
        var outcome = await Client(new FakeStateStore(), handler)
            .RedeemOnboardingCodeAsync("AB0", "s", default);

        Assert.Equal(CodeRedeemOutcome.Malformed, outcome);
        Assert.Null(handler.Uri);
    }

    [Fact]
    public async Task An_enrolled_device_spends_nothing()
    {
        var store = new FakeStateStore();
        new AgentState(store).SaveIdentity(new AgentIdentity { AgentId = "a", Token = "t" });
        var handler = new RecordingHandler(HttpStatusCode.OK, "{}");

        var outcome = await Client(store, handler).RedeemOnboardingCodeAsync("AB3K7Q", "s", default);

        Assert.Equal(CodeRedeemOutcome.AlreadyEnrolled, outcome);
        Assert.Null(handler.Uri);
    }

    private static FleetClient Client(FakeStateStore store, HttpMessageHandler handler)
    {
        var state = new AgentState(store);
        return new FleetClient(NullLogger<FleetClient>.Instance, state,
            new MachineNameProvider(state, "Pixel-9-abcd1234"), capabilities: null, handler);
    }

    private sealed class RecordingHandler(HttpStatusCode status, string reply) : HttpMessageHandler
    {
        public Uri? Uri { get; private set; }
        public string? Body { get; private set; }

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken ct)
        {
            Uri = request.RequestUri;
            Body = request.Content is null ? null : await request.Content.ReadAsStringAsync(ct);
            return new HttpResponseMessage(status)
            {
                Content = new StringContent(reply, Encoding.UTF8, "application/json"),
            };
        }
    }
}
