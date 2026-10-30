# Group Policy

**Group Policy** (Operate → Group Policy, `/group-policy`) lets you write a set of settings
once and apply it to every PC beneath a node of the [PC tree](access.md#the-pc-tree-and-scoping-a-group-by-pc-folder):
every PC, an Active Directory OU, or a hub folder. It follows the model of Windows' Group
Policy Management console on purpose, so the rules for which policy wins are the ones an IT
group already knows.

The page has the tree on the left:

- **Group Policy Objects** lists every policy, with what it sets and where it is linked.
- **Resultant set of policy** shows, for one PC, which policies apply, what every setting ends
  up as, which policy won it, and what the PC reported back.
- **Every PC**, the **hub folders** and the **Active Directory** OUs are the nodes you link
  policies to. Select one to see and change its links.

## What a policy can set

| Kind | What it does | Needs |
|---|---|---|
| **Agent settings** | Overrides hub settings marked as agent settings (report interval, sensor interval, network collection, the ticket tray and its submitters, the sensor preference) on the PCs the policy applies to | any agent version |
| **Administrative Templates** | The settings `gpedit.msc` shows -- Enabled, Disabled or Not configured, with their options -- saved as the registry values they write | an agent that supports Group Policy; the templates come from your PCs (see below) |
| **Registry values** | Writes DWORD, string, expandable-string and multi-string values under `HKLM\SOFTWARE\Policies` | an agent that supports Group Policy (see below) |
| **Scripts** | Runs a library script once on each PC the policy applies to, and again only when the script itself changes | the *Issue commands* capability as well |

**Registry values are limited to `HKLM\SOFTWARE\Policies`**, the tree Windows reserves for
policy and the one every ADMX template writes. `SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate`
(owned by [patch management](managing-machines.md)) and `SOFTWARE\Policies\FleetHub` are refused.
The agent checks the same list itself, so a policy can never write anywhere else even if the
hub were wrong. To find the value an ADMX setting writes, look it up in the template's
documentation (Edge, Chrome, Office and Windows all publish them) and enter the key path,
value name, type and value.

**The values go into the PC's local Group Policy.** Since agent 3.50 the agent writes them
into the PC's own local GPO (`C:\Windows\System32\GroupPolicy\Machine\Registry.pol`) and
asks Windows to apply it, exactly as `gpedit.msc` does when you click OK. So `gpedit.msc` on
the PC shows FleetHub's settings as configured, `gpresult` lists them under Local Group Policy,
and Windows itself writes the values and removes them again when they stop applying. A domain
GPO is applied after the local one and so still wins over FleetHub, which is the order an
administrator expects. Older agents write the values straight into the registry; the resultant
set says which a PC does.

**FleetHub never overwrites a value it did not set.** A value that already exists, set by a
domain GPO, Intune or a person, is left alone and shown as **Managed elsewhere** in the
resultant set; so is a setting somebody made in `gpedit.msc` on the PC. A value FleetHub set
that somebody later changes in `gpedit.msc` is let go and shown the same way, so FleetHub never
fights the PC's own administrator. A value FleetHub set that somebody simply removes (sets to
*Not configured*) is put back. When a domain policy overrides a FleetHub value, the PC reports
it as **Managed elsewhere** a few minutes after the change; the FleetHub value stays in the local
GPO and takes effect again if the domain policy goes. When a value stops applying (its policy
is unlinked, disabled or deleted), the agent takes it out of the local GPO, as long as it still
holds what FleetHub wrote.

**Scripts** run through the normal command queue, on PCs that are online, as
`gpo:<policy id>`. The only variable a policy script can use is `{{sys.machine}}`; a script that
needs a live reading is skipped with a reason rather than run with "unknown" in it. A script a
policy uses cannot be deleted from the library.

## Administrative Templates

In the policy editor, **Browse templates…** opens the tree Windows' Local Group Policy Editor
shows: **Computer Configuration** and its categories on the left, the settings of the selected
category on the right. Search finds a setting by name across every category. Open a setting
to choose **Enabled**, **Disabled** or **Not configured**, fill in its options (they apply only
when Enabled, as in gpedit) and read its help text.

**The templates come from your PCs.** A Windows agent that supports them hashes its own
`C:\Windows\PolicyDefinitions` (the `.admx` files with their English, German and Spanish
`.adml`) and the hub asks one PC per Windows build to send the set. Every PC on that build
shares it, so a fleet sends a handful of sets, not one per PC. When your PCs run different
builds, pick the set at the top of the browser; the one most of your PCs report is selected
first. Templates you add to a PC's `PolicyDefinitions` (Edge, Chrome, Office) are picked up
when that PC's folder changes.

A template setting is saved as the registry values it writes, checked by the same rules as
values typed in by hand, and those values are what the PCs receive. So:

- **Settings that write outside `HKLM\SOFTWARE\Policies`**, and settings that use a QWORD
  value, are listed greyed out with the reason and cannot be set.
- **User Configuration** settings are shown but cannot be set yet.
- **Some settings delete a value when Disabled.** FleetHub deletes it too, the way gpedit
  does: the local GPO says "delete this value", and the resultant set shows it as **Deleted**.
  This needs agent 3.50; an older agent is not sent deletes, and the resultant set says so.
- **A policy sets each registry value once.** A template setting and a typed value, or two
  template settings, that write the same value are refused when the policy is saved.

Between policies, a template setting follows the usual order: the policy that wins replaces the
setting whole, so a nearer policy's **Disabled** does not leave the farther policy's options
behind.

## Which policy wins

PCs are processed from the top of the tree down, and **the last policy applied wins** a
conflict:

1. **Every PC**.
2. The **AD domain**, then each **OU** down to the PC's own.
3. The **hub folders**, top folder first, down to the PC's own folder.

Hub folders come after AD on purpose: a folder is the hub's more specific statement about a PC
that AD files somewhere broader.

Within one node, **link order 1 wins**; use ↑ and ↓ to change it. Two more switches change the
order, exactly as in Windows:

- **Block inheritance** on a node stops policies linked above it from applying to the PCs
  beneath it.
- **Enforced** on a link means nothing below can override it, and Block inheritance does not
  stop it. Between two enforced links, the one higher in the tree wins.

A disabled policy, or a disabled link, does not apply. Scripts are not "last wins": every
script from every applying policy runs, and the same script from two policies runs once.

When something is not what you expect on a PC, open **Resultant set of policy** for it. It
lists the policies in processing order and names the winner of every setting.

## Who can do what

- Reading policies and a PC's resultant set needs **View** (and the PC in your scope).
- Creating, editing, linking and deleting policies, and blocking inheritance, needs
  **Manage Group Policy**. Treat it as security-sensitive: `HKLM\SOFTWARE\Policies` includes
  Windows Defender and firewall policy, so a policy can weaken a PC's protection, even
  without *Issue commands*. A policy that runs a script, or an edit that adds or removes one,
  also needs **Issue commands**.
- **Every PC a change can reach must be in your own scope.** Linking at a node, changing a link
  or blocking inheritance reaches every PC beneath that node; editing or deleting a policy
  reaches every PC beneath every node it is linked to.
- **Editing a library script that a policy runs** counts as a change to that policy's reach:
  a changed script runs again on every PC the policy applies to, so the editor's scope must
  cover every PC beneath the nodes the policy is linked to. The edit's audit entry lists the
  policies as well as the rules that use the script.
- **A link remembers its author's scope.** A link created by an operator scoped to some PCs
  applies only to those PCs, even if another PC is later filed beneath the node. Such links
  are marked in the node view.

Every change is in the audit log at security level.

## Agent support

Agent settings and scripts work with every agent already in the field. Registry values need an
agent that supports Group Policy: such an agent reports it on its heartbeat, and the resultant
set says when a PC's agent has not, instead of showing the values as pending forever. The
agent applies its document on its own one-minute loop and keeps it across restarts, so a PC
that boots without network keeps its policy.

Agent 3.50 and newer write the local GPO; the resultant set says so for each PC. The first
time agent 3.50 runs on a PC, it moves the values an older agent wrote directly into the local
GPO with the same data, so nothing a user sees changes. If Windows does not apply a value that
is in the local GPO (local policy processing turned off by a domain policy, for example), the
PC reports an error for it after a few minutes, and the agent forces one policy refresh an hour
at most to try again.

When the hub uninstalls an agent, the agent first takes the values it set out of the local GPO.
An uninstall with `agent-install.ps1 -Uninstall` does the same for the values listed in the
agent's marker (`HKLM\SOFTWARE\FleetHub\GroupPolicy`), copies every other entry of the local
GPO back unchanged, and leaves a local GPO it cannot read exactly as it is.

## Not supported

- **User settings (HKCU)** are not supported yet, typed or from a template.
- FleetHub does not create or edit **real Windows GPOs** in your domain, and does not touch
  SYSVOL. It edits only each PC's own local GPO.
- Leaving `gpedit.msc` open on a PC while FleetHub changes that PC's policy, and then clicking
  OK, can overwrite FleetHub's change; the agent puts its values back within a minute.
