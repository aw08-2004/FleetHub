# Hub release notes

One file per hub version, named `<version>.md`, for every version published as a container image
(`ghcr.io/aw08-2004/temp_monitor-hub:<version>`), which is every hub version since `1.143.0`.
The directory listing is the archive; there is no separate changelog to keep in sync with it.
Hub versions before 1.143.0 have no file here: the hub's history up to then lives in
`ROADMAP.MD`, entry by entry, and in `git log`.

**What these files are for.** A hub is not released the way an agent is. A native hub updates
itself from `main` and an image hub is `docker compose pull`ed, and neither reads these files.
They exist for the operator deciding *whether and when* to take a version: what changed, what
must be deployed in which order, and what has to be done by hand. No GitHub release is created
for a hub version (`gh release list` shows agent, Linux, Android and client releases only), so
if one is wanted, publish the file with `--notes-file`, never the text (the reasons are in
`agent/release-notes/README.md`), for example `gh release create hub-v<version> --notes-file
deploy/docker/release-notes/<version>.md`. The `hub-v` tag prefix follows the other components'
(`agent-v`, `linux-agent-v`, `client-v`) and is a suggestion: no hub release has used one yet.

## Format

The same Conventional Changelog layout `release-please` produces, and the one the agent notes use
from 3.43.0 on:

- A heading `## X.Y.Z (YYYY-MM-DD)`, the date the version reached `main`.
- Only the sections that have entries, in this order: `### ⚠ BREAKING CHANGES`, `### Features`,
  `### Bug Fixes`, `### Performance Improvements`, `### Code Refactoring`, `### Documentation`,
  `### Tests`.
- One bullet per change: `* **scope:** what an operator will notice (#PR) (commit)`, with the
  scope an area of the product (`remote`, `devices`, `installer`, ...).
- Anything that constrains the rollout -- an agent version that must be deployed first or must
  wait, a setting to change by hand -- goes under BREAKING CHANGES as a full sentence saying what
  to do. **Deploy order is hub first, then the agent**, so a hub note says what the agent will
  need from it, and an agent note says which hub it needs.

A version's number is the container tag: `HUB_VERSION` in `hub/app.py`, strictly increasing and
three numeric components (`VERSIONING.md`). `.github/workflows/hub-image.yml` refuses to publish
a tag that already exists, so a version has exactly one set of notes.
