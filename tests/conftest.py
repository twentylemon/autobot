"""Shared fixtures and fakes for autobot tests."""

import json
import re
from pathlib import Path
from typing import AsyncIterator

import pytest

from autobot.config import Config
from autobot.state import State


@pytest.fixture
def config(tmp_path: Path) -> Config:
    home = tmp_path / "home"
    return Config(
        github_token="y",
        inbox_dir=home / "inbox",
        processing_dir=home / "processing",
        work_dir=home / "work",
        results_dir=home / "results",
        logs_dir=home / "logs",
        state_db=home / "state.db",
        default_repo=None,
        max_diff_loc=2000,
    )


@pytest.fixture
def state(tmp_path: Path) -> State:
    s = State(tmp_path / "state.db")
    yield s
    s.close()


_RESULT_FILE_LINE = re.compile(r"Result file:\s+(\S+)")


def fake_query(payload: dict | None, *, session_id: str = "sess-1"):
    """Build a query_fn that extracts the `Result file:` path from the prompt
    and writes `payload` there as a side effect — same shape as a real Claude run.

    `payload=None` simulates Claude failing to write the result file at all.
    """

    async def gen(prompt: str, options) -> AsyncIterator:
        if payload is not None:
            m = _RESULT_FILE_LINE.search(prompt)
            assert m is not None, "fake_query: prompt has no `Result file:` line"
            p = Path(m.group(1))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload), encoding="utf-8")
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


def boom_query(message: str = "boom"):
    """Build a query_fn that raises on first iteration, simulating an SDK crash."""

    def factory(prompt, options):
        async def gen():
            raise RuntimeError(message)
            yield  # pragma: no cover - makes this an async generator

        return gen()

    return factory


def ok_gh(*, is_draft: bool = True, state: str = "OPEN"):
    """Build a gh_fn that satisfies revise_task's isDraft+state pre-check.

    Tests that don't care about the pre-check just pass `gh_fn=ok_gh()`.
    """

    def gh(args: list[str]):
        if args[0] == "pr" and args[1] == "view":
            return {"isDraft": is_draft, "state": state}
        raise AssertionError(f"unexpected gh call in revision test: {args!r}")

    return gh
