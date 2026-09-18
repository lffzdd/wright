from __future__ import annotations

import os
from dataclasses import replace

from ..session import Session
from ..tools.base import tool_runtime_for_session
from ..tools.file_tools import (
    FILE_UNCHANGED,
    FileView,
    _remembered_file_view,
    edit_file,
    edit_file_tool,
    glob_files,
    grep_files,
    list_directory,
    read_file,
    write_file,
)


def _runtime(tmp_path):
    return tool_runtime_for_session(
        Session.create("file tools", tmp_path), workspace_dir=tmp_path
    )


def test_list_directory_is_sorted_bounded_and_hides_dotfiles(tmp_path):
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / ".secret").write_text("x", encoding="utf-8")
    (tmp_path / "dir").mkdir()

    result = list_directory(runtime=_runtime(tmp_path), max_entries=2)

    assert result.ok
    assert [entry["name"] for entry in result.data["entries"]] == ["a.txt", "b.txt"]
    assert result.data["truncated"] is True


def test_glob_and_grep_have_distinct_structured_results(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle = 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("needle\n", encoding="utf-8")
    runtime = _runtime(tmp_path)

    paths = glob_files("**/*.py", runtime=runtime)
    matches = grep_files("needle", glob="*.py", runtime=runtime)

    assert paths.ok and paths.data["matches"] == [{"path": "src/a.py", "type": "file"}]
    assert matches.ok
    assert matches.data["matches"] == [
        {"path": "src/a.py", "line": 1, "column": 1, "text": "needle = 1"}
    ]


def test_read_file_supports_line_ranges_and_continuation(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    ranged = read_file("a.txt", start_line=2, end_line=3, runtime=_runtime(tmp_path))
    clipped = read_file("a.txt", start_line=2, max_chars=2, runtime=_runtime(tmp_path))

    assert ranged.ok and ranged.data["content"] == "2|two\n3|three\n"
    assert ranged.data["start_line"] == 2 and ranged.data["end_line"] == 3
    assert clipped.data["content"] == "2|tw"
    assert clipped.data["truncated"] is True
    assert clipped.data["next_start_line"] == 2
    assert clipped.data["next_start_column"] == 3
    continued = read_file(
        "a.txt", start_line=2, start_column=3, runtime=_runtime(tmp_path)
    )
    assert continued.data["content"] == "2|o\n3|three\n"


def test_relative_file_paths_follow_session_cwd_but_stay_in_workspace(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "a.txt").write_text("inside", encoding="utf-8")
    runtime = _runtime(tmp_path)
    runtime.capabilities.set_cwd(nested)

    result = read_file("a.txt", runtime=runtime)
    escaped = read_file("../../outside.txt", runtime=runtime)

    assert result.ok and result.data["content"] == "1|inside"
    assert not escaped.ok and "Unsafe path" in escaped.err


def test_edit_file_replaces_unique_text_and_rejects_escape(tmp_path):
    (tmp_path / "a.txt").write_text("old line\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok

    result = edit_file("a.txt", "old line\n", "new line\n", runtime=runtime)
    escaped = edit_file("../../outside.txt", "a", "b", runtime=runtime)
    missing = edit_file("a.txt", "missing", "x", runtime=runtime)

    assert result.ok and result.data["replacements"] == 1
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new line\n"
    assert not escaped.ok and "Unsafe path" in escaped.err
    assert not missing.ok and "not found" in missing.err
    assert missing.data["reason"] == "not_found"
    assert missing.data["line_count"] == 1
    assert missing.data["occurrences"] == []


def test_edit_file_rejects_ambiguous_matches_with_nearby_context(tmp_path):
    (tmp_path / "a.txt").write_text("alpha foo\nfoo\nomega foo\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok
    result = edit_file("a.txt", "foo", "bar", runtime=runtime)
    assert not result.ok
    assert "ambiguous" in result.err
    assert result.data["reason"] == "ambiguous"
    assert result.data["count"] == 3
    assert [item["line"] for item in result.data["occurrences"]] == [1, 2, 3]
    assert "1|alpha foo" in result.data["occurrences"][0]["preview"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "alpha foo\nfoo\nomega foo\n"


def test_edit_file_replace_all_replaces_every_occurrence(tmp_path):
    (tmp_path / "a.txt").write_text("foo\nfoo\nfoo\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok
    result = edit_file(
        "a.txt", "foo", "bar", replace_all=True, runtime=runtime
    )
    assert result.ok and result.data["replacements"] == 3
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "bar\nbar\nbar\n"


def test_edit_file_not_found_reports_whitespace_mismatch(tmp_path):
    (tmp_path / "a.txt").write_text("hello world\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok
    result = edit_file(
        "a.txt", "  hello world  ", "hi", runtime=runtime
    )
    assert not result.ok
    assert result.data["reason"] == "not_found"
    assert result.data["whitespace_differs"] is True
    assert result.data["occurrences"][0]["line"] == 1
    assert "1|hello world" in result.data["occurrences"][0]["preview"]


def test_edit_file_caps_ambiguous_previews(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n" * 6, encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok
    result = edit_file("a.txt", "foo", "bar", runtime=runtime)
    assert not result.ok
    assert result.data["count"] == 6
    assert len(result.data["occurrences"]) == 5
    assert result.data["truncated"] is True


def test_edit_file_requires_a_fresh_read(tmp_path):
    (tmp_path / "a.txt").write_text("old line\n", encoding="utf-8")
    runtime = _runtime(tmp_path)

    unread = edit_file("a.txt", "old line\n", "new line\n", runtime=runtime)
    assert not unread.ok
    assert unread.data["reason"] == "not_read"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "old line\n"

    assert read_file("a.txt", runtime=runtime).ok
    (tmp_path / "a.txt").write_text("tampered\n", encoding="utf-8")
    stale = edit_file("a.txt", "tampered\n", "new line\n", runtime=runtime)
    assert not stale.ok
    assert stale.data["reason"] == "stale"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "tampered\n"

    assert read_file("a.txt", runtime=runtime).ok
    edited = edit_file("a.txt", "tampered\n", "new line\n", runtime=runtime)
    assert edited.ok
    second = edit_file("a.txt", "new line\n", "again\n", runtime=runtime)
    assert second.ok
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "again\n"


def test_write_file_grounds_a_follow_up_edit(tmp_path):
    runtime = _runtime(tmp_path)
    assert write_file("a.txt", "old line\n", runtime=runtime).ok
    result = edit_file("a.txt", "old line\n", "new line\n", runtime=runtime)
    assert result.ok
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new line\n"


def test_write_file_creates_without_a_prior_read(tmp_path):
    runtime = _runtime(tmp_path)
    result = write_file("a.txt", "hello\n", runtime=runtime)
    assert result.ok
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello\n"


def test_write_file_requires_a_complete_read_to_overwrite(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("old line\n", encoding="utf-8")
    runtime = _runtime(tmp_path)

    unread = write_file("a.txt", "new line\n", runtime=runtime)
    assert not unread.ok
    assert unread.data["reason"] == "not_read"
    assert path.read_text(encoding="utf-8") == "old line\n"

    assert read_file("a.txt", start_line=1, end_line=1, runtime=runtime).ok
    incomplete = write_file("a.txt", "new line\n", runtime=runtime)
    assert not incomplete.ok
    assert incomplete.data["reason"] == "incomplete"
    assert path.read_text(encoding="utf-8") == "old line\n"

    assert read_file("a.txt", runtime=runtime).ok
    written = write_file("a.txt", "new line\n", runtime=runtime)
    assert written.ok
    assert path.read_text(encoding="utf-8") == "new line\n"

    again = write_file("a.txt", "again\n", runtime=runtime)
    assert again.ok
    assert path.read_text(encoding="utf-8") == "again\n"


def test_write_file_rejects_stale_overwrite(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("old line\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok
    path.write_text("tampered\n", encoding="utf-8")

    stale = write_file("a.txt", "new line\n", runtime=runtime)

    assert not stale.ok
    assert stale.data["reason"] == "stale"
    assert path.read_text(encoding="utf-8") == "tampered\n"


def test_file_view_is_shared_across_replaced_runtimes(tmp_path):
    (tmp_path / "a.txt").write_text("old line\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file(
        "a.txt", runtime=replace(runtime, tool_name="read_file")
    ).ok
    result = edit_file(
        "a.txt",
        "old line\n",
        "new line\n",
        runtime=replace(runtime, tool_name="edit_file"),
    )
    assert result.ok


def test_read_file_stubs_identical_unchanged_rereads(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    runtime = _runtime(tmp_path)

    first = read_file("a.txt", runtime=runtime)

    def fail_open(*_args, **_kwargs):
        raise AssertionError("unchanged read_file should not reopen the file")

    monkeypatch.setattr("builtins.open", fail_open)
    second = read_file("a.txt", runtime=runtime)
    monkeypatch.undo()
    ranged = read_file("a.txt", start_line=2, end_line=2, runtime=runtime)

    assert first.ok and first.data["content"] == "1|one\n2|two\n"
    assert "unchanged" not in first.data
    assert second.ok
    assert second.data["unchanged"] is True
    assert second.data["content"] == FILE_UNCHANGED
    assert second.data["end_line"] == 2
    assert ranged.ok and ranged.data["content"] == "2|two\n"
    assert "unchanged" not in ranged.data


def test_edit_file_accepts_complete_view_when_mtime_lies(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("old line\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))

    result = edit_file("a.txt", "old line\n", "new line\n", runtime=runtime)

    assert result.ok
    assert path.read_text(encoding="utf-8") == "new line\n"


def test_edit_file_rejects_partial_view_when_mtime_lies(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", start_line=1, end_line=1, runtime=runtime).ok
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))

    stale = edit_file("a.txt", "one\n", "uno\n", runtime=runtime)

    assert not stale.ok
    assert stale.data["reason"] == "stale"
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


def test_file_view_stores_raw_content_not_numbered_output(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    result = read_file("a.txt", runtime=runtime)
    view = _remembered_file_view(runtime, tmp_path / "a.txt")

    assert result.data["content"] == "1|hello\n"
    assert isinstance(view, FileView)
    assert view.content == "hello\n"
    assert view.origin == "read"
    assert view.is_complete is True


def test_write_and_edit_preserve_existing_file_encoding(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes("old line\n".encode("utf-16"))
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok

    written = write_file("a.txt", "new line\n", runtime=runtime)
    assert written.ok
    assert path.read_bytes()[:2] == b"\xff\xfe"
    assert path.read_text(encoding="utf-16") == "new line\n"

    edited = edit_file("a.txt", "new line\n", "again\n", runtime=runtime)
    assert edited.ok
    assert path.read_bytes()[:2] == b"\xff\xfe"
    assert path.read_text(encoding="utf-16") == "again\n"


def test_write_file_creates_utf8_without_bom(tmp_path):
    runtime = _runtime(tmp_path)
    assert write_file("a.txt", "hello\n", runtime=runtime).ok
    assert (tmp_path / "a.txt").read_bytes() == b"hello\n"


def test_write_and_edit_preserve_utf8_bom(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("old line\n", encoding="utf-8-sig")
    runtime = _runtime(tmp_path)
    assert read_file("a.txt", runtime=runtime).ok

    assert write_file("a.txt", "new line\n", runtime=runtime).ok
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf"
    assert path.read_text(encoding="utf-8-sig") == "new line\n"

    assert edit_file("a.txt", "new line\n", "again\n", runtime=runtime).ok
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf"
    assert path.read_text(encoding="utf-8-sig") == "again\n"


def test_edit_file_schema_exposes_replace_all():
    assert "replace_all" in edit_file_tool.parameters["properties"]
    assert edit_file_tool.parameters["properties"]["replace_all"]["default"] is False
