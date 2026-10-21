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
| **Registry values** | Writes DWORD, string, expandable-string and multi-string values under `HKLM\SOFTWARE\Policies` | an agent that supports Group Policy (see below) |
| **Scripts** | Runs a library script once on each PC the policy applies to, and again only when the script itself changes | the *Issue commands* capability as well |

**Registry values are limited to `HKLM\SOFTWARE\Policies`**, the tree Windows reserves for
policy and the one every ADMX template writes. `SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate`
(owned by [patch management](managing-machines.md)) and `SOFTWARE\Policies\FleetHub` are refused.
The agent checks the same list itself, so a policy can never write anywhere else even if the
hub were wrong. To find the value an ADMX setting writes, look it up in the template's
documentation (Edge, Chrome, Office and Windows all publish them) and enter the key path,
value name, type and value.

**FleetHub never overwrites a value it did not write.** A value that already exists, set by a
domain GPO, Intune or a person, is left alone and shown as **Managed elsewhere** in the
resultant set. A value FleetHub set that something else later changes is let go and shown the
same way, so FleetHub never fights a domain GPO. A value FleetHub set that somebody simply
deletes is put back. When a value stops applying (its policy is unlinked, disabled or
deleted), the agent removes it, as long as it still holds what FleetHub wrote.

**Scripts** run through the normal command queue, on PCs that are online, as
`gpo:<policy id>`. The only variable a policy script can use is `{{sys.machine}}`; a script that
needs a live reading is skipped with a reason rather than run with "unknown" in it. A PC whose
name is not a plain hostname (letters, digits, `.`, `_`, `-`) is skipped too, so a name is never
written into code that a shell could read as syntax. A script a
policy uses cannot be deleted from the library.

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

When the hub uninstalls an agent, the agent first removes the registry values it wrote. An
uninstall with `agent-install.ps1 -Uninstall` removes the values listed in the agent's marker
(`HKLM\SOFTWARE\FleetHub\GroupPolicy`).

## Not supported

- **ADMX templates** are not imported; enter the values they write instead.
- **User settings (HKCU)** are not supported. Per-user hives are loaded only while somebody is
  signed in.
- FleetHub does not create or edit **real Windows GPOs** in your domain, and does not touch
  SYSVOL.
