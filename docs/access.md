# Sign-in, permissions and Active Directory

## Sign-in setup (OIDC)

Viewing the dashboard (`/`, `/machine/<name>`, and the `/api/machines`,
`/api/machines/<name>` endpoints, plus live Socket.IO updates) requires signing in. Sign-in succeeds for
an address in `ALLOWED_EMAILS` (break-glass superusers), one that belongs to at
least one permission group, or one whose provider says it is in a **directory group**
some permission group maps (see [Permission groups](#permission-groups)); anyone else
is refused at the callback with 403 rather than admitted to an empty dashboard. Agent endpoints such as
`POST /api/report` use agent credentials instead of sign-in.

Sign-in is **OpenID Connect**, and any OIDC provider will do. Configure Google,
another issuer, or both — the hub refuses to start with none.

Always required, whichever provider you use:

```
FLASK_SECRET_KEY=a-long-random-string   # signs the session cookie
ALLOWED_EMAILS=you@example.com,teammate@example.com
HUB_URL=https://your.domain.com         # public URL of this hub
SESSION_LIFETIME_DAYS=7                 # optional; see "Staying signed in" below
```

**Google.** In the [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
create an **OAuth 2.0 Client ID** (Application type: Web application) and add the
redirect URI `https://your.domain.com/auth/callback` (plus
`http://localhost:3001/auth/callback` for local dev). Then:

```
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

**Any other OIDC provider** (Microsoft Entra ID, Okta, Authentik, Keycloak,
Auth0…). Register a web/confidential application with the redirect URI
`https://your.domain.com/auth/oidc/callback`, then:

```
OIDC_DISPLAY_NAME=Microsoft              # what the sign-in button says
OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_SCOPES=openid email profile         # optional
```

The issuer's `/.well-known/openid-configuration` is discovered automatically, so
there is no per-vendor code and adding a provider is configuration, not a release.
(Set `OIDC_METADATA_URL` instead if your provider's discovery document isn't at
the conventional path.)

**You can also edit all of the above from the console**, at **Settings → Sign-in** —
`.env` is only needed to bootstrap the first provider. Changes are written to `.env` and
applied immediately, with no restart. Client secrets are write-only: the editor shows
whether one is saved, never what it is, and leaving the placeholder untouched keeps it.

> ⚠️ **Only the break-glass admins in `ALLOWED_EMAILS` can change sign-in settings** —
> deliberately not `manage_settings`, and not any permission group, however privileged.
> Whoever configures the identity provider can point the hub at an issuer they control and
> sign in as anyone, so this is the one thing that stays with the accounts that already
> hold total access. A group holding *every* capability is still refused.

Guards, because this is the way back in:

- A configuration that would leave **no working provider is refused** — a hub with none
  can only be fixed by editing `.env` on the server.
- A **half-filled provider is refused**, not silently ignored, so a client ID with no
  secret is an error rather than a button that never appears.
- The **issuer must be https**. The discovery document names the token endpoint and the
  signing keys, so anything that can rewrite it in flight chooses who the hub believes
  you are.
- If the new settings are rejected when the clients are re-registered, **everything is
  rolled back** — `.env`, the live process, and the running clients — before the error
  comes back.
- Every change is audited by **field name only**; secrets never enter the database.

**Identity.** Whichever provider is used, the **email** is the identity, and it goes
through the same permission groups and the same break-glass `ALLOWED_EMAILS` list.
Some providers don't send an `email` claim — Entra often doesn't — so
`preferred_username` and `upn` are accepted as fallbacks *when they look like an
address*; a bare username is refused rather than allowed to collide with a granted
mailbox name. A provider that reports `email_verified: false` is refused. A provider
that omits the claim entirely is trusted, because you configured it.

> ⚠️ **Two providers are two doors to the same rooms.** An operator's access is
> whatever the *weaker* issuer will assert about their address. Don't enable a
> second issuer that lets users self-assert an email you've granted access to.

### Staying signed in

Sessions are **persistent and rolling**: closing the browser doesn't sign you out, and
every request pushes the expiry back, so day-to-day use never hits a login prompt. A
browser left untouched for `SESSION_LIFETIME_DAYS` (default **7**) has to sign in
again. The expiry lives inside the signed cookie, so it can't be extended client-side.

This is a security control, not just convenience — a console session can run code as
SYSTEM on any enrolled machine (see below). Shorten it if operators sign in from
machines they don't control; `/logout` always ends a session immediately.

## Permission groups

Signing in is one gate; **what you can then do is a second one**, enforced per
endpoint in [hub/permissions.py](../hub/permissions.py) (model) and
[hub/permissions_web.py](../hub/permissions_web.py) (the shared `access` object and the
admin API). Administered at `/permissions`.

A **permission group** carries a set of capabilities and a machine scope. An operator
gets the union of every group they're in. Scope is an explicit machine list (`list`), the
whole fleet (`all`), the PCs in Active Directory OUs (`ad_ou`, below) or the PCs in hub PC
folders (`folder`, below).

| Capability | Grants |
|---|---|
| `view` | see these machines, their history and their command results |
| `view_audit_log` | read the audit log (info + notice; **not** machine-scoped) |
| `view_security_audit` | additionally see security-level entries — does nothing alone |
| `issue_commands` | run scripts and restart/shutdown/install — **code execution as SYSTEM** |
| `remote_control` | start a remote view/control session |
| `deploy_packages` | schedule software deployments |
| `manage_backups` | configure backups and trigger restores (deliberately fleet-wide) |
| `manage_firmware` | change BIOS settings and flash firmware — the one action with no way back |
| `manage_patches` | approve updates, schedule maintenance windows, push a run outside one |
| `read_recovery_keys` | read an escrowed BitLocker recovery password |
| `locate_device` | ask a device where it is, on demand |
| `manage_device_policy` | write app and time policy for managed devices |
| `wipe_device` | lock or factory-reset a managed device |
| `manage_rules` | write rules (a rule that sends commands also needs `issue_commands`) |
| `manage_device_groups` | write device groups — saved target filters |
| `manage_pc_tree` | create PC folders and file PCs into them — a folder is a scope, so filing is a grant |
| `manage_group_policy` | write Group Policy and link it to the PC tree ([group-policy.md](group-policy.md)); a policy that runs a script also needs `issue_commands` |
| `view_tickets` / `manage_tickets` | read / answer tickets opened from a PC |
| `manage_onboarding` | create and revoke onboarding codes |
| `manage_settings` | change hub settings; delete/merge machines, pin sensors, dismiss alerts |
| `manage_users` | edit the registered-users directory (a profile directory, not access) |
| `manage_permission_groups` | create and edit groups — i.e. grant anyone, including themselves, any of the above |

The authoritative list, with the reasoning behind each split, is `CAPABILITIES` in
[hub/permissions.py](../hub/permissions.py); the group editor renders from it.

### Nesting groups

Groups nest the way Active Directory groups do. Give a group a **Parent group** in the
editor, or open a group and press **New subgroup**. The page then shows the tree on the
left and, on the right, the selected group and the groups directly beneath it.

**A member of a subgroup holds everything every group above it grants**, capabilities and
machines both, and a fleet-wide (`all`) group anywhere above makes every group beneath it
fleet-wide. A subgroup can only **add** to what its parents grant, never take anything
away: if "Hospital" grants *View* over the hospital PCs and "Hospital / Ward 3 admins"
adds *Issue commands* over the Ward 3 PCs, an admin in the subgroup can view every
hospital PC and run commands on all of them, while a plain "Hospital" member gets nothing
extra. Where that is too broad, make a narrower group beside it, not beneath it.

In the editor, inherited capabilities show ticked and locked with the group they come
from, and the scope section says what the parents already grant. In the list, inherited
capabilities are dashed badges. Neither can be changed on the subgroup — edit the parent.

- Groups nest at most **8** levels deep, and a group cannot be moved beneath itself or one
  of its own subgroups.
- **A group with subgroups cannot be deleted.** Move or delete the subgroups first; the
  hub will not silently re-parent them or take their access away.
- An invite link into a subgroup is checked against what joining it really grants, parents
  included — so an empty subgroup of a fleet-wide group cannot be handed out by someone
  whose own access is narrower.
- Moving a group to a new parent is in the audit log at security level, because it changes
  what every member of it and of every group beneath it holds.

> ⚠️ **`ALLOWED_EMAILS` bypasses all of it.** It is the break-glass superuser list:
> every capability over every machine, and the hub refuses to start with it empty.
> Keep it to the smallest possible set of accounts and put everyone else in a group.

### Inviting someone with a link

**Permission Groups → Invite Links** (`manage_permission_groups`) creates a link that grants
the groups you choose to whoever signs in through it. It is the same grant as typing their
address into a group — a redeemer becomes an ordinary group member — but you do not have to
know the address first, and the person gets told.

Each link carries its own bounds:

| | |
|---|---|
| **Grants** | one or more existing permission groups, or a new group you define in the form (which is created for real, and is editable on this page like any other) |
| **Number of uses** | how many *different* people the link admits. 1 is a single-use invite; the same person signing in twice does not spend a second use |
| **Expires after** | 1, 7 or 30 days, or never. 7 days is the default; choose "never" only if you will revoke the link by hand |
| **Restrict to these addresses** | optional. Blank means anyone holding the link, up to the number of uses |

The link is shown **once**, when you create it — the hub stores only its hash and cannot show
it again. An invite can never grant more than you hold yourself, and never grants break-glass.
**Revoke** kills a link while keeping the record of who joined through it; **Delete** removes
both, and neither takes anyone's access away — that is done by removing them from the
permission group, the same as for anyone else.

Someone opening the link sees the invite's label, who sent it, and the names of the groups it
grants, then signs in through your ordinary provider. Every create, redemption, revoke and
delete is in the audit log at security level.

### Granting a group by directory group

A permission group can also name **directory groups** — Entra security groups, AD
groups, or whatever else your issuer asserts — instead of listing operators by email.
Anyone whose sign-in carries a matching group gets that permission group, so a new
hire is granted access by being put in the right group in your directory, and never
touches the FleetHub console at all.

Add them in the group editor's **Directory groups** field. Paste whatever your
provider sends: Entra sends group **object IDs** (GUIDs), on-prem/ADFS issuers send
distinguished names, some send plain names. Matching is exact but case-insensitive —
nothing is prefix- or substring-matched, so `CN=Hospital` will not open a group named
`CN=Hospital IT`.

To find out what your provider actually sends, sign in and open the group editor: the
tokens **your own** sign-in carried are listed under the field, and clicking one adds
it. (Shown only to people who can edit permission groups.)

Configure your issuer to send the claim first — the hub reads `groups`, `roles` and
`wids`. For Entra, that means adding a **groups claim** to the app registration
(App registration → Token configuration → Add groups claim); without it the ID token
carries no groups and every mapping matches nothing.

Three things worth knowing before you rely on this:

- **Membership is read once, at sign-in.** Removing someone from a directory group
  takes effect the next time they sign in, not immediately — so revoking urgent access
  means removing them from the group *and* waiting out `SESSION_LIFETIME_DAYS`, or
  shortening it. The same is true in reverse: a newly added mapping or membership
  needs a fresh sign-in.
- **Only groups this hub maps are remembered.** The session records the mappings it
  matched, not every group you're in — a user in 200 Entra groups would otherwise
  overflow the signed session cookie.
- **Entra stops sending the claim past ~200 groups.** It sends a Graph pointer
  instead, which this hub does not follow. Such an account is refused with a message
  saying so; grant it by email address instead.

`/permissions` shows a group's mappings beside its members, but it cannot list *who*
is in a mapped directory group — the hub never queries your directory, it only
believes what an issuer signs at sign-in. Your directory remains the place that
answers "who has this access?".

### Scoping a group by Active Directory OU

With **Active Directory sync** on (below), a group's machine scope can be *"machines in
an AD OU"* instead of an explicit list. Machines then follow the directory: a PC re-filed
into a scoped OU joins the group at the next sync, and one moved out leaves it. **Nested
OUs are included** — scoping to `OU=Clinical` covers `OU=Ward 3,OU=Clinical` — and
matching is component-wise, so `OU=Clinical` never captures `OU=NotClinical`.

The group editor shows which machines the OUs currently resolve to, because an OU scope
is a rule and the only way to check you picked the right one is to see its effect.

### The PC tree, and scoping a group by PC folder

**PC tree** (in Operate, `/pc-tree`) shows the fleet the way ADUC shows a domain: the
tree on the left, the PCs under the selected node on the right. It has two halves:

- **Active Directory**, read-only, built from the OU each PC was last synced into. It is
  exactly what AD says; change it in AD.
- **Hub folders**, the hub's own nested folders, for what AD does not express — workgroup
  PCs, a hub with no directory at all, or a grouping that cuts across OUs. A PC sits in
  **at most one** folder. Tick PCs and choose **Move selected to…**, or drag rows onto a
  folder in the tree.

A permission group's scope can be *"machines in a PC folder"*: it then covers every PC
filed in the chosen folders **or in any folder beneath them**, and follows the tree at
once — filing a PC into the folder grants it, moving it out revokes it. Rules, watchdogs
and device groups can aim at a folder the same way (the **PC folder** target, with
*and sub-folders*).

Because a folder is a scope, **filing a PC is a grant**. That is why it needs its own
capability, `manage_pc_tree`, and why an operator can file or move only PCs already in
their own scope, and can move a folder only if every PC beneath it is in their scope.
Everyone with `view` can browse the tree, narrowed to their scope: an OU holding none of
your PCs is not shown. A folder cannot be deleted while it holds PCs or subfolders, or
while a rule, watchdog, device group or folder-scoped permission group still names it.

The rules **AD organisational unit** target now matches OUs component-wise, like
permission scope always has: `OU=Cajas` no longer reaches `OU=NoCajas` (hub 1.158.0).

## Active Directory sync

Optional, off by default, and **nothing contacts a domain controller until you turn it
on**. When enabled, the hub binds to a DC over LDAPS with a read-only service account and
reads **computer objects**, so each machine record gains its distinguished name, OU,
object GUID, owner (`managedBy`) and AD-recorded OS. That is what makes OU-based scoping
above possible.

Configure it at **Settings → Active Directory**:

| Setting | |
|---|---|
| `directory.enabled` | master switch; off by default |
| `directory.server` | `ldaps://dc1.corp.local` |
| `directory.base_dn` | where to search, e.g. `OU=Computers,DC=corp,DC=local` |
| `directory.bind_dn` | a **read-only** service account |
| `directory.sync_interval_minutes` | 60 by default |
| `directory.computer_filter` | `(objectClass=computer)` by default |

The bind **password** goes in `.env`, never in hub settings — the settings table is
readable by anyone with `manage_settings` and is included in the hub database backup:

```
DIRECTORY_BIND_PASSWORD=...
```

Sync needs the `ldap3` package (`pip install -r hub/requirements.txt`). It is imported
lazily, so a hub that never enables AD does not need it installed; if the feature is on
and the library is missing, the status card says exactly that.

**Press "Sync now"** after configuring — it runs a pass synchronously and reports what it
found, so a wrong bind DN takes seconds to diagnose instead of an hour.

Things worth knowing:

- **Nothing is ever written to your directory**, and no machine records are created from
  it. AD computers with no agent installed are counted and otherwise ignored — inventing
  machines from AD would fill the console with rows that never report.
- **A managed machine with no computer object raises a review alert** (`ad_unmatched`),
  auto-resolved when it reappears. Usually it means a PC was never domain-joined, was
  renamed, or is half-decommissioned. Turn it off with
  `directory.alert_on_unmatched`.
- **A machine that leaves AD has its AD fields cleared**, deliberately. A stale OU left
  behind on a deleted computer account would keep granting access through an OU-scoped
  group indefinitely.
- **Plain `ldap://` is refused by default** — a simple bind sends the service account's
  password in cleartext. There is an explicit opt-out for isolated labs.
- Searches are **always paged**. AD's default server limit is 1000 results and it
  truncates *silently*, which would look like half your fleet vanishing from the
  directory.

> ⚠️ **Removing a machine from an OU removes access at the next sync, not instantly.**
> OU scope is as current as your last sync (hourly by default). For urgent revocation,
> change the group rather than the directory.

The UI hides buttons an operator can't use, but that's presentation — the server-side
gate on each endpoint is the control, and `/api/permissions/me` deliberately reveals
nothing the caller doesn't already hold.

Two adjacent pages: `/users` (the registered-users directory — profiles, auto-created
on first login, `manage_users`) and `/inventory` (fleet-wide hardware/asset view).
