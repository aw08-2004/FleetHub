# Android agent release notes

One file per release, named `<version>.md`, holding the body published with that version's
`android-agent-v<version>` GitHub release. The directory listing is the archive; there is no
separate changelog to keep in sync with it. Same convention as `agent/release-notes`, so read
that one's README too -- everything it says about publishing applies here unchanged.

Write for the operators who run the fleet, not for whoever changed the code: what they will
notice, what it costs them, and what to do when it misbehaves.

From 0.3.0 on, the layout is the Conventional Changelog one that release-please produces, the same
as the Windows agent's from 3.43.0 (`agent/release-notes/README.md`, *Format*): a heading
`## X.Y.Z (YYYY-MM-DD)`, then only the sections that have entries (`### ⚠ BREAKING CHANGES`,
`### Features`, `### Bug Fixes`, `### Performance Improvements`, `### Code Refactoring`,
`### Documentation`, `### Tests`), one bullet per change as `* **scope:** what an operator will
notice (#PR) (commit)`. A file for a release that has not been cut carries `(unreleased)` in place
of the date; replace it with the date the release is cut. `0.2.0.md` to `0.2.3.md` are long-form
prose and stay as they were published.

Two things are different from the Windows agent, and both belong in every file here, whatever the
layout -- put them under Features, Bug Fixes or Documentation as fits:

- **A managed phone can do things a PC cannot, to a person rather than to a machine.** Locating a
  device, listing the apps somebody uses and erasing what they are holding are not features of
  the same kind as a disk report. Say plainly what starts being collected, who can see it, and
  what the person holding the device sees.
- **The version line is not comparable to the other two agents'.** A reader who knows the Windows
  agent is on 3.x will read a 0.x phone as neglected unless the file says otherwise.

## Publishing one

Pass the path, never the text:

```powershell
.\release.ps1 -Version 0.2.0 -NotesFile .\release-notes\0.2.0.md
```

`-NotesFile` hands the path to `gh --notes-file`, so gh reads the bytes and nothing parses them
on the way. Prose routed through PowerShell's native-argument binding has mangled a published
release body twice on the Windows agent -- escapes eaten, and an embedded quote splitting the
argument so gh went looking for a file named after a word in the middle of a sentence. Anything
longer than one plain sentence goes in a file.

To fix a release already published with a mangled body:

```powershell
gh release edit android-agent-v0.2.0 --repo aw08-2004/Temp_Monitor --notes-file .\release-notes\0.2.0.md
```
