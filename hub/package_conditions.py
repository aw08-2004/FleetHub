"""Package conditions -- the `if` in a package's step list, and the "only download when" on a
payload (roadmap #5).

A package was a straight line: every payload downloaded, every step run, in order. That
cannot say the thing a driver rollout most needs to say -- *on a Latitude 5440 install this
pack, on a ThinkPad X1 that one, on anything else stop* -- so operators wrote it as one big
PowerShell step, and every machine downloaded every model's driver pack to run 2% of it. This
module is the grammar that replaces that: a condition over what the machine IS (model,
manufacturer, OS, architecture, ...), what is ON it (a file, a registry value, an installed
program), what a PowerShell probe answers, and what the previous step returned.

**The AST is the rules engine's, unchanged** (`rules.validate_condition` / `rules.evaluate`):
`{"op": "and"|"or"|"not", "nodes": [...]}` over leaves `{"var", "cmp", "value"}`, with the
same operators, the same case-insensitive text equality and the same wildcard `matches`. One
condition language in the console, not two that disagree about what `==` means on "LENOVO".
Two things are added for packages, and only here:

  * **A `version` kind**, so `os.version >= 10.0.22631` compares 10.0.9200 below 10.0.22631
    instead of above it, which is what text comparison does. Alert rules never needed it and
    stay untouched; changing `rules.py` would have meant re-proving every alert rule in the
    field for a feature that has nothing to do with them.
  * **Probe leaves** -- `{"probe": "file_exists" | "registry_value" | "installed_version" |
    "powershell", ...}` -- which are not variables at all but questions only the machine can
    answer at the moment the step runs. The first three are exactly the detection rule shapes
    (packages.validate_detection), so a package's "is it installed" and "should I install it"
    are written the same way.

**Conditions are evaluated ON THE MACHINE, live, not by the hub from `machine_info`.** The
hub's copy of a model string is read once at agent start and can be months old; a disk swap,
a re-image or a motherboard replacement moves a driver pack's correct answer without telling
anyone. The pack has to match the hardware that is in the chassis NOW, so the agent reads its
own facts when the deploy runs. Variables only the hub knows -- the AD OU, an operator's
custom fields -- are resolved by the hub at dispatch and shipped in `params.facts`
(`hub_facts`). Rejected: evaluating everything hub-side and sending each machine a pre-pruned
step list. It would have worked on every agent already in the field, which is a real argument,
but it answers the hardware question with stale data and cannot answer the file / registry /
PowerShell questions at all.

**UNKNOWN never silently becomes "else".** Three-valued logic comes with the rules engine: a
model the agent could not read makes `hw.model == "20XW"` UNKNOWN, not false. The obvious
two-valued reading -- unknown is false, take the else branch -- is exactly how a fleet ends up
with the generic driver pack installed on every machine whose WMI was having a bad day. So an
`if` whose condition is UNKNOWN FAILS the step by default ("could not decide"); an operator who
genuinely means "anything I can't identify goes down the else branch" says so per `if`
(`on_unknown: "else"`).

Kept free of Flask, like packages.py, so it is testable on its own. Imported BY packages.py;
the one call back into it (detection-shaped probes) is a function-level import, so the two
modules do not need each other at import time.
"""
import re
from collections import namedtuple

import rules

# ================================
# VOCABULARY
# ================================
KIND_TEXT = rules.KIND_TEXT
KIND_NUMBER = rules.KIND_NUMBER
KIND_BOOL = rules.KIND_BOOL
KIND_CHOICE = rules.KIND_CHOICE
KIND_VERSION = "version"

# Where a variable's value comes from. Not cosmetic: it decides who evaluates it (see the
# module docstring), what the console's preview can say about it, and whether it may appear
# in a payload's download condition.
SOURCE_AGENT = "agent"      # read live by the agent when the deploy runs
SOURCE_HUB = "hub"          # resolved by the hub at dispatch, shipped in params.facts
SOURCE_RUNTIME = "runtime"  # produced by the deploy itself (the previous step's exit code)

PackageVar = namedtuple("PackageVar", "name kind group source binding choices")
PackageVar.__new__.__defaults__ = (None, ())

GROUP_HW = "hw"
GROUP_OS = "os"
GROUP_BIOS = "bios"
GROUP_SYS = "sys"
GROUP_AD = "ad"
GROUP_STEP = "step"
GROUP_FIELD = rules.GROUP_FIELD

# The machine's own answers. `binding` is the `{name}` a step can substitute the same value
# by -- `{model}` in a path is how one extract step serves every model when the vendor names
# its folders after them. Every one of these is read by agent/.../PackageConditions.cs, which
# must spell the names identically; a variable listed here the agent does not know reads
# UNKNOWN there, which fails closed (see the module docstring), never open.
#
# `hw.model_name` exists for Lenovo, and it is the reason this list is not just "what
# machine_info already has". Win32_ComputerSystem.Model on a ThinkPad is the machine-type
# code ("21HD0012US"), one per configuration, so a rule written against it needs a list of
# every SKU ever bought. The marketing name an operator thinks in ("ThinkPad X1 Carbon Gen
# 11") is Win32_ComputerSystemProduct.Version. Dell and HP put the readable name in Model, and
# their Version is usually empty, which reads UNKNOWN here like any other absent value.
CHASSIS_CHOICES = ("laptop", "desktop", "server", "tablet", "other")
ARCH_CHOICES = ("x64", "x86", "arm64")

STATIC_VARIABLES = (
    PackageVar("hw.manufacturer", KIND_TEXT, GROUP_HW, SOURCE_AGENT, "manufacturer"),
    PackageVar("hw.model", KIND_TEXT, GROUP_HW, SOURCE_AGENT, "model"),
    PackageVar("hw.model_name", KIND_TEXT, GROUP_HW, SOURCE_AGENT, "model_name"),
    PackageVar("hw.sku", KIND_TEXT, GROUP_HW, SOURCE_AGENT, "sku"),
    PackageVar("hw.serial_number", KIND_TEXT, GROUP_HW, SOURCE_AGENT),
    PackageVar("hw.chassis", KIND_CHOICE, GROUP_HW, SOURCE_AGENT, None, CHASSIS_CHOICES),
    PackageVar("hw.memory_gb", KIND_NUMBER, GROUP_HW, SOURCE_AGENT),
    PackageVar("hw.cpu", KIND_TEXT, GROUP_HW, SOURCE_AGENT),
    PackageVar("os.name", KIND_TEXT, GROUP_OS, SOURCE_AGENT),
    PackageVar("os.version", KIND_VERSION, GROUP_OS, SOURCE_AGENT),
    PackageVar("os.build", KIND_NUMBER, GROUP_OS, SOURCE_AGENT, "os_build"),
    PackageVar("os.display_version", KIND_TEXT, GROUP_OS, SOURCE_AGENT),
    PackageVar("os.arch", KIND_CHOICE, GROUP_OS, SOURCE_AGENT, "arch", ARCH_CHOICES),
    PackageVar("bios.version", KIND_TEXT, GROUP_BIOS, SOURCE_AGENT),
    PackageVar("sys.hostname", KIND_TEXT, GROUP_SYS, SOURCE_AGENT, "hostname"),
    PackageVar("sys.domain", KIND_TEXT, GROUP_SYS, SOURCE_AGENT),
    PackageVar("sys.domain_joined", KIND_BOOL, GROUP_SYS, SOURCE_AGENT),
    PackageVar("sys.agent_version", KIND_VERSION, GROUP_SYS, SOURCE_AGENT),
    PackageVar("ad.ou", KIND_TEXT, GROUP_AD, SOURCE_HUB),
    PackageVar("ad.dn", KIND_TEXT, GROUP_AD, SOURCE_HUB),
    PackageVar("ad.owner", KIND_TEXT, GROUP_AD, SOURCE_HUB),
    # The exit code of the most recent step that started a process. UNKNOWN before the first
    # one, so `step.last_exit_code == 3010` cannot be true by accident at the top of a list.
    PackageVar("step.last_exit_code", KIND_NUMBER, GROUP_STEP, SOURCE_RUNTIME),
)
STATIC_BY_NAME = {v.name: v for v in STATIC_VARIABLES}

# `{name}` -> the variable it substitutes. Read-only: a payload or an extract step claiming
# one of these names would shadow the machine's answer with a file path, so validation refuses
# them as names (packages.validate_source / validate_steps).
FACT_BINDINGS = {v.binding: v.name for v in STATIC_VARIABLES if v.binding}

# Probe leaves. Detection-shaped probes reuse packages.validate_detection for their fields.
PROBE_FILE_EXISTS = "file_exists"
PROBE_REGISTRY_VALUE = "registry_value"
PROBE_INSTALLED_VERSION = "installed_version"
PROBE_POWERSHELL = "powershell"
PROBE_KINDS = (PROBE_FILE_EXISTS, PROBE_REGISTRY_VALUE, PROBE_INSTALLED_VERSION,
               PROBE_POWERSHELL)

# Which probe fields are `{variable}`-substituted on the agent, and so checked for unbound
# names at save time -- the same contract as packages._STEP_SUBSTITUTED.
PROBE_SUBSTITUTED = {
    PROBE_FILE_EXISTS: ("path",),
    PROBE_REGISTRY_VALUE: ("key",),
    PROBE_INSTALLED_VERSION: (),
    PROBE_POWERSHELL: ("script",),
}

# A PowerShell probe is a question, not an installer: it should answer in seconds. The cap is
# what stops a probe that waits on the network from holding the whole deploy for the package's
# full install timeout before anyone sees it is stuck.
DEFAULT_PROBE_TIMEOUT = 60
MAX_PROBE_TIMEOUT = 600
MAX_PROBE_SCRIPT_CHARS = 4000

# Operators per kind. The four rules kinds are the rules engine's own lists, so the builder
# offers the same choices on both pages; `version` is ordered plus equality, because "in" on a
# version invites `in ["10.0"]` matching nothing that reports "10.0.19045".
OPERATORS_BY_KIND = dict(rules.OPERATORS_BY_KIND)
OPERATORS_BY_KIND[KIND_VERSION] = (
    rules.CMP_GT, rules.CMP_GTE, rules.CMP_LT, rules.CMP_LTE, rules.CMP_EQ, rules.CMP_NE,
    rules.CMP_IS_KNOWN, rules.CMP_IS_UNKNOWN)

ON_UNKNOWN_FAIL = "fail"
ON_UNKNOWN_ELSE = "else"
ON_UNKNOWN_CHOICES = (ON_UNKNOWN_FAIL, ON_UNKNOWN_ELSE)

# Labels for the console live in the catalogs, like every other vocabulary served by
# packages_web: `packages.cond.var.<name>`, `packages.cond.probe.<kind>`.
VAR_TEXT_KEY = "packages.cond.var"
PROBE_TEXT_KEY = "packages.cond.probe"

_VERSION_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)")
FIELD_PREFIX = "field."
_FIELD_NAME_RE = re.compile(r"^field\.[a-z][a-z0-9_]{0,63}$")


# ================================
# CATALOG
# ================================
def field_variables(db_path):
    """{name: PackageVar} for every operator-defined custom field, resolved by the hub.

    Degrades to {} rather than raising: a package is still saveable on a hub whose custom
    fields table cannot be read, it just cannot test custom fields, and the error for
    `field.x` then names the variable rather than a database."""
    if not db_path:
        return {}
    try:
        fields = rules.field_variables(db_path)
    except Exception:  # noqa: BLE001
        # See the docstring: no fields beats no packages.
        return {}
    return {name: PackageVar(name, var.kind, GROUP_FIELD, SOURCE_HUB)
            for name, var in fields.items()}


def lookup(name, extra=None):
    """The variable called `name`, or None."""
    if name in STATIC_BY_NAME:
        return STATIC_BY_NAME[name]
    return (extra or {}).get(name)


def catalog(extra=None):
    """Every variable the condition builder may offer, with its operators. For the API."""
    variables = list(STATIC_VARIABLES) + sorted((extra or {}).values(), key=lambda v: v.name)
    return [{"name": v.name, "kind": v.kind, "group": v.group, "source": v.source,
             "binding": v.binding, "choices": list(v.choices),
             "operators": list(OPERATORS_BY_KIND.get(v.kind, ()))}
            for v in variables]


# ================================
# VALIDATION
# ================================
class Context:
    """What a condition may refer to at the point it is written.

    `bound` is the set of `{names}` a probe's path or script may substitute -- the same set
    packages.validate_steps tracks for the step it sits in. `allow_probes` and
    `allow_runtime` are both off for a payload's download condition: downloads happen before
    any step, so there is no previous exit code, and running a PowerShell probe before the
    deploy has even fetched its files would be a step that is not in the step list.
    """

    def __init__(self, extra=None, bound=None, allow_probes=True, allow_runtime=True):
        # extra=None (rather than {}) means "the custom fields could not be looked up here" --
        # the assistant's pre-check, which has no database. A `field.x` clause is then let
        # through as written rather than refused, so the pre-check never turns away a
        # definition the route itself would accept; the route, which does pass the fields,
        # is still the one that decides.
        self.fields_unknown = extra is None
        self.extra = extra or {}
        # None: not checked -- the console's preview, which judges a condition on its own,
        # outside the step list that decides what is bound.
        self.bound = None if bound is None else set(bound)
        self.allow_probes = allow_probes
        self.allow_runtime = allow_runtime
        self.nodes = 0


def validate(node, ctx):
    """Normalise + validate one condition. Returns the normalised AST or raises ValueError.

    Unknown keys are dropped, for the reason validate_detection drops them: this object is
    executed on the agent, and preserving arbitrary fields would make the grammar whatever
    the agent happens to read rather than what the hub checked.
    """
    return _validate(node, ctx, 0)


def _validate(node, ctx, depth):
    ctx.nodes += 1
    if ctx.nodes > rules.MAX_CONDITION_NODES:
        raise ValueError(
            f"a condition can have at most {rules.MAX_CONDITION_NODES} clauses")
    if depth > rules.MAX_CONDITION_DEPTH:
        raise ValueError(
            f"a condition can be nested at most {rules.MAX_CONDITION_DEPTH} groups deep")
    if not isinstance(node, dict):
        raise ValueError("a condition must be an object")

    op = node.get("op")
    if op in ("and", "or", "not"):
        nodes = node.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(f"'{op}' needs at least one clause")
        if op == "not" and len(nodes) != 1:
            raise ValueError("'not' takes exactly one clause")
        return {"op": op, "nodes": [_validate(child, ctx, depth + 1) for child in nodes]}
    if op is not None:
        raise ValueError(f"unknown condition group {op!r} -- use and, or, not")

    if "probe" in node:
        return _validate_probe(node, ctx)
    return _validate_leaf(node, ctx)


def _validate_leaf(node, ctx):
    name = str(node.get("var") or "").strip()
    var = lookup(name, ctx.extra)
    if var is None:
        return _unlisted_leaf(node, name, ctx)
    if var.source == SOURCE_RUNTIME and not ctx.allow_runtime:
        raise ValueError(f"{name} is not known yet here -- no step has run")

    cmp_op = node.get("cmp")
    allowed = OPERATORS_BY_KIND.get(var.kind, ())
    if cmp_op not in allowed:
        raise ValueError(f"operator {cmp_op!r} cannot be used with {name} ({var.kind}); "
                         f"allowed: {', '.join(allowed)}")
    if cmp_op in rules.NULLARY_OPERATORS:
        return {"var": name, "cmp": cmp_op}
    raw = node.get("value")
    if cmp_op in rules.LIST_OPERATORS:
        return {"var": name, "cmp": cmp_op, "value": _list_literal(var, cmp_op, raw)}
    if cmp_op == rules.CMP_MATCHES:
        pattern = str(raw if raw is not None else "")
        err = rules.validate_pattern(pattern)
        if err:
            raise ValueError(err)
        return {"var": name, "cmp": cmp_op, "value": pattern}
    return {"var": name, "cmp": cmp_op, "value": _literal(var, raw)}


def _unlisted_leaf(node, name, ctx):
    """A clause naming no variable in the catalog: a custom field the caller could not look
    up (passed through as written -- see Context), or an error that says which kind of name
    it was."""
    is_field = bool(_FIELD_NAME_RE.match(name))
    if is_field and ctx.fields_unknown:
        cmp_op = node.get("cmp")
        if cmp_op not in rules.ALL_OPERATORS:
            raise ValueError(f"unknown operator {cmp_op!r}")
        leaf = {"var": name, "cmp": cmp_op}
        if cmp_op not in rules.NULLARY_OPERATORS:
            leaf["value"] = node.get("value")
        return leaf
    if is_field:
        raise ValueError(f"there is no custom field called {name[len(FIELD_PREFIX):]!r}")
    raise ValueError(f"unknown variable: {name or '(missing)'}")


def _list_literal(var, cmp_op, raw):
    if isinstance(raw, str):
        # The builder's value box is one line of text; "a, b, c" is the obvious way to
        # type a list into it, and refusing it would only send the operator to JSON.
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"operator {cmp_op!r} needs a list of values")
    if len(raw) > rules.MAX_LIST_ITEMS:
        raise ValueError(f"too many values (limit {rules.MAX_LIST_ITEMS})")
    return [_literal(var, item) for item in raw]


def _literal(var, raw):
    """A right-hand side coerced to the variable's kind, once, at save time -- so neither the
    hub's preview nor the agent ever has to guess what "8" means against a number."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValueError(f"{var.name}: a value is required")
    if var.kind == KIND_VERSION:
        text = str(raw).strip()
        if not _VERSION_RE.match(text):
            raise ValueError(f"{var.name}: {text!r} is not a version -- use dotted numbers, "
                             f"e.g. 10.0.22631")
        return text
    if var.kind == KIND_CHOICE and var.choices:
        text = str(raw).strip().lower()
        if text not in var.choices:
            raise ValueError(f"{var.name} is one of {', '.join(var.choices)} -- not {raw!r}")
        return text
    # Handed a rules.Var rather than our own tuple: the coercion reads `unit` (for duration
    # literals), which no package variable has -- so "7d" against a number is refused there
    # as "not a duration" rather than tripping over a missing attribute here. Private, and
    # used anyway: one coercion in one place beats a copy that drifts from the Rules page.
    err, value = rules._coerce_literal(  # noqa: SLF001
        rules.Var(var.name, var.kind, var.group, None), raw)
    if err:
        raise ValueError(f"{var.name}: {err}")
    return value


def _validate_probe(node, ctx):
    probe = str(node.get("probe") or "").strip()
    if probe not in PROBE_KINDS:
        raise ValueError(f"unknown check {probe!r}")
    if not ctx.allow_probes:
        raise ValueError("a download condition can only test what the machine is -- move "
                         "file, registry and script checks into an If step")
    if probe == PROBE_POWERSHELL:
        out = _validate_powershell_probe(node)
    else:
        import packages   # function-level: packages imports this module at load time
        rule = packages.validate_detection({**node, "kind": probe})
        rule.pop("kind")
        out = {"probe": probe, **rule}
    _check_probe_variables(probe, out, ctx.bound)
    return out


def _validate_powershell_probe(node):
    script = str(node.get("script") or "").strip()
    if not script:
        raise ValueError("a PowerShell check needs a script")
    if len(script) > MAX_PROBE_SCRIPT_CHARS:
        raise ValueError(f"a PowerShell check must be {MAX_PROBE_SCRIPT_CHARS} "
                         f"characters or fewer")
    out = {"probe": PROBE_POWERSHELL, "script": script}
    timeout = node.get("timeout_seconds")
    if timeout in (None, ""):
        return out
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        raise ValueError("a PowerShell check's timeout must be a whole number")
    if not (5 <= timeout <= MAX_PROBE_TIMEOUT):
        raise ValueError(f"a PowerShell check's timeout must be between 5 and "
                         f"{MAX_PROBE_TIMEOUT} seconds")
    out["timeout_seconds"] = timeout
    return out


def _check_probe_variables(probe, out, bound):
    """Every `{name}` a check substitutes must be bound where it sits. None skips the check
    (see Context)."""
    if bound is None:
        return
    unknown = set()
    for field in PROBE_SUBSTITUTED[probe]:
        unknown |= variables_in(out.get(field)) - bound
    if unknown:
        raise ValueError(
            f"the {probe.replace('_', ' ')} check refers to {{{min(unknown)}}}, which "
            f"nothing before it provides")


_VARIABLE_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")


def variables_in(text):
    """`{names}` in a string -- the grammar packages._VARIABLE_RE defines."""
    return set(_VARIABLE_RE.findall(str(text or "")))


# ================================
# READING A CONDITION
# ================================
def walk(node):
    """Every node of a condition, depth-first."""
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("nodes") or ():
        yield from walk(child)


def referenced_variables(node):
    return {n["var"] for n in walk(node) if n.get("var")}


def substituted_variables(node):
    """`{names}` a condition's probes open -- payload usage is counted from these too, so a
    payload only a check looks at is not refused as unused."""
    used = set()
    for n in walk(node):
        probe = n.get("probe")
        for field in PROBE_SUBSTITUTED.get(probe, ()):
            used |= variables_in(n.get(field))
    return used


def hub_variables(conditions, extra=None):
    """The hub-resolved variables any of `conditions` reads -- what `hub_facts` must send."""
    names = set()
    for condition in conditions:
        for name in referenced_variables(condition):
            var = lookup(name, extra)
            if (var is not None and var.source == SOURCE_HUB) or name.startswith(FIELD_PREFIX):
                names.add(name)
    return names


# ================================
# EVALUATION (the hub's preview; the agent has its own, PackageConditions.cs)
# ================================
UNKNOWN = rules.UNKNOWN


def parse_version(text):
    """Dotted numeric prefix as a tuple, or None. "10.0.22631.4460" -> (10, 0, 22631, 4460);
    "N3KET60W (1.40)" -> None, which compares as UNKNOWN rather than as version 0."""
    match = _VERSION_RE.match(str(text or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def compare_versions(left, right):
    """-1/0/1, padding the shorter with zeros so 10.0 == 10.0.0. None if either is not a
    version. Mirrors VersionUtil.Compare on the agent, minus its "unparseable is 0"."""
    a, b = parse_version(left), parse_version(right)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def evaluate(node, facts, extra=None):
    """True / False / UNKNOWN for `node` against `facts` ({variable name: value}).

    Probes are always UNKNOWN here -- the hub cannot look at a machine's disk -- which is
    what the console's preview means by "decided on the machine". Kleene and/or/not, exactly
    as rules.evaluate.
    """
    if not isinstance(node, dict):
        return UNKNOWN
    op = node.get("op")
    if op in ("and", "or"):
        return _kleene(op, [evaluate(child, facts, extra) for child in node.get("nodes") or ()])
    if op == "not":
        inner = node.get("nodes") or []
        result = evaluate(inner[0], facts, extra) if len(inner) == 1 else UNKNOWN
        return UNKNOWN if result is UNKNOWN else (not result)
    if "probe" in node:
        return UNKNOWN
    return _evaluate_leaf(node, facts, extra)


def _kleene(op, results):
    """Kleene and/or: TRUE settles an or, FALSE settles an and, otherwise one UNKNOWN makes
    the whole thing UNKNOWN."""
    if not results:
        return UNKNOWN
    decisive = op == "or"
    if any(r is decisive for r in results):
        return decisive
    return UNKNOWN if any(r is UNKNOWN for r in results) else (not decisive)


_VERSION_ORDER = {
    rules.CMP_GT: lambda o: o > 0, rules.CMP_GTE: lambda o: o >= 0,
    rules.CMP_LT: lambda o: o < 0, rules.CMP_LTE: lambda o: o <= 0,
    rules.CMP_EQ: lambda o: o == 0, rules.CMP_NE: lambda o: o != 0,
}


def _evaluate_leaf(node, facts, extra):
    name = node.get("var")
    cmp_op = node.get("cmp")
    value = (facts or {}).get(name, UNKNOWN)
    if value is None or (isinstance(value, str) and not value.strip()):
        value = UNKNOWN
    if cmp_op == rules.CMP_IS_KNOWN:
        return value is not UNKNOWN
    if cmp_op == rules.CMP_IS_UNKNOWN:
        return value is UNKNOWN
    if value is UNKNOWN:
        return UNKNOWN
    var = lookup(name, extra)
    if var is None or var.kind != KIND_VERSION:
        # Private, and used anyway: the same comparison as the Rules page, not a copy of it.
        return rules._compare(cmp_op, value, node.get("value"))  # noqa: SLF001
    order = compare_versions(value, node.get("value"))
    test = _VERSION_ORDER.get(cmp_op)
    return UNKNOWN if order is None or test is None else test(order)


def result_name(result):
    if result is UNKNOWN:
        return "unknown"
    return "true" if result else "false"


def explain(node, facts, extra=None):
    """`node` annotated with each clause's actual value and result -- the preview's "why"."""
    if not isinstance(node, dict):
        return {"result": "unknown"}
    if node.get("op") in ("and", "or", "not"):
        return {"op": node["op"],
                "nodes": [explain(child, facts, extra) for child in node.get("nodes") or ()],
                "result": result_name(evaluate(node, facts, extra))}
    if "probe" in node:
        return {**node, "result": "unknown", "on_machine": True}
    actual = (facts or {}).get(node.get("var"))
    return {**node, "actual": actual, "known": actual not in (None, ""),
            "result": result_name(evaluate(node, facts, extra))}


# ================================
# FACTS THE HUB KNOWS
# ================================
# The hub's own (possibly stale) answer for each agent variable it has a copy of. Used only
# by the console's preview -- what the deploy acts on is the agent's live reading.
# The same name on both sides for every variable the rules engine also has, except the
# hostname, which it calls sys.machine, and the architecture, whose raw "64-bit" is turned
# into the agent's slug by _arch_slug instead.
_HUB_COPY = {v.name: v.name for v in STATIC_VARIABLES
             if v.name in rules.STATIC_BY_NAME and v.name != "os.arch"}
_HUB_COPY["sys.hostname"] = "sys.machine"

def _arch_slug(value):
    """Win32_OperatingSystem.OSArchitecture ("64-bit", "ARM 64-bit Processor") as the choice
    slug the agent reports. The agent reads the processor architecture directly; this only
    serves the preview."""
    text = str(value or "").lower()
    if not text:
        return None
    if "arm" in text:
        return "arm64"
    if "64" in text:
        return "x64"
    if "32" in text or "86" in text:
        return "x86"
    return None


def _known_value(resolved, rule_name):
    entry = resolved.get(rule_name)
    value = entry.value if isinstance(entry, rules.Value) else entry
    return None if value is UNKNOWN or value in (None, "") else value


def resolve_hub_facts(db_path, machine, names=None):
    """{variable: value} for one machine from what the hub has stored. UNKNOWN values are
    left out. `names` limits it to the variables a caller actually needs."""
    try:
        resolved = rules.resolve_machine_vars(db_path, machine)
    except Exception:  # noqa: BLE001
        # A preview with no facts is still a preview: every clause reads unknown.
        return {}
    pairs = [(ours, _known_value(resolved, theirs)) for ours, theirs in _HUB_COPY.items()]
    pairs.append(("os.arch", _arch_slug(_known_value(resolved, "os.arch"))))
    pairs.extend((name, _known_value(resolved, name))
                 for name in resolved if name.startswith(FIELD_PREFIX))
    return {name: value for name, value in pairs
            if value is not None and (names is None or name in names)}


def hub_facts(db_path, machine, names):
    """The `params.facts` a deploy carries: hub-resolved variables only (AD, custom fields),
    and only those the package reads. Agent variables are never sent -- the agent's own live
    answer is the point, and a hub copy riding along would be one more value to disagree
    with it."""
    wanted = {n for n in names if n.startswith(FIELD_PREFIX)
              or (n in STATIC_BY_NAME and STATIC_BY_NAME[n].source == SOURCE_HUB)}
    if not wanted:
        return {}
    return resolve_hub_facts(db_path, machine, wanted)
