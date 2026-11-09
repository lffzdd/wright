from pathlib import Path

import pytest

from wright.application.workspace.folders import FolderBrowseError, browse_folders


def test_lists_only_visible_subdirectories_sorted(tmp_path: Path):
    (tmp_path / "beta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "Alpha" / ".git").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x")

    listing = browse_folders(str(tmp_path))

    assert listing["path"] == str(tmp_path.resolve())
    assert listing["parent"] == str(tmp_path.resolve().parent)
    assert [entry["name"] for entry in listing["entries"]] == ["Alpha", "beta"]
    assert [entry["git"] for entry in listing["entries"]] == [True, False]
    assert listing["truncated"] is False


def test_defaults_to_home_directory():
    listing = browse_folders(None)
    assert listing["path"] == str(Path.home().resolve())
    assert listing["home"] == str(Path.home())


def test_rejects_missing_and_relative_paths(tmp_path: Path):
    with pytest.raises(FolderBrowseError):
        browse_folders(str(tmp_path / "missing"))
    with pytest.raises(FolderBrowseError):
        browse_folders("relative/path")


def test_caps_entry_count(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("wright.application.workspace.folders.MAX_ENTRIES", 3)
    for index in range(5):
        (tmp_path / f"d{index}").mkdir()
    listing = browse_folders(str(tmp_path))
    assert len(listing["entries"]) == 3
    assert listing["truncated"] is True
