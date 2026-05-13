import asyncio
from datetime import datetime, timezone

from autobot import worker
from autobot.config import Config
from autobot.state import State

from tests.conftest import boom_query, fake_query, ok_gh


def _seed_needs_revision(state: State, task_id: str = "local:foo-abc123", *, last_comment_id: int = 100) -> None:
    state.insert_task(
        task_id=task_id,
        source="local_file", source_ref="/tmp/foo.md", repo="twentylemon/duckbot",
        title="Foo", body="Do the foo.",
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    state.update_status(task_id, "submitted", branch="twentylemon/autobot/foo-abc123",
                        pr_url="https://github.com/twentylemon/duckbot/pull/7", pr_number=7)
    state.record_poll_result(task_id, last_comment_id=last_comment_id)


def test_revised_result_returns_to_submitted_and_increments_count(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")
    payload = {"status": "revised", "last_comment_id": 200, "head_sha": "abc"}
    asyncio.run(worker.revise_task(row, state, config, query_fn=fake_query(payload), gh_fn=ok_gh()))

    final = state.get_by_id(row.id)
    assert final.status == "submitted"
    assert final.revision_count == 1
    assert final.last_comment_id == 200


def test_needs_revision_mid_pass_returns_to_needs_revision(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")
    payload = {"status": "needs_revision", "last_comment_id": 150}
    asyncio.run(worker.revise_task(row, state, config, query_fn=fake_query(payload), gh_fn=ok_gh()))

    final = state.get_by_id(row.id)
    assert final.status == "needs_revision"
    # Mid-pass NeedsRevision shouldn't bump revision_count — that only happens on Revised.
    assert final.revision_count == 0


def test_no_pr_result_terminates_as_failed_revision(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")
    payload = {"status": "no_pr", "reason": "push 403", "branch": "twentylemon/autobot/foo-abc123"}
    asyncio.run(worker.revise_task(row, state, config, query_fn=fake_query(payload), gh_fn=ok_gh()))

    assert state.get_by_id(row.id).status == "failed_revision"


def test_unknown_result_terminates_as_failed_revision(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")
    asyncio.run(worker.revise_task(row, state, config, query_fn=fake_query({"status": "garbage"}), gh_fn=ok_gh()))

    assert state.get_by_id(row.id).status == "failed_revision"


def test_revision_writes_per_pass_log(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")
    payload = {"status": "revised", "last_comment_id": 200, "head_sha": "abc"}
    asyncio.run(worker.revise_task(row, state, config, query_fn=fake_query(payload), gh_fn=ok_gh()))

    # First revision should land at logs/foo-abc123.revision-1.log
    log = config.logs_dir / "foo-abc123.revision-1.log"
    assert log.exists()


def test_revision_sdk_crash_leaves_row_in_revising(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")
    asyncio.run(worker.revise_task(row, state, config, query_fn=boom_query("api down"), gh_fn=ok_gh()))

    # Stale-lease recovery (a separate phase) is what bumps it back to needs_revision.
    assert state.get_by_id(row.id).status == "revising"


def _no_sdk_call(prompt, options):
    raise AssertionError("SDK should not be invoked when PR is no longer managed")


def test_revision_marks_completed_when_pr_closed(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")
    asyncio.run(worker.revise_task(
        row, state, config,
        query_fn=_no_sdk_call,
        gh_fn=ok_gh(state="CLOSED"),
    ))

    assert state.get_by_id(row.id).status == "completed"


def test_revision_marks_completed_when_pr_ready_for_review(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")
    asyncio.run(worker.revise_task(
        row, state, config,
        query_fn=_no_sdk_call,
        gh_fn=ok_gh(is_draft=False),
    ))

    assert state.get_by_id(row.id).status == "completed"


def test_revision_pre_check_gh_failure_leaves_row_in_needs_revision(state: State, config: Config) -> None:
    _seed_needs_revision(state)
    row = state.get_by_id("local:foo-abc123")

    def boom_gh(args):
        raise RuntimeError("gh down")

    result = asyncio.run(worker.revise_task(
        row, state, config,
        query_fn=_no_sdk_call,
        gh_fn=boom_gh,
    ))
    # Pre-check failure should NOT lock the row into `revising` — it stays
    # in needs_revision so the next tick can retry once gh is back.
    assert state.get_by_id(row.id).status == "needs_revision"
    from autobot import results
    assert isinstance(result, results.Unknown)
