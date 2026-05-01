# PR comment-revision loop — design memo (v0.1)

A planned milestone, not yet built. v0 ships the
inbox → draft-PR pipeline; v0.1 closes the conversational loop so a
human comment on a draft PR triggers a revision pass. See
[`../roadmap.md`](../roadmap.md) for phasing.

## Why

v0 has no path back from "draft PR exists" to "draft PR is what I
actually want." Today, a flawed PR means closing it, refining the
task file, and dropping a new file in `inbox/` — and the new task
runs from a clean clone with no memory of the previous attempt.

v0.1 makes the loop conversational: comment on the PR, autobot picks
up your comment on the next tick, Claude addresses it on the *same*
branch with full thread context.

## Architectural choices

Two non-obvious decisions inform the rest of this memo.

**Pure agent-driven for everything `gh`-related.** The worker stays a
passthrough: Claude itself shells `gh api`/`gh pr view` for comment
polling and PR state. No PyGithub wrapper. This matches v0's
philosophy ("worker is a thin shell; Claude does git/gh") and keeps
[`autobot/github_client.py`](../../autobot/github_client.py) (the v0
stub) deletable. The cost trade-off — every tick burns tokens for
every open PR — is real and tracked in
[`../open-questions.md`](../open-questions.md) under "Cost / rate-limit
story."

**Richer state machine.** The worker needs to know the *mode* of each
invocation: initial work vs. revision vs. recovery from a crashed
run. Today's three-state machine (`pending → submitted | failed_*`)
can't express any of that. v0.1 introduces `needs_revision`,
`revising`, `failed_revision`, and `failed_too_large`.

## State-machine extension

```
pending
  → submitted          [PR opened]
  → failed_no_changes  [terminal]
  → failed_no_pr       [terminal]
  → failed_too_large   [terminal — new in v0.1]
  → failed_unknown     [terminal]

submitted
  → needs_revision     [poll detected new comments — new in v0.1]

needs_revision
  → revising           [revision pass starting; lock — new in v0.1]

revising
  → submitted          [pass succeeded; no new comments since pass began]
  → needs_revision     [pass succeeded; more comments arrived during]
  → failed_revision    [pass failed; terminal until human intervention]
  → failed_too_large   [revision diff blew the threshold]
```

(v0.2 adds `completed`/`abandoned` reachable from `submitted`,
`needs_revision`, and `revising` — see
[`v0-2-merge-detection.md`](v0-2-merge-detection.md).)

**Stale-lease recovery.** A `revising` row whose `updated_at` is older
than 30 minutes (configurable) is transitioned back to
`needs_revision` at the start of the next tick. This is how we
recover from a worker crash mid-revision; without it, a crashed
revision row would sit in `revising` forever.

## Mode awareness

The worker picks the prompt template by row state:

- `pending` → `render_initial_prompt` (today's 7-step).
- `submitted` → `render_poll_prompt` (cheap "any new comments?" pass).
- `revising` → `render_revision_prompt` (full revision context).
- All v0.2 reconcile checks → `render_reconcile_prompt`.

Each template owns its own result-file vocabulary (see "Result-file
contract additions" below).

## Per-tick worker phases

```
1. Discover                  (today's: inbox → state.db)
2. Poll                      (new: submitted rows → maybe needs_revision)
3. Recover stale leases      (new: stuck revising → needs_revision)
4. (v0.2 reconcile slots in here)
5. Execute pending           (today's: pending rows → submitted/failed_*)
6. Execute revisions         (new: needs_revision rows → revising → submitted/failed_*)
```

Each phase is its own helper in
[`autobot/main.py`](../../autobot/main.py); failures inside one phase
don't block subsequent phases.

### Phase 2: Poll

For each row with `status='submitted'`, invoke Claude with
`render_poll_prompt`. The prompt instructs Claude to:

1. Run `gh api repos/{owner}/{repo}/issues/{pr_number}/comments`.
2. Parse the JSON; find comments with `id > {last_comment_id}` (zero
   for the first poll) where `comment.user.login != PR.user.login`
   AND the PR body contains the `<!-- autobot -->` sentinel.
3. If any qualify, write a result file
   `{status: 'needs_revision', last_comment_id: <max id>}`.
4. Else write `{status: 'no_action'}`.

Worker dispatch: `needs_revision` → transition row +
`record_poll_result(task_id, last_comment_id)`; `no_action` →
just bump `last_revision_at` (so we know polling happened).

### Phase 3: Recover stale leases

Single SQL query: `SELECT id FROM tasks WHERE status='revising' AND
updated_at < ?` with a cutoff 30 minutes ago. For each row, transition
to `needs_revision`. No Claude invocation needed.

### Phase 6: Execute revisions

For each `needs_revision` row:

1. `record_revision_start(task_id)` → transition to `revising`.
2. Render revision prompt (see "Revision prompt" below).
3. Invoke Claude.
4. Parse result, dispatch:
   - `revised` → transition `revising → submitted`, increment
     `revision_count`, store new `last_comment_id` and `head_sha`.
   - `needs_revision` (Claude noticed comments arrived during pass)
     → transition `revising → needs_revision`.
   - `failed_too_large` → transition to terminal.
   - Anything else → `failed_revision`.

### Race-condition note

If Phase 4 (v0.2 reconcile) detects a merge while a revision is
in-flight in Phase 6, the row is transitioned to `completed` mid-pass.
The revision's result-handler must check current state and no-op if
the row is no longer `revising`. The
[`State.update_status`](../../autobot/state.py) function already
enforces "terminal states cannot transition further" — extend it to
"transitions out of `revising` are silently ignored if the row is now
terminal." Detail tracked in
[`v0-2-merge-detection.md`](v0-2-merge-detection.md#race-condition-handling).

## The revision prompt

Outline (full template lives in `prompts.py` once built):

> Here is the original task: `{task.title}` / `{task.body}`.
>
> The draft PR is at `{pr_url}` on branch `{branch}`. The existing
> worktree is at `{worktree_dir}` — reuse it; do not re-clone.
>
> Run `cd {worktree_dir} && git status`. If there are uncommitted
> changes, they're from a prior crashed revision pass — inspect them,
> decide whether to keep, refine, or discard based on the comment
> thread you're about to address, and commit or `git checkout`
> accordingly before continuing.
>
> Refresh the base branch: `git fetch origin main`.
>
> Fetch the comment thread:
> `gh api repos/{repo}/issues/{pr_number}/comments`
>
> Fetch the current diff against the freshly-fetched base:
> `git diff origin/main..HEAD`
>
> Address the unresolved comments (those with `id > {last_comment_id}`
> from non-bot authors) by editing files in the worktree. Commit with
> a message that summarizes the comments you addressed. Push to the
> same branch. Leave the PR draft.
>
> Then run the hardening checks (PR-draft sanity + sprawling-diff
> guard — see "Worker-side hardening" below) and write a revision
> result file with the new `last_comment_id` and `head_sha`.

The prompt body intentionally does not include the comment thread or
diff verbatim — Claude fetches them itself, which keeps the prompt
size bounded as PRs grow.

## Worker-side hardening

Three items folded in from `open-questions.md` because they share the
v0.1 surface area.

### 1. PR-draft sanity check (in prompt)

Both initial and revision prompts add a final pre-result step:

```
gh pr view {pr_number} --json isDraft
```

If `isDraft == false`, run:

```
gh pr ready --undo {pr_number}
```

(flips the PR back to draft). Re-check; if still not draft, write
`failed_no_pr` with reason `"could not restore draft state"`.

This is in-prompt rather than worker-side because it's the same shape
as the existing 7-step script and Claude already has `gh` in scope.

### 2. Sprawling-diff guard (in prompt)

Before writing `submitted` (or `revised`), run:

```
git -C {worktree_dir} diff --shortstat origin/main..HEAD
```

Parse the output (`N insertions(+), M deletions(-)`). If
`N + M > 2000` (configurable via `AUTOBOT_MAX_DIFF_LOC`), do not push;
write a result `{status: 'failed_too_large', insertions: N, deletions: M}`.
Worker transitions to `failed_too_large` (terminal).

This is the same threshold as the in-prompt self-circuit-break in
v0's task prompt. The point of doing it again here is *verifiability*
— the result file is self-reported, and `git diff --shortstat` is
deterministic. Belt-and-suspenders, not stricter.

### 3. `PreToolUse` hook on `Bash` (worker-side SDK)

The first two checks live in the prompt because they're verifiable.
The destructive-pattern filter cannot — Claude can't be trusted to
police itself on the *attempt*. Wired in
[`autobot/worker.py`](../../autobot/worker.py)'s `execute_task`:

```python
options = ClaudeAgentOptions(
    cwd=str(config.work_dir),
    permission_mode="bypassPermissions",
    disallowed_tools=["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"],
    hooks=[("PreToolUse", block_destructive_bash)],
)
```

The hook function lives in `autobot/sdk_hooks.py` (new module — keeps
it testable in isolation) and rejects `Bash` tool uses whose `command`
matches:

- `git push --force` and `-f` (the unconditional forms).
- `git reset --hard`.
- `rm -rf` of any path under the canonical clone (`work/.../main/`).
- Direct writes under any `.git/` directory.

`git push --force-with-lease` is **allowed** — it fails if the remote
ref has been updated since Claude last fetched, so it's the safe form
to use after a rebase. Blocking it would prevent Claude from doing
the right thing on a revision that needed a rebase.

On rejection, the hook returns a tool-use error so Claude sees the
failure in-context and can adapt (e.g., switch from `--force` to
`--force-with-lease`).

## Bot identity caveat

Currently the PAT is personal (see "Bot identity" in
[`../open-questions.md`](../open-questions.md)). Filtering "human
comments" by `comment.author == bot` is unreliable because the bot
*is* the human user.

The poll prompt's filter uses `comment.user.login != PR.user.login`
(skip self-comments) AND the `<!-- autobot -->` sentinel-in-body
requirement (only autobot PRs are revision targets). Belt-and-
suspenders. v1 (separate bot account) cleans this up.

## Schema / contract changes

### `tasks` table

Three new columns:

```sql
ALTER TABLE tasks ADD COLUMN last_comment_id INTEGER;
ALTER TABLE tasks ADD COLUMN revision_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN last_revision_at TEXT;
```

No new table — revisions are 1:1 with tasks.

### Status enum

Adds: `needs_revision`, `revising`, `failed_revision`,
`failed_too_large`. Terminal: `failed_revision`, `failed_too_large`.

### Result-file JSON

New `status` values, additive to today's contract (see
[`autobot/results.py`](../../autobot/results.py)):

| `status`           | Variant         | Required fields                        |
|--------------------|-----------------|----------------------------------------|
| `needs_revision`   | poll result     | `last_comment_id`                      |
| `no_action`        | poll result     | (none)                                 |
| `revised`          | revision result | `last_comment_id`, `head_sha`          |
| `failed_too_large` | initial/revision | `insertions`, `deletions`              |

Each new variant is its own frozen dataclass in `results.py`; the
`Result` union widens. Existing variants (`Submitted`, `NoChanges`,
`NoPr`, `Unknown`) are unchanged.

## Refactor task list

1. **Delete** [`autobot/github_client.py`](../../autobot/github_client.py)
   — pure agent-driven, no wrapper needed.
2. [`autobot/state.py`](../../autobot/state.py) — extend `STATUSES`,
   `TERMINAL_STATUSES`, and the transition table; add the three
   columns; new accessors:
   - `get_submitted() -> list[TaskRow]`
   - `get_needs_revision() -> list[TaskRow]`
   - `get_stale_revising(older_than: timedelta) -> list[TaskRow]`
   - `record_poll_result(task_id, last_comment_id)`
   - `record_revision_start(task_id)`
   - `record_revision_result(task_id, head_sha, last_comment_id)`
3. [`autobot/prompts.py`](../../autobot/prompts.py) — new
   `render_poll_prompt()`, `render_revision_prompt()`. Both stay
   self-contained (no imports from `worker.py`/`state.py`) per the
   v0.3 forward-compat note in
   [`destinations.md`](destinations.md#cheap-forward-compat-moves-to-make-now).
4. [`autobot/results.py`](../../autobot/results.py) — extend the
   tagged union: new `NeedsRevision`, `NoAction`, `Revised`,
   `FailedTooLarge` variants.
5. [`autobot/worker.py`](../../autobot/worker.py) — new async fns:
   `poll_pr(row, ...)`, `recover_stale_leases(...)`,
   `revise_task(row, ...)`. Move `_apply_result` dispatch to a
   per-state-transition map so it stays readable. Wire
   `ClaudeAgentOptions(hooks=…)`.
6. **New**: `autobot/sdk_hooks.py` — `block_destructive_bash` lives
   here so it's unit-testable without a full SDK invocation.
7. [`autobot/main.py`](../../autobot/main.py) — `_tick` runs the six
   phases in order. Each phase is its own helper.
8. `tests/` — new fixture `_fake_query_with_gh` (records the `gh`
   shell-outs Claude attempted, via the log stream); new test files:
   `test_poll_loop.py`, `test_revision_loop.py`,
   `test_stale_lease_recovery.py`, `test_sdk_hooks.py`,
   `test_results_v0_1.py`.

## Forward-compat moves to make now

These keep the v0.3 destination refactor mechanical (see
[`destinations.md`](destinations.md)):

- Keep prompt rendering in `prompts.py` so v0.3's
  `Destination.prompt_fragment` can absorb initial + revision + poll
  fragments together.
- Keep result-shape parsing in `results.py` (new variants are
  mechanical additions to the union).
- Keep state-machine transitions in `state.py` — don't sprinkle
  status writes into the worker.
- Keep `sdk_hooks.py` destination-agnostic (it filters `Bash`,
  which is shell-shaped, not PR-shaped).

## Things to NOT do prematurely

- **No webhook listener.** Polling is fine for personal-tool cadence;
  webhooks add a network-facing service to host.
- **No inline review-comment threading.** Issue-style comments only in
  v0.1; review threads are v2.
- **No CI-failure auto-retry.** v2 territory.
- **Don't persist full comment threads in state.db.** Re-fetch each
  tick — `gh` is fast and the source of truth lives on GitHub.
- **Don't merge poll + revision into one Claude invocation per tick.**
  The separation makes monitoring readable (you can grep state.db for
  rows in `needs_revision` to see what's queued).
- **Don't make the rate limit configurable per-PR yet.** A single
  global cap (e.g., 10 revisions/PR, 3/hour/PR) is enough until we
  see a real PR exceed it.

## Triggers

Two should be true before starting v0.1:

1. v0 has been used on real tasks for ~2 weeks without intervention
   (no daily babysitting, no manual state.db edits).
2. At least one v0 PR has been left as a "this isn't quite right"
   case where the revision loop would have helped — concrete evidence
   of the gap.

## Verification

1. Drop a task in `inbox/`. Wait for it to land as a draft PR
   (`status=submitted`).
2. Comment on the PR: `"use a different config key name"`.
3. On the next tick, observe the row transition
   `submitted → needs_revision` (Phase 2 poll).
4. On the same or following tick, observe
   `needs_revision → revising → submitted` (Phase 6 execute revisions).
5. Verify a new commit on the branch with a message that references
   the comment.
6. Verify `revision_count` is 1, `last_comment_id` matches the GitHub
   comment id, and `head_sha` matches the new commit.

If the row stays in `revising` past the stale-lease cutoff, observe
Phase 3 returning it to `needs_revision` on the next tick — that's
the crash-recovery path working.

If either the `revised → submitted` transition or the stale-lease
recovery doesn't compose with v0.2's `revising → completed`
transition, the design is wrong — see
[`v0-2-merge-detection.md`](v0-2-merge-detection.md#race-condition-handling)
before adjusting.
