import json
from pathlib import Path

from autobot import results


def _write(path: Path, data: dict | str) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_submitted_parses(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", {"status": "submitted", "pr_url": "https://github.com/x/y/pull/3", "pr_number": 3, "branch": "twentylemon/autobot/foo", "head_sha": "abc123"})
    r = results.read(p)
    assert isinstance(r, results.Submitted)
    assert r.pr_number == 3
    assert r.pr_url.endswith("/pull/3")


def test_submitted_coerces_numeric_string_pr_number(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", {"status": "submitted", "pr_url": "u", "pr_number": "9", "branch": "b", "head_sha": "s"})
    r = results.read(p)
    assert isinstance(r, results.Submitted)
    assert r.pr_number == 9


def test_no_changes_parses(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", {"status": "no_changes", "reason": "nothing to do"})
    r = results.read(p)
    assert isinstance(r, results.NoChanges)
    assert r.reason == "nothing to do"


def test_no_pr_parses_with_branch(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", {"status": "no_pr", "reason": "push failed: 403", "branch": "twentylemon/autobot/x"})
    r = results.read(p)
    assert isinstance(r, results.NoPr)
    assert r.branch == "twentylemon/autobot/x"


def test_no_pr_parses_without_branch(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", {"status": "no_pr", "reason": "couldn't push"})
    r = results.read(p)
    assert isinstance(r, results.NoPr)
    assert r.branch is None


def test_missing_file_is_unknown(tmp_path: Path) -> None:
    r = results.read(tmp_path / "nope.json")
    assert isinstance(r, results.Unknown)
    assert "missing" in r.reason


def test_malformed_json_is_unknown(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", "{not valid json")
    r = results.read(p)
    assert isinstance(r, results.Unknown)


def test_non_object_root_is_unknown(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", "[]")
    r = results.read(p)
    assert isinstance(r, results.Unknown)


def test_submitted_missing_required_field_is_unknown(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", {"status": "submitted", "pr_url": "u", "branch": "b", "head_sha": "s"})  # no pr_number
    r = results.read(p)
    assert isinstance(r, results.Unknown)


def test_submitted_non_int_pr_number_is_unknown(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", {"status": "submitted", "pr_url": "u", "pr_number": "not-a-number", "branch": "b", "head_sha": "s"})
    r = results.read(p)
    assert isinstance(r, results.Unknown)


def test_unknown_status_is_unknown(tmp_path: Path) -> None:
    p = _write(tmp_path / "r.json", {"status": "wat", "details": "?"})
    r = results.read(p)
    assert isinstance(r, results.Unknown)
    assert "wat" in r.reason
