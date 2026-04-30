"""Worker: orchestrates one task end-to-end.

The worker is intentionally tiny — it computes paths, hands a fully-rendered
prompt to Claude, streams Claude's messages to a log file, then reads the
JSON result file Claude writes and updates state.db accordingly. Claude
performs all git/gh operations.
"""

import dataclasses
import json
from dataclasses import dataclass
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

from autobot import results
from autobot.config import Config
from autobot.prompts import PromptInputs, render_initial_prompt
from autobot.sources.base import Task, TaskSource
from autobot.state import State, TaskRow


@dataclass(frozen=True)
class TaskPaths:
    canonical_dir: Path
    worktree_dir: Path
    branch: str
    result_file: Path
    log_file: Path


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
    options = ClaudeAgentOptions(
        cwd=str(config.work_dir),
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
    )

    session_id: str | None = None
    try:
        session_id = await _stream_to_log(query_fn(prompt, options), paths.log_file)
    except Exception as e:  # noqa: BLE001 - want to convert any SDK failure to a state transition
        _write_error_sidecar(paths.result_file, f"claude SDK error: {e!r}", paths.log_file)
        state.update_status(row.id, "failed_unknown", session_id=session_id)
        return results.Unknown(reason=f"sdk error: {e!r}")

    result = results.read(paths.result_file)
    _apply_result(state, row.id, result, session_id, paths)
    return result


def _apply_result(
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
    # Unknown
    _write_error_sidecar(paths.result_file, f"unknown: {result.reason}", paths.log_file)
    state.update_status(task_id, "failed_unknown", session_id=session_id)
