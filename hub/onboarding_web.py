"""Flask surface for onboarding codes (roadmap #29) -- the Onboarding admin page, and the
public `/onboarding` wizard an unenrolled PC is set up through.

**The admin routes are gated on MANAGE_ONBOARDING, a capability of its own.** Handing out a
code is handing out the right to put a machine into this fleet, which is narrower than
MANAGE_SETTINGS (the Android provisioning QR sits there because it carries the shared
secret itself) and is exactly the job a helpdesk technician does without being trusted with
the hub's configuration. None of these routes names a machine, so `access.require` is the
gate and not `require_machine` -- there is no machine scope to check a code against until a
machine has spent it.

**Everything under `/onboarding` and `/api/onboarding/{check,redeem,status}` is public**, for
the reason `/invite/<code>` is: the caller is by definition not signed in. What keeps that
safe is written down here so it cannot drift:

  * **The browser never receives a token.** The wizard calls `check` (consumes nothing) and
    `status` (says "waiting" / "enrolled" / "online" and, once spent, the machine name).
    Only `redeem` returns a credential, and it is called by the install script running
    elevated on the PC, after the agent is installed.
  * **Every refusal reads the same** -- onboarding.INVALID_MESSAGE, 404 -- for an unknown,
    spent, expired, revoked or malformed code, so the routes are not an oracle.
  * **Rate-limited per client AND in total.** Per client keyed on `remote_addr`, which
    ProxyFix has already resolved from the one trusted hop; NOT the raw X-Forwarded-For the
    shared limiter reads, which a caller can set to anything and so rotate past a per-IP
    limit. The global ceiling is what bounds a guesser with many addresses: at
    GLOBAL_PER_MINUTE a 31^6 space takes years per code, and every code is dead in a day.
  * **No CSRF token**, deliberately: these routes carry no cookie authority. login_required
    is what enforces CSRF on this hub, and these routes are not behind it. Bodies are still
    read with get_json(silent=True), which needs Content-Type: application/json.

The install script is a Jinja template rendered per code (templates/onboarding/). It lives
under hub/ because hub self-update and the container image ship only hub/; the agent
installer it fetches is the repo's own agent/install/agent-install.ps1, which verifies the
binary against the signed manifest exactly as a technician's install does.
"""
import re

from flask import Blueprint, Response, jsonify, render_template, request

import onboarding
import permissions
import permissions_web
import refusals
from rate_limit import RateLimiter

#: Per client: a wizard user types a code a few times, runs the script, and is done.
PER_CLIENT_PER_MINUTE = 10
#: Across every client. See the module docstring for why this one exists at all.
GLOBAL_PER_MINUTE = 300
#: The wizard polls status every few seconds while it waits for the PC to appear.
STATUS_PER_MINUTE = 40
#: Status also answers 404 for an unknown code, so it gets a global ceiling of its own.
#: Higher than the code routes' because every waiting wizard polls it.
STATUS_GLOBAL_PER_MINUTE = 1200

#: Where the install script fetches the agent installer from. The same source install.ps1
#: uses -- the repository's main branch -- so an onboarded PC runs exactly the installer a
#: technician would, signature check included.
AGENT_INSTALLER_URL = (
    "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/install/agent-install.ps1")

_code_limiter = RateLimiter()
_SAFE_URL = re.compile(r"https?://[A-Za-z0-9.\-:\[\]/_]+")
_status_limiter = RateLimiter()


def _client_key(req):
    return req.remote_addr or "0.0.0.0"


def _global_key(_req):
    return "onboarding:global"


def _ps_quote(value):
    """A PowerShell single-quoted literal. Inside '...' only a quote is special, and it is
    escaped by doubling -- so this is the whole escaping rule, not an approximation of it."""
    return "'" + str(value).replace("'", "''") + "'"


def run_command(script_url):
    """The PowerShell command that fetches and runs the install script, elevated. ONE copy,
    used by both the .cmd launcher and the wizard's copy-paste box, so the two cannot drift.

    **The script is saved into a folder only SYSTEM and Administrators can write to.** The
    first file-based version saved it as %TEMP%\\FleetHub-Onboard.ps1 and then ran it
    elevated; %TEMP% is the same user's folder, so any non-elevated process of that user could
    swap the file between the download and the run, and its replacement ran as admin. A random
    name alone was rejected: a process watching the folder sees the new file anyway. The
    folder is a fresh GUID under %ProgramData% and gets its ACL in the same call that creates
    it -- CreateDirectory(path, DirectorySecurity) -- so there is no moment it is open. Users
    may create folders in %ProgramData% but cannot touch another's, and cannot guess the GUID
    to pre-create it.

    Saved and run with -File under RemoteSigned, never downloaded into memory and run from
    there: that is the dropper pattern antivirus blocks (Kaspersky did). Built from single
    quotes only, with no percent sign, because the .cmd carries it inside one double-quoted
    cmd.exe argument; _SAFE_URL keeps the URL to characters that survive that too. The admin
    check is by SID, because the group's NAME is translated on a German or Spanish Windows.
    """
    return (
        "& { "
        "if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]"
        "::GetCurrent()).IsInRole([Security.Principal.SecurityIdentifier]'S-1-5-32-544')) "
        "{ Write-Host 'Run this from PowerShell opened with Run as administrator.' "
        "-ForegroundColor Red; return }; "
        "[Net.ServicePointManager]::SecurityProtocol = "
        "[Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; "
        "$d = Join-Path $env:ProgramData ('FleetHub-Onboard-' + [guid]::NewGuid().ToString('N')); "
        "$s = New-Object Security.AccessControl.DirectorySecurity; "
        "$s.SetAccessRuleProtection($true, $false); "
        "foreach ($i in 'S-1-5-18', 'S-1-5-32-544') { $s.AddAccessRule((New-Object "
        "Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier $i), "
        "'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))) }; "
        "[void][IO.Directory]::CreateDirectory($d, $s); "
        "$f = Join-Path $d 'onboard.ps1'; "
        "try { "
        f"Invoke-WebRequest -UseBasicParsing -Uri {_ps_quote(script_url)} -OutFile $f -ErrorAction Stop; "
        "Unblock-File -LiteralPath $f; "
        "powershell -NoProfile -ExecutionPolicy RemoteSigned -File $f "
        "} catch { Write-Host ('Could not download the onboarding script: ' + $_.Exception.Message "
        "+ ' Check the connection, or ask for a new code.') -ForegroundColor Red "
        "} finally { Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue } }"
    )


def create_onboarding_blueprint(db_path, login_required, access, hub_url="",
                                valid_machine_name=None, unsigned_reporters=None,
                                machine_record=None):
    """Build the onboarding Blueprint.

    `hub_url` is the hub's public origin, used for the link the admin copies and the URL the
    install script reports to -- assembled here for the reason invites_web gives.
    `valid_machine_name` is app.is_valid_machine_name, passed in so a name the telemetry
    route would refuse cannot be minted a credential here. `unsigned_reporters` is
    app.unsigned_reporters: the machines that would be cut off by enrolled-only telemetry,
    which is the list an operator needs codes for. `machine_record` is app's view of
    machine_info, which redeem uses to bind a name with history to its BIOS serial -- see
    onboarding._check_known_machine.
    """
    bp = Blueprint("onboarding", __name__)
    base = str(hub_url or "").rstrip("/")
    _add_admin_routes(bp, db_path, login_required, access, base, unsigned_reporters)
    _add_public_routes(bp, db_path, base, valid_machine_name, machine_record)
    return bp


def _refused():
    return jsonify({"error": onboarding.INVALID_MESSAGE}), 404


def _add_admin_routes(bp, db_path, login_required, access, base, unsigned_reporters):
    """The Onboarding page and its API, behind MANAGE_ONBOARDING. Split from the public half
    so the two gates are never read in the same screenful as each other's routes."""
    manage = access.require(permissions.MANAGE_ONBOARDING)

    @bp.route("/onboarding-codes", methods=["GET"])
    @login_required
    @manage
    def onboarding_page():
        return render_template("onboarding_codes.html")

    @bp.route("/api/onboarding/codes", methods=["GET"])
    @login_required
    @manage
    def list_codes():
        return jsonify({
            "codes": onboarding.list_codes(db_path),
            "unsigned_reporters": unsigned_reporters() if unsigned_reporters else [],
            "ttl_choices": list(onboarding.TTL_CHOICES),
            "default_ttl": onboarding.DEFAULT_TTL_SECONDS,
        }), 200

    @bp.route("/api/onboarding/codes", methods=["POST"])
    @login_required
    @manage
    def create_code():
        data = request.get_json(silent=True) or {}
        try:
            code_id, code = onboarding.create_code(
                db_path, label=data.get("label"),
                ttl_seconds=data.get("ttl_seconds", onboarding.DEFAULT_TTL_SECONDS),
                actor=permissions_web.current_actor())
        except onboarding.OnboardingError as e:
            return refusals.refuse(e)
        row = onboarding.get_code(db_path, code_id)
        # The only response that ever carries the code -- see onboarding.create_code.
        row["code"] = code
        row["link"] = f"{base}/onboarding?code={code}"
        return jsonify(row), 201

    @bp.route("/api/onboarding/codes/<code_id>/revoke", methods=["POST"])
    @login_required
    @manage
    def revoke_code(code_id):
        try:
            onboarding.revoke_code(db_path, code_id, actor=permissions_web.current_actor())
        except KeyError:
            return jsonify({"error": "unknown code"}), 404
        return jsonify(onboarding.get_code(db_path, code_id)), 200



def _add_public_routes(bp, db_path, base, valid_machine_name, machine_record):
    """The unauthenticated wizard and the routes its install script calls. See the module
    docstring for what each may return. Each route is one line over a module-level function,
    so the decorators -- the limits -- are what is read here."""
    @bp.route("/onboarding", methods=["GET"])
    def onboarding_wizard():
        """The page a person at an unenrolled PC opens. **No login gate.** Renders nothing
        about this hub beyond its name; the code in `?code=` is only pre-filled into the
        input, and checked by the page's own call to /api/onboarding/check."""
        return render_template("onboarding.html", hub_url=base)

    @bp.route("/api/onboarding/check", methods=["POST"])
    @_code_limiter.limit(f"{GLOBAL_PER_MINUTE}/minute", key_func=_global_key)
    @_code_limiter.limit(f"{PER_CLIENT_PER_MINUTE}/minute", key_func=_client_key)
    def check_code():
        return _check(db_path, base, request.get_json(silent=True) or {})

    @bp.route("/onboarding/run/<code>.ps1", methods=["GET"])
    @_code_limiter.limit(f"{GLOBAL_PER_MINUTE}/minute", key_func=_global_key)
    @_code_limiter.limit(f"{PER_CLIENT_PER_MINUTE}/minute", key_func=_client_key)
    def run_script(code):
        return _script_response(db_path, base, code)

    @bp.route("/onboarding/run/<code>.cmd", methods=["GET"])
    @_code_limiter.limit(f"{GLOBAL_PER_MINUTE}/minute", key_func=_global_key)
    @_code_limiter.limit(f"{PER_CLIENT_PER_MINUTE}/minute", key_func=_client_key)
    def run_cmd(code):
        return _launcher_response(db_path, base, code)

    @bp.route("/api/onboarding/redeem", methods=["POST"])
    @_code_limiter.limit(f"{GLOBAL_PER_MINUTE}/minute", key_func=_global_key)
    @_code_limiter.limit(f"{PER_CLIENT_PER_MINUTE}/minute", key_func=_client_key)
    def redeem_code():
        return _redeem(db_path, request.get_json(silent=True) or {}, request.remote_addr,
                       valid_machine_name, machine_record)

    @bp.route("/api/onboarding/status/<code>", methods=["GET"])
    @_status_limiter.limit(f"{STATUS_GLOBAL_PER_MINUTE}/minute", key_func=_global_key)
    @_status_limiter.limit(f"{STATUS_PER_MINUTE}/minute", key_func=_client_key)
    def code_status(code):
        state = onboarding.status_for_code(db_path, code)
        return (jsonify(state), 200) if state is not None else _refused()


def _check(db_path, base, data):
    found = onboarding.check_code(db_path, data.get("code"))
    if found is None:
        return _refused()
    code = onboarding.normalize_code(data.get("code"))
    return jsonify({
        "label": found["label"],
        "expires_at": found["expires_at"],
        "code": code,
        "script_url": f"{base}/onboarding/run/{code}.ps1",
        "cmd_url": f"{base}/onboarding/run/{code}.cmd",
        "command": run_command(f"{base}/onboarding/run/{code}.ps1"),
    }), 200


def _live_code(db_path, code):
    """The normalised code if it is live right now, else None."""
    norm = onboarding.normalize_code(code)
    if norm is None or onboarding.check_code(db_path, norm) is None:
        return None
    return norm


def _script_response(db_path, base, code):
    """The install script for one live code, as text/plain, fetched by run_command.

    Rendered only for a code that is live right now, so a guessed or spent code gets a 404
    and not a script. The code is re-normalised and must match the alphabet before it is put
    into the script, and the hub URL is quoted with _ps_quote -- the template never
    interpolates anything else.
    """
    norm = _live_code(db_path, code)
    if norm is None:
        return _refused()
    body = render_template("onboarding/onboard.ps1",
                           hub_url=_ps_quote(base), code=_ps_quote(norm),
                           installer_url=_ps_quote(AGENT_INSTALLER_URL))
    resp = Response(body, mimetype="text/plain")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _launcher_response(db_path, base, code):
    """A double-clickable launcher: elevates, then fetches and runs the .ps1 above.

    A .cmd rather than a .ps1 download because Windows opens a downloaded .ps1 in Notepad,
    and the person double-clicking it is not going to know why. It carries the code and the
    hub URL and nothing else -- no token, nothing that outlives the code. CRLF line endings,
    because cmd.exe misparses labels and blocks in an LF file.
    """
    norm = _live_code(db_path, code)
    if norm is None:
        return _refused()
    script_url = f"{base}/onboarding/run/{norm}.ps1"
    # cmd.exe has no quoting rule that survives every character (% and " both escape it), so
    # rather than escape the URL this refuses one that needs escaping. HUB_URL is operator
    # config and an ordinary one always passes.
    if not _SAFE_URL.fullmatch(script_url):
        return jsonify({"error": "HUB_URL contains characters the launcher cannot carry; "
                                 "use the PowerShell command instead"}), 409
    body = render_template("onboarding/onboard.cmd", command=run_command(script_url))
    resp = Response(body.replace("\r\n", "\n").replace("\n", "\r\n"),
                    mimetype="application/octet-stream")
    resp.headers["Content-Disposition"] = f'attachment; filename="FleetHub-Onboard-{norm}.cmd"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _redeem(db_path, data, ip, valid_machine_name, machine_record):
    """Spend a code and return this machine's credential, once. Called by the install script,
    never by the wizard page -- see the module docstring. `serial` is the PC's BIOS serial,
    which onboarding.redeem_code checks when the name already has history."""
    machine = str(data.get("machine") or "").strip()
    if valid_machine_name is not None and not valid_machine_name(machine):
        return jsonify({"error": "invalid machine name"}), 400
    try:
        agent_id, token = onboarding.redeem_code(
            db_path, data.get("code"), machine, ip=ip, serial=data.get("serial"),
            machine_record=machine_record)
    except onboarding.NameEnrolled as e:
        # A live code, a taken name: say so, so the person knows the code is unspent.
        return refusals.refuse(e, 409)
    except onboarding.OnboardingError as e:
        if str(e) == onboarding.INVALID_MESSAGE:
            return _refused()
        return refusals.refuse(e)
    resp = jsonify({"agent_id": agent_id, "token": token})
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200
