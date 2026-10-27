#!/usr/bin/env bash
# FleetHub onboarding script for a Linux machine (roadmap #29). Rendered by the hub for ONE
# onboarding code -- see hub/onboarding_web.py -- and piped into bash as root by the command the
# onboarding page shows. The Linux counterpart of onboard.ps1, step for step:
#
#   1. Installs the agent with agent-linux/install/install.sh and NO enrollment secret. That
#      installer checks the binary against the fleet's signed manifest, exactly as a
#      technician's install does.
#   2. Only then spends the code. A failed install must not burn a one-time code.
#   3. Writes the credential the hub returned to /var/lib/fleethub/agent/agent.json, 0600
#      root, and restarts the service so the agent loads it.
# The token goes from the hub straight into that file. It is never printed.
#
# Everything is inside main(), called on the last line, for the reason install.sh gives: this
# arrives through a pipe, and a connection that drops halfway must define half a function and
# run nothing -- not half an install.

set -euo pipefail

main() {
    local hub_url={{ hub_url }}
    local code={{ code }}
    local installer_url={{ installer_url }}
    local service=fleethub-agent
    local state_dir=/var/lib/fleethub/agent
    local identity="$state_dir/agent.json"

    fail() { local msg="$*"; printf '\nFleetHub onboarding failed: %s\n' "$msg" >&2; exit 1; }
    step() { local msg="$*"; printf '\n== %s\n' "$msg"; }
    have() { local tool="$1"; command -v "$tool" >/dev/null 2>&1; }

    echo "FleetHub onboarding"
    echo "  Hub: $hub_url"

    [[ "$(id -u)" -eq 0 ]] || fail "this must run as root. Pipe it to 'sudo bash', or become root with 'su -' first."
    # The credential travels in the redeem response, so the hub has to be the hub. Plain http is
    # allowed only to this machine itself, which is a developer's hub. Same rule as onboard.ps1.
    case "$hub_url" in
        https://*|http://localhost|http://localhost:*|http://localhost/*|http://127.0.0.1|http://127.0.0.1:*|http://127.0.0.1/*) ;;
        *) fail "the hub address is not https ($hub_url). Ask your administrator to set HUB_URL to the hub's https address." ;;
    esac
    # curl or wget, whichever the machine has -- Debian minimal and OpenMediaVault ship only
    # wget. The command that fetched THIS script needed one of them already.
    have curl || have wget || fail "neither curl nor wget is installed."

    # A private working folder: mktemp -d is 0700 and root's, so nothing else on the machine
    # can swap the installer between the download and the run.
    local work
    work="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$work'" EXIT

    step "Installing the FleetHub agent"
    if have curl; then curl -fsSL -o "$work/install.sh" "$installer_url"
    else wget -q -O "$work/install.sh" "$installer_url"
    fi || fail "could not download the agent installer from $installer_url. The onboarding code was NOT used."
    # FLEETHUB_ONBOARDING tells install.sh that no secret is expected, so it does not end on its
    # TELEMETRY ONLY warning. An install.sh too old to know the variable ignores it, which is
    # why the line after the install says the same thing in case that warning did appear.
    FLEETHUB_ONBOARDING=1 bash "$work/install.sh" --hub "$hub_url" \
        || fail "the agent installer failed. The onboarding code was NOT used; fix the problem above and run this again."
    echo "  (A 'no secret' or 'telemetry only' notice above is expected: the code replaces the secret.)"

    step "Enrolling this machine"
    # The name the agent will report: the hostname up to the first dot, which is what .NET's
    # Environment.MachineName gives the agent (AgentConfig.MachineName). A different spelling
    # here would mint a credential for a name the agent never sends.
    local machine
    machine="$(uname -n)"
    machine="${machine%%.*}"
    case "$machine" in
        ''|*[!A-Za-z0-9._-]*) fail "this machine's hostname ('$machine') is not one the hub accepts. Give it a plain name and run this again." ;;
        *) ;;
    esac
    # The BIOS serial, read where the agent reads it (SystemInfo, /sys/class/dmi/id). The hub
    # uses it only when this name already has history, to check it is the same hardware -- see
    # onboarding._check_known_machine. JSON-escaped, and control characters dropped.
    local serial=""
    if [[ -r /sys/class/dmi/id/product_serial ]]; then
        serial="$(tr -d '\000-\037' < /sys/class/dmi/id/product_serial | sed 's/^ *//; s/ *$//; s/\\/\\\\/g; s/"/\\"/g')"
    fi
    printf '{"code":"%s","machine":"%s","serial":"%s"}' "$code" "$machine" "$serial" > "$work/redeem.json"

    local ok=0
    if have curl; then
        local status
        status="$(curl -sS -o "$work/answer.json" -w '%{http_code}' -X POST \
            -H 'Content-Type: application/json' --data-binary @"$work/redeem.json" \
            "$hub_url/api/onboarding/redeem")" || status=000
        [[ "$status" == "200" ]] && ok=1
    else
        wget -q --content-on-error -O "$work/answer.json" \
            --header='Content-Type: application/json' --post-file="$work/redeem.json" \
            "$hub_url/api/onboarding/redeem" && ok=1
    fi
    if [[ "$ok" -ne 1 ]]; then
        # The hub's own sentence when it sent one -- "already enrolled" needs a different fix
        # from "this code has expired", and only the body says which.
        local reason
        reason="$(sed -n 's/.*"error" *: *"\([^"]*\)".*/\1/p' "$work/answer.json" 2>/dev/null | head -1)"
        fail "the hub refused to enrol this machine: ${reason:-no answer from $hub_url}"
    fi
    local agent_id token
    agent_id="$(sed -n 's/.*"agent_id" *: *"\([A-Za-z0-9_-]*\)".*/\1/p' "$work/answer.json" | head -1)"
    token="$(sed -n 's/.*"token" *: *"\([A-Za-z0-9_-]*\)".*/\1/p' "$work/answer.json" | head -1)"
    [[ -n "$agent_id" && -n "$token" ]] || fail "the hub's answer did not contain a credential."

    step "Saving this machine's credential"
    systemctl stop "$service" 2>/dev/null || true
    install -d -m 0700 "$state_dir"
    if [[ -f "$identity" ]]; then
        # A machine that was enrolled before. Its old credential is kept beside the new one
        # rather than deleted, so a mistake here is recoverable by hand; the hub simply stops
        # hearing from the old agent id. Same as onboard.ps1.
        mv -f "$identity" "$identity.previous-$(date +%Y%m%d%H%M%S)"
    fi
    # install(1) with the mode set, from stdin: the file never exists at a wider mode, not even
    # for a moment, which a redirect followed by chmod would allow.
    printf '{"agent_id":"%s","token":"%s"}' "$agent_id" "$token" \
        | install -m 0600 -o root -g root /dev/stdin "$identity"
    agent_id=""; token=""
    rm -f "$work/answer.json"

    systemctl start "$service"
    printf '\nDone. %s is enrolled and should appear in FleetHub within a minute.\n' "$machine"
}

# Load-bearing: see the note at the top.
main "$@"
