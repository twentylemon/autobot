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
- Poll bot-authored PRs each tick via `PyGithub` (already a dep).
- Identify new human comments since the last revision (filter by
  comment author + the `<!-- autobot -->` sentinel in the PR body).
- Build a revision prompt: original task + full thread + current diff,
  invoke a fresh `query()` per revision (stateless; survives restarts).
- New `pr_revisions` table to track per-PR revision count and last-seen
  comment id.
- Rate limits: ~3 revisions/hour/PR, hard cap ~10 total/PR.

**Scope — out.** Inline review-comment threading; CI-failure auto-retry;
non-PR destinations (see v0.3).

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
- Polling-based merge check (no webhooks).
- Worktree cleanup on merge.
- New `done/` directory for completed source files.

**Scope — out.** Reverting on revert; tracking the merged commit SHA
back to anything beyond state.db.

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
