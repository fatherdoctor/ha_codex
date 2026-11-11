# Updates and rollback

How a release of this fork reaches a Home Assistant install, how it is tested before it is tagged, and how a bad
release is undone without anyone at the house doing anything.

## How an update reaches an install

This repository is a Home Assistant app (add-on) repository: `repository.yaml` at the root and the app in
`codex-cli-worker/`. An install subscribes to it once (**Settings** > **Apps** > **Install app** > menu >
**Repositories**, add `https://github.com/fatherdoctor/ha_codex`). From then on the Supervisor reads
`codex-cli-worker/config.yaml` on the default branch, and when its `version` is newer than the installed one, offers
the update. The app is not built on the install: `config.yaml` names an image (`ghcr.io/fatherdoctor/codex-cli-worker`)
and the Supervisor pulls the tag equal to the version, which the release workflow builds and publishes when a
`v<version>` tag is pushed.

So the order of a release is fixed: the version change merges to `main`, the tag is pushed, the workflow publishes the
images and the HACS ZIP. Until the images exist, an install that sees the new version cannot pull it; publish the tag
soon after the merge. (The release and image workflows are disabled on this fork until its first release is planned.)

The integration (`custom_components/codex_cli`) carries the same version and updates through HACS from the release's
`codex_cli.zip`.

## On the install: auto-update and a backup first

- **Auto-update:** on the app's page, turn on **Auto update**. The Supervisor then installs a new version by itself.
- **Backup before updating:** a manual update in Home Assistant offers to back up the app first; leave it on. Whether an
  *automatic* update also takes that backup depends on the Supervisor version in use, so check it on the test
  machine for the version you run before relying on it. The backup is the second way back (below), not the first.
- The app's backup holds its data folder, including the Codex sign-in (`/data/codex-home`), and leaves out the
  documentation cache, the data cache and `.ssh` (`backup_exclude` in `config.yaml`). Task logs are under
  `/config/codex_tasks`, which the Home Assistant configuration backup covers.

## Before a tag: the lab pass

Every release is installed and tested on a Home Assistant OS test machine before it is tagged. The test is run on the
merged commit, through the Supervisor, as an install would get it. When it passes, add a record on `main`:

`release/lab-passes/v<version>.json`

```json
{
  "version": "0.1.81.2",
  "commit": "<the full 40-character commit that was tested>",
  "result": "pass",
  "lab": "Home Assistant OS test machine",
  "tested_on": "2026-11-10",
  "notes": "What was run, or a link to the checklist."
}
```

Then tag:

```bash
python3 scripts/fork_release.py check-tag 0.1.81.2   # what would be tagged, and why; changes nothing
python3 scripts/fork_release.py tag 0.1.81.2 --push
```

The script refuses when there is no record or it does not say `pass`; when the commit is not on `main`; when the app,
the integration and the changelog do not all say that version at that commit; when the tag already exists; or when
the version is not newer than every existing tag (the Supervisor only offers a newer version). The tag goes on the
tested commit, not on the later commit that added the record.

## Rollback: roll forward to the last good code

The Supervisor never offers an older version, so a rollback is a new release whose code is the last good one:

```bash
git checkout -b rollback-0.1.81.3 origin/main
python3 scripts/fork_release.py rollback 0.1.81.1          # the version to go back to
git commit -m "Roll back to 0.1.81.1 as 0.1.81.3"
# open a pull request, merge it, then on main:
python3 scripts/fork_release.py tag 0.1.81.3 --push
```

`rollback` puts `codex-cli-worker/` and `custom_components/` back exactly as they were at `v0.1.81.1` (files added
since are removed), sets the next version (`0.1.81.3` here; `--to` picks another), adds a changelog entry above the
existing history, and writes a rollback record. The install takes it as an ordinary update; nobody there has to do
anything.

A rollback needs no new lab pass, because its code already passed one: `tag` checks that the version it restores has
its own lab-pass record and tag, and that the rollback differs from that tag only in the version fields and the
changelog. Any other change, however small, is refused; that would be a new release and needs its own lab pass.

The image is rebuilt from the restored files. The base image and the Codex CLI are pinned by digest and checksum, so
they are the same; Alpine packages installed in the image are not pinned and can be newer than in the original build.

### Stored data

A rollback runs the old code on the data the newer release left behind. A release that changes how the app stores
anything (conversations, queue, settings, sign-in) must say so in its changelog with a bullet that starts
`Stored data:`, and say whether older builds still read it. `rollback` stops when a release newer than the target has
such a bullet; check by hand that the older build copes, then run it again with `--accept-stored-data`. If it does
not, use the second way.

### The second way: restore the backup

If the roll-forward cannot be used (the stored data changed, or the install cannot reach the update), restore the
app's pre-update backup from **Settings** > **System** > **Backups**. That needs someone with access to the install.
