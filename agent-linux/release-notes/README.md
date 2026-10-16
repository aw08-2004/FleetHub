# Linux agent release notes

One file per Linux agent release, named `<version>.md`, holding the body published with that
version's `linux-agent-v<version>` GitHub release. The conventions are the Windows agent's --
see [agent/release-notes/README.md](../../agent/release-notes/README.md) -- and above all the
same rule for publishing: pass the path with `-NotesFile`, never the text.

```powershell
.\release.ps1 -Version 0.3.0 -NotesFile .\release-notes\0.3.0.md
```

0.1.0 and 0.2.0 were published without a file here; their bodies live only on their GitHub
releases.

## Format

From 0.4.0 on, notes use the Conventional Changelog layout that release-please produces, the same
as the Windows agent's from 3.43.0 (`agent/release-notes/README.md`, *Format*): a heading
`## X.Y.Z (YYYY-MM-DD)`, then only the sections that have entries (`### ⚠ BREAKING CHANGES`,
`### Features`, `### Bug Fixes`, `### Performance Improvements`, `### Code Refactoring`,
`### Documentation`, `### Tests`), one bullet per change as `* **scope:** what an operator will
notice (#PR) (commit)`. Anything that constrains the rollout -- a hub version that must be
deployed first, a step to do by hand on each box -- goes under BREAKING CHANGES as a full
sentence. `0.3.0.md` predates the layout and stays as it was published. A file for a release that
has not been cut carries `(unreleased)` in place of the date, and `release.ps1` publishes it as
written, so replace that with the date the release is cut.
