# Destinations — design memo (v0.3)

A forward-looking design for generalizing autobot beyond GitHub PRs.
Deferred per [`../roadmap.md`](../roadmap.md) until v0.1 and v0.2 land
*and* a real first non-PR destination is on the table.

## Why generalize

Today's worker is PR-shaped end-to-end:

- `Task.repo` is mandatory ([`autobot/sources/base.py`](../../autobot/sources/base.py)).
- [`autobot/prompts.py`](../../autobot/prompts.py) hardcodes seven git/`gh`
  steps (clone, worktree, edit, commit, push, `gh pr create --draft`,
  write result).
- [`autobot/results.py`](../../autobot/results.py) defines `Submitted`,
  `NoChanges`, `NoPr` — all PR-specific success/failure shapes.
- [`autobot/state.py`](../../autobot/state.py) has dedicated `repo`,
  `branch`, `pr_url`, `pr_number` columns.
- [`autobot/worker.py`](../../autobot/worker.py)'s `compute_paths()`
  assumes a canonical clone + per-task worktree.

Tasks that aren't a code change to a repo (research one-pagers, drafts,
external-system mutations) don't fit. The fix is a `Destination`
abstraction that mirrors the existing `TaskSource`.

## The `Destination` ABC

Source describes where work *comes from*; destination describes where
work *goes*. Both pluggable, both selected per task.

```python
class Destination(ABC):
    name: str  # "github_pr", "local_file", "confluence", ...

    def workspace(self, task: Task, config: Config) -> Path:
        """Where Claude does its work. Git worktree for github_pr; a
        scratch dir for local_file; possibly None for one-shot answers."""

    def prompt_fragment(self, task: Task, paths: TaskPaths) -> str:
        """Destination-specific block appended to the task body in the
        prompt. For github_pr this is today's 7-step git/gh script. For
        local_file it might be 'write your output to <dir>/output.md
        and any supporting files alongside it'."""

    def parse_result(self, raw: dict) -> Result:
        """Validate Claude's destination-specific result JSON. Different
        destinations have different success shapes (PrSubmitted,
        DocumentWritten, MessageSent, ...)."""

    def record(self, state: State, task_id: str, result: Result,
               session_id: str | None) -> None:
        """Apply the outcome to state.db (whatever fields make sense)."""
```

## Frontmatter contract

A task picks its destination via frontmatter. `github_pr` is the default
so existing inbox files keep working unchanged.

```markdown
---
destination: github_pr   # default — today's behavior
repo: twentylemon/duckbot
---
Add a --dry-run flag to the deploy subcommand.
```

```markdown
---
destination: local_file
output_dir: ~/Documents/autobot-out
---
Research how launchd handles env inheritance and write me a one-pager.
```

Each destination owns the validation of its own required fields. PR
destinations require `repo`; `local_file` requires `output_dir`; etc.

## Refactor task list (when v0.3 lands)

1. **`autobot/sources/base.py`** — `Task.repo` becomes `Optional[str]`.
2. **`autobot/destinations/`** — new package: `base.py` (the ABC),
   `github_pr.py` (extracted from today's `prompts.py` +
   `worker._apply_result`), and a registry mapping `name → Destination`.
3. **`autobot/prompts.py`** — becomes a thin assembler: base preamble +
   `destination.prompt_fragment(...)` + result-file instruction. Stops
   knowing about git/`gh`.
4. **`autobot/worker.py`** — `compute_paths()` and `_apply_result()`
   delegate to the destination. Workspace path moves into
   `destination.workspace(...)`.
5. **`autobot/results.py`** — either expand the union with new variants,
   or (cleaner) let each destination own its result types and have the
   worker only see a generic `Outcome{kind, data, error}`.
6. **`autobot/state.py`** — replace dedicated PR columns with
   `destination TEXT NOT NULL`, `outcome_kind TEXT`, `outcome_data JSON`.
   One-shot migration: move existing `pr_url`/`branch`/`pr_number` rows
   into `outcome_data` blobs with `outcome_kind='submitted'` and
   `destination='github_pr'`.
7. **`autobot/sources/local_files.py`** — read `destination:` from
   frontmatter (default `github_pr`). Reject the file early if the
   declared destination is missing required fields.

## Cheap forward-compat moves to make *now*

Don't ship the abstraction yet, but a few hygiene rules make the future
refactor mechanical instead of surgical:

- Keep [`autobot/prompts.py`](../../autobot/prompts.py) self-contained —
  no imports from `worker.py` or `state.py` so it can later move into
  `destinations/github_pr.py` whole.
- Keep result-shape parsing in [`autobot/results.py`](../../autobot/results.py),
  not inlined into the worker.
- Keep `Result` as a tagged union (it already is) so adding a fourth
  variant is mechanical.
- Treat the result-file JSON as a versioned contract: add fields
  additively; never change a field's type or meaning.

## Things to NOT do prematurely

- **Don't add a `destination` field to `Task` yet.** Until there's a
  second destination, the field is vestigial.
- **Don't rewrite the state schema.** Today's PR-shaped columns are
  fine while there's only one destination kind.
- **Don't build a destination registry / dispatcher.** Single
  destination → no dispatch needed; an `if destination == "github_pr"`
  branch is premature when there's only one branch.
- **Don't pre-design `LocalFileDestination`'s result schema.** The right
  shape will reveal itself when we build a real second destination.

## Triggers for revisiting

Three concrete triggers should kick off the refactor:

1. v0.1 + v0.2 are merged and stable.
2. There's a real, named first non-PR destination — i.e. you have a task
   you'd actually file as a doc / message / something.
3. The PR-revision loop has surfaced enough about what "Claude takes
   feedback on its output" looks like that the pattern can be reused
   for non-PR destinations (a Confluence-page comment loop? Document
   review round-trip via local file edits?).

## Verification (once implemented)

This is design-only today. When the abstraction lands, the criterion is
shape-correctness:

- The existing [`tests/test_worker_smoke.py`](../../tests/test_worker_smoke.py)
  cases run unchanged against `GithubPrDestination`.
- A new `tests/test_destinations_local_file.py` (when the first non-PR
  destination ships) is expressible with the same fixtures + a swapped
  destination object.

If either bends, the abstraction is the wrong shape — keep iterating
before adding a real second destination.
