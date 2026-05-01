import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Status set. Terminal statuses cannot transition further (except to themselves).
STATUSES = {
    "pending",
    "submitted",
    "needs_revision",
    "revising",
    "completed",
    "failed_no_changes",
    "failed_no_pr",
    "failed_too_large",
    "failed_revision",
    "failed_unknown",
}
TERMINAL_STATUSES = {
    "completed",
    "failed_no_changes",
    "failed_no_pr",
    "failed_too_large",
    "failed_revision",
    "failed_unknown",
}

# Explicit transition table. A status is allowed to transition to itself
# (idempotent) on top of these rules. Anything not listed raises ValueError.
# `completed` = "PR is no longer ours" (user closed, merged, or marked ready
# for review). It can be reached from any active state.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"submitted", "failed_no_changes", "failed_no_pr", "failed_too_large", "failed_unknown"},
    "submitted": {"needs_revision", "completed"},
    "needs_revision": {"revising", "completed"},
    "revising": {"submitted", "needs_revision", "completed", "failed_revision", "failed_too_large"},
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,
  source            TEXT NOT NULL,
  source_ref        TEXT,
  repo              TEXT NOT NULL,
  title             TEXT NOT NULL,
  body              TEXT NOT NULL,
  status            TEXT NOT NULL,
  branch            TEXT,
  pr_url            TEXT,
  pr_number         INTEGER,
  session_id        TEXT,
  last_comment_id   INTEGER,
  revision_count    INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""

# Columns added after the v0 schema. ALTER TABLE is idempotent via try/except
# so re-opening an existing v0 DB migrates it forward in place.
_V0_1_COLUMNS = (
    ("last_comment_id", "INTEGER"),
    ("revision_count", "INTEGER NOT NULL DEFAULT 0"),
)


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
    last_comment_id: int | None
    revision_count: int
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
        last_comment_id=row["last_comment_id"],
        revision_count=row["revision_count"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class State:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate_v0_1_columns()

    def _migrate_v0_1_columns(self) -> None:
        for name, decl in _V0_1_COLUMNS:
            try:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise

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
        return self._query_status("pending")

    def get_submitted(self) -> list[TaskRow]:
        """Rows in `submitted` with a known PR — candidates for the poll phase."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'submitted' AND pr_number IS NOT NULL ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def get_needs_revision(self) -> list[TaskRow]:
        return self._query_status("needs_revision")

    def get_stale_revising(self, cutoff: datetime) -> list[TaskRow]:
        """Rows stuck in `revising` whose updated_at is older than `cutoff` — i.e. crashed mid-pass."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'revising' AND updated_at < ? ORDER BY updated_at ASC",
            (cutoff.isoformat(),),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def _query_status(self, status: str) -> list[TaskRow]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC", (status,)
        ).fetchall()
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
            cur = current["status"]
            if cur != status and status not in ALLOWED_TRANSITIONS.get(cur, set()):
                if cur in TERMINAL_STATUSES:
                    raise ValueError(f"task {task_id} is in terminal state {cur!r}; cannot transition to {status!r}")
                raise ValueError(f"task {task_id} cannot transition from {cur!r} to {status!r}")
            c.execute(
                "UPDATE tasks SET status = ?, branch = COALESCE(?, branch), pr_url = COALESCE(?, pr_url), "
                "pr_number = COALESCE(?, pr_number), session_id = COALESCE(?, session_id), updated_at = ? WHERE id = ?",
                (status, branch, pr_url, pr_number, session_id, _now(), task_id),
            )

    def record_poll_result(self, task_id: str, last_comment_id: int) -> None:
        """Phase 2 dispatch: mark a polled `submitted` row as needing a revision."""
        with self._tx() as c:
            self._guard_transition(c, task_id, "submitted", "needs_revision")
            c.execute(
                "UPDATE tasks SET status = 'needs_revision', last_comment_id = ?, updated_at = ? WHERE id = ?",
                (last_comment_id, _now(), task_id),
            )

    def record_revision_start(self, task_id: str) -> None:
        """Phase 6 dispatch: lock a `needs_revision` row by transitioning it to `revising`."""
        with self._tx() as c:
            self._guard_transition(c, task_id, "needs_revision", "revising")
            c.execute(
                "UPDATE tasks SET status = 'revising', updated_at = ? WHERE id = ?",
                (_now(), task_id),
            )

    def record_revision_result(self, task_id: str, last_comment_id: int) -> None:
        """Phase 6 dispatch: a revision succeeded — back to `submitted`, count it, stamp metadata.

        head_sha lives in the result file on disk for audit; state.db only tracks
        what the state machine needs.
        """
        with self._tx() as c:
            self._guard_transition(c, task_id, "revising", "submitted")
            c.execute(
                "UPDATE tasks SET status = 'submitted', revision_count = revision_count + 1, "
                "last_comment_id = ?, updated_at = ? WHERE id = ?",
                (last_comment_id, _now(), task_id),
            )

    def _guard_transition(self, c: sqlite3.Connection, task_id: str, expected: str, target: str) -> None:
        current = c.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if current is None:
            raise KeyError(f"no task with id {task_id}")
        cur = current["status"]
        if cur != expected:
            raise ValueError(f"task {task_id} expected status {expected!r} for transition to {target!r}, found {cur!r}")
