"""Entry point for autobot. `python -m autobot --once` runs one tick."""

import argparse
import asyncio
import logging
import sys
import time

from autobot import worker
from autobot.config import Config, load
from autobot.sources.local_files import LocalFileSource, MissingRepoError
from autobot.state import State

log = logging.getLogger("autobot")


def _build_source(config: Config) -> LocalFileSource:
    return LocalFileSource(
        inbox_dir=config.inbox_dir,
        processing_dir=config.processing_dir,
        default_repo=config.default_repo,
    )


def _ingest(source: LocalFileSource, state: State) -> int:
    """Discover new tasks from the source and persist them. Returns count inserted."""
    inserted = 0
    for task in source.discover():
        try:
            new = state.insert_task(
                task_id=task.id,
                source=task.source,
                source_ref=task.source_ref,
                repo=task.repo,
                title=task.title,
                body=task.body,
                created_at=task.created_at,
            )
        except Exception:
            log.exception("failed to insert task %s", task.id)
            continue
        if new:
            inserted += 1
            log.info("ingested task %s (repo=%s)", task.id, task.repo)
    return inserted


def _poll_open_prs(state: State, config: Config) -> int:
    rows = state.get_submitted()
    for row in rows:
        log.info("polling task %s (pr=%s)", row.id, row.pr_url)
        try:
            worker.poll_pr(row, state, config)
        except Exception:
            log.exception("poll crashed on task %s", row.id)
    return len(rows)


async def _execute_pending(source: LocalFileSource, state: State, config: Config) -> int:
    pending = state.get_pending()
    for row in pending:
        log.info("executing task %s", row.id)
        try:
            await worker.execute_task(row, source, state, config)
        except Exception:
            log.exception("worker crashed on task %s", row.id)
    return len(pending)


async def _execute_revisions(state: State, config: Config) -> int:
    rows = state.get_needs_revision()
    for row in rows:
        log.info("revising task %s (pr=%s, prior_revisions=%d)", row.id, row.pr_url, row.revision_count)
        try:
            await worker.revise_task(row, state, config)
        except Exception:
            log.exception("revision crashed on task %s", row.id)
    return len(rows)


def _tick(config: Config) -> None:
    """Per-tick phases: discover → poll → recover → execute pending → execute revisions.

    Per-task failures inside each phase are caught and logged with task id +
    phase verb + traceback, so one bad row doesn't skip the rest of its phase
    or block subsequent phases. (v0.2 will slot a reconcile phase between
    recover and execute pending.)
    """
    source = _build_source(config)
    state = State(config.state_db)
    try:
        try:
            ingested = _ingest(source, state)
        except MissingRepoError as e:
            log.error("inbox file rejected: %s", e)
            ingested = 0

        polled = _poll_open_prs(state, config)
        recovered = worker.recover_stale_leases(state)

        async def run_async_phases() -> tuple[int, int]:
            executed = await _execute_pending(source, state, config)
            revised = await _execute_revisions(state, config)
            return executed, revised

        executed, revised = asyncio.run(run_async_phases())
        log.info(
            "tick complete: ingested=%d polled=%d recovered=%d executed=%d revised=%d",
            ingested, polled, recovered, executed, revised,
        )
    finally:
        state.close()


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autobot")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    parser.add_argument("--loop", action="store_true", help="Run forever, sleeping between ticks.")
    parser.add_argument("--interval", type=int, default=600, help="Seconds between ticks in --loop mode.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not (args.once or args.loop):
        parser.error("specify --once or --loop")

    config = load()
    if args.once:
        _tick(config)
        return 0
    while True:
        try:
            _tick(config)
        except Exception:
            log.exception("tick failed")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(cli())
