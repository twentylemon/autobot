# Merge detection — design memo (v0.2)

A planned milestone, not yet built. Sits after v0.1 (see
[`v0-1-pr-revision.md`](v0-1-pr-revision.md)) and before v0.3 (see
[`destinations.md`](destinations.md)). Closes the terminal-state gap
v0 left open.

## Why

v0 has no terminal "this is shipped" signal. v0.1 makes the loop
conversational but doesn't help either: a merged PR sits in
`status=submitted` forever. Symptoms:

- **Worktrees accumulate.** Every task leaves a per-task worktree on
  disk; v0.1 reuses them across revisions but never removes them. A
  few months in, `~/.autobot/work/<owner>__<repo>/wt/` is full of
  dead trees for shipped work.
- **Source files sit forever.** `processing/{slug}.md` for a merged
  task is indistinguishable from one whose worker just crashed.
- **state.db is misleading.** A row in `submitted` could be live, in
  review, merged, or closed-without-merge. Polling against it (v0.1's
  Phase 2) keeps re-checking PRs that are long-since done.

v0.2 adds a reconcile phase that detects merges (and closures) and
transitions the state machine to a true terminal state.

## Architectural choices

Inherited from v0.1, restated here for self-containment:

- **Pure agent-driven.** Reconcile uses Claude shelling `gh pr view`
  rather than a worker-side PyGithub call, matching v0/v0.1.
- **Richer state machine.** v0.2 adds two more terminal states —
  `completed` and `abandoned` — reachable from any non-terminal
  post-`pending` state.

## State-machine extension

```
{submitted, needs_revision, revising}
  → completed   [PR merged]
  → abandoned   [PR closed without merge]
```

Both terminal. Distinguishing them lets future ops queries answer
"how many of my tasks shipped?" vs. "how many got rejected?"

`revising → completed/abandoned` is allowed because a human can merge
a draft mid-revision; the in-flight revision is moot at that point.
The worker doesn't kill the revision (the SDK invocation has no
clean cancel point) — it lets the revision finish and ignores the
result. See [Race-condition handling](#race-condition-handling).

## Per-tick reconcile phase

Slots in as Phase 4 from the
[v0.1 phase list](v0-1-pr-revision.md#per-tick-worker-phases) —
between stale-lease recovery and execute-pending:

```
1. Discover
2. Poll                 (v0.1)
3. Recover stale leases (v0.1)
4. Reconcile            (v0.2 — new)
5. Execute pending
6. Execute revisions    (v0.1)
```

For each row in `{submitted, needs_revision, revising}` with
`pr_number` set, invoke Claude with `render_reconcile_prompt`:

> Run `gh pr view {pr_number} --repo {repo} --json state,merged,mergedAt`.
> Write a result file:
> - `{status: 'merged', merged_at: <ISO>}` if `merged == true`.
> - `{status: 'closed_unmerged'}` if `state == 'CLOSED'` and not merged.
> - `{status: 'open'}` otherwise.

Worker dispatch:

| Result            | Action                                                                   |
|-------------------|--------------------------------------------------------------------------|
| `merged`          | Transition row → `completed`; archive source file; clean up worktree     |
| `closed_unmerged` | Transition row → `abandoned`; archive source file; clean up worktree     |
| `open`            | No-op                                                                    |

Result files and logs stay on disk (audit trail). Only the source
file moves and the worktree is removed.

### Source-file archival

`source.mark_completed(task)` (currently a no-op stub on
[`TaskSource`](../../autobot/sources/base.py)) becomes meaningful:

- For `LocalFileSource`, move `processing/{slug}.md → done/{slug}.md`.
- Collision handling mirrors today's `mark_picked_up`: if the
  destination already exists, rename to `done/{slug}-{hash}.md`.

Add `source.mark_abandoned(task)` for the parallel `closed_unmerged`
path. Behavior identical to `mark_completed` for `LocalFileSource` —
they only differ in the state.db terminal status. Splitting them
keeps the source interface honest about which case happened.

### Worktree cleanup

`git worktree remove --force {worktree_dir}` happens *before* the
source file move. Reasoning:

- If `git worktree remove` succeeds and the file move fails, the
  worktree is gone and `processing/` still has the file → the
  reconcile pass will re-run next tick, the worktree is already
  gone (idempotent), and the file move will retry.
- If the order were reversed and the file move succeeded but the
  worktree removal failed, the file would be in `done/` (looks
  shipped) but the worktree would persist (looks live). That's the
  bad direction.

If `git worktree remove` fails (worktree has uncommitted changes —
shouldn't happen post-merge), log a warning, leave the worktree on
disk, **still complete the state transition and source-file move**.
Manual `git worktree prune` is the escape hatch (already mentioned
in `README.md`).

## Race-condition handling

If Phase 4 (reconcile) detects a merge while a revision is in-flight
in Phase 6 (revise), the row transitions to `completed` mid-pass.
When the revision pass returns and tries to write its result, the
state-update guard must silently no-op.

Today, [`State.update_status`](../../autobot/state.py) raises if a
row is in a terminal state. v0.2 needs to relax this for the
specific case of `revising → submitted/needs_revision` writes when
the row is now terminal — promote them to silent no-ops with a log
warning instead of an exception.

The unsafe case (terminal → some other terminal) stays an exception.
The safe case (revision finished but already-merged) becomes a
warning. Test coverage for this lives in
`tests/test_reconcile_race_condition.py`.

## Runtime layout addition

```
~/.autobot/
  done/           ← new in v0.2
    add-caching-abc123.md
    fix-typos-def456.md
    ...
```

[`config.load()`](../../autobot/config.py) creates `done/` on first
run, mirroring how it creates `processing/` today.

## Schema / contract changes

### Status enum

Adds: `completed`, `abandoned`. Both terminal.

### Allowed transitions

`{submitted, needs_revision, revising} → {completed, abandoned}`.

The `revising → completed/abandoned` case is the one that requires
the [race-condition guard](#race-condition-handling).

### Result-file JSON

Three new `status` values for the reconcile-prompt vocabulary
(additive to the v0.1 contract):

| `status`          | Variant         | Required fields  |
|-------------------|-----------------|------------------|
| `merged`          | reconcile result | `merged_at`      |
| `closed_unmerged` | reconcile result | (none)           |
| `open`            | reconcile result | (none)           |

No `tasks` schema changes. No frontmatter changes.

## Refactor task list

1. [`autobot/state.py`](../../autobot/state.py) — extend `STATUSES`,
   `TERMINAL_STATUSES`, transition rules; new accessor
   `get_reconcilable() -> list[TaskRow]` (rows in
   `{submitted, needs_revision, revising}` with `pr_number` set);
   relax the "transitions out of terminal" guard for the specific
   `revising → {submitted, needs_revision}` race case (silent no-op
   if row is now `completed` or `abandoned`).
2. [`autobot/sources/base.py`](../../autobot/sources/base.py) —
   `mark_completed(task)` becomes meaningful; add
   `mark_abandoned(task)`.
3. [`autobot/sources/local_files.py`](../../autobot/sources/local_files.py)
   — implement both: move `processing/{slug}.md → done/{slug}.md`
   with collision handling mirroring today's `mark_picked_up`.
4. [`autobot/config.py`](../../autobot/config.py) — `done_dir`
   computed path; `mkdir` on load.
5. [`autobot/prompts.py`](../../autobot/prompts.py) — new
   `render_reconcile_prompt()`. Tiny fragment compared to
   initial/revision; just a `gh pr view` call + result-file write.
   Self-contained (no imports from `worker.py`/`state.py`).
6. [`autobot/results.py`](../../autobot/results.py) — extend the
   tagged union: new `Merged`, `ClosedUnmerged`, `Open` variants.
7. [`autobot/worker.py`](../../autobot/worker.py) — new async fn
   `reconcile_pr(row, ...)`; **extract the worktree-cleanup helper**
   to a top-level function (e.g., `remove_worktree(worktree_dir)`)
   so v0.3 destinations can reuse the pattern.
8. [`autobot/main.py`](../../autobot/main.py) — `_tick` slots
   `_reconcile()` between stale-lease recovery and execute-pending.
9. [`README.md`](../../README.md) — runtime layout adds `done/`;
   recovery section mentions abandoned-PR cleanup.
10. `tests/` — extend `_fake_query_with_gh` (introduced in v0.1) to
    stub PR-state responses; new tests:
    `test_merge_detection.py`, `test_abandoned_handling.py`,
    `test_reconcile_race_condition.py`.

## Forward-compat moves to make now

- **Single-source the merge transition.** All `{submitted,
  needs_revision, revising} → completed` writes go through one
  helper (e.g., `worker._mark_merged(row, merged_at)`); don't
  sprinkle `state.update_status(..., status='completed')` calls.
- **Worktree-cleanup helper is destination-agnostic.** v0.3's
  `Destination.cleanup(workspace)` can reuse it for non-PR
  workspaces (e.g., a one-shot scratch dir for `local_file`
  destination).
- **Reconcile prompt stays self-contained.** Same rule as v0.1
  prompts — no imports from `worker.py` or `state.py`.

## Things to NOT do prematurely

- **No webhook listener.** Polling is fine for personal-tool cadence
  (same call as v0.1).
- **No revert handling.** If a merged PR gets reverted upstream,
  autobot won't notice; the row stays `completed`. Acceptable.
- **Don't track merge SHA beyond what state.db needs.** The PR URL
  + `merged_at` are enough for an audit trail; the merge SHA can be
  fetched on demand from the result file or `gh`.
- **No auto-archive of old `done/` files.** Manual `find ~/.autobot/done
  -mtime +90 -delete` is fine until volume justifies it.
- **No GC of orphaned `results/`/`logs/` files.** Retain forever for
  audit. They're tiny.

## Triggers

v0.1 has been stable for ~2 weeks. Specifically: the
`needs_revision`/`revising` states are exercising correctly, the
stale-lease recovery has fired at least once for real (or has been
exercised in a forced test), and the rate limits feel right.

Without v0.1 stable, v0.2's race-condition handling can't be
validated.

## Verification

1. Merge an existing draft PR (one created by v0).
2. On the next tick, observe:
   - Row transitions `submitted → completed` (or whatever non-terminal
     state it was in).
   - Source file moves: `processing/{slug}.md` no longer exists;
     `done/{slug}.md` does.
   - Worktree directory `work/{owner}__{repo}/wt/{slug}` is gone.
   - Result file `results/{slug}.json` and log
     `logs/{slug}.log` still on disk.
3. Close another draft PR without merging. Observe `submitted →
   abandoned` with the same file/worktree handling.
4. Force a race: start a revision (Phase 6) on a long-running task,
   then merge the PR before the revision finishes. The reconcile
   pass should transition `revising → completed`; the revision pass
   should silently no-op when it returns. Check logs for the warning
   line.
