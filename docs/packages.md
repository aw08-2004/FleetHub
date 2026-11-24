# Package deployment

## Package deployment (PDQ-style)

Define an installer once — payload, silent command line, what proves it worked — then
aim it at machines and watch it land. Core logic in [packages.py](../hub/packages.py), HTTP
surface in [packages_web.py](../hub/packages_web.py), UI at `/packages`, agent side in
`agent/src/TempMonitorAgent/Fleet/Executors/DeployPackageExecutor.cs`. State lives in the
same SQLite DB (`packages`, `package_sources`, `deployments`, `deployment_targets`).

**A package is a recipe plus its payloads.** A payload is either a file uploaded to the
hub (stored beside the database under `logs/packages/`, content-addressed by SHA-256 and
shared between packages built from the same installer) or an external reference — a
winget id, an `https://` URL, or a UNC path. The recipe is the command line (with
`{file}` standing in for the resolved payload), a timeout, the accepted exit codes, and a
detection rule.

**A recipe can also be several steps.** One command line covers an MSI and not much else;
a driver pack is download a zip, unpack it, hand the folder to `pnputil`. So a package may
instead carry an ordered list of steps, each with its own timeout and accepted exit codes:

| Step | What it does |
|---|---|
| `run` | an executable with arguments — an MSI, a vendor `setup.exe` |
| `powershell` | an inline script as SYSTEM; the escape hatch for copy / registry / services |
| `winget` | `winget install --id …`, with its own trust chain |
| `extract` | unpacks a `.zip` into a folder later steps can point at |
| `pnputil` | stages and installs a folder of `.inf` files |
| `apk` | installs an Android app, silently, on a fully managed Android device |

**An `apk` step makes the package Android-only**, and then it may hold nothing else: every
payload must be an upload or a URL pinned by sha256 (the device installs it with no prompt),
and the detection rule must be *none* -- the device checks with Android that the app is
installed at the version the APK declares. The scheduler keeps each package on its own
platform: a phone is never sent a Windows package and a PC never an APK, and either target is
marked failed with the reason. See [android.md](android.md).

Payloads are **named slots**, so steps say which file they mean: `{drivers}` is the
payload named `drivers`, `{work}` is the attempt's own directory, and an `extract` step
binds its output folder for the steps after it. `{file}` still means the payload when
there is exactly one, so nothing written before steps needs changing. A variable nothing
provides — including one a *later* step would bind — is refused when the package is saved
rather than reaching a machine as a literal brace pair; the grammar is `{lowercase_words}`
only, so a literal MSI product code like `{90160000-008C-0000-1000-0000000FF1CE}` passes
through untouched. A payload no step ever opens is refused for the same reason `{file}`
has always had to appear somewhere.

The first step that fails stops the deploy, unless it is marked "carry on". Detection runs
once at the end — it is a claim about the package, not about a step. `pnputil` steps
default to accepting `{0, 259, 3010}`: 259 is a driver install that simply ran out of INFs,
and failing it would paint a clean driver rollout red the way failing 3010 would paint a
fleet of MSI installs red.

> **Steps need agent 3.22.0 or newer.** An older agent reads only the single-command
> fields, so the hub sends a step-based package with no command for it to run: it fails the
> deploy with "no install command" and the target retries until the update reaches it.
> Packages written as one command still go out in the old shape and run on every agent in
> the field.

### If / Else, Stop, and download conditions

> **Needs agent 3.54.0 or newer** (and hub 1.187.0). A machine on an older agent is not sent a
> package that uses any of this: it fails at once with the version it needs, and *Retry
> failures* sends it after the agent updates.

Two more step kinds make a package branch:

| Step | What it does |
|---|---|
| `if` | checks a condition on the machine and runs its **Then** steps or its **Else** steps |
| `stop` | ends the deploy here, as *Succeeded* (nothing to do on this machine) or *Failed* with a message |

*Add Else if* puts another If inside Else. If steps nest up to four deep, and the package's
step limit (60) counts every step inside them. A successful Stop skips the detection check:
the package has just said the software is not meant to be on this machine.

**What a condition can test.** On the machine itself: manufacturer, model, *model name*,
system SKU, serial, chassis type (laptop, desktop, server, tablet, other), memory in GB,
processor, Windows edition, version (`10.0.22631`), build, release (`23H2`), architecture
(x64, x86, arm64), BIOS version, computer name, domain or workgroup, whether it is joined to a
domain, agent version, and the exit code of the previous step. From the hub: Active Directory
OU, distinguished name and owner, and every custom field. And four checks run on the machine:
a **file or folder exists**, a **registry value** exists (or equals a value), a **program is
installed** (optionally at a minimum version), and a **PowerShell check** (exit 0 is true, 1 is
false, anything else means *can't tell* -- and so does a script that throws or does not
parse, so a typo never quietly sends machines down Else; 60 seconds by default, at most 600).

Clauses combine as *All of these*, *Any of these* or *None of these*, and groups nest. The
operators are the Rules page's: *is*, *is not*, *contains*, *starts with*, *ends with*,
*matches the pattern* (`*` and `?`), *is one of* (type values separated by commas), *is known*,
and *is more than / at least / less than / at most* for numbers and versions. Text compares
without regard to case, so `LENOVO` is `Lenovo`; versions compare as versions, so 10.0.22631 is
above 10.0.9200.

> **Lenovo:** `Model` on a ThinkPad is the machine-type code (`21HD0012US`), one per
> configuration. Test **Model name** instead -- that is the *ThinkPad X1 Carbon Gen 11* an
> operator thinks in. Dell and HP put the readable name in Model.

**If the machine cannot tell, the If fails.** A model that could not be read makes
`Model contains "Latitude"` *unknown*, not false, and the If step fails with *could not
decide* rather than running Else -- so a generic driver pack is never installed on a machine
nobody identified. Set *If the machine cannot tell* to *Run Else* to choose otherwise for that
step, or tick *skip Then and Else and carry on*.

**Only download on some machines.** A payload can carry its own condition (on what the machine
is -- no file or PowerShell checks, and no step results, since downloads come first). Where it
is false the file is not downloaded, the console shows the download as *Not run*, and a step
that uses it fails with that reason -- so put those steps inside an If with the same condition.

**The machine's own values in steps.** `{model}`, `{model_name}`, `{manufacturer}`, `{sku}`,
`{os_build}`, `{arch}` and `{hostname}` work in any step field that takes variables, e.g. an
extract to `{work}\{model}`. A value that cannot be read fails the step. A payload or an
extract step cannot use these names. A name an extract step binds inside one branch is only
available after the If when every branch that carries on binds it.

**Test on a machine.** Each If card can test its condition against one machine, from what the
hub last heard: it says which branch would run and shows each clause's actual value. File,
registry and PowerShell checks and Lenovo's model name are only known to the machine, so they
read *checked on the machine*. The deploy itself always reads the machine live.

**Numbering and progress.** Steps are numbered depth-first: an If, then its Then steps, then
its Else steps, then what comes after. That is the number in error messages and in the
deployment view, where steps inside an If are indented and labelled *Then* / *Else*, and the
branch not taken reads *Not run*. *Step n of m* counts only the steps that run.

A worked example, one package for a mixed fleet:

```
Payloads:  dell    (URL, only download when Manufacturer starts with "Dell")
           lenovo  (URL, only download when Manufacturer is "LENOVO")
1  If  Manufacturer starts with "Dell"
2    Then  Unpack a .zip  {dell}    -> drivers
3    Else  If  Manufacturer is "LENOVO" and Model name contains "ThinkPad"
4            Then  Unpack a .zip  {lenovo}  -> drivers
5            Else  Stop (failed) "Unsupported model"
6  Install drivers  {drivers}
```

**Success is exit code AND detection, both.** An installer exiting 0 is evidence, not
proof — silent installers routinely return 0 having done nothing, and on a fleet-wide push
that is the failure you least want reported as success. So every package also carries a
post-install check: a file exists, a registry value exists (optionally matching exactly),
or a product appears in Windows' installed-programs list at or above a given version.
Anything the agent cannot evaluate counts as *not* detected, never as detected.

**Trust.** The hub computes the SHA-256 of an uploaded payload itself, at upload, from the
bytes it writes — a client-supplied digest is never accepted — and the agent re-verifies
it before executing, deleting the file on a mismatch. That plus the authenticated HTTPS
channel is the whole integrity story; there is deliberately no new offline signing key
(see the command-channel section above for why that model was removed). URL/UNC payloads
can be hash-pinned too; winget has its own trust chain. The agent works in one directory
per attempt under `%ProgramData%` (SYSTEM-owned, not `%TEMP%`, so a half-finished install
cannot leave an executable where a standard user could swap it out first) and deletes it
whole on every path. `extract` refuses any archive entry that would land outside that
directory.

**Scheduling layers on the existing command queue, it does not replace it.** A deployment
holds one row per target machine; the hub's scheduler thread turns a due target into an
ordinary `deploy_package` command with the usual TTL, then reads that command's terminal
status back. A machine that is offline therefore costs one expired command and one
backoff, using the same expiry the queue already enforces. Retries are per machine
(default 3 attempts, backoff doubling from 15 minutes), and a deployment can carry a
window — don't start before X, give up after Y.

**Watching a deployment.** Open a deployment from the Deployments tab. Each machine shows
what it is doing — *Queued*, *Sent, waiting for the machine* (it has not picked the job up:
offline, or between polls), *Running*, *Aborting...*, *Waiting to retry*, *Finishing*, then the
result — and, while it runs, which step it is on (*Step 2 of 3 · Install drivers*) and for how
long. Click the machine name for its step list: every download, every step and the detection
check, each with its result, exit code and duration, and the reason a failed step gave.
**Log** under a step shows that step's own output and follows it while it runs; the log is kept
as long as command output is (24 hours by default), after which the whole run's output is still
in the machine's command history.

Select machines with the checkboxes to act on them:

- **Abort** — a machine that has not started is cancelled at once. One that is installing is
  stopped at the step it is on (that step's program is ended, which can leave the software
  half-installed) within about ten seconds; the row reads *Aborting...* until the agent
  confirms, then *Cancelled*. An aborted machine is not retried. If the abort reaches the
  machine after its last step, the row records *Succeeded*, because it did.
- **Redeploy** — run the package again on the selected machines in the same deployment, with
  a fresh set of attempts, including machines where it already succeeded. It uses the
  package as it is defined now.
- **Retry now** — a machine waiting out its retry backoff is sent on the next pass. A
  deployment's window is never pulled forward.

*Retry failures* at the bottom redeploys every failed, expired or cancelled machine you can
see.

> **The step list and Abort need agent 3.53.0 or newer** (and hub 1.183.0). An older agent
> installs exactly as before and shows as *Running* with its planned steps until its result
> arrives; it cannot be told to stop mid-install, so Abort answers that instead of pretending.

**Authorization** is the `deploy_packages` capability plus machine scope, from Permission
Groups. Targets are checked *before* anything is written and the request is refused whole
if any single machine is out of scope — a deploy that quietly installs on nine of the ten
machines you asked for is worse than one that fails. Reads are scoped the other way: an
operator sees only the target rows they could have created.

**Endpoints.** Console (`deploy_packages`): `GET|POST /api/packages`,
`GET|PUT|DELETE /api/packages/<id>`, `POST /api/packages/upload`,
`POST /api/packages/conditions/preview` (`machine` + `condition`; machine scope applies),
`GET|POST /api/deployments`, `GET /api/deployments/<id>`,
`POST /api/deployments/<id>/cancel`, `POST /api/deployments/<id>/retry` (optional
`machines` to redeploy a selection), `POST /api/deployments/<id>/retry-now`,
`POST /api/deployments/<id>/abort`,
`GET /api/deployments/<id>/targets/<machine>/steps/<phase>/<ordinal>/output`.
Agent (token auth): `GET /api/agent/packages/<sha256>`,
`POST /api/agent/commands/<id>/progress` (step events; the answer carries the abort).

> The upload endpoint is the one place that accepts `multipart/form-data` rather than
> JSON — a file upload cannot be JSON. It is deliberately inert to compensate: it stores
> bytes and returns a hash, creating no package and touching no machine. Turning that hash
> into something that runs anywhere requires the JSON-bodied endpoints, which a cross-site
> form cannot reach. Don't make it create a package as a convenience.

Tunables live in Settings under **Package Deployment**: retry defaults, the upload size
limit, and the scheduler interval.

**If an upload fails with HTTP 413, it is not the hub.** The hub's own limit is
`deploy.max_upload_mb` (512 MB by default) and it is enforced in Python, so exceeding it
gives you a JSON error naming the limit. A raw `413` means the TLS terminator in front of
the hub rejected the body before Flask ever saw it — and **nginx's default
`client_max_body_size` is 1 MB**, so the very first real installer anyone uploads hits it.
Raise it to something above your largest payload and reload:

```nginx
server {
    client_max_body_size 600m;   # >= deploy.max_upload_mb
    # A large upload over a slow link can also hit the read timeout:
    proxy_read_timeout 300s;
    proxy_request_buffering off; # stream to the hub instead of buffering the whole file
}
```

The equivalents elsewhere: IIS `maxAllowedContentLength` (default ~30 MB) *and*
`maxRequestLength`; Apache `LimitRequestBody`; Cloudflare's proxy caps free plans at
100 MB and it cannot be raised from the origin.
