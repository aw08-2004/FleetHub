"""Operating-system bucketing -- what a machine is running, out of what it told us.

Moved out of app.py (hub 1.160.0) so rules.py can use it: an operator could build a device
group from the directory's `operatingSystem` attribute and from nothing else, which left every
machine that is not in AD -- and every one whose AD record lags its last upgrade -- out of a
"Windows PCs" group. rules.py is deliberately app-free (importing app there is circular and
drags Flask in), so the one function both the Dashboard tally and the rules engine read has to
live somewhere neither of them owns.

**One bucketer, not two.** The alternative was a second, rules-only copy of the needles below.
Rejected: the Dashboard would then say "312 Windows 11" while a group built on the same words
held 309, and the first operator to compare them would trust neither.

**Still a fuzzy DISPLAY bucketer over a caption a remote machine chose.** Using it to TARGET
machines is fine -- a target is narrowed to its author's scope on the resolved membership, so
a machine that lies about its OS can only move itself into or out of work its operator could
already send it. Using it to AUTHORIZE anything is not; capabilities.platform_of is the closed
vocabulary for that.

Kept free of Flask so it can be unit-tested in isolation.
"""

#: The buckets a fleet is counted in. Deliberately coarse: the Dashboard's question is
#: "what are we still running", and a breakdown by build number answers a different one
#: (that lives on the machine page, where the exact caption and build are shown as-is).
OS_BUCKETS = ("windows_11", "windows_10", "windows_legacy", "windows_server", "linux", "android",
              "unknown")

#: Substrings that put a caption in a bucket, checked in order. Server is first because a
#: Server caption also contains "Windows"; android is before linux because an Android device
#: IS running a Linux kernel and a caption that mentioned both would otherwise be filed with
#: the servers. Without this entry every phone counted as `unknown` -- the agent reports
#: "Android 16" honestly rather than smuggling "Linux" into the caption, which would have
#: shown an operator a string the device never said (roadmap #23).
#:
#: The linux needles carry a second job: a Linux agent reports its distribution's own
#: PRETTY_NAME verbatim rather than smuggling the word "Linux" into a string the machine never
#: said (roadmap #22), so the bucket has to recognise the distributions that do not name the
#: kernel. "Pop!_OS 22.04 LTS", "elementary OS 7", "Zorin OS 16", "NixOS", "EndeavourOS",
#: "Deepin", "Solus" and "KDE neon" all bucketed as `unknown` until this list grew -- which put
#: real, managed machines in the Dashboard's leftovers pile beside the ones nothing is known
#: about, and that pile is the one an operator is supposed to be able to act on.
#:
#: **A list of names is the wrong SHAPE of fix and is kept anyway**, so say why. The right one
#: is to bucket on what the machine reported about ITSELF -- os-release's ID_LIKE, or the
#: platform the agent now sends on its heartbeat (capabilities.platform_of). Both were
#: rejected here: ID_LIKE is a field no agent sends today and would need a wire change, a
#: column and a migration to reach this function, and platform_of is a per-machine DB read in
#: a function whose whole contract is that it costs no round-trip (see the comment on
#: machine_status above). The needles cost nothing, are one line to extend when somebody
#: enrolls something exotic, and their failure mode is the one this bucketing already has
#: everywhere else: a caption nobody recognised reads as `unknown` rather than as wrong.
_OS_MATCHES = (
    ("windows_server", ("windows server", "server 20")),
    ("windows_11", ("windows 11",)),
    ("windows_10", ("windows 10",)),
    # Everything before 10, in one bucket. **These used to be counted as Windows 10**: no
    # needle matched, so the caption fell through to the build check below, and build 7601
    # is "below 22000". The machines most worth finding -- out of support, unpatched --
    # were hidden inside the healthiest-looking bar on the Dashboard. One bucket rather
    # than one per release because the operator's question is "how many are left", and the
    # machine page shows the exact caption anyway.
    ("windows_legacy", ("windows 8", "windows 7", "windows vista", "windows xp")),
    ("android", ("android",)),
    ("linux", ("linux", "ubuntu", "debian", "red hat", "rhel", "centos", "fedora",
               "suse", "alma", "rocky",
               # Distributions whose PRETTY_NAME does not contain "Linux" at all. Only
               # Linux ones: a FreeBSD-based appliance is not a Linux machine, and filing
               # it here to avoid an `unknown` would put a caption in a bucket that lies
               # about what is running.
               "pop!_os", "pop os", "elementary", "zorin", "nixos", "endeavour",
               "deepin", "solus", "kde neon", "raspbian", "armbian")),
)

#: The first Windows 11 build. Below it is 10, at or above it is 11 -- see normalize_os.
_WINDOWS_11_BUILD = 22000
#: The first Windows 10 build (10240, July 2015). Below it a build says nothing about 10 vs
#: 11 -- it is an older Windows -- so normalize_os files it as windows_legacy rather than
#: letting "below 22000" make it a Windows 10 machine.
_WINDOWS_10_BUILD = 10240


def _bucket_from_caption(caption):
    """Bucket a raw OS name, or None if it says nothing recognisable."""
    text = str(caption or "").strip().lower()
    if not text:
        return None
    for bucket, needles in _OS_MATCHES:
        if any(needle in text for needle in needles):
            return bucket
    return None


def _bucket_by_build(bucket, caption, os_build):
    """Let a parseable build decide 10 vs 11 vs legacy -- see normalize_os for why it beats
    the caption. Only 10-vs-11 is decided this way; a Server or Linux caption is already
    unambiguous, and no build number would improve it. A caption that matched nothing is
    only trusted to be Windows if it says so."""
    if bucket not in (None, "windows_10", "windows_11"):
        return bucket
    build = build_number(os_build)
    if build is None or (bucket is None and "windows" not in caption.lower()):
        return bucket
    if build >= _WINDOWS_11_BUILD:
        return "windows_11"
    if build >= _WINDOWS_10_BUILD:
        return "windows_10"
    return "windows_legacy"


def normalize_os(os_caption, os_build, ad_os):
    """Decide which operating system a machine is running, and say where that came from.

    Returns {"bucket", "label", "source"}: `bucket` is one of OS_BUCKETS and is what the
    fleet is counted by; `label` is the raw string to show a human, which is ARBITRARY TEXT
    from a remote machine and must be rendered with textContent; `source` is "agent", "ad"
    or None.

    Two decisions worth spelling out.

    THE BUILD NUMBER BEATS THE CAPTION. Early Windows 11 machines report a caption that
    still says "Windows 10" -- Microsoft shipped 11 as build 22000 of the same product and
    did not change the product name in WMI. A console that trusted the caption would report
    a fleet as years behind on an upgrade it had already done. So a parseable build decides
    between 10 and 11, and the caption only decides what it alone can (Server, Linux).

    AD IS THE FALLBACK, NOT THE OTHER WAY ROUND. machine_info.ad_os is the directory sync's
    copy of the same fact, and it is what every machine has until its agent is new enough to
    report OS itself. It is coarse (a raw `operatingSystem` attribute, no build number) and
    it is as fresh as the last sync rather than the last heartbeat, so the agent wins
    wherever both exist. `source` is how the UI can say which one it is looking at.
    """
    caption = str(os_caption or "").strip()
    if caption:
        bucket = _bucket_by_build(_bucket_from_caption(caption), caption, os_build)
        return {"bucket": bucket or "unknown", "label": caption, "source": "agent"}

    directory = str(ad_os or "").strip()
    if directory:
        return {"bucket": _bucket_from_caption(directory) or "unknown",
                "label": directory, "source": "ad"}

    return {"bucket": "unknown", "label": None, "source": None}


#: The family a bucket belongs to. "Every Windows PC" is the group people actually build, and
#: spelling it as four buckets means a group that silently misses the day a fifth Windows
#: bucket is added -- the same reason rules.py carries disk.max_used_pct beside the per-drive
#: variables. `unknown` has no family: a machine nobody has described is not "not Windows".
FAMILY_WINDOWS = "windows"
FAMILY_LINUX = "linux"
FAMILY_ANDROID = "android"
_FAMILY_BY_BUCKET = {
    "windows_11": FAMILY_WINDOWS, "windows_10": FAMILY_WINDOWS,
    "windows_legacy": FAMILY_WINDOWS, "windows_server": FAMILY_WINDOWS,
    "linux": FAMILY_LINUX, "android": FAMILY_ANDROID,
}


def family_of(bucket):
    """windows / linux / android for a bucket, or None for `unknown` (or anything else)."""
    return _FAMILY_BY_BUCKET.get(bucket)


def build_number(os_build):
    """The leading integer of a reported build ("22631", "22631.4317"), or None.

    The same parse normalize_os uses to tell 10 from 11, exposed so a rule can say
    `os.build < 19045` without re-deciding what a build string means. A Linux agent's build is
    a kernel release ("6.8.0-45-generic"), which parses to 6 -- harmless, since nobody compares
    a Linux build against a Windows one, and refusing it would make the variable UNKNOWN on
    machines that did report one.
    """
    try:
        return int(str(os_build or "").strip().split(".")[0])
    except (TypeError, ValueError):
        return None
