"""The agent-only listener's path filter (hub/agent_listener.py, hub 1.188.0).

The silent failures this file exists to catch, one per direction:

  * **the console reachable from the internet again.** The agent listener is the port the
    reverse proxy publishes. A console route, Socket.IO or the console's scripts answering
    there produces no error anywhere -- the login page simply renders for everyone, which is
    the exposure the split was made to remove. Traversal and look-alike paths are the other
    way to get there.
  * **an agent route refused on the agent port.** Agents do not alert on a 404; they retry
    and fall quiet, so a missing prefix reads as a fleet that went offline for no reason.
    Checked from both ends: the hub's live url_map, and the path literals the agents
    themselves are compiled with.

A new route outside /api/agent/ and /api/peer/ that the filter lets through fails
test_public_surface_is_exactly_the_decided_one until someone decides on purpose which side
of the split it belongs to.

Runs against the real app for the url_map; the filter itself is exercised as plain WSGI.
"""
import os
import re
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "hub"))

_TMPDIR = tempfile.mkdtemp(prefix="hub-agent-listener-test-")
os.environ["HUB_LOG_DIR"] = os.path.join(_TMPDIR, "logs")
os.environ.pop("CONSOLE_URL", None)
os.chdir(_TMPDIR)

import agent_listener
import app

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def sample_path(rule):
    """A concrete path for a url_map rule: every <converter:name> becomes one segment 'x'."""
    return re.sub(r"<[^>]+>", "x", rule)


# The public surface outside the two prefix-owned namespaces (/api/agent/, /api/peer/), as
# url_map rules. Changing this set is a decision about what the internet can reach.
DECIDED_PUBLIC_RULES = {
    "/api/report",
    "/onboarding",
    "/api/onboarding/check",
    "/api/onboarding/redeem",
    "/api/onboarding/status/<code>",
    "/onboarding/run/<code>.ps1",
    "/onboarding/run/<code>.cmd",
    "/onboarding/run/<code>.sh",
}


def test_every_agent_route_passes():
    print("\n-- every /api/agent/ and /api/peer/ route in the live url_map is served --")
    refused = [r.rule for r in app.app.url_map.iter_rules()
               if r.rule.startswith(("/api/agent/", "/api/peer/"))
               and not agent_listener.is_agent_path(sample_path(r.rule))]
    count = sum(1 for r in app.app.url_map.iter_rules() if r.rule.startswith("/api/agent/"))
    check(f"the url_map has agent routes to check ({count})", count > 30)
    check(f"none of them is refused (refused: {refused})", not refused)


def test_public_surface_is_exactly_the_decided_one():
    print("\n-- what the filter lets through outside /api/agent/ and /api/peer/ --")
    allowed = set()
    for rule in app.app.url_map.iter_rules():
        if rule.rule.startswith(("/api/agent/", "/api/peer/")):
            continue
        if agent_listener.is_agent_path(sample_path(rule.rule)):
            allowed.add(rule.rule)
    apk = {r for r in allowed if r.startswith("/provisioning/apk/")}
    check(f"the APK download is served ({sorted(apk)})", len(apk) == 1)
    surplus = allowed - DECIDED_PUBLIC_RULES - apk
    missing = DECIDED_PUBLIC_RULES - allowed
    check(f"nothing public beyond the decided set (surplus: {sorted(surplus)})", not surplus)
    check(f"everything decided is still a live, served route (missing: {sorted(missing)})",
          not missing)


def test_agent_sources_are_served():
    print("\n-- every hub path the agents are compiled with is served --")
    roots = [os.path.join(_REPO, d) for d in ("agent/src", "agent-shared", "agent-linux/src",
                                              "agent-android")]
    literal = re.compile(r'"[^"\n]*?(/api/[A-Za-z0-9_{}./-]*)')
    found = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("bin", "obj", "build", ".gradle")]
            for name in filenames:
                if not name.endswith((".cs", ".kt")):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        stripped = line.lstrip()
                        if stripped.startswith(("//", "*", "/*")):
                            continue
                        for frag in literal.findall(line):
                            found.add(frag)
    check(f"the agent sources were found and name hub paths ({len(found)})", len(found) > 10)
    refused = []
    for frag in sorted(found):
        path = re.sub(r"\{[^}]*\}?.*$", "x", frag).rstrip(".")
        if path.endswith("/"):
            path += "x"
        if not agent_listener.is_agent_path(path):
            refused.append(frag)
    check(f"none is refused by the agent listener (refused: {refused})", not refused)


def test_console_is_refused():
    print("\n-- console, Socket.IO and the console's assets are refused --")
    for path in ("/", "/login", "/login/google", "/auth/callback", "/logout", "/settings",
                 "/machine/PC-1", "/socket.io/", "/api/machines", "/api/fleet/summary",
                 "/api/fleet/commands", "/api/hub/update", "/api/language",
                 "/api/tokens/exchange", "/download/manifest.json", "/app/pair",
                 "/invite/abc", "/onboarding-codes", "/api/onboarding/codes",
                 "/api/onboarding/codes/1/revoke", "/provisioning", "/api/provisioning/apk",
                 "/static/js/app.js", "/static/css/app.css", "/static/", "/api/agent",
                 "/api/agents/x", "/api/reports", "/api/report/", "/api/report/x",
                 "/onboarding/", "/onboarding/run/", "/api/remote/session/x/relay/up"):
        check(f"refused: {path}", not agent_listener.is_agent_path(path))


def test_tricks_are_refused():
    print("\n-- traversal and look-alikes --")
    for path in ("/api/agent/../machines", "/api/agent/x/../../machines", "/api/agent/./x",
                 "//api/agent/x", "/api/agent//x", "/api/report/..", "/api/agent/%2e%2e/x",
                 "/api/agent/%2E%2E/machines", "/api/agent/x%2fy", "/api/agent\\..\\machines",
                 "/api/agent/x\x00", "/static/../templates/base.html",
                 "/static/js/../js/app.js", "api/agent/x", "", None,
                 "/onboarding/run/../../login"):
        check(f"refused: {path!r}", not agent_listener.is_agent_path(path))
    for path in ("/api/agent/heartbeat", "/api/agent/commands/42/result",
                 "/api/agent/files/stream/abc/chunk/3", "/onboarding/run/ABCD-1234.ps1",
                 "/api/onboarding/status/ABCD-1234", "/api/peer/catalogue",
                 "/static/css/onboarding.css", "/api/report"):
        check(f"served: {path}", agent_listener.is_agent_path(path))


def test_onboarding_page_assets_are_served():
    print("\n-- the public onboarding page can load everything it links --")
    with open(os.path.join(_REPO, "hub", "templates", "onboarding.html"), encoding="utf-8") as fh:
        page = fh.read()
    linked = set(re.findall(r"url_for\('static',\s*filename='([^']+)'\)", page))
    check(f"the template links static files ({len(linked)})", len(linked) >= 5)
    with open(os.path.join(_REPO, "hub", "static", "vendor", "fonts", "inter.css"),
              encoding="utf-8") as fh:
        fonts = {"vendor/fonts/" + f for f in re.findall(r"url\('\./([^']+)'\)", fh.read())}
    check(f"inter.css names its font files ({len(fonts)})", len(fonts) >= 1)
    missing = sorted(f for f in linked | fonts
                     if not agent_listener.is_agent_path("/static/" + f))
    check(f"every one is served (missing: {missing})", not missing)
    absent = sorted(f for f in agent_listener.STATIC_FILES
                    if not os.path.isfile(os.path.join(_REPO, "hub", "static", f)))
    check(f"every allow-listed static file exists (absent: {absent})", not absent)
    check("the page opens no language picker, so /api/language staying refused costs nothing",
          "language-picker" not in page)


def test_wsgi_filter():
    print("\n-- AgentSurfaceFilter as WSGI --")
    reached = []

    def inner(environ, start_response):
        reached.append(environ["PATH_INFO"])
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"inner"]

    wrapped = agent_listener.AgentSurfaceFilter(inner)

    def call(path):
        status = {}

        def start_response(s, headers):
            status["s"] = s
            status["h"] = dict(headers)

        body = b"".join(wrapped({"PATH_INFO": path, "REQUEST_METHOD": "GET"}, start_response))
        return status["s"], status["h"], body

    s, _, body = call("/api/agent/heartbeat")
    check("an agent path reaches the app", s.startswith("200") and body == b"inner")
    reached.clear()
    s, headers, body = call("/login")
    check("a console path is a 404", s.startswith("404"))
    check("...that never reached the app", reached == [])
    check("...with a body that names nothing about the hub",
          body == b"Not Found" and headers.get("Content-Length") == str(len(body)))
    reached.clear()
    s, _, _ = call("/socket.io/")
    check("Socket.IO is refused before engine.io sees it", s.startswith("404") and reached == [])


def test_console_url_defaults_to_hub_url():
    print("\n-- CONSOLE_URL --")
    check("unset, it is HUB_URL, so an unsplit hub is unchanged",
          app.CONSOLE_URL == app.HUB_URL)


def main():
    test_every_agent_route_passes()
    test_public_surface_is_exactly_the_decided_one()
    test_agent_sources_are_served()
    test_console_is_refused()
    test_tricks_are_refused()
    test_onboarding_page_assets_are_served()
    test_wsgi_filter()
    test_console_url_defaults_to_hub_url()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
