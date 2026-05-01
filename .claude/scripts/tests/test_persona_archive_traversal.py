"""PRP-7b WS4 — archive traversal safety tests.

Disposition coverage:
    - `_normalize_profile_archive_parts` rejects absolute paths, `..`,
      drive letters, empty parts.
    - `_safe_extract_profile_archive` uses two layers (data_filter as
      Layer 1, manual containment as Layer 2).
    - R1 M2 fix — `tarfile.data_filter` is ACTUALLY called per member
      (the earlier-draft bug used `tf.extraction_filter` + `extractfile()`
      which never triggered the filter).
    - R1 M2 — symlink TarInfo + device file rejection.
    - `_inspect_profile_archive_roots` returns top-level dir set.
    - Multi-root archive rejection.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from personas.clone import (
    _inspect_profile_archive_roots,
    _normalize_profile_archive_parts,
    _safe_extract_profile_archive,
    import_profile,
)


# ---------------------------------------------------------------------------
# _normalize_profile_archive_parts
# ---------------------------------------------------------------------------


def test_normalize_simple_path():
    assert _normalize_profile_archive_parts("foo/bar.txt") == ["foo", "bar.txt"]


def test_normalize_rejects_dotdot():
    with pytest.raises(ValueError):
        _normalize_profile_archive_parts("foo/../bar")


def test_normalize_rejects_empty():
    with pytest.raises(ValueError):
        _normalize_profile_archive_parts("")


def test_normalize_rejects_dot_only():
    with pytest.raises(ValueError):
        _normalize_profile_archive_parts(".")


def test_normalize_rejects_absolute_posix():
    with pytest.raises(ValueError):
        _normalize_profile_archive_parts("/etc/passwd")


def test_normalize_rejects_drive_letter():
    """Windows drive letter rejected even on POSIX (defense in depth)."""
    with pytest.raises(ValueError):
        _normalize_profile_archive_parts("C:\\Windows\\foo")


def test_normalize_rejects_traversal_prefix():
    with pytest.raises(ValueError):
        _normalize_profile_archive_parts("../escape")


# ---------------------------------------------------------------------------
# _inspect_profile_archive_roots
# ---------------------------------------------------------------------------


def _make_archive(tmp_path: Path, members: list[tuple[str, bytes]]) -> Path:
    """Build a .tar.gz archive with given (name, content) members."""
    archive = tmp_path / "test.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return archive


def _make_dir_archive(tmp_path: Path, names: list[str]) -> Path:
    """Build a .tar.gz with explicit DIRTYPE members for each name."""
    archive = tmp_path / "dirs.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name in names:
            info = tarfile.TarInfo(name=name)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
    return archive


def test_inspect_archive_roots_single_root(tmp_path):
    archive = _make_archive(
        tmp_path,
        [
            ("sales/SOUL.md", b"soul"),
            ("sales/MEMORY.md", b"memory"),
        ],
    )
    assert _inspect_profile_archive_roots(archive) == {"sales"}


def test_inspect_archive_roots_multi_root(tmp_path):
    archive = _make_archive(
        tmp_path,
        [
            ("foo/x.txt", b"x"),
            ("bar/y.txt", b"y"),
        ],
    )
    assert _inspect_profile_archive_roots(archive) == {"foo", "bar"}


def test_inspect_archive_roots_empty(tmp_path):
    archive = tmp_path / "empty.tar.gz"
    with tarfile.open(archive, "w:gz") as _tf:
        pass
    assert _inspect_profile_archive_roots(archive) == set()


# ---------------------------------------------------------------------------
# _safe_extract_profile_archive — security
# ---------------------------------------------------------------------------


def test_safe_extract_rejects_dotdot_member(tmp_path):
    """Archive member with `..` path raises (Layer 1 data_filter)."""
    archive = _make_archive(
        tmp_path, [("sales/../etc/passwd", b"hostile")]
    )
    dest = tmp_path / "dest"
    with pytest.raises((tarfile.FilterError, ValueError, tarfile.AbsolutePathError)):
        _safe_extract_profile_archive(archive, dest)


def test_safe_extract_rejects_absolute_member(tmp_path):
    """Archive member with absolute path raises."""
    archive = _make_archive(tmp_path, [("/etc/passwd", b"hostile")])
    dest = tmp_path / "dest"
    with pytest.raises((tarfile.FilterError, ValueError, tarfile.AbsolutePathError)):
        _safe_extract_profile_archive(archive, dest)


def test_safe_extract_rejects_symlink_outside(tmp_path):
    """R1 M2 — symlink TarInfo (SYMTYPE) pointing outside is rejected."""
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo(name="sales/escape-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        tf.addfile(info)
    dest = tmp_path / "dest"
    with pytest.raises((tarfile.FilterError, ValueError, tarfile.LinkOutsideDestinationError)):
        _safe_extract_profile_archive(archive, dest)


def test_safe_extract_rejects_device_member(tmp_path):
    """R1 M2 — character/block device members rejected."""
    archive = tmp_path / "dev.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo(name="sales/devnull")
        info.type = tarfile.CHRTYPE
        info.devmajor = 1
        info.devminor = 3
        tf.addfile(info)
    dest = tmp_path / "dest"
    with pytest.raises((tarfile.FilterError, ValueError)):
        _safe_extract_profile_archive(archive, dest)


def test_safe_extract_data_filter_actually_runs(tmp_path, monkeypatch):
    """R1 M2 fix verification — `tarfile.data_filter` is called per member.

    Earlier-draft bug used `tf.extraction_filter` + `extractfile()` which
    NEVER triggers the filter (extractfile is a raw stream read, not an
    extraction). This test wraps `tarfile.data_filter` with a counter and
    asserts it was invoked once per non-rejected member.
    """
    archive = _make_archive(
        tmp_path,
        [
            ("sales/SOUL.md", b"soul"),
            ("sales/USER.md", b"user"),
            ("sales/MEMORY.md", b"memory"),
        ],
    )
    dest = tmp_path / "dest"
    dest.mkdir()

    call_count = {"n": 0}
    real_filter = tarfile.data_filter

    def counting_filter(member, dest_path):
        call_count["n"] += 1
        return real_filter(member, dest_path)

    monkeypatch.setattr(tarfile, "data_filter", counting_filter)
    _safe_extract_profile_archive(archive, dest)
    # 3 members in the archive -> 3 invocations of data_filter.
    assert call_count["n"] == 3, (
        f"data_filter called {call_count['n']} times; expected 3"
    )


def test_safe_extract_valid_single_root(tmp_path):
    """Valid single-root archive extracts cleanly."""
    archive = _make_archive(
        tmp_path,
        [
            ("sales/SOUL.md", b"soul-content"),
            ("sales/memory/MEMORY.md", b"memory-content"),
        ],
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    _safe_extract_profile_archive(archive, dest)
    assert (dest / "sales" / "SOUL.md").read_bytes() == b"soul-content"
    assert (dest / "sales" / "memory" / "MEMORY.md").read_bytes() == b"memory-content"


# ---------------------------------------------------------------------------
# import_profile — multi-root + empty rejection
# ---------------------------------------------------------------------------


def test_import_profile_multi_root_archive_raises(tmp_path, empty_homie_root):
    archive = _make_archive(
        tmp_path,
        [("foo/x.txt", b"x"), ("bar/y.txt", b"y")],
    )
    with pytest.raises(ValueError, match="exactly one top-level"):
        import_profile(str(archive))


def test_import_profile_empty_archive_raises(tmp_path, empty_homie_root):
    archive = tmp_path / "empty.tar.gz"
    with tarfile.open(archive, "w:gz") as _tf:
        pass
    with pytest.raises(ValueError, match="empty"):
        import_profile(str(archive))
