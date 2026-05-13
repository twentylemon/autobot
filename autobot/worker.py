"""Worker: orchestrates one task end-to-end across the v0.1 phases.

Initial run, poll, stale-lease recovery, and revision pass all go through
helpers here. Each helper is small — paths come from `compute_paths`,
prompts from `prompts.py`, results from `results.py`, state writes from
`state.py`. Claude does all git/gh.
"""

import dataclasses
import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    SystemMessage,
    query,
)
from claude_agent_sdk.types import HookMatcher

from autobot import results
from autobot.config import Config
from autobot.prompts import PromptInputs, RevisionInputs, render_initial_prompt, render_revision_prompt
from autobot.sdk_hooks import make_block_destructive_bash
from autobot.sources.base import Task, TaskSource
from autobot.state import State, TaskRow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskPaths:
    canonical_dir: Path
    worktree_dir: Path
    branch: str
    result_file: Path
    log_file: Path  # initial-run log; poll/revision get derived siblings


def _repo_dir_name(repo: str) -> str:
    owner, _, name = repo.partition("/")
    return f"{owner}__{name}"


def _slug_from_id(task_id: str) -> str:
    # task ids look like "local:add-dark-mode-9c1f4a"
    _, _, rest = task_id.partition(":")
    return rest or task_id


def compute_paths(task: TaskRow | Task, config: Config) -> TaskPaths:
    repo_dir = config.work_dir / _repo_dir_name(task.repo)
    slug = _slug_from_id(task.id)
    return TaskPaths(
        canonical_dir=repo_dir / "main",
        worktree_dir=repo_dir / "wt" / slug,
        branch=f"twentylemon/autobot/{slug}",
        result_file=config.results_dir / f"{slug}.json",
        log_file=config.logs_dir / f"{slug}.log",
    )


def _task_from_row(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        source=row.source,
        source_ref=row.source_ref or "",
        repo=row.repo,
        title=row.title,
        body=row.body,
        created_at=row.created_at,
    )


# Type alias for an injectable SDK invoker so tests can substitute a fake.
QueryFn = Callable[[str, ClaudeAgentOptions], AsyncIterator[Message]]


def _default_query(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
    return query(prompt=prompt, options=options)


def _build_options(config: Config, canonical_dir: Path) -> ClaudeAgentOptions:
    """Same options shape for every phase: bypass permissions, block stalling tools,
    and refuse the destructive-bash patterns that would be expensive to recover from."""
    return ClaudeAgentOptions(
        cwd=str(config.work_dir),
        permission_mode="bypassPermissions",
        disallowed_tools=["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"],
        hooks={
            "PreToolUse": [HookMatcher(matcher="Bash", hooks=[make_block_destructive_bash(canonical_dir)])],
        },
    )


async def _stream_to_log(stream: AsyncIterator[Message], log_file: Path) -> str | None:
    """Write each message to log_file as JSONL. Return final session_id if seen."""
    session_id: str | None = None
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        async for msg in stream:
            f.write(_message_to_log_line(msg) + "\n")
            f.flush()
            if isinstance(msg, ResultMessage):
                session_id = msg.session_id
            elif isinstance(msg, AssistantMessage) and msg.session_id and not session_id:
                session_id = msg.session_id
            elif isinstance(msg, SystemMessage):
                sid = msg.data.get("session_id") if isinstance(msg.data, dict) else None
                if sid and not session_id:
                    session_id = sid
    return session_id


def _message_to_log_line(msg: Message) -> str:
    payload: dict = {"type": type(msg).__name__}
    try:
        payload.update(dataclasses.asdict(msg))
    except TypeError:
        payload["repr"] = repr(msg)
    return json.dumps(payload, default=str)


def _write_error_sidecar(result_file: Path, reason: str, log_file: Path) -> None:
    sidecar = result_file.with_suffix(result_file.suffix + ".error")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(f"{reason}\nlog: {log_file}\n", encoding="utf-8")


def _fs_safe_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(":", "-")


# ---- Phase 5: Execute pending --------------------------------------------


async def execute_task(
    row: TaskRow,
    source: TaskSource,
    state: State,
    config: Config,
    *,
    query_fn: QueryFn = _default_query,
) -> results.Result:
    """Run one pending task to completion. Returns the parsed result."""
    task = _task_from_row(row)
    paths = compute_paths(row, config)

    # Move the source file out of the inbox BEFORE invoking Claude so a crash
    # mid-run doesn't cause the next tick to re-pick the same task.
    source.mark_picked_up(task)

    prompt = render_initial_prompt(
        PromptInputs(
            task=task,
            canonical_dir=paths.canonical_dir,
            worktree_dir=paths.worktree_dir,
            branch=paths.branch,
            result_file=paths.result_file,
        )
    )
    options = _build_options(config, paths.canonical_dir)

    session_id: str | None = None
    try:
        session_id = await _stream_to_log(query_fn(prompt, options), paths.log_file)
    except Exception as e:  # noqa: BLE001 - want to convert any SDK failure to a state transition
        _write_error_sidecar(paths.result_file, f"claude SDK error: {e!r}", paths.log_file)
        state.update_status(row.id, "failed_unknown", session_id=session_id)
        return results.Unknown(reason=f"sdk error: {e!r}")

    result = results.read(paths.result_file)
    _apply_initial_result(state, row.id, result, session_id, paths)
    return result


def _apply_initial_result(
    state: State,
    task_id: str,
    result: results.Result,
    session_id: str | None,
    paths: TaskPaths,
) -> None:
    if isinstance(result, results.Submitted):
        state.update_status(
            task_id,
            "submitted",
            branch=result.branch,
            pr_url=result.pr_url,
            pr_number=result.pr_number,
            session_id=session_id,
        )
        return
    if isinstance(result, results.NoChanges):
        _write_error_sidecar(paths.result_file, f"no_changes: {result.reason}", paths.log_file)
        state.update_status(task_id, "failed_no_changes", session_id=session_id)
        return
    if isinstance(result, results.NoPr):
        _write_error_sidecar(paths.result_file, f"no_pr: {result.reason}", paths.log_file)
        state.update_status(task_id, "failed_no_pr", branch=result.branch, session_id=session_id)
        return
    # Unknown, or any shape that doesn't belong on an initial run (NeedsRevision,
    # NoAction, Revised) — treat as Unknown.
    reason = result.reason if isinstance(result, results.Unknown) else f"unexpected initial result type: {type(result).__name__}"
    _write_error_sidecar(paths.result_file, f"unknown: {reason}", paths.log_file)
    state.update_status(task_id, "failed_unknown", session_id=session_id)


# ---- Phase 2: Poll -------------------------------------------------------


# Injectable for tests; in production we shell out to `gh` (already required + authed).
GhFn = Callable[[list[str]], Any]


def _default_gh(args: list[str]) -> Any:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def poll_pr(
    row: TaskRow,
    state: State,
    config: Config,
    *,
    gh_fn: GhFn = _default_gh,
) -> results.Result:
    """Cheap poll on a submitted PR. Pure HTTP + JSON filtering — no LLM call.

    Transitions submitted → needs_revision if a new non-bot comment exists past
    `last_comment_id`. If the PR has left draft+open state (closed, merged, or
    marked ready for review), the user has taken over — transition to `completed`.
    """
    if row.pr_number is None or row.pr_url is None:
        log.warning("skipping poll for %s: no pr_number/pr_url recorded", row.id)
        return results.Unknown(reason="missing pr_number or pr_url")

    try:
        meta = gh_fn(["pr", "view", str(row.pr_number), "--repo", row.repo, "--json", "isDraft,state,author"])
        if meta.get("state") != "OPEN" or not meta.get("isDraft"):
            log.info("poll: %s no longer managed (state=%s isDraft=%s) — marking completed",
                     row.id, meta.get("state"), meta.get("isDraft"))
            state.update_status(row.id, "completed")
            return results.NoAction()
        author_login = meta["author"]["login"]
        comments = gh_fn(["api", f"repos/{row.repo}/issues/{row.pr_number}/comments", "--paginate"])
    except Exception as e:  # noqa: BLE001 - any gh failure is a no-op + warning
        log.warning("poll gh error for %s: %r", row.id, e)
        return results.Unknown(reason=f"gh error: {e!r}")

    last = row.last_comment_id or 0
    qualifying = [c for c in comments if c["id"] > last and c["user"]["login"] != author_login]
    if not qualifying:
        return results.NoAction()

    new_last = max(c["id"] for c in qualifying)
    state.record_poll_result(row.id, new_last)
    log.info("poll: %s has %d new comment(s) (last_comment_id=%d)", row.id, len(qualifying), new_last)
    return results.NeedsRevision(last_comment_id=new_last)


# ---- Phase 3: Recover stale leases ---------------------------------------


def recover_stale_leases(state: State, *, cutoff_minutes: int = 30) -> int:
    """Send `revising` rows older than the cutoff back to `needs_revision`.

    A row gets stuck in `revising` when the worker crashes mid-revision; without
    this recovery step the row would never get retried.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cutoff_minutes)
    stale = state.get_stale_revising(cutoff)
    for row in stale:
        log.warning("stale-lease recovery: %s revising since %s — back to needs_revision", row.id, row.updated_at.isoformat())
        try:
            state.update_status(row.id, "needs_revision")
        except Exception:
            log.exception("recover crashed on task %s", row.id)
    return len(stale)


# ---- Phase 6: Execute revisions ------------------------------------------


async def revise_task(
    row: TaskRow,
    state: State,
    config: Config,
    *,
    query_fn: QueryFn = _default_query,
    gh_fn: GhFn = _default_gh,
) -> results.Result:
    """Run one revision pass for a `needs_revision` row. Per-PR rate is bounded
    naturally: only one row per PR is in `needs_revision` at a time, and the
    next revision can't start until this one finishes (or stale-lease recovery
    bumps it back)."""
    # Pre-check: if the user has taken the PR out of draft+open state, hand it
    # back without burning an SDK call. Cheaper than discovering it inside Claude.
    if row.pr_number is not None:
        try:
            meta = gh_fn(["pr", "view", str(row.pr_number), "--repo", row.repo, "--json", "isDraft,state"])
        except Exception as e:  # noqa: BLE001 - skip this tick, retry next
            log.warning("revision pre-check gh error for %s: %r", row.id, e)
            return results.Unknown(reason=f"gh pre-check error: {e!r}")
        if meta.get("state") != "OPEN" or not meta.get("isDraft"):
            log.info("revision: %s no longer managed (state=%s isDraft=%s) — marking completed",
                     row.id, meta.get("state"), meta.get("isDraft"))
            state.update_status(row.id, "completed")
            return results.NoAction()

    paths = compute_paths(row, config)
    state.record_revision_start(row.id)
    revision_n = row.revision_count + 1
    revision_log = paths.log_file.with_name(f"{paths.log_file.stem}.revision-{revision_n}.log")
    revision_result_file = paths.result_file.with_name(f"{paths.result_file.stem}.revision-{revision_n}.json")

    task = _task_from_row(row)
    prompt = render_revision_prompt(
        RevisionInputs(
            task=task,
            pr_url=row.pr_url or "",
            pr_number=row.pr_number or 0,
            branch=row.branch or paths.branch,
            worktree_dir=paths.worktree_dir,
            last_comment_id=row.last_comment_id or 0,
            result_file=revision_result_file,
        )
    )
    options = _build_options(config, paths.canonical_dir)

    session_id: str | None = None
    try:
        session_id = await _stream_to_log(query_fn(prompt, options), revision_log)
    except Exception as e:  # noqa: BLE001 - SDK failure leaves row in `revising`; stale-lease recovery handles it
        _write_error_sidecar(revision_result_file, f"claude SDK error: {e!r}", revision_log)
        log.warning("revision SDK error for %s: %r — leaving in `revising` for stale-lease recovery", row.id, e)
        return results.Unknown(reason=f"sdk error: {e!r}")

    result = results.read(revision_result_file)
    _apply_revision_result(state, row.id, result, session_id, revision_result_file, revision_log)
    return result


def _apply_revision_result(
    state: State,
    task_id: str,
    result: results.Result,
    session_id: str | None,
    result_file: Path,
    log_file: Path,
) -> None:
    if isinstance(result, results.Revised):
        state.record_revision_result(task_id, last_comment_id=result.last_comment_id, session_id=session_id)
        return
    if isinstance(result, results.NeedsRevision):
        # Mid-pass: new comments arrived. Don't bump revision_count; just queue another pass.
        state.update_status(task_id, "needs_revision", session_id=session_id)
        return
    # Anything else (Unknown, NoPr, NoChanges, Submitted, NoAction) → failed_revision.
    reason = result.reason if isinstance(result, (results.Unknown, results.NoPr, results.NoChanges)) else f"unexpected revision result type: {type(result).__name__}"
    _write_error_sidecar(result_file, f"failed_revision: {reason}", log_file)
    state.update_status(task_id, "failed_revision", session_id=session_id)
