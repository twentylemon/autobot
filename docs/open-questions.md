# Open questions / known risks

Surviving open items from the v0 plan plus things that came up while
building it. Grouped by when they actually need a decision so the list
stays scannable.

## Open — decide before relying on this in the background

### `gh` auth in launchd context

launchd jobs don't inherit your interactive shell env. The plist must
explicitly set:

- `PATH` — so `gh` and `git` resolve.
- `HOME` — so `~/.config/gh/hosts.yml` is found *and* so the bundled
  `claude` CLI locates `~/.claude/` for subscription auth.
- `GITHUB_TOKEN` — passed through to Claude's `Bash` env.

Verify after install with `launchctl print gui/<uid>/com.twentylemon.autobot`.
A `python -m autobot install-launchd` helper that writes a known-good
plist would mitigate this; not built yet.

### Cost / rate-limit story

v0 uses Claude subscription auth via the bundled CLI, which means
autobot eats from the same per-window quota as your interactive Claude
Code sessions. If heavy autobot use starts capping your terminal work,
the path forward is either:

- Switch to a metered API key (`ANTHROPIC_API_KEY`). Isolates billing
  but introduces real per-token cost.
- Add a per-day token budget guard in the worker so a runaway loop
  can't drain your subscription window.

Either is worth doing before enabling the revision loop in v0.1, which
will dramatically increase token volume per task.

### Bot identity

v0 uses your personal PAT — commits, PRs, and `gh pr create` calls all
show as you. Fine for personal use; introduce a separate `autobot`
GitHub account (scoped PAT) when you want clean separation. This also
enables the human-vs-bot comment filter in v0.1 (filter `comment.user
== you` instead of relying solely on the `<!-- autobot -->` sentinel).

## Open — revisit when v0.1 lands

### PR-draft sanity check

The result file says `submitted` but doesn't guarantee `--draft` was
honored. v0.1 has `PyGithub` available — add a worker-side check via
`pr_url`/`pr_number` and either flip back to draft or surface a
warning if Claude forgot the flag.

### Sprawling-diff belt-and-suspenders

v0 trusts Claude to self-circuit-break at ~2000 LOC (per the prompt
guardrail). v0.1 should add a worker-side check (`git diff --stat
main..head`) and refuse to mark `submitted` past a threshold, since
the result file is self-reported.

### `PreToolUse` hook on `Bash`

Today only the prompt guards against destructive git invocations
(`push --force`, `reset --hard`, anything touching the canonical
clone). The Claude Agent SDK supports `PreToolUse` hooks — wire one
up to deny these patterns regardless of what the model decides to do.

### Other potentially-stallable tools

v0 disallows `AskUserQuestion`, `EnterPlanMode`, `ExitPlanMode` (see
[`autobot/worker.py`](../autobot/worker.py)). If we observe hangs in
practice, revisit `ScheduleWakeup` (delays execution) and the
`Enter/ExitWorktree` MCP-style tools (could conflict with our own
worktree management).

## Open — revisit if/when scope grows

### Unrestricted `Bash`

Acceptable while autobot only operates on your own machine in your
own worktrees. The #1 thing to harden before any multi-tenant or
shared-infra deployment.

### Worktree accumulation

v0 leaves per-task worktrees on disk forever (no merge detection).
v0.2 cleans on merge; manual `git worktree prune` is the escape
hatch until then. Good enough for personal-tool cadence (a few tasks
per week).

## Resolved during v0 (so they don't get re-raised)

- **`ANTHROPIC_API_KEY` requirement** — removed; bundled CLI uses
  `~/.claude/` subscription auth.
- **Crash mid-execution** — source file moved to `processing/` *before*
  invoking Claude, so the next tick won't re-pick a half-done task.
- **Branch-name collisions** — task ID always includes a 6-char hash of
  `created_at`, so two files with the same stem don't collide.
- **Default-repo behavior** — optional. Frontmatter wins; env-var
  fallback (`AUTOBOT_DEFAULT_REPO`); no implicit default.
- **Default `~/.autobot/` directories** — `config.load()` creates them
  on first run; user doesn't have to `mkdir` anything.
- **Headless stalls** — `permission_mode="bypassPermissions"` + an
  explicit `disallowed_tools` list keeps Claude from invoking tools
  that would block waiting for human input.
