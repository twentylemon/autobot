import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Status transitions enforced by update_status:
#   pending -> submitted | failed_no_changes | failed_no_pr | failed_unknown
# Terminal states never transition further.
STATUSES = {"pending", "submitted", "failed_no_changes", "failed_no_pr", "failed_unknown"}
TERMINAL_STATUSES = {"submitted", "failed_no_changes", "failed_no_pr", "failed_unknown"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  source_ref    TEXT,
  repo          TEXT NOT NULL,
  title         TEXT NOT NULL,
  body          TEXT NOT NULL,
  status        TEXT NOT NULL,
  branch        TEXT,
  pr_url        TEXT,
  pr_number     INTEGER,
  session_id    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


@dataclass(frozen=True)
class TaskRow:
    id: str
    source: str
    source_ref: str | None
    repo: str
    title: str
    body: str
    status: str
    branch: str | None
    pr_url: str | None
    pr_number: int | None
    session_id: str | None
    created_at: datetime
    updated_at: datetime


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_task(row: sqlite3.Row) -> TaskRow:
    return TaskRow(
        id=row["id"],
        source=row["source"],
        source_ref=row["source_ref"],
        repo=row["repo"],
        title=row["title"],
        body=row["body"],
        status=row["status"],
        branch=row["branch"],
        pr_url=row["pr_url"],
        pr_number=row["pr_number"],
        session_id=row["session_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class State:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def insert_task(self, task_id: str, source: str, source_ref: str | None, repo: str, title: str, body: str, created_at: datetime) -> bool:
        """Returns True if inserted, False if a row with this id already existed."""
        now = _now()
        try:
            with self._tx() as c:
                c.execute(
                    "INSERT INTO tasks (id, source, source_ref, repo, title, body, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (task_id, source, source_ref, repo, title, body, created_at.isoformat(), now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_pending(self) -> list[TaskRow]:
        rows = self._conn.execute("SELECT * FROM tasks WHERE status = 'pending' ORDER BY created_at ASC").fetchall()
        return [_row_to_task(r) for r in rows]

    def get_by_id(self, task_id: str) -> TaskRow | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def update_status(
        self,
        task_id: str,
        status: str,
        *,
        branch: str | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
        session_id: str | None = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        with self._tx() as c:
            current = c.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(f"no task with id {task_id}")
            if current["status"] in TERMINAL_STATUSES and current["status"] != status:
                raise ValueError(f"task {task_id} is in terminal state {current['status']!r}; cannot transition to {status!r}")
            c.execute(
                "UPDATE tasks SET status = ?, branch = COALESCE(?, branch), pr_url = COALESCE(?, pr_url), "
                "pr_number = COALESCE(?, pr_number), session_id = COALESCE(?, session_id), updated_at = ? WHERE id = ?",
                (status, branch, pr_url, pr_number, session_id, _now(), task_id),
            )
