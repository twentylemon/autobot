import asyncio
from datetime import datetime, timezone
from pathlib import Path

from autobot import worker
from autobot.config import Config
from autobot.state import State

from tests.conftest import boom_query, fake_query


def _submitted_row(state: State, task_id: str = "local:foo-abc123") -> None:
    state.insert_task(
        task_id=task_id,
        source="local_file",
        source_ref="/tmp/foo.md",
        repo="twentylemon/duckbot",
        title="Foo",
        body="Do the foo.",
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    state.update_status(task_id, "submitted", branch="twentylemon/autobot/foo-abc123",
                        pr_url="https://github.com/twentylemon/duckbot/pull/7", pr_number=7)


def test_poll_with_new_comment_transitions_to_needs_revision(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    payload = {"status": "needs_revision", "last_comment_id": 4242}
    asyncio.run(worker.poll_pr(row, state, config, query_fn=fake_query(payload)))

    final = state.get_by_id(row.id)
    assert final.status == "needs_revision"
    assert final.last_comment_id == 4242
    assert final.last_revision_at is not None


def test_poll_with_no_action_only_stamps_revision_at(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    payload = {"status": "no_action"}
    asyncio.run(worker.poll_pr(row, state, config, query_fn=fake_query(payload)))

    final = state.get_by_id(row.id)
    assert final.status == "submitted"
    assert final.last_comment_id is None
    assert final.last_revision_at is not None


def test_poll_writes_per_invocation_log(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    asyncio.run(worker.poll_pr(row, state, config, query_fn=fake_query({"status": "no_action"})))

    poll_logs = list(config.logs_dir.glob("foo-abc123.poll-*.log"))
    assert len(poll_logs) == 1


def test_poll_unexpected_result_leaves_row_unchanged(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    # An initial-style "submitted" payload shouldn't come back from a poll.
    payload = {"status": "submitted", "pr_url": "u", "pr_number": 1, "branch": "b", "head_sha": "s"}
    asyncio.run(worker.poll_pr(row, state, config, query_fn=fake_query(payload)))

    final = state.get_by_id(row.id)
    assert final.status == "submitted"  # unchanged
    assert final.last_comment_id is None


def test_poll_sdk_crash_does_not_change_state(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    asyncio.run(worker.poll_pr(row, state, config, query_fn=boom_query("api down")))

    final = state.get_by_id(row.id)
    assert final.status == "submitted"


def test_poll_skips_row_with_no_pr_number(state: State, config: Config) -> None:
    from autobot import results

    state.insert_task(
        task_id="local:nopr",
        source="local_file", source_ref="/tmp/x.md", repo="twentylemon/duckbot",
        title="x", body="x", created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    # Manually shove status into 'submitted' without setting pr_number so we can exercise the guard.
    state._conn.execute("UPDATE tasks SET status='submitted' WHERE id='local:nopr'")
    row = state.get_by_id("local:nopr")

    # boom_query would raise if poll_pr actually invoked the SDK.
    result = asyncio.run(worker.poll_pr(row, state, config, query_fn=boom_query("should not be called")))
    assert isinstance(result, results.Unknown)
    assert "pr_number" in result.reason
