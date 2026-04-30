from datetime import datetime, timezone
from pathlib import Path

import pytest

from autobot.state import State


@pytest.fixture
def state(tmp_path: Path) -> State:
    s = State(tmp_path / "state.db")
    yield s
    s.close()


def _make_task(state: State, task_id: str = "local:foo") -> None:
    state.insert_task(
        task_id=task_id,
        source="local_file",
        source_ref="/tmp/foo.md",
        repo="twentylemon/duckbot",
        title="Foo",
        body="Do the foo.",
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )


def test_schema_bootstrap_is_idempotent(tmp_path: Path) -> None:
    State(tmp_path / "state.db").close()
    State(tmp_path / "state.db").close()


def test_insert_task_returns_true_on_first_insert(state: State) -> None:
    inserted = state.insert_task(
        task_id="local:foo",
        source="local_file",
        source_ref="/tmp/foo.md",
        repo="twentylemon/duckbot",
        title="Foo",
        body="Do the foo.",
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    assert inserted is True


def test_insert_task_returns_false_on_duplicate(state: State) -> None:
    _make_task(state)
    duplicate = state.insert_task(
        task_id="local:foo",
        source="local_file",
        source_ref="/tmp/foo.md",
        repo="twentylemon/duckbot",
        title="Foo",
        body="Do the foo.",
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    assert duplicate is False


def test_get_pending_returns_only_pending(state: State) -> None:
    _make_task(state, "local:a")
    _make_task(state, "local:b")
    state.update_status("local:a", "submitted", pr_url="https://github.com/x/y/pull/1", pr_number=1, branch="b")
    pending = state.get_pending()
    assert [t.id for t in pending] == ["local:b"]


def test_get_by_id_returns_row(state: State) -> None:
    _make_task(state)
    row = state.get_by_id("local:foo")
    assert row is not None
    assert row.title == "Foo"
    assert row.status == "pending"


def test_update_status_to_submitted_persists_pr_fields(state: State) -> None:
    _make_task(state)
    state.update_status("local:foo", "submitted", branch="twentylemon/autobot/foo", pr_url="https://github.com/x/y/pull/7", pr_number=7, session_id="sess-1")
    row = state.get_by_id("local:foo")
    assert row.status == "submitted"
    assert row.pr_number == 7
    assert row.pr_url == "https://github.com/x/y/pull/7"
    assert row.branch == "twentylemon/autobot/foo"
    assert row.session_id == "sess-1"


def test_update_status_rejects_unknown_status(state: State) -> None:
    _make_task(state)
    with pytest.raises(ValueError, match="unknown status"):
        state.update_status("local:foo", "bogus")


def test_update_status_rejects_unknown_task(state: State) -> None:
    with pytest.raises(KeyError):
        state.update_status("local:missing", "submitted")


def test_update_status_blocks_transition_out_of_terminal(state: State) -> None:
    _make_task(state)
    state.update_status("local:foo", "failed_no_changes")
    with pytest.raises(ValueError, match="terminal state"):
        state.update_status("local:foo", "submitted")


def test_update_status_idempotent_within_same_terminal_status(state: State) -> None:
    _make_task(state)
    state.update_status("local:foo", "failed_unknown")
    state.update_status("local:foo", "failed_unknown")  # no error
    assert state.get_by_id("local:foo").status == "failed_unknown"
