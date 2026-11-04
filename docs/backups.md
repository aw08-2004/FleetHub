# Backups

## Backups

A consistent snapshot of the hub database, compressed, encrypted **on the hub** and pushed
offsite on a schedule. Core logic in [backups.py](../hub/backups.py), HTTP surface in
[backups_web.py](../hub/backups_web.py), UI at `/backups`, and the restore tool at
[restore_backup.py](../hub/restore_backup.py). State lives in the same SQLite DB
(`backup_destinations`, `backup_runs`, `backup_state`, plus `backup_machine_config`,
`backup_file_sets`, `backup_files` and `backup_restores` for the per-PC half below).

**`VACUUM INTO`, never a file copy.** The database is opened WAL and written live by the
ingest path and the `db_writer` thread. Copying `temp_v2.db` while that is happening gives
you a torn file plus a `-wal` sidecar you didn't copy — a backup that restores to
"database disk image is malformed", discovered on the day you need it. `VACUUM INTO` asks
SQLite for a transactionally consistent, already-compacted snapshot instead.

**The provider only ever sees ciphertext.** Snapshot → gzip → AES-256-GCM in 4 MiB chunks
→ HTTPS PUT. Each artifact gets its own random data key, wrapped by the master key. Chunk
AAD binds `sha256(header) ‖ counter ‖ final-flag`, so a tampered header, a reordered chunk
and — the one that actually happens — a **truncated upload** all fail to decrypt rather
than restoring as a plausible-looking corrupt database.

> ### ⚠️ The master key is not recoverable
>
> `BACKUP_MASTER_KEY` in `.env` is the only thing that can decrypt your backups. It is
> deliberately **not** in the hub database — a key stored inside the thing it protects
> protects nothing. If this server is lost and the key was never written down elsewhere,
> every backup ever taken is permanently unreadable.
>
> The hub generates it once, shows it once, and nags on **Fleet tasks → Backup** until an operator
> confirms it is stored somewhere else. Every reveal is written to the audit log.
>
> **Moved or reinstalled the hub?** A hub that comes up without its `.env` generates a new
> key, and from that moment it cannot read a single archive the old installation wrote —
> even though you still have the key that opens them. Tools → Backup → **Use an existing
> key** takes that key and adopts it, replacing the generated one (it asks first, and names
> the key it is about to discard). Destination credentials are re-encrypted under the
> imported key in the same step, so the schedules keep working; any it could not read are
> counted back to you so you know which ones to re-enter. The key goes in and is never
> echoed back, and the import is written to the audit log like every other key event.
>
> To restore, you need the key and the file — **nothing else**. No hub, no database, no
> network:
>
> ```
> python hub/restore_backup.py --in 20260721T030000Z-temp_v2.db.gz.fhb --out temp_v2.db --verify
> python hub/restore_backup.py --in <file>.fhb --info     # just read the header
> ```
>
> Then stop the hub service, move the old `logs/temp_v2.db` aside (with its `-wal`/`-shm`),
> drop the restored file in, and start the service. `--verify` runs
> `PRAGMA integrity_check` first, which is worth the seconds.

**Destinations: S3-compatible or WebDAV, your choice per destination.** S3 covers AWS,
MinIO, Backblaze B2 and Wasabi — signed with SigV4 implemented in ~100 lines of stdlib
`hmac` rather than pulling ~80 MB of botocore onto a hub whose whole install is ~2 MB,
and checked against AWS's published test vectors in the suite. WebDAV covers Nextcloud,
ownCloud and IIS, with Basic auth over TLS. Plain `http://` is refused for anything but a
loopback host, so a typo cannot ship your credentials in clear.

**Credentials never touch the `settings` table.** Settings get rendered into a form,
returned wholesale by `as_dict()`, and partly shipped to agents in `agent_config()` — an
S3 secret key belongs in none of those. They live encrypted with the master key in
`logs/backup_secrets.json`, addressed by destination id (which is the AAD, so a credential
blob copied between destinations fails rather than authenticating somewhere unintended).
They go in and are never returned — the edit form's empty credential field means
"unchanged".

**Rotation reads the bucket, not a local record.** After each successful upload, artifacts
beyond `backup.hub_keep_generations` are deleted from the destination. Ordering comes from
the object key, which is timestamp-prefixed, so "newest N" is a lexicographic sort with no
dependence on remote mtime (S3 and WebDAV report it differently, from different clocks). A
generation you delete by hand stays deleted; `keep < 1` is refused rather than emptying the
bucket.

**Authorization** is the `manage_backups` capability — and it is deliberately *not*
machine-scoped. A hub database backup is the whole hub, so there is no coherent way to
hand it to an operator who sees nine machines out of forty. Read `manage_backups` as "can
eventually read everything, via a restore". The same capability also writes the four
`backup.*` schedule settings through `PUT /api/backups/schedule`; without that, arming a
backup would need `manage_settings` too, which would defeat the point of a narrow
capability.

**Endpoints** (all `manage_backups`): `GET /api/backups`, `GET /api/backups/runs`,
`POST /api/backups/key`, `POST /api/backups/key/reveal`,
`POST /api/backups/key/escrowed`, `POST /api/backups/destinations`,
`PUT|DELETE /api/backups/destinations/<id>`, `POST /api/backups/destinations/<id>/test`,
`PUT /api/backups/schedule`, `POST /api/backups/run`, `GET /api/backups/machines`,
`POST /api/backups/preview` (resolve path patterns against a real machine),
`POST /api/backups/files/run` and `POST /api/backups/files/cancel` (fleet-wide
"back up now" / cancel, over the caller's scope).

The per-machine routes need `manage_backups` **and** that machine in scope:
`GET|PUT /api/backups/machines/<machine>`, `GET /api/backups/machines/<machine>/manifest`,
`POST /api/backups/machines/<machine>/run`, `POST /api/backups/machines/<machine>/cancel`,
`POST /api/backups/machines/<machine>/restore`,
`GET /api/backups/machines/<machine>/restores`.

Agent-facing (token auth): `POST /api/agent/backups/upload/<run_id>`,
`POST /api/agent/backups/<run_id>/result`, and for restores
`GET /api/agent/backups/restore/<id>/plan`, `.../archive/<index>`, `.../result`.

> Revealing the key is a **POST with a JSON body**, not a GET — so it cannot be triggered
> by a link, an `<img src>`, or anything else a browser fetches on someone's behalf. Keep
> it that way.

Tunables live in Settings under **Backups**, or on **Fleet tasks → Backup** itself: on/off,
destination, interval, and generations to keep.

### When backups run

**When to back up** has three modes, the same for the hub database and for PC files:

| Mode | Runs |
|---|---|
| **Every N hours (the interval above)** | N hours after the last attempt. The default, and what every hub did before 1.171.0 |
| **At set times** | on the ticked days, at each time listed, e.g. Mon–Fri at 02:00 and 13:00 |
| **Cron expression (advanced)** | a five-field cron expression, e.g. `0 3 * * 0` for Sundays at 03:00 |

Under the picker, **Next runs** lists the next five runs. The hub works these out, so they
are in the **hub's** time, labelled with its UTC offset, even if your browser is in another
zone. The scheduler uses the same clock.

Set times replace the interval; the interval is greyed out while a schedule is set. A
backup is due at the first slot **after the last attempt**, which gives two guarantees:

- **A missed slot runs once, late.** A hub that was down at 02:00, or a laptop that was
  closed, backs up once when it is back — not once per missed night. A late backup is still
  worth having.
- **A hub that has never backed up runs at once** when you turn backups on, rather than at
  the first slot. You find out the destination works now, not at 02:00.

Cron follows the classic rules: Sunday is `0` or `7`, names like `MON-FRI` and `JAN` work,
and when both day-of-month and day-of-week are set, **either** one matches
(`0 0 13 * 5` is the 13th *and* every Friday). `@daily`, `@weekly` and friends are accepted;
`@reboot`, a seconds field, and Quartz's `L W # ?` are refused.

The settings are `backup.hub_schedule` and `backup.files_schedule`; unset means "use the
interval". `POST /api/schedules/preview` (`view`) answers the picker.

### Per-PC file backups

Configured on **Fleet tasks → Backup → Backup Settings**, per machine on that machine's
**Backup** tab. Path selection lives in [backup_paths.py](../hub/backup_paths.py).

**You never enumerate user profiles.** A pattern is written once with tokens and expanded
on each PC at backup time, so it keeps being right as people come and go:

| Token | Expands to |
|---|---|
| `%Users%` or `%User%` | every real profile (skips Public, Default, service accounts) |
| `%Desktop%` `%Documents%` `%Downloads%` `%Pictures%` `%Favorites%` `%AppData%` `%LocalAppData%` | that user's **actual** folder, per user |
| `%ProgramData%` `%SystemDrive%` `%windir%` `%ProgramFiles%` | machine-wide, no fan-out |

```
%Desktop%                      →  C:\Users\bob\OneDrive - Contoso\Desktop
                                  C:\Users\carol\Desktop
%User%\Scripts                 →  C:\Users\bob\Scripts, C:\Users\carol\Scripts
C:\Users\%Users%\Projects      →  C:\Users\bob\Projects, C:\Users\carol\Projects
```

`%User%` and `%Users%` are the same token — use whichever reads better. `%Users%` suits a
pattern on its own; `%User%\Scripts` suits a custom subfolder. Note that `%User%` does
**not** mean "whoever is logged in now": backups run as SYSTEM on a schedule, routinely
with nobody signed in, so every per-user token covers every profile.

> **Use `%Desktop%`, not `C:\Users\%Users%\Desktop`, for the standard folders.** With
> OneDrive Known Folder Move — common in orgs — the literal path is an empty stub and the
> real data lives under the OneDrive folder. The token reads each user's shell-folder
> registry and follows the redirection; the literal path does not, and would back up
> nothing while reporting success every night.

**An unknown token is refused, never treated as a literal.** `%Userss%` would otherwise
match nothing forever, with a green run beside it. Excludes take the same tokens plus
globs (`*.tmp`, `**\node_modules\**`); a pattern with no backslash matches on filename
anywhere, and excluding a folder excludes everything in it.

The **Preview** panel resolves your patterns against a real machine's reported profiles,
so you can see the actual folders — and the problems ("carol has no %Documents% folder
recorded") — before anything runs.

**Incremental, in chains.** Each run uploads only what changed; a full is forced every
`backup.files_full_every` runs. Rotation deletes **whole chains** — never an archive
inside one, because an incremental without its full restores to nothing.

**A PC that was switched off is not skipped — it catches up.** The scheduler only
dispatches to machines it can currently reach, so a laptop that was closed at 03:00 stays
*due* rather than being marked as attempted, and backs up within a minute of coming back
online. (Queuing a backup for an unreachable machine used to move its clock forward, so
it missed that night *and* the next one.)

**Back up now**, on the machine's **Backup** tab for one PC, or on **Backup Settings** for
everything in your scope. This also works on a machine that is switched off: the request
is remembered and answered when the PC reappears, so the button reports *started* or
*queued* rather than failing. Pressing it twice does not queue two backups, and a machine
already backing up will not start a second run — the request waits its turn.

**Cancel**, in the same two places. What it can stop depends on how far the backup got:
a *queued* request is dropped; a backup that has been sent to the PC but not yet started
is stopped before it begins; and one the PC has **already started** is marked cancelled —
it stops holding a concurrency slot and its result is discarded, but the PC finishes the
transfer it is in the middle of, because there is no way to recall a job an agent has
already picked up. The response says which of these happened. Any archive a cancelled
backup manages to upload is deleted automatically, so a cancel never leaves junk in the
bucket.

> A fleet-wide "back up now" can bring a lot of machines back at once, so
> `backup.files_max_concurrent` (default **3**) limits how many run simultaneously.
> The rest start automatically as slots free up; set it to 0 to remove the limit.

**Agents never hold the destination credential.** For S3 the hub mints a pre-signed PUT
scoped to that machine's folder; for WebDAV the agent uploads to the hub, which streams it
onward. And each machine gets a **derived** key, `HKDF(master, machine)`, not the master —
so a stolen laptop's key opens that laptop's backups and nothing else. Restore is still
one argument: the envelope header names the machine, and the master re-derives.

**Open files are captured via VSS.** The agent creates a Volume Shadow Copy
(`Win32_ShadowCopy`, the client-SKU route — `vssadmin create shadow` is Server-only) and
reads from the snapshot, so an Outlook PST or a document someone left open is still
backed up. If a snapshot cannot be created the run continues against the live filesystem
and reports which files it could not read, rather than failing.

**Junctions are never followed.** A Windows profile contains junctions pointing at their
own ancestors; following them is an infinite walk.

### Restoring a PC's files

The **Backup** tab on a machine's page browses what that machine has backed up — a folder
at a time, with a search box for when you know the filename but not the path. What it
lists is what is actually **recoverable**: files the user has since deleted, and chains
that have rotated away, are already gone from the answer, so you are never offered a
restore that fails halfway.

Tick files or folders, then choose:

- **Restore onto** — this machine, or another one. Restoring PC-3's data onto a brand-new
  PC-9 is the hardware-replacement case, and it needs you to have access to *both*
  machines: reading one PC's files and writing files onto another are separately checked.
- **Write to** — a folder like `C:\Restored` (files land under it in their original tree,
  and nothing live is touched), or blank to put them back where they came from.
- **Overwrite** — off by default, so a restore alongside surviving files is the safe path.

A ticked **folder** restores everything that was ever under it, including files that are
no longer in the folder you are looking at. The hub works out which archives hold which
version of each file and hands the agent a scoped, short-lived download per archive — the
same brokering as the upload path, so no machine ever holds the destination credential.

A restore that writes fewer files than you asked for is reported as **failed**, with the
counts and the first few reasons. "Restored 900 of 1000" needs someone to look at the
other 100, and a green row means nobody does.

> **Recovering without the hub.** The archive is a tar inside the same encrypted envelope,
> so the standalone tool opens it with the master key alone — useful when the hub is gone,
> or when you want one file without pushing anything onto a PC:
>
> ```bash
> python hub/restore_backup.py --in 20260721T030000Z-a1b2c3-000-full.fhb --list
> python hub/restore_backup.py --in <file>.fhb --extract C:\Recovered
> python hub/restore_backup.py --in <file>.fhb --extract C:\Recovered --match "*/Desktop/*"
> ```
>
> One master key opens every machine: the per-machine key is derived from it, and the
> envelope header says which machine to derive for.
