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

Worth keeping in mind for v0.1: pure-agent-driven polling (see
[`design/v0-1-pr-revision.md`](design/v0-1-pr-revision.md)) means
every tick burns tokens for every open PR — one cheap poll-prompt
invocation, plus a v0.2 reconcile-prompt invocation, per row. With N
open PRs and a 10-minute tick interval, that's `N × 2 × 144` Claude
invocations per day even when nothing's happening. The per-day budget
guard isn't required to land with v0.1; revisit if real usage shows a
cost problem.

### Bot identity

v0 uses your personal PAT — commits, PRs, and `gh pr create` calls all
show as you. Fine for personal use; introduce a separate `autobot`
GitHub account (scoped PAT) when you want clean separation. This also
enables the human-vs-bot comment filter in v0.1 (filter `comment.user
== you` instead of relying solely on the `<!-- autobot -->` sentinel).

## Open — revisit when v0.1 lands

The PR-draft sanity check, sprawling-diff guard, and `PreToolUse`
hook on `Bash` previously listed here are now designed in
[`design/v0-1-pr-revision.md`](design/v0-1-pr-revision.md) under
"Worker-side hardening" — they ride along with the v0.1 surface area.

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
