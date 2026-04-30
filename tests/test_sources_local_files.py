from pathlib import Path

import pytest

from autobot.sources.local_files import LocalFileSource, MissingRepoError


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    inbox = tmp_path / "inbox"
    processing = tmp_path / "processing"
    inbox.mkdir()
    processing.mkdir()
    return inbox, processing


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_discover_uses_default_repo_when_no_frontmatter(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(inbox / "fix-typos.md", "Fix typos in README.\n")
    src = LocalFileSource(inbox, processing, default_repo="twentylemon/duckbot")
    tasks = list(src.discover())
    assert len(tasks) == 1
    assert tasks[0].repo == "twentylemon/duckbot"
    assert tasks[0].title == "Fix typos in README."
    assert tasks[0].body == "Fix typos in README."
    assert tasks[0].source == "local_file"
    assert tasks[0].id.startswith("local:fix-typos-")


def test_discover_frontmatter_overrides_default_repo(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(inbox / "task.md", "---\nrepo: other/proj\n---\n\nDo a thing.\n")
    src = LocalFileSource(inbox, processing, default_repo="twentylemon/duckbot")
    tasks = list(src.discover())
    assert tasks[0].repo == "other/proj"
    assert tasks[0].body == "Do a thing."


def test_discover_raises_when_no_repo_available(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(inbox / "no-repo.md", "Do a thing.\n")
    src = LocalFileSource(inbox, processing, default_repo=None)
    with pytest.raises(MissingRepoError):
        list(src.discover())


def test_slug_handles_messy_filenames(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(inbox / "Add !! Dark__Mode.md", "Add dark mode.\n")
    src = LocalFileSource(inbox, processing, default_repo="o/r")
    tasks = list(src.discover())
    assert tasks[0].id.startswith("local:add-dark-mode-")


def test_title_caps_at_70_chars(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    long_line = "A" * 200
    _write(inbox / "long.md", long_line + "\n")
    src = LocalFileSource(inbox, processing, default_repo="o/r")
    tasks = list(src.discover())
    assert tasks[0].title == "A" * 70


def test_mark_picked_up_moves_file(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(inbox / "task.md", "Do a thing.\n")
    src = LocalFileSource(inbox, processing, default_repo="o/r")
    [task] = list(src.discover())

    src.mark_picked_up(task)

    assert not (inbox / "task.md").exists()
    assert (processing / "task.md").exists()


def test_mark_picked_up_handles_destination_collision(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(processing / "task.md", "an old result\n")
    _write(inbox / "task.md", "Do a thing.\n")
    src = LocalFileSource(inbox, processing, default_repo="o/r")
    [task] = list(src.discover())

    src.mark_picked_up(task)

    assert not (inbox / "task.md").exists()
    assert (processing / "task.md").read_text() == "an old result\n"
    moved = list(processing.glob("task-*.md"))
    assert len(moved) == 1


def test_mark_picked_up_is_idempotent_when_file_already_moved(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(inbox / "task.md", "Do a thing.\n")
    src = LocalFileSource(inbox, processing, default_repo="o/r")
    [task] = list(src.discover())
    src.mark_picked_up(task)
    src.mark_picked_up(task)  # should not raise


def test_mark_completed_is_noop_in_v0(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(inbox / "task.md", "Do a thing.\n")
    src = LocalFileSource(inbox, processing, default_repo="o/r")
    [task] = list(src.discover())
    src.mark_completed(task, "https://github.com/x/y/pull/1")  # no error, no side effects


def test_discover_yields_tasks_in_filename_order(dirs: tuple[Path, Path]) -> None:
    inbox, processing = dirs
    _write(inbox / "b.md", "Second.\n")
    _write(inbox / "a.md", "First.\n")
    src = LocalFileSource(inbox, processing, default_repo="o/r")
    tasks = list(src.discover())
    assert [t.title for t in tasks] == ["First.", "Second."]
