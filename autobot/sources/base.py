from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class Task:
    id: str  # e.g. "local:add-dark-mode-9c1f4a"
    source: str  # e.g. "local_file"
    source_ref: str  # source-specific reference (file path, issue URL, etc.)
    repo: str  # owner/name
    title: str
    body: str
    created_at: datetime


class TaskSource(ABC):
    name: str

    @abstractmethod
    def discover(self) -> Iterable[Task]:
        """Yield tasks not yet recorded in state. Caller is responsible for dedup via state.db."""

    @abstractmethod
    def mark_picked_up(self, task: Task) -> None:
        """Called by the worker before invoking Claude. Must make the task non-discoverable on the next tick."""

    @abstractmethod
    def mark_completed(self, task: Task, pr_url: str) -> None:
        """Called when the PR for this task merges. No-op in v0 (no merge detection)."""
