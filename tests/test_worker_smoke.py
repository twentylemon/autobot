import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest

from autobot import worker
from autobot.config import Config
from autobot.sources.base import Task, TaskSource
from autobot.state import State


class FakeSource(TaskSource):
    name = "local_file"

    def __init__(self) -> None:
        self.picked: list[str] = []
        self.completed: list[tuple[str, str]] = []

    def discover(self):
        return []

    def mark_picked_up(self, task: Task) -> None:
        self.picked.append(task.id)

    def mark_completed(self, task: Task, pr_url: str) -> None:
        self.completed.append((task.id, pr_url))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    home = tmp_path / "home"
    return Config(
        anthropic_api_key="x",
        github_token="y",
        inbox_dir=home / "inbox",
        processing_dir=home / "processing",
        work_dir=home / "work",
        results_dir=home / "results",
        logs_dir=home / "logs",
        state_db=home / "state.db",
        default_repo=None,
    )


@pytest.fixture
def state(tmp_path: Path) -> State:
    s = State(tmp_path / "state.db")
    yield s
    s.close()


def _seed(state: State, task_id: str = "local:foo-abc123") -> None:
    state.insert_task(
        task_id=task_id,
        source="local_file",
        source_ref="/tmp/foo.md",
        repo="twentylemon/duckbot",
        title="Foo",
        body="Do the foo.",
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )


def _fake_query(result_file: Path, payload: dict | None, session_id: str = "sess-1"):
    """Build a query_fn that drops `payload` at result_file as a side effect."""

    async def gen(prompt, options) -> AsyncIterator:
        # Side effect: simulate Claude writing the result file.
        if payload is not None:
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text(json.dumps(payload), encoding="utf-8")
        # Emit the same shapes the real SDK does: a SystemMessage("init") then
        # a ResultMessage. We emit dataclass-ish objects so dataclasses.asdict works.
        from claude_agent_sdk import ResultMessage, SystemMessage

        yield SystemMessage(subtype="init", data={"session_id": session_id})
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=session_id,
        )

    return gen


def test_compute_paths_uses_repo_and_slug(config: Config) -> None:
    row = _row(config, repo="twentylemon/duckbot", task_id="local:foo-abc123")
    paths = worker.compute_paths(row, config)
    assert paths.canonical_dir == config.work_dir / "twentylemon__duckbot" / "main"
    assert paths.worktree_dir == config.work_dir / "twentylemon__duckbot" / "wt" / "foo-abc123"
    assert paths.branch == "twentylemon/autobot/foo-abc123"
    assert paths.result_file == config.results_dir / "foo-abc123.json"
    assert paths.log_file == config.logs_dir / "foo-abc123.log"


def _row(config: Config, repo: str, task_id: str):
    # Construct a TaskRow without going through State; worker.compute_paths only reads .repo + .id.
    from autobot.state import TaskRow

    return TaskRow(
        id=task_id,
        source="local_file",
        source_ref="/tmp/foo.md",
        repo=repo,
        title="Foo",
        body="b",
        status="pending",
        branch=None,
        pr_url=None,
        pr_number=None,
        session_id=None,
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )


def test_submitted_result_transitions_to_submitted(state: State, config: Config) -> None:
    _seed(state)
    row = state.get_by_id("local:foo-abc123")
    src = FakeSource()
    paths = worker.compute_paths(row, config)
    payload = {
        "status": "submitted",
        "pr_url": "https://github.com/x/y/pull/4",
        "pr_number": 4,
        "branch": paths.branch,
        "head_sha": "abc",
    }
    asyncio.run(worker.execute_task(row, src, state, config, query_fn=_fake_query(paths.result_file, payload)))

    assert src.picked == [row.id]  # source picked up before invoking Claude
    final = state.get_by_id(row.id)
    assert final.status == "submitted"
    assert final.pr_number == 4
    assert final.pr_url == "https://github.com/x/y/pull/4"
    assert final.branch == paths.branch
    assert final.session_id == "sess-1"
    # No error sidecar on success.
    assert not paths.result_file.with_suffix(".json.error").exists()


def test_no_changes_result_transitions_and_writes_sidecar(state: State, config: Config) -> None:
    _seed(state)
    row = state.get_by_id("local:foo-abc123")
    src = FakeSource()
    paths = worker.compute_paths(row, config)
    payload = {"status": "no_changes", "reason": "nothing to do"}
    asyncio.run(worker.execute_task(row, src, state, config, query_fn=_fake_query(paths.result_file, payload)))
    assert state.get_by_id(row.id).status == "failed_no_changes"
    sidecar = paths.result_file.with_suffix(".json.error")
    assert sidecar.exists()
    assert "nothing to do" in sidecar.read_text(encoding="utf-8")


def test_no_pr_result_records_branch(state: State, config: Config) -> None:
    _seed(state)
    row = state.get_by_id("local:foo-abc123")
    src = FakeSource()
    paths = worker.compute_paths(row, config)
    payload = {"status": "no_pr", "reason": "push 403", "branch": paths.branch}
    asyncio.run(worker.execute_task(row, src, state, config, query_fn=_fake_query(paths.result_file, payload)))
    final = state.get_by_id(row.id)
    assert final.status == "failed_no_pr"
    assert final.branch == paths.branch
    sidecar = paths.result_file.with_suffix(".json.error")
    assert sidecar.exists()
    assert "push 403" in sidecar.read_text(encoding="utf-8")


def test_missing_result_file_is_failed_unknown(state: State, config: Config) -> None:
    _seed(state)
    row = state.get_by_id("local:foo-abc123")
    src = FakeSource()
    paths = worker.compute_paths(row, config)
    # payload=None means the fake doesn't write the result file.
    asyncio.run(worker.execute_task(row, src, state, config, query_fn=_fake_query(paths.result_file, None)))
    assert state.get_by_id(row.id).status == "failed_unknown"
    assert paths.result_file.with_suffix(".json.error").exists()


def test_sdk_exception_is_failed_unknown(state: State, config: Config) -> None:
    _seed(state)
    row = state.get_by_id("local:foo-abc123")
    src = FakeSource()
    paths = worker.compute_paths(row, config)

    def boom(prompt, options):
        async def gen():
            raise RuntimeError("api down")
            yield  # pragma: no cover - makes this an async generator

        return gen()

    asyncio.run(worker.execute_task(row, src, state, config, query_fn=boom))
    assert state.get_by_id(row.id).status == "failed_unknown"
    sidecar = paths.result_file.with_suffix(".json.error")
    assert sidecar.exists()
    assert "api down" in sidecar.read_text(encoding="utf-8")


def test_source_picked_up_even_if_claude_crashes(state: State, config: Config) -> None:
    _seed(state)
    row = state.get_by_id("local:foo-abc123")
    src = FakeSource()

    def boom(prompt, options):
        async def gen():
            raise RuntimeError("boom")
            yield  # pragma: no cover

        return gen()

    asyncio.run(worker.execute_task(row, src, state, config, query_fn=boom))
    assert src.picked == [row.id]


def test_log_file_written_with_messages(state: State, config: Config) -> None:
    _seed(state)
    row = state.get_by_id("local:foo-abc123")
    src = FakeSource()
    paths = worker.compute_paths(row, config)
    payload = {
        "status": "submitted",
        "pr_url": "u",
        "pr_number": 1,
        "branch": paths.branch,
        "head_sha": "s",
    }
    asyncio.run(worker.execute_task(row, src, state, config, query_fn=_fake_query(paths.result_file, payload)))
    assert paths.log_file.exists()
    lines = [ln for ln in paths.log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2  # SystemMessage + ResultMessage
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[0]["type"] == "SystemMessage"
    assert parsed[1]["type"] == "ResultMessage"
