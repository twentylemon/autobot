"""Worker: orchestrates one task end-to-end across the v0.1 phases.

Initial run, poll, stale-lease recovery, and revision pass all go through
helpers here. Each helper is small — paths come from `compute_paths`,
prompts from `prompts.py`, results from `results.py`, state writes from
`state.py`. Claude does all git/gh.
"""

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable

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
from autobot.prompts import PollInputs, PromptInputs, RevisionInputs, render_initial_prompt, render_poll_prompt, render_revision_prompt
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
            max_diff_loc=config.max_diff_loc,
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
    if isinstance(result, results.FailedTooLarge):
        _write_error_sidecar(paths.result_file, f"failed_too_large: {result.insertions}+{result.deletions} loc", paths.log_file)
        state.update_status(task_id, "failed_too_large", session_id=session_id)
        return
    # Unknown, or any shape that doesn't belong on an initial run (NeedsRevision,
    # NoAction, Revised) — treat as Unknown.
    reason = result.reason if isinstance(result, results.Unknown) else f"unexpected initial result type: {type(result).__name__}"
    _write_error_sidecar(paths.result_file, f"unknown: {reason}", paths.log_file)
    state.update_status(task_id, "failed_unknown", session_id=session_id)


# ---- Phase 2: Poll -------------------------------------------------------


async def poll_pr(
    row: TaskRow,
    state: State,
    config: Config,
    *,
    query_fn: QueryFn = _default_query,
) -> results.Result:
    """Cheap poll on a submitted PR. Transitions submitted → needs_revision if a new
    human comment exists, else just stamps last_revision_at."""
    paths = compute_paths(row, config)
    if row.pr_number is None or row.pr_url is None:
        log.warning("skipping poll for %s: no pr_number/pr_url recorded", row.id)
        return results.Unknown(reason="missing pr_number or pr_url")

    poll_log = paths.log_file.with_name(f"{paths.log_file.stem}.poll-{_fs_safe_timestamp()}.log")
    poll_result_file = paths.result_file.with_name(f"{paths.result_file.stem}.poll-{_fs_safe_timestamp()}.json")

    prompt = render_poll_prompt(
        PollInputs(
            repo=row.repo,
            pr_url=row.pr_url,
            pr_number=row.pr_number,
            last_comment_id=row.last_comment_id or 0,
            result_file=poll_result_file,
        )
    )
    options = _build_options(config, paths.canonical_dir)

    try:
        await _stream_to_log(query_fn(prompt, options), poll_log)
    except Exception as e:  # noqa: BLE001 - convert SDK failure to a no-op + warning
        log.warning("poll SDK error for %s: %r", row.id, e)
        return results.Unknown(reason=f"sdk error: {e!r}")

    result = results.read(poll_result_file)
    if isinstance(result, results.NeedsRevision):
        state.record_poll_result(row.id, result.last_comment_id)
        log.info("poll: %s has new comments (last_comment_id=%d)", row.id, result.last_comment_id)
        return result
    if isinstance(result, results.NoAction):
        state.record_no_action(row.id)
        return result
    log.warning("poll for %s returned unexpected result %r — leaving row unchanged", row.id, result)
    return result


# ---- Phase 3: Recover stale leases ---------------------------------------


def recover_stale_leases(state: State, *, cutoff_minutes: int = 30) -> int:
    """Send `revising` rows older than the cutoff back to `needs_revision`.

    A row gets stuck in `revising` when the worker crashes mid-revision; without
    this recovery step the row would never get retried.
    """
    cutoff = datetime.now(timezone.utc) - _minutes(cutoff_minutes)
    stale = state.get_stale_revising(cutoff)
    for row in stale:
        log.warning("stale-lease recovery: %s revising since %s — back to needs_revision", row.id, row.updated_at.isoformat())
        state.update_status(row.id, "needs_revision")
    return len(stale)


def _minutes(n: int):
    from datetime import timedelta

    return timedelta(minutes=n)


# ---- Phase 6: Execute revisions ------------------------------------------


async def revise_task(
    row: TaskRow,
    state: State,
    config: Config,
    *,
    query_fn: QueryFn = _default_query,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> results.Result:
    """Run one revision pass for a `needs_revision` row. Enforces the per-PR
    revision cap and cooldown before invoking Claude."""
    paths = compute_paths(row, config)

    # Hard cap: too many revisions, give up.
    if row.revision_count >= config.revision_cap:
        log.warning("revision cap hit for %s (%d >= %d) — marking failed_revision", row.id, row.revision_count, config.revision_cap)
        # needs_revision -> revising -> failed_revision keeps within the allowed table.
        state.record_revision_start(row.id)
        _write_error_sidecar(paths.result_file, f"revision cap reached: {row.revision_count}", paths.log_file)
        state.update_status(row.id, "failed_revision")
        return results.Unknown(reason=f"revision cap {config.revision_cap} reached")

    # Cooldown: revised too recently — skip silently this tick.
    if row.last_revision_at is not None:
        elapsed = now() - row.last_revision_at
        if elapsed < _minutes(config.revision_cooldown_minutes):
            log.debug("revision cooldown for %s (last %s ago) — skipping this tick", row.id, elapsed)
            return results.NoAction()

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
            max_diff_loc=config.max_diff_loc,
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
        state.record_revision_result(task_id, last_comment_id=result.last_comment_id)
        if session_id:
            # Stamp the latest session_id without changing status.
            state.update_status(task_id, "submitted", session_id=session_id)
        return
    if isinstance(result, results.NeedsRevision):
        # Mid-pass: new comments arrived. Don't bump revision_count; just queue another pass.
        state.update_status(task_id, "needs_revision", session_id=session_id)
        return
    if isinstance(result, results.FailedTooLarge):
        _write_error_sidecar(result_file, f"failed_too_large: {result.insertions}+{result.deletions} loc", log_file)
        state.update_status(task_id, "failed_too_large", session_id=session_id)
        return
    # Anything else (Unknown, NoPr, NoChanges, Submitted, NoAction) → failed_revision.
    reason = result.reason if isinstance(result, (results.Unknown, results.NoPr, results.NoChanges)) else f"unexpected revision result type: {type(result).__name__}"
    _write_error_sidecar(result_file, f"failed_revision: {reason}", log_file)
    state.update_status(task_id, "failed_revision", session_id=session_id)
