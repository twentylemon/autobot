"""SDK hooks. Currently: a PreToolUse hook that blocks destructive Bash patterns.

Lives in its own module so it can be unit-tested without spinning up the SDK.
The hook is intentionally narrow — Claude is trusted; this catches the
specific footguns that would be hard to recover from (force-pushed branches,
wiped canonical clones, corrupted .git directories).
"""

import re
from pathlib import Path
from typing import Any, Awaitable, Callable

# Shell metacharacters that separate subcommands. We tokenize each subcommand
# independently so `cd /x && git push --force` is detected.
_SUBCOMMAND_SEP = re.compile(r"&&|\|\||;|\|")

# A path looks "underneath" canonical_dir if it equals canonical_dir or starts
# with `<canonical_dir>/`. Done as a string check (not Path.resolve) because
# we're inspecting a command we haven't run — the path may not exist yet.
def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _has_short_flag(token: str, letter: str) -> bool:
    """True if `token` is a short-flag bundle like '-rf' that includes `letter`."""
    return token.startswith("-") and not token.startswith("--") and letter in token[1:]


def _is_unconditional_push_force(tokens: list[str]) -> bool:
    """True for `git push ... --force` / `-f`. False for `--force-with-lease` / `--force-if-includes`."""
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "push":
        return False
    for tok in tokens[2:]:
        # --force-with-lease and --force-if-includes are the safe variants.
        if tok.startswith("--force-with-lease") or tok.startswith("--force-if-includes"):
            continue
        if tok == "--force" or tok.startswith("--force="):
            return True
        if tok == "-f":
            return True
        if _has_short_flag(tok, "f"):
            return True
    return False


def _is_reset_hard(tokens: list[str]) -> bool:
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "reset":
        return False
    return "--hard" in tokens


def _is_rm_rf_under(tokens: list[str], canonical_dir: str) -> bool:
    """`rm` with both -r and -f flags targeting a path under canonical_dir."""
    if not tokens or tokens[0] != "rm":
        return False
    has_recursive = any(
        tok in {"-r", "-R", "--recursive"} or _has_short_flag(tok, "r") or _has_short_flag(tok, "R")
        for tok in tokens[1:]
    )
    has_force = any(
        tok == "-f" or tok == "--force" or _has_short_flag(tok, "f")
        for tok in tokens[1:]
    )
    if not (has_recursive and has_force):
        return False
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        if _is_under(tok, canonical_dir):
            return True
    return False


# Matches direct writes to `.git/<something>`:
#   `> .git/HEAD`, `>> .git/config`,
#   `tee .git/HEAD`, `tee -a .git/HEAD`,
#   `cp X .git/HEAD`, `mv X .git/HEAD`.
# `.gitignore`, `.gitattributes`, `.gitlab/...` do NOT match — the trailing
# slash after `.git` is required.
_GIT_DIR_WRITE_RE = re.compile(r"(?:>>?|\btee\b|\bcp\b|\bmv\b|\binstall\b)[^;|&]*?(?:^|/|\s)\.git/")


def _writes_to_git_dir(command: str) -> bool:
    return bool(_GIT_DIR_WRITE_RE.search(command))


def _classify(command: str, canonical_dir: str) -> str | None:
    """Return a human-readable reason if the command is destructive, else None."""
    for sub in _SUBCOMMAND_SEP.split(command):
        tokens = sub.split()
        if _is_unconditional_push_force(tokens):
            return "blocked: unconditional `git push --force` / `-f`. Use `--force-with-lease` if you genuinely need to rewrite history."
        if _is_reset_hard(tokens):
            return "blocked: `git reset --hard` discards uncommitted work. Use `git checkout -- <paths>` or `git reset` (mixed) instead."
        if _is_rm_rf_under(tokens, canonical_dir):
            return f"blocked: `rm -rf` of a path under the canonical clone {canonical_dir!r}. The canonical clone is shared across tasks; never delete it."
    if _writes_to_git_dir(command):
        return "blocked: direct write to a `.git/` directory. Use `git` commands instead of editing internal git state."
    return None


def make_block_destructive_bash(canonical_dir: Path) -> Callable[[dict, str | None, dict], Awaitable[dict]]:
    """Build a PreToolUse hook that denies the destructive patterns above.

    Wire it into `ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[fn])]})`.
    """
    canonical = str(canonical_dir).rstrip("/")

    async def hook(input_data: dict, _tool_use_id: str | None, _context: dict) -> dict[str, Any]:
        if input_data.get("tool_name") != "Bash":
            return {}
        command = (input_data.get("tool_input") or {}).get("command", "")
        reason = _classify(command, canonical)
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return hook
