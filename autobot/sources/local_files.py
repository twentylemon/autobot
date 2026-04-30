import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from autobot.sources.base import Task, TaskSource

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


class MissingRepoError(ValueError):
    pass


def _slug(stem: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return cleaned or "task"


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    parsed = yaml.safe_load(m.group(1)) or {}
    if not isinstance(parsed, dict):
        return {}, raw
    return parsed, m.group(2)


def _first_nonempty_line(body: str, cap: int = 70) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line[:cap]
    return ""


def _parse_file(path: Path, default_repo: str | None) -> Task:
    raw = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(raw)
    repo = fm.get("repo") or default_repo
    if not repo:
        raise MissingRepoError(f"{path}: no `repo:` frontmatter and AUTOBOT_DEFAULT_REPO is unset")
    created_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    slug = _slug(path.stem)
    task_id = f"local:{slug}-{_short_hash(f'{path}:{created_at.isoformat()}')}"
    title = _first_nonempty_line(body) or slug
    return Task(
        id=task_id,
        source="local_file",
        source_ref=str(path),
        repo=repo,
        title=title,
        body=body.strip(),
        created_at=created_at,
    )


class LocalFileSource(TaskSource):
    name = "local_file"

    def __init__(self, inbox_dir: Path, processing_dir: Path, default_repo: str | None) -> None:
        self.inbox_dir = inbox_dir
        self.processing_dir = processing_dir
        self.default_repo = default_repo

    def discover(self) -> Iterable[Task]:
        return list(self._iter_inbox())

    def _iter_inbox(self) -> Iterator[Task]:
        for path in sorted(self.inbox_dir.glob("*.md")):
            yield _parse_file(path, self.default_repo)

    def mark_picked_up(self, task: Task) -> None:
        src = Path(task.source_ref)
        if not src.exists():
            return  # already moved on a previous tick
        dst = self.processing_dir / src.name
        if dst.exists():
            dst = self.processing_dir / f"{src.stem}-{_short_hash(task.id)}{src.suffix}"
        src.rename(dst)

    def mark_completed(self, task: Task, pr_url: str) -> None:
        # v0: no-op. v0.2 will move processing/<file> -> done/<file>.
        return
