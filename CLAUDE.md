# FleetHub

Windows fleet management / RMM for one IT helpdesk group. Three shipped components on three
independent version lines, each with a real updater running in the field — so a version
mistake here reaches real machines, or silently reaches none of them.

- `hub/` — Flask + Socket.IO console and API. The only thing an operator sees.
- `agent/` — C#/.NET 10 Windows Service, LocalSystem, installed on every managed PC.
- `app/` — Flutter desktop client (roadmap #11).
- `tests/` — the hub's Python suite.
- `install.ps1` — unified menu-driven installer for all three.

Four canonical docs already exist. Go to them by question, not by default:

| Question | File |
|---|---|
| What a feature does, or how to operate it | `docs/` — one file per area, grep it; `README.md` is the short overview |
| Is this planned or shipped, and what was rejected | `ROADMAP.MD` — features `#1`–`#22` |
| Anything about a version number | `VERSIONING.md` — 120 lines, read it whole |
| Auth, capabilities, threat model | `SECURITY.MD` |
| What is waiting on Windows, a phone or the signing key | `ROADMAP.MD` → *Owed locally*, top of file -- surface it at the start of a local session |

## Search hygiene

- **Exclude `.claude/worktrees/`** from every Glob and Grep — it is a full duplicate checkout
  of this repo and doubles every hit.
- **Exclude `flutter_sdk/`** — a vendored Flutter SDK checkout, gitignored, never edited.
- Also not yours: `logs/`, `.venv/`, `agent/**/bin/`, `agent/**/obj/`, `agent/dist/`, `.env`,
  `sign_release.py`.

## Versioning — the rules that break production

- **Beta first, always (since 2026-10-21).** Every change -- hub, agent, client -- goes to the
  `beta` branch first: branch from `origin/beta`, open the PR **into `beta`**, never into `main`.
  Each one adds what an operator must check to ROADMAP.MD → *On beta, waiting for
  confirmation*. Only when everything on that list is confirmed does `beta` reach `main`, as
  **one** `beta` → `main` PR that empties the list. Agent betas are cut with
  `-Channel beta`. Nothing is pushed to or PR'd into `main` directly.
- **Bump `HUB_VERSION` (near the top of `hub/app.py`) on every push to `beta` that touches `hub/`.** Do it
  as part of the change, unasked; the comment two lines above it says the same thing. The hub
  compares its channel branch's value against the running one, so a push without a bump never
  reaches a deployed hub. The number is the next one after **`beta`'s** value, not `main`'s --
  `beta` is ahead, and a number `beta` already shipped is a reuse. The `beta` → `main` PR
  carries `beta`'s number as it stands; `main` skipping numbers is fine, repeating one is not.
  Docs-only and `tests/`-only pushes need no bump.
- **Every `HUB_VERSION` bump writes `deploy/docker/release-notes/<version>.md` in the same
  change.** Same release-please layout as `agent/release-notes/README.md`, written for the
  operators. The hub has no GitHub releases, so this file *is* the release notes: the sidebar's
  "Hub vX is available" notice links straight to it on the channel's branch, and a missing file
  is a 404 in front of every operator. Docs-only and `tests/`-only pushes need no bump and so
  no notes.
- **Exactly `MAJOR.MINOR.PATCH`, three numeric components, no suffix.** No `-rc1`, no `+build`,
  never two or four components — `parse_hub_version()` matches digits and dots only, so a
  suffix parses as `None` and the hub silently stops discovering updates fleet-wide.
- **Strictly increasing. Never reuse or lower a number.** Every updater asks "is the other side
  *strictly* newer", so a repeated or lowered number means nothing happens anywhere while the
  release looks shipped. This has bitten twice.
- **Agent and client versions move only when cutting a release, and only via the scripts** —
  `agent/release.ps1 -Version x.y.z` and `python release_client.py --set-version x.y.z`. Each
  has a two-file pair that must agree, and the scripts are what keep them in sync. Unreleased
  source on `main` keeps the old number.
- **Agent MINOR has teeth.** Anything the hub might gate on needs one, because the hub hardcodes
  the minor that introduced a feature — `MIN_PTY_AGENT`, `MIN_PROCESS_AGENT`, `MIN_OPEN_AGENT`
  and friends in `hub/static/js/`. Those constants are a compatibility API, not trivia.
- **Deploy order is always hub first, then agent**, which propagates in about 15 minutes. The
  reverse leaves the console offering a feature no machine answers.
- Everything else about versions → `VERSIONING.md`.

## Don't "clean this up"

Each of these looks like leftover mess and is load-bearing.

- **`Temp_Monitor` / `TempMonitorAgent` / `TempMonitorHub`** in code, paths, service names and
  namespaces are **correct**. The product was renamed FleetHub, and so was the GitHub repo
  (`aw08-2004/FleetHub`) -- every URL and `owner/repo` slug uses the new name, but local
  identifiers keep the old one. Do not rename them. Never create a new repo called
  `Temp_Monitor`: field builds still fetch through GitHub's redirect from the old name. **One exception:** the Windows agent's *service* is
  `FleetHubAgent` since 2026-10-12, and `TempMonitorAgent` survives only as the legacy service
  name on un-migrated PCs -- so anything naming the service must accept both (ROADMAP.MD,
  *Agent service renamed to `FleetHubAgent`*). **The exe followed on 2026-10-13:** a fresh
  install is `FleetHubAgent.exe`, and `TempMonitorAgent.exe` survives on PCs nobody has
  re-installed, so anything naming the *process* must accept both (ROADMAP.MD, *Agent exe
  renamed to `FleetHubAgent.exe`*). Namespaces, the csproj and its folder keep the old name.
- **Never remove the `-text` pins in `.gitattributes`**, and never let a tool rewrite line
  endings on the signed manifests or their `.sig` files. A signature covers exact bytes, so a
  rewritten file is indistinguishable from a tampered one and the fleet rejects every update
  with no obvious cause.
- **Command signing was removed deliberately.** `ALLOWED_EMAILS` plus permission groups are the
  whole perimeter. Don't reintroduce signing, and don't weaken a route's gate.
- **No linter or formatter config exists anywhere.** That is deliberate — don't add one.

## Testing

- Hub: `python tests/run_all.py` (`-q` for a summary). It runs **each module in its own fresh
  interpreter on purpose** — modules set up a temp DB, import `app` (which starts a
  process-lifetime `db_writer` daemon and keeps in-memory caches) and assert on wall-clock
  online/offline state. `pytest tests/` shares one process and goes flaky. Don't "fix" the
  runner into a plain pytest call.
- Editing one module: `pytest tests/test_x.py` works — `tests/conftest.py` adapts the house
  pattern.
- Agent: `dotnet test agent/TempMonitorAgent.slnx`.
  Publish: `dotnet publish agent/src/TempMonitorAgent/TempMonitorAgent.csproj -c Release -o agent/dist`.
- **From Linux, the agent still compiles** -- run `dotnet build agent/TempMonitorAgent.slnx -p:EnableWindowsTargeting=true`
  (after `apt-get install -y dotnet-sdk-10.0`). The tests cannot be *run* there, but compiling
  is not optional: nothing else builds this project, and a `csproj` or C# change that was never
  compiled has reached `main` broken before.
- **Trap:** an agent `dotnet test` run rewrites `tests/fixtures/*.fhb` in place. Check
  `git status` before staging.
- **No CI runs these tests** — only CodeQL and a Claude PR review. If you didn't run them,
  nobody did.
- A new test module follows the house pattern: module-level `PASS`/`FAIL` counters, a
  `check(name, cond)` helper, a `main()` printing `==== N passed, M failed ====`, and
  `if __name__ == "__main__": sys.exit(main())`. Its docstring names the *silent failure* the
  file exists to catch, not the function it covers.

## Code conventions

**Hub.** `x.py` is the model half and is strictly **Flask-free** — pure functions over SQLite,
unit-testable standalone. `x_web.py` is the HTTP surface, exposed as a
`create_<x>_blueprint(...)` factory and registered from `app.py`. Keep the split when adding to
a pair; a new feature area gets a new pair, not a route in `app.py`. `app.py` is the composition
root only — config, DB init, Socket.IO, OAuth, `/api/report` ingest, self-update, blueprint
registration.

**Authorization is two gates, and a machine route needs both.** They come from the `access`
object built in `hub/permissions_web.py`: `access.require(cap)` for a capability-only route,
**`access.require_machine(cap)`** for anything naming a machine (capability *and* scope), and
`access.in_scope()` / `filter_rows()` / `filter_machines()` for scoping reads. `hub/wake_web.py`
is the canonical example — copy the gate from a sibling `*_web.py` rather than inventing one.

**Frontend.** Vanilla JS, IIFE + `'use strict'`. Scripts in `hub/static/js/`, templates in
`hub/templates/` and `templates/partials/`, CSS custom properties in `hub/static/css/tokens.css`.
**No framework and no build step by default** -- a framework is allowed only when the user has
explicitly approved that specific one. Approved so far:

- **Tailwind CSS**, built with the pinned *standalone CLI* (`tools/build_css.ps1`), never npm.
  The compiled `hub/static/css/app.css` is **committed**, because hub self-update ships `hub/`
  as plain files and a deployed hub never runs a build. Rebuild after changing classes in a
  template or script, or the class silently has no CSS behind it. Preflight stays off while
  `components.css` still styles the older pages; theme colours map onto `tokens.css` variables.

Still not allowed without asking: a JS framework, a bundler, npm/`node_modules` for the hub.

**Agent.** `net10.0-windows`, published self-contained single-file win-x64, runs as a Windows
Service under LocalSystem. `Worker.cs` is six independent concurrent loops. Class suffixes are
load-bearing: `*Executor.cs` implements `ICommandExecutor` for one fleet command type (its
`Type` must match the hub's `COMMAND_TYPE`; routed by `Fleet/CommandDispatcher.cs`),
`*Reporter.cs` pushes change-only data on the heartbeat, `*Reader.cs` is a local read-only
probe. File-scoped namespaces, primary constructors, `sealed` by default, XML doc comments.

## i18n is enforced by tests, not by convention

- All user-facing English lives in `hub/locales/en.json`, and `de.json` + `es.json` must be
  updated in the **same change**. `tests/test_i18n.py::test_key_parity_with_english` fails on
  missing *and* extra keys, and placeholders must match English exactly.
- Only **literal** `t('key')` calls are scanned, so prefer literal keys over computed ones —
  `t('a.' + kind)` is invisible to the test that would have caught the typo.
- Server-supplied UI text counts too: a new `settings.REGISTRY` entry, a new
  `permissions.CAPABILITIES` entry, an enum choice, a unit slug. No catalog entry, no passing
  test.

## House style

The most distinctive thing about this codebase, and what a generic model gets wrong by default.

- **Comments are design rationale, not description.** Every module opens with a docstring
  answering why it exists and what silent failure it prevents, usually citing a roadmap number
  (`roadmap #14`). Match the density of the file you're editing, and don't strip comments you
  didn't write.
- **Write down the alternatives you rejected, and why.** Record history when something has
  bitten before ("This has bitten twice already"). That is the point of the density.
- `**bold**` inside docstrings for the load-bearing claim. Use `--`, not em dashes, in Python
  and C# source. **Not in XML** (`.csproj`, templates): a comment there may not contain `--`
  at all, and MSBuild refuses to load the whole project file over it.
- With no linter, style comes from imitating the surrounding file. Read it before adding to it.
- **Commit messages use the same voice**: a title stating the change in plain language, a body
  explaining **why**, not what. For calibration: *"Narrow the maintenance-window read, and stop
  the winget parse eating the source column"*.
- **Don't create new top-level `.md` files** for summaries, notes or plans. Findings go in the
  reply; durable decisions go in `ROADMAP.MD`.
- **No AI attribution anywhere in the repo or its releases.** No "Generated with Claude Code",
  no `Co-Authored-By: Claude`, no `Claude-Session:` trailer, no link back to a chat session --
  not in commit messages, pull request titles or bodies, PR comments, release notes, tags, or
  source files. The reason is not modesty: these artifacts are read by the helpdesk group and
  quoted into change tickets, and a session URL nobody outside this machine can open is noise
  that outlives the change. Default tooling appends this footer on its own, so strip it before
  committing or opening a PR rather than trusting the default.

## Roadmap upkeep

`ROADMAP.MD` is the live board — features `#1`–`#22` marked ✅ done / 🚧 partial / 📋 planned.
Update an entry when a feature's status actually changes, and record the decision and the
alternatives you rejected there. Code comments cite these numbers, so a stale board makes them
lie.

## Before you call it done

```
[ ] Touched hub/?           -> bump HUB_VERSION in hub/app.py
[ ] Bumped HUB_VERSION?     -> deploy/docker/release-notes/<version>.md
[ ] New user-facing string? -> en.json + de.json + es.json, same change
[ ] Changed Tailwind classes? -> tools/build_css.ps1, commit app.css
[ ] python tests/run_all.py -q      (+ dotnet test if agent/ changed)
[ ] Ran agent tests?        -> git status for rewritten tests/fixtures/*.fhb
[ ] New machine route?      -> access.require_machine(), not just access.require()
[ ] Feature status changed? -> ROADMAP.MD
[ ] Commit message says WHY
[ ] Commit / PR / release notes -> no AI attribution or session link
```
