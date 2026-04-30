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


async def _execute_pending(source: LocalFileSource, state: State, config: Config) -> int:
    pending = state.get_pending()
    for row in pending:
        log.info("executing task %s", row.id)
        try:
            await worker.execute_task(row, source, state, config)
        except Exception:
            log.exception("worker crashed on task %s", row.id)
    return len(pending)


def _tick(config: Config) -> None:
    source = _build_source(config)
    state = State(config.state_db)
    try:
        try:
            inserted = _ingest(source, state)
        except MissingRepoError as e:
            log.error("inbox file rejected: %s", e)
            inserted = 0
        executed = asyncio.run(_execute_pending(source, state, config))
        log.info("tick complete: ingested=%d executed=%d", inserted, executed)
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
