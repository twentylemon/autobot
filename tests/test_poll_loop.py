from datetime import datetime, timezone

from autobot import results, worker
from autobot.config import Config
from autobot.state import State


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


def fake_gh(*, is_draft: bool = True, state: str = "OPEN", author: str = "autobot[bot]", comments=()):
    """Return a GhFn that dispatches based on the gh args.

    The first arg distinguishes calls: `pr` → metadata, `api` → comments list.
    """
    def gh(args: list[str]):
        if args[0] == "pr" and args[1] == "view":
            return {"isDraft": is_draft, "state": state, "author": {"login": author}}
        if args[0] == "api":
            return list(comments)
        raise AssertionError(f"unexpected gh call: {args!r}")
    return gh


def boom_gh(message: str = "boom"):
    def gh(args: list[str]):
        raise RuntimeError(message)
    return gh


def test_poll_with_new_human_comment_transitions_to_needs_revision(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    comments = [{"id": 4242, "user": {"login": "alice"}}]
    worker.poll_pr(row, state, config, gh_fn=fake_gh(comments=comments))

    final = state.get_by_id(row.id)
    assert final.status == "needs_revision"
    assert final.last_comment_id == 4242


def test_poll_with_no_new_comments_leaves_row_unchanged(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    worker.poll_pr(row, state, config, gh_fn=fake_gh(comments=[]))

    final = state.get_by_id(row.id)
    assert final.status == "submitted"
    assert final.last_comment_id is None


def test_poll_filters_self_comments(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    comments = [
        {"id": 100, "user": {"login": "autobot[bot]"}},  # self — skip
        {"id": 101, "user": {"login": "autobot[bot]"}},  # self — skip
    ]
    worker.poll_pr(row, state, config, gh_fn=fake_gh(author="autobot[bot]", comments=comments))

    assert state.get_by_id(row.id).status == "submitted"


def test_poll_filters_already_seen_comments(state: State, config: Config) -> None:
    _submitted_row(state)
    state._conn.execute("UPDATE tasks SET last_comment_id = 200 WHERE id = 'local:foo-abc123'")
    row = state.get_by_id("local:foo-abc123")
    comments = [
        {"id": 100, "user": {"login": "alice"}},  # too old
        {"id": 200, "user": {"login": "alice"}},  # already seen
        {"id": 201, "user": {"login": "alice"}},  # new
    ]
    worker.poll_pr(row, state, config, gh_fn=fake_gh(comments=comments))

    final = state.get_by_id(row.id)
    assert final.status == "needs_revision"
    assert final.last_comment_id == 201


def test_poll_marks_completed_when_pr_closed(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    # Comments would qualify, but the user closed the PR — that's their "I'm taking over" signal.
    comments = [{"id": 999, "user": {"login": "alice"}}]
    worker.poll_pr(row, state, config, gh_fn=fake_gh(state="CLOSED", comments=comments))

    final = state.get_by_id(row.id)
    assert final.status == "completed"
    assert final.last_comment_id is None


def test_poll_marks_completed_when_pr_marked_ready_for_review(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    comments = [{"id": 999, "user": {"login": "alice"}}]
    worker.poll_pr(row, state, config, gh_fn=fake_gh(is_draft=False, comments=comments))

    assert state.get_by_id(row.id).status == "completed"


def test_poll_marks_completed_when_pr_merged(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    worker.poll_pr(row, state, config, gh_fn=fake_gh(state="MERGED"))

    assert state.get_by_id(row.id).status == "completed"


def test_poll_gh_failure_does_not_change_state(state: State, config: Config) -> None:
    _submitted_row(state)
    row = state.get_by_id("local:foo-abc123")
    result = worker.poll_pr(row, state, config, gh_fn=boom_gh("api down"))

    assert isinstance(result, results.Unknown)
    assert state.get_by_id(row.id).status == "submitted"


def test_poll_skips_row_with_no_pr_number(state: State, config: Config) -> None:
    state.insert_task(
        task_id="local:nopr",
        source="local_file", source_ref="/tmp/x.md", repo="twentylemon/duckbot",
        title="x", body="x", created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    # Manually shove status into 'submitted' without setting pr_number so we can exercise the guard.
    state._conn.execute("UPDATE tasks SET status='submitted' WHERE id='local:nopr'")
    row = state.get_by_id("local:nopr")

    # boom_gh would raise if poll_pr actually called gh.
    result = worker.poll_pr(row, state, config, gh_fn=boom_gh("should not be called"))
    assert isinstance(result, results.Unknown)
    assert "pr_number" in result.reason
