import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    github_token: str
    inbox_dir: Path
    processing_dir: Path
    work_dir: Path
    results_dir: Path
    logs_dir: Path
    state_db: Path
    default_repo: str | None  # owner/name, optional
    max_diff_loc: int  # sprawling-diff guard threshold (insertions + deletions)
    revision_cap: int  # hard cap on total revisions per PR
    revision_cooldown_minutes: int  # min minutes between revisions on the same PR


def _autobot_home() -> Path:
    return Path(os.environ.get("AUTOBOT_HOME", "~/.autobot")).expanduser()


def load() -> Config:
    home = _autobot_home()
    inbox_dir = Path(os.environ.get("AUTOBOT_INBOX_DIR", home / "inbox")).expanduser()
    work_dir = Path(os.environ.get("AUTOBOT_WORK_DIR", home / "work")).expanduser()
    state_db = Path(os.environ.get("AUTOBOT_STATE_DB", home / "state.db")).expanduser()

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise RuntimeError("GITHUB_TOKEN is required")

    config = Config(
        github_token=github_token,
        inbox_dir=inbox_dir,
        processing_dir=home / "processing",
        work_dir=work_dir,
        results_dir=home / "results",
        logs_dir=home / "logs",
        state_db=state_db,
        default_repo=os.environ.get("AUTOBOT_DEFAULT_REPO") or None,
        max_diff_loc=int(os.environ.get("AUTOBOT_MAX_DIFF_LOC", "2000")),
        revision_cap=int(os.environ.get("AUTOBOT_REVISION_CAP", "10")),
        revision_cooldown_minutes=int(os.environ.get("AUTOBOT_REVISION_COOLDOWN_MINUTES", "20")),
    )
    for d in (config.inbox_dir, config.processing_dir, config.work_dir, config.results_dir, config.logs_dir, config.state_db.parent):
        d.mkdir(parents=True, exist_ok=True)
    return config
