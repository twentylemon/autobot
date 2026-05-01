import asyncio
from pathlib import Path

import pytest

from autobot.sdk_hooks import _classify, make_block_destructive_bash

CANONICAL = "/work/twentylemon__duckbot/main"


@pytest.mark.parametrize(
    "command",
    [
        "git push --force",
        "git push -f",
        "git push origin main --force",
        "git push --force origin main",
        "cd /tmp && git push --force",
        "git push -fu origin main",   # short-flag bundle including 'f'
        "git reset --hard",
        "git reset --hard HEAD~1",
        f"rm -rf {CANONICAL}",
        f"rm -rf {CANONICAL}/foo",
        f"rm -fr {CANONICAL}",
        "echo x > .git/HEAD",
        "echo x >> .git/config",
        "tee .git/HEAD",
        "cp foo .git/HEAD",
        f"echo x > {CANONICAL}/.git/HEAD",
    ],
)
def test_classifier_blocks_destructive_command(command: str) -> None:
    assert _classify(command, CANONICAL) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git push --force-with-lease",
        "git push --force-with-lease=ref:expected",
        "git push --force-if-includes",
        "git push origin main",
        "git push -u origin foo",
        "git status",
        "git reset HEAD~1",
        "git reset --soft HEAD~1",
        "rm /tmp/foo",
        "rm -rf /tmp/foo",          # not under canonical
        "rm -rf /work/other-repo",  # not under canonical
        "cat .gitignore",
        "echo > git_log.txt",
        "echo > .gitignore",
        "ls .gitlab/foo",
    ],
)
def test_classifier_allows_safe_command(command: str) -> None:
    assert _classify(command, CANONICAL) is None


def test_hook_returns_deny_for_destructive_bash() -> None:
    hook = make_block_destructive_bash(Path(CANONICAL))
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push --force"}}
    out = asyncio.run(hook(payload, "tu-1", {}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "force" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_returns_empty_for_safe_bash() -> None:
    hook = make_block_destructive_bash(Path(CANONICAL))
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    out = asyncio.run(hook(payload, "tu-1", {}))
    assert out == {}


def test_hook_returns_empty_for_non_bash_tool() -> None:
    hook = make_block_destructive_bash(Path(CANONICAL))
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "/x"}}
    out = asyncio.run(hook(payload, "tu-1", {}))
    assert out == {}
