import json
from dataclasses import dataclass
from pathlib import Path
from typing import Union


@dataclass(frozen=True)
class Submitted:
    pr_url: str
    pr_number: int
    branch: str
    head_sha: str


@dataclass(frozen=True)
class NoChanges:
    reason: str


@dataclass(frozen=True)
class NoPr:
    reason: str
    branch: str | None


@dataclass(frozen=True)
class FailedTooLarge:
    insertions: int
    deletions: int


@dataclass(frozen=True)
class NeedsRevision:
    last_comment_id: int


@dataclass(frozen=True)
class NoAction:
    pass


@dataclass(frozen=True)
class Revised:
    last_comment_id: int
    head_sha: str


@dataclass(frozen=True)
class Unknown:
    reason: str


Result = Union[Submitted, NoChanges, NoPr, FailedTooLarge, NeedsRevision, NoAction, Revised, Unknown]


def read(result_file: Path) -> Result:
    if not result_file.exists():
        return Unknown(reason=f"result file missing at {result_file}")
    try:
        raw = result_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        return Unknown(reason=f"could not read/parse result file: {e}")
    if not isinstance(data, dict):
        return Unknown(reason=f"result file root is not an object: {type(data).__name__}")
    return _parse(data)


def _parse(data: dict) -> Result:
    status = data.get("status")
    if status == "submitted":
        try:
            return Submitted(
                pr_url=data["pr_url"],
                pr_number=int(data["pr_number"]),
                branch=data["branch"],
                head_sha=data["head_sha"],
            )
        except (KeyError, TypeError, ValueError) as e:
            return Unknown(reason=f"submitted result missing/invalid fields: {e}")
    if status == "no_changes":
        return NoChanges(reason=str(data.get("reason", "")))
    if status == "no_pr":
        return NoPr(reason=str(data.get("reason", "")), branch=data.get("branch"))
    if status == "failed_too_large":
        try:
            return FailedTooLarge(insertions=int(data["insertions"]), deletions=int(data["deletions"]))
        except (KeyError, TypeError, ValueError) as e:
            return Unknown(reason=f"failed_too_large result missing/invalid fields: {e}")
    if status == "needs_revision":
        try:
            return NeedsRevision(last_comment_id=int(data["last_comment_id"]))
        except (KeyError, TypeError, ValueError) as e:
            return Unknown(reason=f"needs_revision result missing/invalid fields: {e}")
    if status == "no_action":
        return NoAction()
    if status == "revised":
        try:
            return Revised(last_comment_id=int(data["last_comment_id"]), head_sha=str(data["head_sha"]))
        except (KeyError, TypeError, ValueError) as e:
            return Unknown(reason=f"revised result missing/invalid fields: {e}")
    return Unknown(reason=f"unknown status in result file: {status!r}")
