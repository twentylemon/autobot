from datetime import datetime, timezone

from autobot import worker
from autobot.state import State


def _to_revising(state: State, task_id: str) -> None:
    state.insert_task(
        task_id=task_id, source="local_file", source_ref=f"/tmp/{task_id}.md",
        repo="twentylemon/duckbot", title="t", body="b",
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    state.update_status(task_id, "submitted", branch="b", pr_url="u", pr_number=1)
    state.update_status(task_id, "needs_revision")
    state.update_status(task_id, "revising")


def _backdate(state: State, task_id: str, iso: str) -> None:
    state._conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (iso, task_id))


def test_stale_revising_row_recovered_to_needs_revision(state: State) -> None:
    _to_revising(state, "local:stale")
    _backdate(state, "local:stale", "2020-01-01T00:00:00+00:00")

    recovered = worker.recover_stale_leases(state, cutoff_minutes=30)
    assert recovered == 1
    assert state.get_by_id("local:stale").status == "needs_revision"


def test_fresh_revising_row_left_alone(state: State) -> None:
    _to_revising(state, "local:fresh")
    # updated_at is "now" — not stale.

    recovered = worker.recover_stale_leases(state, cutoff_minutes=30)
    assert recovered == 0
    assert state.get_by_id("local:fresh").status == "revising"


def test_recovery_handles_mixed_set(state: State) -> None:
    _to_revising(state, "local:stale1")
    _to_revising(state, "local:stale2")
    _to_revising(state, "local:fresh")
    _backdate(state, "local:stale1", "2020-01-01T00:00:00+00:00")
    _backdate(state, "local:stale2", "2021-01-01T00:00:00+00:00")

    recovered = worker.recover_stale_leases(state, cutoff_minutes=30)
    assert recovered == 2
    assert state.get_by_id("local:stale1").status == "needs_revision"
    assert state.get_by_id("local:stale2").status == "needs_revision"
    assert state.get_by_id("local:fresh").status == "revising"
