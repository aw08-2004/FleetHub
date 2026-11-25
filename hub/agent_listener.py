"""The agent surface: the only paths the hub's internet-facing listener serves (hub 1.188.0).

WHY THIS EXISTS. Until 1.188.0 one listener served everything, so the hostname agents on
internet PCs need was also the hostname of the console's login page, the whole operator API
and Socket.IO. An operator who wanted agents outside the LAN had to publish the console with
them. serve.py can now run a second listener (FLEETHUB_AGENT_PORT) wrapped in
`AgentSurfaceFilter`, and the reverse proxy publishes only that one. The console listener
stays on the LAN, reached through CONSOLE_URL.

The silent failures this module exists to prevent, one in each direction:
  * **a console path answering on the agent port.** Nothing errors: the login page simply
    renders for the whole internet again, which is exactly what the split was for. The
    allow-list below is explicit and segment-exact, and a path it does not name gets a 404
    before the Flask app sees the request.
  * **an agent path refused on the agent port.** Agents do not alert on a 404 -- they retry
    and fall quiet -- so a route missing here reads as a fleet that went offline for no
    reason. tests/test_agent_listener.py walks the live url_map so a new `/api/agent/` route
    is covered by the prefix, and a new public route outside it fails the test until someone
    decides on purpose which side it belongs to.

**The filter is additive, never a gate.** Every route it lets through still carries its own
`agent_auth`, bearer check, onboarding code or peer token. Nothing here may be read as
"reachable on the agent port, therefore trusted".

WHY ONE PROCESS WITH TWO LISTENERS, NOT TWO PROCESSES. The remote relay, the PTY bridge,
live folder copy, the command long-poll wakeups, the Socket.IO emits from after_request hooks
and the schedulers all live in this process's memory (remote_relay.py says so; the Dockerfile
says "exactly one replica, one process, never workers"). An agent-only second process would
hold half of every relay and silently break all of them.

WHY FILTER BY PATH, NOT BY BLUEPRINT. fleet, files, backups, bios, remote, tickets,
folder_relay, packages and others each register agent and operator routes on one blueprint,
and app.py holds routes on `app` directly. There is no blueprint boundary to cut along; there
is a path boundary, because every agent route was already put under /api/agent/.

Rejected:
  * nginx `location` rules alone. They are the second layer (docs/installation.md), but one
    mistyped or reordered block re-exposes the console with no symptom. This filter makes the
    console unreachable on the agent port whatever the proxy in front of it does.
  * A second Flask app holding only the agent blueprints -- see "filter by path" above.
  * VPN-only agents. Internet PCs without a tunnel are the requirement this serves.
  * 403 for a refused path. 404 says nothing about whether a console exists behind the port.

Flask-free on purpose, like every model half: a WSGI callable and a pure predicate.
"""
from urllib.parse import unquote

# Whole paths the agent listener serves. Exact match only: "/api/report" must never let
# "/api/reports" or "/api/report/x" through.
EXACT_PATHS = frozenset({
    # Telemetry ingest (app.py). Bearer-optional until security.require_enrolled_telemetry
    # is on -- SECURITY.MD says why that setting matters more once this port is public.
    "/api/report",
    # The public onboarding wizard and the two calls its script and page make (roadmap #29).
    # NOT /api/onboarding/codes, which is the console's admin API under the same prefix.
    "/onboarding",
    "/api/onboarding/check",
    "/api/onboarding/redeem",
})

# Path prefixes the agent listener serves. Each ends in "/" so a match is always a whole
# segment: "/api/agent/" never matches "/api/agents".
PREFIXES = (
    # Every agent route: enroll, heartbeat, commands, PTY, remote relay, files, backups...
    "/api/agent/",
    # The generated install launchers (.ps1/.cmd/.sh) an onboarding code is redeemed through.
    "/onboarding/run/",
    "/api/onboarding/status/",
    # The Android device-owner APK, token in the URL (provisioning_web.py). NOT
    # /provisioning or /api/provisioning/*, which are the console's.
    "/provisioning/apk/",
    # Cross-hub sharing (roadmap #15): another hub calls these with its peer token.
    "/api/peer/",
)

# The static files the standalone onboarding page loads, and nothing else under /static/.
# Named file by file rather than by folder: /static/js/ also holds the whole console's
# script, which is not secret but is a map of every operator route. The test parses
# templates/onboarding.html so a new url_for('static', ...) there cannot be forgotten here.
STATIC_FILES = frozenset({
    "favicon.svg",
    "apple-touch-icon.png",
    "vendor/fonts/inter.css",
    "vendor/fonts/inter-latin-wght-normal.woff2",
    "vendor/fonts/inter-latin-ext-wght-normal.woff2",
    "css/tokens.css",
    "css/base.css",
    "css/login.css",
    "css/onboarding.css",
    "js/i18n.js",
    "js/onboarding.js",
})

STATIC_PREFIX = "/static/"


def is_agent_path(path):
    """True when `path` (a WSGI PATH_INFO) belongs to the agent surface.

    Anything that could make the filter and the router disagree is refused outright, before
    matching: a `..` or `.` segment, an empty segment (`//`), a backslash, a NUL, or a
    percent-escape that still decodes to something else. PATH_INFO arrives already decoded,
    so a surviving escape is a double-encoding -- no agent sends one, and refusing it costs
    nothing, whereas reasoning about which layer decodes it a second time is how traversal
    bugs are born.

    **The price: no agent route may carry free text in its path.** A filename holding a
    literal "%" arrives here as "%" and is refused, so the agent gets a 404 it retries
    silently. Every path variable on this surface today is an id the hub minted (hex,
    URL-safe token, onboarding code); a name goes in the query string or the body, which this
    never sees. tests/test_agent_listener.py fails on a path variable outside that set."""
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    if "\\" in path or "\x00" in path or unquote(path) != path:
        return False
    segments = path[1:].split("/")
    # A trailing slash leaves one empty last segment ("/api/agent/" -> [..., ""]); every
    # other empty segment is a "//".
    if any(seg in ("", ".", "..") for seg in segments[:-1]) or segments[-1] in (".", ".."):
        return False
    if path in EXACT_PATHS:
        return True
    if path.startswith(STATIC_PREFIX):
        return path[len(STATIC_PREFIX):] in STATIC_FILES
    return any(path.startswith(p) and len(path) > len(p) for p in PREFIXES)


class AgentSurfaceFilter:
    """WSGI middleware: pass the agent surface through to `app`, answer everything else with
    a bare 404 that never reaches it.

    Checks PATH_INFO, the same value werkzeug routes on, so what this lets through and what
    the router dispatches are the same string. Wraps the outermost application (outside
    ProxyFix and engine.io), so /socket.io/ is refused here rather than by engine.io."""

    _BODY = b"Not Found"

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if is_agent_path(environ.get("PATH_INFO", "")):
            return self.app(environ, start_response)
        start_response("404 Not Found", [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(self._BODY))),
        ])
        return [self._BODY]
