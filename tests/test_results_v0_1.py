import json
from pathlib import Path

from autobot import results


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_parse_failed_too_large(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write(p, {"status": "failed_too_large", "insertions": 1500, "deletions": 700})
    r = results.read(p)
    assert isinstance(r, results.FailedTooLarge)
    assert r.insertions == 1500
    assert r.deletions == 700


def test_parse_failed_too_large_coerces_strings(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write(p, {"status": "failed_too_large", "insertions": "1200", "deletions": "100"})
    r = results.read(p)
    assert isinstance(r, results.FailedTooLarge)
    assert r.insertions == 1200


def test_parse_failed_too_large_missing_field_is_unknown(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write(p, {"status": "failed_too_large", "insertions": 100})
    r = results.read(p)
    assert isinstance(r, results.Unknown)
    assert "failed_too_large" in r.reason


def test_parse_needs_revision(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write(p, {"status": "needs_revision", "last_comment_id": 12345})
    r = results.read(p)
    assert isinstance(r, results.NeedsRevision)
    assert r.last_comment_id == 12345


def test_parse_needs_revision_missing_field_is_unknown(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write(p, {"status": "needs_revision"})
    r = results.read(p)
    assert isinstance(r, results.Unknown)


def test_parse_no_action(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write(p, {"status": "no_action"})
    r = results.read(p)
    assert isinstance(r, results.NoAction)


def test_parse_revised(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write(p, {"status": "revised", "last_comment_id": 99, "head_sha": "deadbeef"})
    r = results.read(p)
    assert isinstance(r, results.Revised)
    assert r.last_comment_id == 99
    assert r.head_sha == "deadbeef"


def test_parse_revised_missing_field_is_unknown(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    _write(p, {"status": "revised", "last_comment_id": 1})  # missing head_sha
    r = results.read(p)
    assert isinstance(r, results.Unknown)
