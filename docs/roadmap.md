# Roadmap

Forward-looking phasing for autobot. Each phase is the smallest useful
slice that closes a real gap left by the previous one. Status reflects
what's actually merged on `main`.

## v0 — local-file source → draft PR ✅ done

`~/.autobot/inbox/*.md` → `discover()` → Claude does the work in a
git worktree → opens a draft PR → worker records state. See
[`README.md`](../README.md) for usage and the runtime layout.

## v0.1 — PR comment-revision loop (planned)

**What.** When a human leaves a comment on one of autobot's open draft
PRs, re-invoke Claude with the concatenated comments and let it revise
the PR (new commits on the same branch).

**Why.** v0 has no path back from "draft PR exists" to "draft PR is what
I actually want." Today a flawed PR means closing it and dropping a
refined task file in `inbox/`. v0.1 makes the loop conversational.

**Scope — in.**
- Pure agent-driven polling: Claude shells `gh api` from the worker's
  poll prompt (no PyGithub).
- Identify new human comments since the last revision (filter by
  `comment.user.login != PR.user.login` + the `<!-- autobot -->`
  sentinel in the PR body).
- Richer state machine: new `needs_revision` and `revising` states
  plus `failed_revision` and `failed_too_large` terminals; stale-lease
  recovery for crashed revisions.
- Build a revision prompt: original task + comment thread + current
  diff (Claude fetches both itself), invoke a fresh `query()` per
  revision (stateless; survives restarts).
- Two new columns on `tasks` (`last_comment_id`, `revision_count`) —
  no separate revision table.
- Per-PR revision rate is bounded naturally — only one revision per PR
  is in flight at a time, so tick frequency is the rate limit.
- Worker-side hardening folded in: PR-draft sanity check (in prompt),
  sprawling-diff guard (in prompt), `PreToolUse` hook on `Bash`
  (worker-side SDK).

**Scope — out.** Inline review-comment threading; CI-failure auto-retry;
non-PR destinations (see v0.3).

**Design.** See [`design/v0-1-pr-revision.md`](design/v0-1-pr-revision.md).

**Status.** Planned.

## v0.2 — merge detection (planned)

**What.** Each tick, list bot-authored PRs in `closed` state since the
last check. If `merged`, transition `submitted → completed`, move the
source file `processing/ → done/`, and `git worktree remove` the
per-task worktree.

**Why.** v0 has no terminal "this is shipped" signal. Worktrees
accumulate; the source file sits in `processing/` forever; state.db
doesn't know what's live.

**Scope — in.**
- Polling-based reconcile (Claude shells `gh pr view`); no webhooks.
- New terminal states: `completed` (merged) and `abandoned`
  (closed-without-merge), reachable from `submitted`,
  `needs_revision`, or `revising`.
- Worktree cleanup on either terminal.
- New `done/` directory for archived source files.
- Race-condition handling for revisions in-flight at merge time.

**Scope — out.** Reverting on revert; tracking the merged commit SHA
back to anything beyond state.db.

**Design.** See [`design/v0-2-merge-detection.md`](design/v0-2-merge-detection.md).

**Status.** Planned.

## v0.3 — Destination abstraction (deferred until v0.1+v0.2 land)

**What.** Generalize beyond GitHub PRs: introduce a `Destination` ABC
that mirrors the existing `TaskSource`. Today's PR flow becomes
`GithubPrDestination`; future destinations (local-file, Confluence,
Slack, etc.) plug in alongside it.

**Why.** Some tasks aren't tied to a repo or shouldn't produce a PR —
e.g. "research X and write a one-pager," or "draft an email about Y."
The current architecture bakes PR assumptions into `Task.repo`,
`prompts.py`, `Result`, and `state.py` schema.

**Scope — in.** The `Destination` ABC, refactor of the existing PR flow
into a `GithubPrDestination`, and a frontmatter contract for tasks to
pick their destination. See [`design/destinations.md`](design/destinations.md)
for the full design memo.

**Scope — out.** Any concrete second destination. v0.3 lands the
abstraction; the first real non-PR destination is its own milestone
once we know which one we actually need.

**Status.** Deferred. Triggers for starting:
1. v0.1 + v0.2 are merged and stable.
2. There's a real, named first non-PR destination on the table.
3. PR-revision loop has surfaced enough about "Claude takes feedback on
   its output" that the pattern can be reused for non-PR destinations.

## v1 — multi-source / GitHub issues (speculative)

`GitHubIssueSource` alongside `LocalFileSource`. Multi-repo allowlist via
env var. Structured logging, basic per-task token-spend tracking, and
introduction of a separate `autobot` GitHub identity for clean
human-vs-bot separation. See `bot identity` in
[`open-questions.md`](open-questions.md).

## v2 — hosted / external sources (speculative)

Linear / Jira sources, AWS Fargate hosting per duckbot's CDK pattern,
PR review-comment threading, retry-on-CI-failure loop.
