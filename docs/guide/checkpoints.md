# Checkpoints and going back

Noctyx keeps a known state of each machine before something changes it, and the way back from each change it
makes:

| What changed | Kept before | Going back |
|---|---|---|
| a Noctyx upgrade, an automatic upgrade, an undo | a **checkpoint** of the machine (a restic snapshot), and the files the change replaces | `noctyx revert <change>` |
| apt changing packages on a node | a **checkpoint** (the apt hook) | `noctyx restore` from that snapshot |
| the site config | a copy of the file at every rewrite | `noctyx config-restore` |
| Noctyx itself | a **self-checkpoint**: its git commit and the site config | `noctyx self-rollback` |
| what a recipe slot serves | its previous deployment ([recipes](recipes.md)) | `noctyx recipe-rollback` |

## Checkpoints

A checkpoint is a restic snapshot of a machine's `system` profile, in that machine's own backup repository. It
is taken through the same agent and repository as the nightly backups, so everything that reads backups reads
checkpoints too: the Snapshot Browser, `noctyx snapshots`, `noctyx restore`, retention and verify.

If the machine already has a system snapshot younger than `max_age_hours` (6 hours), that snapshot is the
checkpoint, and it costs nothing. Otherwise the agent takes a new one. It is incremental against the last one,
so it usually takes minutes; on the GB10 nodes here, 2 to 12 minutes. It is tagged `checkpoint` and
`before-change`, `before-apt` or `before-manual`.

When a checkpoint cannot be made, the change still runs and its record says why. The reasons are:
- the repository does not answer;
- another backup holds the NAS (the machine's newest snapshot is used instead);
- `timeout_minutes` (45) passed.

Set `"require": true` to refuse the change instead.

A snapshot in which some files could not be read (restic exit 3) still counts as a checkpoint, marked
incomplete; the agent's `last-backup.log` names those files.

```bash
sudo noctyx checkpoint --host gpu-01            # one now (or --host all; --max-age MIN reuses a recent snapshot)
sudo noctyx checkpoints                         # every machine's checkpoints, and the changes each was taken before
sudo noctyx restore --host gpu-01 --snapshot 1a2b3c4d --path /etc/nginx          # put a path back from one
```

To put a whole machine back as it was, follow the [restore guide](restore.md) with the checkpoint's snapshot id.

### Before apt

`noctyx checkpoint-set` puts a hook into each backed-up node's apt
(`/etc/apt/apt.conf.d/80<prefix>-checkpoint`). Before dpkg runs, it takes a checkpoint: within `max_age_hours` the
newest snapshot counts, else a new one, for at most `apt_timeout_minutes` (20). After dpkg, it writes a
`checkpoint-after` line to the node's audit log.

The hook never stops apt: a checkpoint that fails is skipped, and apt goes on. A hook that apt cannot read is
taken off again at once. `"before_apt": false` and `noctyx checkpoint-set` take the hook off every node.

```bash
sudo noctyx checkpoint-set --json '{}'                          # put the hook on every node, with the policy as it is
sudo noctyx checkpoint-set --json '{"before_apt": false}'       # take it off
```

## Undoing a change

Every change Noctyx makes to a machine is a transaction recorded in `<state>/changes.jsonl`. This covers
upgrades, automatic upgrades, heal's drop-in repairs and undos. Each one records the checkpoint it waited for and a
copy of what it replaced. Copies are kept per change, the last 3 per machine and component:
- **node_exporter:** its binary and drop-in, on the node;
- **Prometheus and Alertmanager:** their binaries and drop-in, and a copy of their data. Only one data copy is
  kept, the newest change's.

```bash
sudo noctyx changes                             # what is in effect (←) and what each change waited for
sudo noctyx revert 4fb445c6                     # what undoing it would do
sudo noctyx revert 4fb445c6 --yes               # do it
```

An undo is a change of its own:
1. It waits for a checkpoint.
2. It keeps a copy of what runs now.
3. It puts back what ran before the change.
4. It checks that the component works (an exporter that scrapes, a Prometheus or Alertmanager that answers with
   its targets and rules).
5. If that check fails, it puts back what ran before the undo.

An undo is not itself undone: to go forward again, run the change again (`noctyx upgrade`, the console's upgrade
button).

Changes stack per machine and component, and the newest one in effect is undone first, then the one before it.
heal's own undo, and a rollout's canary rolled back, count as undone.

**Prometheus and Alertmanager.** Undoing their upgrade puts their data back as it was before the upgrade, and moves
what was recorded since to `<data>.noctyx-failed`. `--keep-data` keeps today's data instead, and puts back only
the binaries and the drop-in. Use it when the data copy belongs to a later change, or when you would rather keep
the history and the older release still reads it. A minor release does; going back from Prometheus 3 to 2.x may not.

**When the copy is gone** (more than 3 changes since), the undo restores the old binary and drop-in from the
change's checkpoint snapshot with restic. A change made before checkpoints existed, with no copy left, can only be
put back by hand.

On the console: **Self-repair › Changes** lists the changes with their state and checkpoint, and an **undo**
button on the ones in effect (admin).

## The site config's history

Every rewrite of the site config first keeps a copy of the file as it was, in `<state>/config-history` (the last
50). This covers every `noctyx *-set` command, `recipe-catalog`, the console's Configure page and `config-restore` itself.

```bash
sudo noctyx config-history                                       # the copies, newest first
sudo noctyx config-history --diff 20261024T155452989735Z-heal.json   # what restoring it would change
sudo noctyx config-restore 20261024T155452989735Z-heal.json --yes    # put it back (the current one is kept first)
```

On the console: Configure › Checkpoints and history. A hand edit of the file is not recorded; copy the file first,
as the [upgrade guide](upgrade.md#before-you-start) says.

## Noctyx itself

A self-checkpoint records which Noctyx runs, in `<state>/self-checkpoints` (the last 10):
- its version;
- its git commit, when it runs from a checkout, or was installed from the checkout in `NOCTYX_SRC`
  (default `/opt/noctyx-src`);
- a copy of the site config.

The console takes one whenever it starts on code it has not recorded. So after an upgrade and a restart, the one
before is the version that ran until then.

```bash
sudo noctyx self-checkpoint --note "before the upgrade"     # take one by hand
sudo noctyx self-checkpoint --list
sudo noctyx self-rollback                                   # what going back would do
sudo noctyx self-rollback --yes [--config] [--to ID]        # go back
```

`self-rollback` does these steps:
1. Takes a self-checkpoint first.
2. Checks out the recorded commit (detached), and runs `pip install` again for an installed Noctyx.
3. With `--config`, puts that checkpoint's site config back.
4. Renders the head's units with that code (`install-head --apply`).
5. Restarts the console, directory and HF mirror.

It refuses when the checkout has uncommitted changes. To return to the newer code, `git checkout <branch>` and
restart again. It restarts the console, so the console shows the command rather than running it.

The node agents are not part of a self-rollback. Run `noctyx install-agent` afterwards if the agent changed
between the two versions.

## The policy

The site config's `checkpoints` block ([site config](site-config.md#checkpoints)), set with `noctyx checkpoint-set`
or on the console (Configure › Checkpoints and history):

| Key | Meaning | Default |
|---|---|---|
| `before_changes` | a change waits for a checkpoint of its machine | `true` |
| `before_apt` | the apt hook on the nodes | `true` (put on by `checkpoint-set`) |
| `max_age_hours` | a system snapshot this recent is the checkpoint (0..168) | `6` |
| `timeout_minutes` | how long a change waits for a new one (1..240) | `45` |
| `apt_timeout_minutes` | how long apt waits for one (1..120) | `20` |
| `require` | a change whose checkpoint cannot be made does not run | `false` |

Checkpoint snapshots are kept by the same retention as the rest, in their own groups (restic groups by host and
tags): daily, weekly and monthly, per reason.
