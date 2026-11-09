import hashlib
from zipfile import ZipFile

import pytest

from sysmac_mcp.archive import ArchiveError, ProjectArchive, compare_files, inspect_file


def test_inspection_is_read_only_and_identifies_zip(project_file):
    before = project_file.read_bytes()
    result = inspect_file(project_file)
    assert result["sha256"] == hashlib.sha256(before).hexdigest()
    assert result["container"] == "zip"
    assert result["entry_count"] == 9
    assert project_file.read_bytes() == before


def test_compare_reports_added_removed_and_changed(tmp_path):
    a, b = tmp_path / "a.smc2", tmp_path / "b.smc2"
    with ZipFile(a, "w") as z:
        z.writestr("same", b"a")
        z.writestr("changed", b"b")
        z.writestr("removed", b"c")
    with ZipFile(b, "w") as z:
        z.writestr("same", b"a")
        z.writestr("changed", b"B")
        z.writestr("added", b"d")
    result = compare_files(a, b)
    assert result["changed_entries"] == ["changed"]
    assert result["added_entries"] == ["added"]
    assert result["removed_entries"] == ["removed"]


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "C:/windows/file", "folder/../escape", "..\\escape"]
)
def test_rejects_unsafe_member_paths(tmp_path, name):
    p = tmp_path / "unsafe.smc2"
    with ZipFile(p, "w") as z:
        z.writestr(name, b"data")
    with pytest.raises(ArchiveError, match="path"):
        ProjectArchive(p)


def test_rejects_duplicates_and_expansion_limit(tmp_path):
    p = tmp_path / "large.smc2"
    with ZipFile(p, "w") as z:
        z.writestr("big", b"x" * 10000)
    with pytest.raises(ArchiveError, match="limit"):
        ProjectArchive(p, max_total_bytes=100)


def test_binary_file_is_reported_without_claiming_project_support(tmp_path):
    p = tmp_path / "opaque.smc2"
    p.write_bytes(b"binary")
    assert inspect_file(p)["container"] == "unknown"
    with pytest.raises(ArchiveError):
        ProjectArchive(p)


def test_virtual_additions_are_readable_and_leave_original_immutable(project_file):
    original = project_file.read_bytes()
    archive = ProjectArchive(project_file)
    archive.apply_overrides({"s/new.xml": b"new"})
    assert "s/new.xml" in archive.members and "s/new.xml" in archive.names
    assert archive.read("s/new.xml") == b"new"
    assert archive.hashes()["s/new.xml"] == hashlib.sha256(b"new").hexdigest()
    assert project_file.read_bytes() == original


@pytest.mark.parametrize("name", ["../escape", "s/../escape", "S/BODY.XML", "s\\new.xml"])
def test_virtual_additions_reject_unsafe_and_case_colliding_names(project_file, name):
    archive = ProjectArchive(project_file)
    with pytest.raises(ArchiveError):
        archive.apply_overrides({name: b"new"})


def test_virtual_replacement_sizes_and_entry_counts_are_bounded(tmp_path):
    source = tmp_path / "bounded.smc2"
    with ZipFile(source, "w") as z:
        z.writestr("one", b"a")
        z.writestr("two", b"b")
    archive = ProjectArchive(source, max_total_bytes=5, max_member_bytes=4, max_entries=2)
    with pytest.raises(ArchiveError, match="limit"):
        archive.apply_overrides({"one": b"1234", "two": b"12"})
    assert archive.read("one") == b"a"  # Rejection must not publish partial overrides.
    with pytest.raises(ArchiveError, match="limit"):
        archive.apply_overrides({"three": b"c"})
