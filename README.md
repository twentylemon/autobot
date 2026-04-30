# autobot

A personal service that turns short task descriptions into draft pull requests.

Drop a markdown file in `~/.autobot/inbox/`, wait, and a draft PR shows up
on the target repo. Comment on the PR and (in v0.1+) the bot will revise it.

## v0 scope

- **In:** local-file task source, discovery, single draft PR per task.
- **Out (deferred):** PR comment-revision loop (v0.1), merge detection (v0.2),
  GitHub-issue source (v1).

## How it works

`autobot` is a thin state machine. Per tick:

1. **Discover** — scan `~/.autobot/inbox/*.md`, insert any new tasks into
   `~/.autobot/state.db`.
2. **Execute** — for each pending task, render a prompt and invoke the Claude
   Agent SDK. Claude does *all* git/`gh` operations: refresh the canonical
   clone, create a `git worktree` on a fresh branch, edit files, commit,
   push, and `gh pr create --draft`.
3. **Reconcile** — Claude writes a JSON result file at
   `~/.autobot/results/<task-id>.json`; the worker reads it and transitions
   the task to `submitted` / `failed_no_changes` / `failed_no_pr` /
   `failed_unknown`.

## Setup

```bash
pip install -e .
export ANTHROPIC_API_KEY=...
export GITHUB_TOKEN=...                            # PAT with `repo` scope
export AUTOBOT_DEFAULT_REPO=twentylemon/duckbot    # optional
```

`gh` and `git` must be installed and authenticated — they are invoked by
Claude inside the worker subprocess.

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
interactive shell env).

## Layout

```
~/.autobot/
  inbox/          # drop task files here
  processing/     # files moved here once ingested
  work/<owner>__<repo>/main/         # canonical clone (Claude maintains)
  work/<owner>__<repo>/wt/<task>/    # per-task worktree
  results/<task>.json                # Claude's structured result
  results/<task>.json.error          # sidecar on failure
  logs/<task>.log                    # JSONL of SDK messages
  state.db                           # SQLite task state
```

## Recovery

- **Task stuck pending after a crash:** worker moved the source file to
  `processing/` before invoking Claude, so `--once` will not re-pick it.
  Manually move it back to `inbox/` and delete the row from `state.db`.
- **Stale worktree:** `git -C ~/.autobot/work/<owner>__<repo>/main worktree prune`.

## Tests

```bash
python -m pytest
```
