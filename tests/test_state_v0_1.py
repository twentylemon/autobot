from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autobot.state import State


def _seed(state: State, task_id: str = "local:foo") -> None:
    state.insert_task(
        task_id=task_id,
        source="local_file",
        source_ref="/tmp/foo.md",
        repo="twentylemon/duckbot",
        title="Foo",
        body="Do the foo.",
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )


def _to_submitted(state: State, task_id: str = "local:foo") -> None:
    state.update_status(task_id, "submitted", branch="b", pr_url="https://github.com/x/y/pull/1", pr_number=1)


def test_v0_1_columns_present_on_fresh_db(state: State) -> None:
    _seed(state)
    row = state.get_by_id("local:foo")
    assert row.last_comment_id is None
    assert row.revision_count == 0


def test_v0_1_columns_idempotent_on_reopen(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    State(db).close()
    State(db).close()  # should not raise on duplicate ALTER


def test_submitted_to_needs_revision_is_allowed(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "needs_revision")
    assert state.get_by_id("local:foo").status == "needs_revision"


def test_needs_revision_to_revising_is_allowed(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "needs_revision")
    state.update_status("local:foo", "revising")
    assert state.get_by_id("local:foo").status == "revising"


def test_revising_to_submitted_is_allowed(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "needs_revision")
    state.update_status("local:foo", "revising")
    state.update_status("local:foo", "submitted")
    assert state.get_by_id("local:foo").status == "submitted"


def test_submitted_to_pending_is_rejected(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    with pytest.raises(ValueError, match="cannot transition"):
        state.update_status("local:foo", "pending")


def test_failed_revision_is_terminal(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "needs_revision")
    state.update_status("local:foo", "revising")
    state.update_status("local:foo", "failed_revision")
    with pytest.raises(ValueError, match="terminal state"):
        state.update_status("local:foo", "submitted")


def test_failed_too_large_is_terminal(state: State) -> None:
    _seed(state)
    state.update_status("local:foo", "failed_too_large")
    with pytest.raises(ValueError, match="terminal state"):
        state.update_status("local:foo", "needs_revision")


def test_get_submitted_filters_by_pr_number(state: State) -> None:
    _seed(state, "local:a")
    _seed(state, "local:b")
    state.update_status("local:a", "submitted", branch="b", pr_url="u", pr_number=1)
    # local:b stays pending — get_submitted should only see local:a.
    assert [r.id for r in state.get_submitted()] == ["local:a"]


def test_get_needs_revision_returns_only_needs_revision(state: State) -> None:
    _seed(state, "local:a")
    _seed(state, "local:b")
    _to_submitted(state, "local:a")
    state.update_status("local:a", "needs_revision")
    assert [r.id for r in state.get_needs_revision()] == ["local:a"]


def test_get_stale_revising_filters_by_updated_at(state: State) -> None:
    _seed(state, "local:fresh")
    _seed(state, "local:stale")
    for tid in ("local:fresh", "local:stale"):
        _to_submitted(state, tid)
        state.update_status(tid, "needs_revision")
        state.update_status(tid, "revising")
    # Backdate local:stale by hand.
    state._conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", "local:stale"),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    stale = state.get_stale_revising(cutoff)
    assert [r.id for r in stale] == ["local:stale"]


def test_record_poll_result_stamps_last_comment_id(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.record_poll_result("local:foo", last_comment_id=42)
    row = state.get_by_id("local:foo")
    assert row.status == "needs_revision"
    assert row.last_comment_id == 42


def test_record_poll_result_rejects_wrong_starting_state(state: State) -> None:
    _seed(state)  # status=pending, not submitted
    with pytest.raises(ValueError, match="expected status 'submitted'"):
        state.record_poll_result("local:foo", last_comment_id=1)


def test_record_revision_start_locks_row(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "needs_revision")
    state.record_revision_start("local:foo")
    assert state.get_by_id("local:foo").status == "revising"


def test_record_revision_result_increments_count_and_returns_to_submitted(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "needs_revision")
    state.record_revision_start("local:foo")
    state.record_revision_result("local:foo", last_comment_id=99)
    row = state.get_by_id("local:foo")
    assert row.status == "submitted"
    assert row.revision_count == 1
    assert row.last_comment_id == 99


def test_submitted_to_completed_is_allowed(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "completed")
    assert state.get_by_id("local:foo").status == "completed"


def test_needs_revision_to_completed_is_allowed(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "needs_revision")
    state.update_status("local:foo", "completed")
    assert state.get_by_id("local:foo").status == "completed"


def test_revising_to_completed_is_allowed(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "needs_revision")
    state.update_status("local:foo", "revising")
    state.update_status("local:foo", "completed")
    assert state.get_by_id("local:foo").status == "completed"


def test_completed_is_terminal(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    state.update_status("local:foo", "completed")
    with pytest.raises(ValueError, match="terminal state"):
        state.update_status("local:foo", "needs_revision")


def test_record_revision_result_increments_each_pass(state: State) -> None:
    _seed(state)
    _to_submitted(state)
    for cid in (10, 20, 30):
        state.update_status("local:foo", "needs_revision")
        state.record_revision_start("local:foo")
        state.record_revision_result("local:foo", last_comment_id=cid)
    row = state.get_by_id("local:foo")
    assert row.revision_count == 3
    assert row.last_comment_id == 30
