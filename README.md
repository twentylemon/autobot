# autobot

A personal service that turns short task descriptions into draft pull requests.

Drop a markdown file in `~/.autobot/inbox/`, wait, and a draft PR shows up
on the target repo. Comment on the PR and the bot picks up your feedback on
the next tick, revising the PR on the same branch.

## v0.1 scope

- **In:** local-file task source, discovery, single draft PR per task,
  PR comment-revision loop on the same branch.
- **Out (deferred):** merge detection (v0.2), GitHub-issue source (v1).

## How it works

`autobot` is a thin state machine. Per tick:

1. **Discover** — scan `~/.autobot/inbox/*.md`, insert any new tasks into
   `~/.autobot/state.db`.
2. **Poll** — for each row in `submitted`, shell `gh` directly to
   fetch PR metadata + comments. If the PR is no longer draft+open
   (closed, merged, or marked ready for review), the user has taken
   over — transition to `completed` and stop. Otherwise filter out
   the bot's own comments and any with `id <= last_comment_id`;
   transition to `needs_revision` if a new comment qualifies. No LLM
   call — pure HTTP + JSON filtering.
3. **Recover stale leases** — any row stuck in `revising` past the cutoff
   (default 30 min) is bumped back to `needs_revision` for retry.
4. **Execute pending** — for each `pending` task, render the initial
   prompt and invoke the Claude Agent SDK. Claude does *all* git/`gh`
   operations: refresh the canonical clone, create a `git worktree` on a
   fresh branch, edit files, commit, push, and `gh pr create --draft`.
5. **Execute revisions** — for each `needs_revision` row, Claude
   re-enters the worktree, fetches the new comment thread, addresses
   the feedback, commits, and pushes — keeping the PR in draft state.
   Per-PR revision rate is bounded naturally: a PR is only ever in one
   state at a time, so the next revision can't start until this one
   finishes.
6. **Reconcile** — Claude writes a JSON result file at
   `~/.autobot/results/<task-id>.json`; the worker reads it and applies
   the appropriate state transition.

## Setup

```bash
pip install -e .
export GITHUB_TOKEN=...                            # PAT with `repo` scope
export AUTOBOT_DEFAULT_REPO=twentylemon/duckbot    # optional
export AUTOBOT_MAX_DIFF_LOC=2000                   # optional, sprawl guard
```

The Claude Agent SDK ships a bundled `claude` CLI binary and shells out to
it. It uses whatever credentials live in `~/.claude/`, so a one-time
`claude` + `/login` on this machine is enough — no `ANTHROPIC_API_KEY`
needed. Usage bills against your Claude subscription.

`gh` and `git` must also be installed and authenticated — they are invoked
by Claude inside the worker subprocess.

## Task file format

Plain markdown. Optional YAML frontmatter overrides the default repo:

```markdown
---
repo: twentylemon/duckbot
---

Add a `--dry-run` flag to the `deploy` subcommand. Print the actions it
would take, then exit without doing them.
```

The PR title is derived from the first non-empty line of the body, capped at
70 chars. The branch name is `twentylemon/autobot/<filename-slug>-<hash>`.

## Running

```bash
python -m autobot --once          # single tick
python -m autobot --loop           # poll forever (default 600s interval)
python -m autobot --loop --interval 300
```

For background hosting on macOS, write a `LaunchAgent` plist at
`~/Library/LaunchAgents/com.twentylemon.autobot.plist` invoking
`python -m autobot --once` every 600 seconds. Set `PATH`, `HOME`, and
`GITHUB_TOKEN` explicitly in the plist (launchd does not inherit your
interactive shell env). `HOME` is what lets the bundled `claude` CLI
locate `~/.claude/` and pick up your subscription credentials.

## Layout

```
~/.autobot/
  inbox/          # drop task files here
  processing/     # files moved here once ingested
  work/<owner>__<repo>/main/         # canonical clone (Claude maintains)
  work/<owner>__<repo>/wt/<task>/    # per-task worktree
  results/<task>.json                # Claude's structured result
  results/<task>.json.error          # sidecar on failure
  logs/<task>.log                    # JSONL of SDK messages (initial run)
  logs/<task>.revision-<n>.log       # one per revision pass
  state.db                           # SQLite task state
```

## Recovery

- **Task in `completed`:** terminal — the user took over the PR (closed,
  merged, or marked it ready for review). No action needed; the row stays
  for audit. To reopen iteration, drop a new task file.
- **Task stuck pending after a crash:** worker moved the source file to
  `processing/` before invoking Claude, so `--once` will not re-pick it.
  Manually move it back to `inbox/` and delete the row from `state.db`.
- **Task stuck in `revising` after a crash:** the next tick's stale-lease
  recovery phase will bump it back to `needs_revision` automatically once
  `updated_at` is older than 30 minutes.
- **Task in `failed_revision`:** terminal — Claude couldn't address the
  feedback (e.g. push rejected, ambiguous comment). Inspect
  `logs/<task>.revision-*.log` for the SDK trace; if you want another
  attempt, manually flip the row back to `needs_revision`.
- **Task in `failed_too_large`:** terminal — Claude's diff exceeded
  `AUTOBOT_MAX_DIFF_LOC` (default 2000). Either raise the cap, split the
  task, or close the PR by hand.
- **Stale worktree:** `git -C ~/.autobot/work/<owner>__<repo>/main worktree prune`.

## Tests

```bash
python -m pytest
```

## Roadmap & open questions

See [`docs/`](docs/) — [`roadmap.md`](docs/roadmap.md) for upcoming
phases (v0.2 merge detection, v0.3 destination abstraction) and
[`open-questions.md`](docs/open-questions.md) for surviving risks.
