"""Digest-bound publication transaction for the implement-prp DAG's `publish-pr`
tail (PRP-WF1B-publication-binding-hardening.md).

A manifest is context, never authorization. Approval is exclusively Archon's
`final-approval` node plus the exact revision/digest it rendered -- nothing
in this module accepts an approval field, a completed-node flag, or an
arbitrary command, and no function selects a repository root from artifact
content; the trusted `RepositoryIdentity` is always independently
rediscovered from live Git state.

Pure stdlib. No network, no Archon import, no LLM call. `git`/`gh` are
invoked as already-trusted subprocess dependencies, list argv, `shell=False`.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import random
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

# ---------------------------------------------------------------------------
# §6 canonical bytes / digest vectors
# ---------------------------------------------------------------------------

_DOMAIN_APPROVAL = b"taskchad:implement-prp:approval:v2\0"
_DOMAIN_PACKAGE = b"taskchad:pr-package:bytes:v2\0"
_DOMAIN_BODY = b"taskchad:pr-body:bytes:v2\0"
_DOMAIN_TREE = b"taskchad:git-tree:v2\0"
_DOMAIN_INVENTORY = b"taskchad:git-object-inventory:v2\0"

_NETWORK_TIMEOUT_SECONDS = 30.0

_SURROGATE_LO = 0xD800
_SURROGATE_HI = 0xDFFF


class CanonicalJsonError(ValueError):
    """Raised when a value cannot be encoded as canonical JSON (§6)."""


def _reject_lone_surrogates(s: str) -> None:
    for ch in s:
        if _SURROGATE_LO <= ord(ch) <= _SURROGATE_HI:
            raise CanonicalJsonError("lone surrogate in string")


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, str):
        _reject_lone_surrogates(value)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        seen: set[str] = set()
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError("non-string key")
            _reject_lone_surrogates(key)
            if key in seen:
                raise CanonicalJsonError("duplicate key")
            seen.add(key)
            _validate_json_value(item)
        return
    raise CanonicalJsonError(f"disallowed JSON value type: {type(value)!r}")


def canonical_json(value: object) -> bytes:
    """UTF-8 canonical JSON per PRP §6: sorted keys, compact separators, no
    floats/NaN/Infinity, no lone surrogates, unique object keys."""
    _validate_json_value(value)
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8")


def _strict_json_loads(raw: str | bytes) -> object:
    """Decode one JSON value with unique keys and the canonical scalar domain."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    if not isinstance(raw, str):
        raise CanonicalJsonError("JSON input must be UTF-8 bytes or text")

    def pairs_hook(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CanonicalJsonError(f"duplicate JSON key: {key!r}")
            value[key] = item
        return value

    def reject_constant(token):
        raise CanonicalJsonError(f"disallowed JSON constant: {token}")

    value = json.loads(
        raw, object_pairs_hook=pairs_hook, parse_constant=reject_constant
    )
    _validate_json_value(value)
    return value


def LP(x: bytes) -> bytes:  # noqa: N802 -- PRP §6 names this exactly `LP`
    """Length-prefix: unsigned 64-bit big-endian byte length, then `x`."""
    return len(x).to_bytes(8, "big") + x


def approval_digest(approval_payload: object) -> str:
    return hashlib.sha256(
        _DOMAIN_APPROVAL + LP(canonical_json(approval_payload))
    ).hexdigest()


def package_bytes_digest(package_bytes: bytes) -> str:
    return hashlib.sha256(_DOMAIN_PACKAGE + LP(package_bytes)).hexdigest()


def body_bytes_digest(body_bytes: bytes) -> str:
    return hashlib.sha256(_DOMAIN_BODY + LP(body_bytes)).hexdigest()


def object_state_digest(
    object_format: str, tree_oid: str, object_inventory_digest: str
) -> str:
    return hashlib.sha256(
        _DOMAIN_TREE
        + LP(object_format.encode("ascii"))
        + LP(tree_oid.encode("ascii"))
        + LP(object_inventory_digest.encode("ascii"))
    ).hexdigest()


def _canonical_object_inventory_bytes(
    records: Sequence[tuple[str, str, bytes]],
) -> bytes:
    """Records must already be validated distinct and sorted by raw
    lowercase-ASCII OID bytes -- this function only frames them; validation
    (duplicates, sort order, supported types, OID-hash agreement) lives in
    `verify_object_inventory` (§5.1/§9) once real Git objects are involved."""
    out = bytearray(_DOMAIN_INVENTORY)
    out += len(records).to_bytes(8, "big")
    for type_ascii, oid_ascii, canonical_content in records:
        out += LP(type_ascii.encode("ascii"))
        out += LP(oid_ascii.encode("ascii"))
        out += LP(canonical_content)
    return bytes(out)


def object_inventory_digest(records: Sequence[tuple[str, str, bytes]]) -> str:
    return hashlib.sha256(_canonical_object_inventory_bytes(records)).hexdigest()


_SUPPORTED_OBJECT_TYPES = ("blob", "tree", "commit")
_HEX_LOWER = re.compile(r"^[0-9a-f]+$")


def verify_object_inventory(
    records: Sequence[tuple[str, str, bytes]], object_format: str
) -> str:
    """Validate an inventory built from real `git cat-file` output and return
    its digest. Rejects duplicate OIDs, unsupported types, an OID not equal
    to Git's hash of `type SP decimal-size NUL content`, and any ordering
    other than sorted-by-raw-lowercase-ASCII-OID-bytes (PRP §6)."""
    expected_oid_width = _LOOSE_OBJECT_HEX_LEN.get(object_format)
    if expected_oid_width is None:
        raise CanonicalJsonError(f"unsupported object format: {object_format!r}")
    seen_oids: set[bytes] = set()
    raw_oids: list[bytes] = []
    for type_ascii, oid_ascii, content in records:
        if type_ascii not in _SUPPORTED_OBJECT_TYPES:
            raise CanonicalJsonError(f"unsupported object type: {type_ascii!r}")
        if len(oid_ascii) != expected_oid_width or not _HEX_LOWER.fullmatch(oid_ascii):
            raise CanonicalJsonError(f"OID must be lowercase ASCII hex: {oid_ascii!r}")
        raw_oid = oid_ascii.encode("ascii")
        if raw_oid in seen_oids:
            raise CanonicalJsonError(f"duplicate OID in inventory: {oid_ascii}")
        seen_oids.add(raw_oid)
        raw_oids.append(raw_oid)
        header = f"{type_ascii} {len(content)}\0".encode("ascii")
        expected = hashlib.new(object_format, header + content).hexdigest()
        if expected != oid_ascii:
            raise CanonicalJsonError(f"OID does not hash-verify: {oid_ascii}")
    if raw_oids != sorted(raw_oids):
        raise CanonicalJsonError("object inventory is not sorted by raw OID bytes")
    return object_inventory_digest(records)


def _object_hash_name(oid_hex_len: int) -> str:
    if oid_hex_len == 40:
        return "sha1"
    if oid_hex_len == 64:
        return "sha256"
    raise CanonicalJsonError(f"unsupported OID width: {oid_hex_len}")


# ---------------------------------------------------------------------------
# §4 trusted invocation and baseline identity
# ---------------------------------------------------------------------------

_DOMAIN_WORKTREE_ID = b"taskchad:repository-identity:v2"


class RepositoryIdentityError(ValueError):
    """Raised by `discover_repository` for any condition that fails closed
    with the stable reason `repository_identity_invalid` (PRP §4)."""

    reason = "repository_identity_invalid"


@dataclass(frozen=True)
class RepositoryIdentity:
    root: Path
    git_dir: Path
    common_dir: Path
    worktree_id: str
    object_format: str
    branch_ref: str
    baseline_commit: str


def _run_git_text(args: Sequence[str], cwd: Path, *, check: bool = True):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            shell=False,
            check=False,
        )
    except UnicodeDecodeError as exc:
        raise RepositoryIdentityError(f"non-UTF-8 Git output: {exc}") from exc
    finally:
        pass


def _git_out(args: Sequence[str], cwd: Path) -> str:
    result = _run_git_text(args, cwd)
    if result.returncode != 0:
        raise RepositoryIdentityError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _reject_unc_or_non_utf8(*paths: str) -> None:
    for p in paths:
        if p.startswith("\\\\") or p.startswith("//"):
            raise RepositoryIdentityError(f"UNC path is not permitted: {p}")
        try:
            p.encode("utf-8").decode("utf-8")
        except UnicodeError as exc:
            raise RepositoryIdentityError(f"non-UTF-8 Git path: {p}") from exc


def _has_symlinked_component(path: Path) -> bool:
    cur = path
    while True:
        if cur.exists() and cur.is_symlink():
            return True
        parent = cur.parent
        if parent == cur:
            return False
        cur = parent


def _norm_for_compare(path: Path) -> str:
    resolved = str(path.resolve())
    if sys.platform == "win32":
        import os as _os

        return _os.path.normcase(resolved)
    return resolved


def _parse_worktree_list_porcelain_z(raw: str) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain -z` output into one dict per
    worktree entry. Blocks are separated by a doubled NUL terminator;
    within a block each `key value` line is itself NUL-terminated."""
    entries: list[dict[str, str]] = []
    for block in raw.split("\0\0"):
        if not block:
            continue
        fields: dict[str, str] = {}
        for line in block.split("\0"):
            if not line:
                continue
            if " " in line:
                key, value = line.split(" ", 1)
            else:
                key, value = line, ""
            fields[key] = value
        if fields:
            entries.append(fields)
    return entries


def discover_repository(cwd: Path) -> RepositoryIdentity:
    """Independently rediscover the trusted repository identity from live
    Git state at `cwd`. Never trusts `baseline.json['root']` or any other
    artifact-selected path as authority (PRP §4)."""
    cwd = Path(cwd)
    if not cwd.is_dir():
        raise RepositoryIdentityError(f"cwd is not an existing directory: {cwd}")

    is_bare = _git_out(["rev-parse", "--is-bare-repository"], cwd)
    if is_bare != "false":
        raise RepositoryIdentityError("bare repository is not permitted")

    root_raw = _git_out(["rev-parse", "--show-toplevel"], cwd)
    git_dir_raw = _git_out(["rev-parse", "--path-format=absolute", "--git-dir"], cwd)
    common_dir_raw = _git_out(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd
    )
    _reject_unc_or_non_utf8(root_raw, git_dir_raw, common_dir_raw)

    root_path = Path(root_raw)
    git_dir_path = Path(git_dir_raw)
    common_dir_path = Path(common_dir_raw)
    for p in (root_path, git_dir_path, common_dir_path):
        if not p.is_dir():
            raise RepositoryIdentityError(f"path is missing or not a directory: {p}")
    if _has_symlinked_component(git_dir_path) or _has_symlinked_component(
        common_dir_path
    ):
        raise RepositoryIdentityError("symlinked git_dir/common_dir path component")

    root = root_path.resolve()
    git_dir = git_dir_path.resolve()
    common_dir = common_dir_path.resolve()
    if _norm_for_compare(git_dir_path) == _norm_for_compare(common_dir_path):
        raise RepositoryIdentityError(
            "git_dir == common_dir: a linked worktree is required, not the main checkout"
        )

    ref_result = _run_git_text(["symbolic-ref", "--quiet", "HEAD"], cwd)
    if ref_result.returncode != 0 or not ref_result.stdout.strip():
        raise RepositoryIdentityError("detached HEAD is not permitted")
    branch_ref = ref_result.stdout.strip()

    object_format = _git_out(["rev-parse", "--show-object-format=storage"], cwd)
    if object_format not in ("sha1", "sha256"):
        raise RepositoryIdentityError(f"unsupported object format: {object_format!r}")

    baseline_commit = _git_out(["rev-parse", "--verify", "HEAD^{commit}"], cwd)

    list_result = _run_git_text(["worktree", "list", "--porcelain", "-z"], cwd)
    if list_result.returncode != 0:
        raise RepositoryIdentityError(
            f"git worktree list failed: {list_result.stderr.strip()}"
        )
    entries = _parse_worktree_list_porcelain_z(list_result.stdout)

    root_key = _norm_for_compare(root_path)
    matched = None
    for entry in entries:
        if "worktree" not in entry:
            continue
        if _norm_for_compare(Path(entry["worktree"])) == root_key:
            matched = entry
            break
    if matched is None:
        raise RepositoryIdentityError(
            f"root is not registered by 'git worktree list': {root}"
        )
    if matched.get("HEAD") != baseline_commit:
        raise RepositoryIdentityError("registered worktree HEAD differs from live HEAD")
    if "bare" in matched or "detached" in matched:
        raise RepositoryIdentityError("registered worktree is bare or detached")
    if matched.get("branch") != branch_ref:
        raise RepositoryIdentityError(
            "registered worktree branch differs from live branch"
        )

    worktree_id = _compute_worktree_id(
        root=root,
        git_dir=git_dir,
        common_dir=common_dir,
        branch_ref=branch_ref,
        baseline_commit=baseline_commit,
        object_format=object_format,
    )

    return RepositoryIdentity(
        root=root,
        git_dir=git_dir,
        common_dir=common_dir,
        worktree_id=worktree_id,
        object_format=object_format,
        branch_ref=branch_ref,
        baseline_commit=baseline_commit,
    )


def _compute_worktree_id(
    *,
    root: Path | str,
    git_dir: Path | str,
    common_dir: Path | str,
    branch_ref: str,
    baseline_commit: str,
    object_format: str,
) -> str:
    """Compute the immutable approved identity from approved fields.

    Publish compares this value to the sealed payload independently from the
    freshly observed local ref.  That separation permits the one approved
    baseline->commit transition without weakening worktree substitution
    detection (PRP-WF1B §4 and adversarial remediation E).
    """
    identity_bytes = canonical_json(
        {
            "root": str(root),
            "git_dir": str(git_dir),
            "common_dir": str(common_dir),
            "branch_ref": branch_ref,
            "baseline_commit": baseline_commit,
            "object_format": object_format,
        }
    )
    return hashlib.sha256(_DOMAIN_WORKTREE_ID + b"\0" + identity_bytes).hexdigest()


# ---------------------------------------------------------------------------
# §7 atomic authoritative artifact writes + confined reads
# ---------------------------------------------------------------------------

_MAX_CONFINED_READ_BYTES = 10 * 1024 * 1024


def _random_tmp_suffix() -> str:
    return f"{random.SystemRandom().randrange(16**12):012x}"


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_stat(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _stat_identity(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))


def _stat_snapshot(st: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    """Metadata that must stay stable while an authoritative file is open."""
    return (
        st.st_dev,
        st.st_ino,
        stat.S_IFMT(st.st_mode),
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        getattr(st, "st_file_attributes", 0),
    )


def _lstat_existing_components(path: Path) -> None:
    """Reject symlink/reparse components and non-directory ancestors."""
    path = Path(path).absolute()
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for component in reversed(chain):
        try:
            st = os.lstat(component)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or _is_reparse_stat(st):
            raise OSError(f"symlink/reparse path component is forbidden: {component}")
        if component != path and not stat.S_ISDIR(st.st_mode):
            raise OSError(f"non-directory path ancestor: {component}")


def _destination_stat(path: Path) -> os.stat_result | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or _is_reparse_stat(st):
        raise OSError(f"refusing symlink/reparse destination: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"refusing non-regular destination: {path}")
    return st


def _fsync_dir_best_effort(dir_path: Path) -> None:
    if sys.platform == "win32":
        return  # directory fsync is unsupported on Windows; advisory only
    fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            raise
    finally:
        os.close(fd)


def atomic_write(
    path: Path, data: bytes, *, mode: int = 0o600, overwrite: bool = True
) -> None:
    """Write `data` to `path` atomically (PRP §7): random same-directory temp
    file created `O_CREAT|O_EXCL|O_NOFOLLOW` where available, fsync file then
    best-effort fsync parent, `os.replace` into place. Existing destination
    symlinks/non-regular files are rejected; `overwrite=False` additionally
    rejects an existing regular destination."""
    path = Path(path)
    parent = path.parent
    _lstat_existing_components(parent)
    parent_before = os.lstat(parent)
    if not stat.S_ISDIR(parent_before.st_mode) or _is_reparse_stat(parent_before):
        raise OSError(f"parent directory does not exist or is unsafe: {parent}")
    destination_before = _destination_stat(path)
    if destination_before is not None and not overwrite:
        raise FileExistsError(f"destination already exists: {path}")

    tmp_path = parent / f".{path.name}.{_random_tmp_suffix()}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(tmp_path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse_stat(opened):
            raise OSError("atomic temp is not a regular file")
        opened_identity = _stat_identity(opened)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, mode, follow_symlinks=False)
        except (NotImplementedError, OSError):
            if os.name != "nt":
                raise
        parent_after_write = os.lstat(parent)
        if _stat_identity(parent_after_write) != _stat_identity(parent_before):
            raise OSError("parent directory identity changed during atomic write")
        temp_after_write = os.lstat(tmp_path)
        if (
            _stat_identity(temp_after_write) != opened_identity
            or not stat.S_ISREG(temp_after_write.st_mode)
            or stat.S_ISLNK(temp_after_write.st_mode)
            or _is_reparse_stat(temp_after_write)
        ):
            raise OSError("atomic temp identity changed before install")
        current_destination = _destination_stat(path)
        if destination_before is None and current_destination is not None:
            if overwrite:
                destination_before = current_destination
            else:
                raise FileExistsError(f"destination appeared during write: {path}")
        elif destination_before is not None and (
            current_destination is None
            or _stat_identity(current_destination) != _stat_identity(destination_before)
        ):
            raise OSError("destination identity changed during atomic write")
        if overwrite:
            os.replace(tmp_path, path)
        else:
            # Hard-linking a fully fsynced same-directory temp supplies the
            # no-overwrite atomic create that os.replace cannot provide.
            os.link(tmp_path, path, follow_symlinks=False)
            os.unlink(tmp_path)
        installed = _destination_stat(path)
        if (
            installed is None
            or not stat.S_ISREG(installed.st_mode)
            or _stat_identity(installed) != opened_identity
        ):
            raise OSError("atomic destination did not become a regular file")
        if _stat_identity(os.lstat(parent)) != _stat_identity(parent_before):
            raise OSError("parent directory identity changed after atomic install")
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    _fsync_dir_best_effort(parent)


def read_confined_regular_bytes(
    path: Path, *, max_bytes: int = _MAX_CONFINED_READ_BYTES
) -> bytes:
    """Open once, require a regular non-symlink file, cap at `max_bytes`."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    path = Path(path)
    _lstat_existing_components(path)
    parent_before = os.lstat(path.parent)
    path_before = os.lstat(path)
    if (
        not stat.S_ISREG(path_before.st_mode)
        or stat.S_ISLNK(path_before.st_mode)
        or _is_reparse_stat(path_before)
    ):
        raise OSError(f"not a confined regular file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags)
    try:
        opened_before = os.fstat(fd)
        if not stat.S_ISREG(opened_before.st_mode) or _is_reparse_stat(opened_before):
            raise OSError(f"opened path is not regular: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        opened_after = os.fstat(fd)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise OSError(f"file exceeds {max_bytes}-byte cap: {path}")
    path_after = os.lstat(path)
    parent_after = os.lstat(path.parent)
    # Windows reports different timestamp precision for path stat versus an
    # open-handle fstat. Compare each observation channel over time, then bind
    # the channels with stable identity/type/size/attributes. This retains the
    # mutation check without treating that platform representation difference
    # as an attack.
    identities = {
        _stat_identity(path_before),
        _stat_identity(opened_before),
        _stat_identity(opened_after),
        _stat_identity(path_after),
    }
    sizes = {
        path_before.st_size,
        opened_before.st_size,
        opened_after.st_size,
        path_after.st_size,
    }
    attributes = {
        getattr(path_before, "st_file_attributes", 0),
        getattr(opened_before, "st_file_attributes", 0),
        getattr(opened_after, "st_file_attributes", 0),
        getattr(path_after, "st_file_attributes", 0),
    }
    if (
        _stat_snapshot(path_before) != _stat_snapshot(path_after)
        or _stat_snapshot(opened_before) != _stat_snapshot(opened_after)
        or len(identities) != 1
        or len(sizes) != 1
        or len(attributes) != 1
    ):
        raise OSError(f"file identity/metadata changed during confined read: {path}")
    if _stat_identity(parent_before) != _stat_identity(parent_after):
        raise OSError(f"parent identity changed during confined read: {path.parent}")
    return data


def _validate_confined_regular_file(path: Path, confinement_root: Path) -> None:
    """Require one confined regular file and bind its pathname to a no-follow fd."""
    path = Path(path)
    confinement_root = Path(confinement_root)
    _lstat_existing_components(path)
    root_real = os.path.normcase(os.path.realpath(confinement_root))
    path_real = os.path.normcase(os.path.realpath(path))
    try:
        confined = os.path.commonpath((root_real, path_real)) == root_real
    except ValueError:
        confined = False
    path_before = os.lstat(path)
    if (
        not confined
        or not stat.S_ISREG(path_before.st_mode)
        or stat.S_ISLNK(path_before.st_mode)
        or _is_reparse_stat(path_before)
    ):
        raise OSError(f"not a confined regular file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_stat(opened)
            or _stat_identity(opened) != _stat_identity(path_before)
        ):
            raise OSError(f"opened path is not the confined regular file: {path}")
    finally:
        os.close(fd)


def _validate_confined_real_directory(path: Path, confinement_root: Path) -> None:
    """Require a real, confined directory with no symlink/reparse component."""
    path = Path(path)
    confinement_root = Path(confinement_root)
    _lstat_existing_components(path)
    root_real = os.path.normcase(os.path.realpath(confinement_root))
    path_real = os.path.normcase(os.path.realpath(path))
    try:
        confined = os.path.commonpath((root_real, path_real)) == root_real
    except ValueError:
        confined = False
    path_st = os.lstat(path)
    if (
        not confined
        or not stat.S_ISDIR(path_st.st_mode)
        or stat.S_ISLNK(path_st.st_mode)
        or _is_reparse_stat(path_st)
    ):
        raise OSError(f"not a confined real directory: {path}")


# ---------------------------------------------------------------------------
# §10 locking
# ---------------------------------------------------------------------------

_LOCK_SCHEMA = 1
_PROCESS_START_MARKER = f"{os.getpid()}:{time.time_ns()}:{_random_tmp_suffix()}"
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class PublicationLockedError(RuntimeError):
    reason = "publication_locked"


def _lock_name(common_dir: Path, branch_ref: str) -> str:
    return hashlib.sha256(
        str(common_dir).encode("utf-8") + b"\0" + branch_ref.encode("utf-8")
    ).hexdigest()


def _lock_file_exclusive_or_raise(fd: int, lock_path: Path) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PublicationLockedError(f"lock held: {lock_path}") from exc
        return
    if msvcrt is not None:
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise PublicationLockedError(f"lock held: {lock_path}") from exc
        return
    raise RuntimeError("no supported file-locking primitive on this platform")


def _unlock_file(fd: int) -> None:
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _validate_stale_lock_record(raw: bytes, lock_path: Path) -> None:
    """An unlocked record is replaceable only after its owner is disproven."""
    if not raw or raw == b"\0":
        return
    try:
        record = json.loads(raw.rstrip(b"\0").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationLockedError(
            f"unverifiable stale lock record: {lock_path}"
        ) from exc
    required = {
        "schema",
        "pid",
        "process_start_marker",
        "hostname",
        "run_id",
        "created_at",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise PublicationLockedError(f"unverifiable stale lock record: {lock_path}")
    pid = record.get("pid")
    marker = record.get("process_start_marker")
    hostname = record.get("hostname")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or not isinstance(marker, str)
        or not marker
        or not isinstance(hostname, str)
        or not hostname
    ):
        raise PublicationLockedError(f"unverifiable stale lock record: {lock_path}")
    if hostname != socket.gethostname():
        raise PublicationLockedError(
            f"cannot disprove remote-host lock owner: {lock_path}"
        )
    if pid == os.getpid() and marker == _PROCESS_START_MARKER:
        raise PublicationLockedError(f"live lock owner record remains: {lock_path}")
    if _process_is_alive(pid):
        raise PublicationLockedError(f"cannot disprove live lock owner: {lock_path}")


@contextlib.contextmanager
def acquire_repository_lock(
    artifacts_dir: Path, common_dir: Path, branch_ref: str, *, run_id: str
):
    """One repository-scoped lock, named `sha256(common_dir + branch_ref)`
    (PRP §10). A currently-held OS-level lock always wins regardless of the
    age of any on-disk record: `fcntl.flock`/`msvcrt.locking` are
    process/handle-scoped and release automatically on crash or normal exit,
    so no separate "is the recorded pid still alive" bookkeeping is required
    for correctness -- the pid/hostname/start-time record below is written
    for diagnostics only and is never itself trusted as lock authority."""
    name = _lock_name(Path(common_dir), branch_ref)
    with _thread_locks_guard:
        thread_lock = _thread_locks.setdefault(name, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        raise PublicationLockedError(
            "repository/branch is locked by another thread in this process"
        )
    try:
        lock_dir = Path(artifacts_dir) / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        _lstat_existing_components(lock_dir)
        lock_parent_before = os.lstat(lock_dir)
        if not stat.S_ISDIR(lock_parent_before.st_mode) or _is_reparse_stat(
            lock_parent_before
        ):
            raise PublicationLockedError(
                "lock directory is not a safe regular directory"
            )
        lock_path = lock_dir / f"{name}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        owns_record = False
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or _is_reparse_stat(opened):
                raise PublicationLockedError(
                    f"lock path is not a regular file: {lock_path}"
                )
            path_st = os.lstat(lock_path)
            if _stat_identity(opened) != _stat_identity(path_st):
                raise PublicationLockedError(f"lock path identity changed: {lock_path}")
            if _stat_identity(os.lstat(lock_dir)) != _stat_identity(lock_parent_before):
                raise PublicationLockedError("lock directory identity changed")
            _lock_file_exclusive_or_raise(fd, lock_path)
            os.lseek(fd, 0, os.SEEK_SET)
            existing = os.read(fd, max(os.fstat(fd).st_size, 1))
            _validate_stale_lock_record(existing, lock_path)
            record = {
                "schema": _LOCK_SCHEMA,
                "pid": os.getpid(),
                "process_start_marker": _PROCESS_START_MARKER,
                "hostname": socket.gethostname(),
                "run_id": run_id,
                "created_at": time.time(),
            }
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(record, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
            owns_record = True
            yield
        finally:
            if owns_record:
                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.ftruncate(fd, 0)
                    if msvcrt is not None:
                        os.write(fd, b"\0")
                    os.fsync(fd)
            _unlock_file(fd)
            os.close(fd)
    finally:
        thread_lock.release()


# ---------------------------------------------------------------------------
# §5.1 deterministic prospective Git transaction (build at package-gate)
# ---------------------------------------------------------------------------

_ALLOWED_TREE_MODES = ("100644", "100755", "120000")
_DIFF_TREE_STATUSES = ("A", "M", "D", "T", "R")
_ZERO_OID_RE = re.compile(r"^0+$")


class TransactionError(RuntimeError):
    """Raised while building/sealing a prospective transaction (PRP §5.1).
    `reason` is one of the PRP §8 package-check stable reasons."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SealedTransaction:
    manifest: Mapping[str, object]
    directory: Path


def _scoped_git_env(
    *,
    git_dir: Path,
    work_tree: Path,
    index_file: Path,
    object_dir: Path,
    alt_object_dirs: Path,
) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(work_tree)
    env["GIT_INDEX_FILE"] = str(index_file)
    env["GIT_OBJECT_DIRECTORY"] = str(object_dir)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(alt_object_dirs)
    return env


def _run_scoped_git(
    args: Sequence[str],
    env: Mapping[str, str],
    *,
    cwd: Path,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=dict(env),
        input=input_bytes,
        capture_output=True,
        shell=False,
    )


def _reject_reparse_or_symlink_ancestors(path: Path) -> None:
    try:
        _lstat_existing_components(path)
    except OSError as exc:
        raise TransactionError(
            "prospective_tree_invalid", f"unsafe path component: {path}: {exc}"
        ) from exc


def _canonical_git_path(path: str) -> bool:
    if not path or "\\" in path or "\0" in path:
        return False
    try:
        path.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    parsed = PurePosixPath(path)
    return (
        not parsed.is_absolute()
        and "." not in parsed.parts
        and ".." not in parsed.parts
        and parsed.as_posix() == path
        and not path.endswith("/")
    )


def _parse_diff_tree_raw_z(
    raw: str | bytes, object_format: str
) -> list[dict[str, object]]:
    """Parse `git diff-tree --root -r -z --raw --no-abbrev --find-renames=50%`
    output per the exact grammar in PRP §5.1 step 4."""
    oid_width = {"sha1": 40, "sha256": 64}.get(object_format)
    if not isinstance(raw, (str, bytes)):
        raise TransactionError(
            "git_object_state_mismatch", "diff-tree output is not bytes/text"
        )
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise TransactionError(
                "git_object_state_mismatch", "diff-tree contains non-UTF-8 bytes"
            ) from exc
    if oid_width is None or (raw and not raw.endswith("\0")):
        raise TransactionError(
            "git_object_state_mismatch", "invalid object format or missing terminal NUL"
        )
    tokens = raw.split("\0")[:-1]
    entries: list[dict[str, object]] = []
    seen_records: set[tuple[str, ...]] = set()
    seen_paths: set[str] = set()
    i = 0
    while i < len(tokens):
        header = tokens[i]
        if header == "":
            raise TransactionError(
                "git_object_state_mismatch", "unexpected empty diff-tree token"
            )
        if not header.startswith(":"):
            raise TransactionError(
                "git_object_state_mismatch", f"malformed diff-tree header: {header!r}"
            )
        parts = header[1:].split(" ")
        if len(parts) != 5:
            raise TransactionError(
                "git_object_state_mismatch", f"malformed diff-tree header: {header!r}"
            )
        old_mode, new_mode, old_oid, new_oid, status_and_score = parts
        if not status_and_score:
            raise TransactionError(
                "git_object_state_mismatch", f"missing status: {header!r}"
            )
        status = status_and_score[0]
        score = status_and_score[1:] if len(status_and_score) > 1 else None
        if status not in _DIFF_TREE_STATUSES:
            raise TransactionError(
                "git_object_state_mismatch", f"unknown status: {status}"
            )
        if not re.fullmatch(r"[0-7]{6}", old_mode) or not re.fullmatch(
            r"[0-7]{6}", new_mode
        ):
            raise TransactionError(
                "git_object_state_mismatch", f"malformed mode: {header!r}"
            )
        oid_pattern = rf"[0-9a-f]{{{oid_width}}}"
        if not re.fullmatch(oid_pattern, old_oid) or not re.fullmatch(
            oid_pattern, new_oid
        ):
            raise TransactionError(
                "git_object_state_mismatch", f"malformed object id: {header!r}"
            )
        old_zero = old_oid == "0" * oid_width
        new_zero = new_oid == "0" * oid_width
        path_count = 2 if status == "R" else 1
        if i + path_count >= len(tokens):
            raise TransactionError(
                "git_object_state_mismatch", f"truncated diff-tree record: {header!r}"
            )
        paths = tokens[i + 1 : i + 1 + path_count]
        if any(not _canonical_git_path(path) for path in paths):
            raise TransactionError(
                "git_object_state_mismatch", f"non-canonical Git path: {paths!r}"
            )
        record = tuple([header, *paths])
        if record in seen_records or any(path in seen_paths for path in paths):
            raise TransactionError(
                "git_object_state_mismatch",
                f"duplicate diff-tree record/path: {paths!r}",
            )
        seen_records.add(record)
        seen_paths.update(paths)

        if status == "R":
            if (
                score is None
                or not re.fullmatch(r"[0-9]{3}", score)
                or int(score) > 100
                or old_mode == "000000"
                or new_mode == "000000"
                or old_zero
                or new_zero
                or paths[0] == paths[1]
            ):
                raise TransactionError(
                    "git_object_state_mismatch",
                    f"malformed rename record: {header!r}",
                )
            old_path, new_path = paths
            i += 3
            entries.append(
                {
                    "status": status,
                    "score": score,
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "old_oid": old_oid,
                    "new_oid": new_oid,
                    "old_path": old_path,
                    "new_path": new_path,
                }
            )
            continue
        if score is not None:
            raise TransactionError(
                "git_object_state_mismatch",
                f"score present on non-rename record: {header!r}",
            )
        path = paths[0]
        i += 2
        if status == "A":
            if old_mode != "000000" or not old_zero or new_mode == "000000" or new_zero:
                raise TransactionError(
                    "git_object_state_mismatch", f"malformed A record: {header!r}"
                )
        elif status == "D":
            if new_mode != "000000" or not new_zero or old_mode == "000000" or old_zero:
                raise TransactionError(
                    "git_object_state_mismatch", f"malformed D record: {header!r}"
                )
        elif status == "M":
            if old_mode != new_mode or old_mode == "000000" or old_zero or new_zero:
                raise TransactionError(
                    "git_object_state_mismatch", f"malformed M record: {header!r}"
                )
        elif status == "T":
            if (
                old_mode == new_mode
                or old_mode == "000000"
                or new_mode == "000000"
                or old_zero
                or new_zero
            ):
                raise TransactionError(
                    "git_object_state_mismatch", f"malformed T record: {header!r}"
                )
        entries.append(
            {
                "status": status,
                "score": None,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "old_oid": old_oid,
                "new_oid": new_oid,
                "old_path": None,
                "new_path": path,
            }
        )
    for e in entries:
        for mode_key in ("old_mode", "new_mode"):
            mode = e[mode_key]
            if mode != "000000" and mode not in _ALLOWED_TREE_MODES:
                raise TransactionError(
                    "git_object_state_mismatch", f"disallowed object mode: {mode}"
                )
    return entries


def _parse_commit_object(
    raw: bytes,
    *,
    object_format: str,
    tree_oid: str,
    parent_oid: str,
    author_ident: str,
    committer_ident: str,
    message: bytes,
) -> None:
    """Require byte-for-byte equality for the complete raw commit object."""
    oid_width = {"sha1": 40, "sha256": 64}.get(object_format)
    if oid_width is None:
        raise TransactionError("prospective_commit_invalid", "unknown object format")
    try:
        headers, observed_message = raw.split(b"\n\n", 1)
        header_lines = headers.decode("utf-8", errors="strict").split("\n")
    except (ValueError, UnicodeDecodeError) as exc:
        raise TransactionError(
            "prospective_commit_invalid", "commit object has malformed headers"
        ) from exc
    expected_headers = [
        f"tree {tree_oid}",
        f"parent {parent_oid}",
        f"author {author_ident}",
        f"committer {committer_ident}",
    ]
    oid_re = re.compile(rf"[0-9a-f]{{{oid_width}}}")
    if (
        not oid_re.fullmatch(tree_oid)
        or not oid_re.fullmatch(parent_oid)
        or header_lines != expected_headers
        or observed_message != message
    ):
        raise TransactionError(
            "prospective_commit_invalid",
            "commit tree/parent/identities/timestamps/message are not exactly bound",
        )


def changed_paths_projection(entries: Sequence[Mapping[str, object]]) -> list[str]:
    """Sorted unique union of `path` for A/M/D/T and both `old_path`/`new_path`
    for R (PRP §5.1 step 4). Exposed for callers (e.g. the package-gate
    orchestrator) that need `pkg.changed_files` equality without recomputing
    the projection logic."""
    paths: set[str] = set()
    for e in entries:
        if e["status"] == "R":
            paths.add(e["old_path"])
            paths.add(e["new_path"])
        else:
            paths.add(e["new_path"])
    return sorted(paths)


_IDENT_RE = re.compile(r"^(?P<name_email>.+) (?P<ts>\d+) (?P<tz>[+-]\d{4})$")
_NAME_EMAIL_RE = re.compile(r"^(?P<name>[^<>]+) <(?P<email>[^<>]*)>$")


def _parse_and_split_ident(ident_line: str) -> tuple[str, str]:
    if "\n" in ident_line or "\0" in ident_line:
        raise TransactionError("prospective_commit_invalid", "multiline/NUL identity")
    m = _IDENT_RE.match(ident_line)
    if not m:
        raise TransactionError(
            "prospective_commit_invalid", f"unparsable ident: {ident_line!r}"
        )
    name_email = m.group("name_email")
    nm = _NAME_EMAIL_RE.match(name_email)
    if not nm:
        raise TransactionError(
            "prospective_commit_invalid", f"unparsable ident: {ident_line!r}"
        )
    return nm.group("name"), nm.group("email")


def _resolve_publication_target(identity: RepositoryIdentity) -> tuple[str, str]:
    """Resolve the canonical GitHub repository and default PR base internally."""
    repository_slug = _derive_origin_repository_slug(identity.root)
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository_slug):
        raise TransactionError("package_invalid", "invalid canonical repository slug")
    ok, repo = _gh_json(
        [
            "repo",
            "view",
            "--repo",
            repository_slug,
            "--json",
            "nameWithOwner,defaultBranchRef",
        ],
        identity.root,
    )
    if (
        not ok
        or not isinstance(repo, dict)
        or set(repo) != {"nameWithOwner", "defaultBranchRef"}
        or repo.get("nameWithOwner") != repository_slug
        or not isinstance(repo.get("defaultBranchRef"), dict)
        or set(repo["defaultBranchRef"]) != {"name"}
    ):
        raise TransactionError(
            "package_invalid", "gh repo view returned an unexpected repository shape"
        )
    pr_base_branch = repo["defaultBranchRef"].get("name")
    if (
        not isinstance(pr_base_branch, str)
        or not pr_base_branch
        or any(c in pr_base_branch for c in "\r\n\0")
    ):
        raise TransactionError("package_invalid", "invalid PR base branch")
    return repository_slug, pr_base_branch


def build_sealed_transaction(
    identity: RepositoryIdentity,
    artifacts_dir: Path,
    package_bytes: bytes,
    body_bytes: bytes,
    allowed_paths: Sequence[str],
) -> SealedTransaction:
    """Build the prospective Git tree/commit for `package-gate` (PRP §5.1
    steps 2-6). Never touches the user's real index/HEAD/refs; every Git
    plumbing command runs against a temporary index and temporary object
    directory, with the trusted `common_dir/objects` supplied only as a
    read-only alternate."""
    artifacts_dir = Path(artifacts_dir)
    if (
        not allowed_paths
        or any(
            not isinstance(path, str) or not _canonical_git_path(path)
            for path in allowed_paths
        )
        or len(set(allowed_paths)) != len(allowed_paths)
    ):
        raise TransactionError(
            "changed_paths_mismatch", "allowed path roots are invalid"
        )
    repository_slug, pr_base_branch = _resolve_publication_target(identity)
    try:
        body_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TransactionError("package_invalid", "PR body is not UTF-8") from exc
    if not body_bytes or b"\0" in body_bytes:
        raise TransactionError("package_invalid", "PR body is empty or contains NUL")
    run_id = _random_run_id()
    tx_root = artifacts_dir / "publication-transactions"
    tx_root.mkdir(parents=True, exist_ok=True)
    _reject_reparse_or_symlink_ancestors(tx_root)

    tmp_dir = tx_root / f".{run_id}.tmp"
    tmp_dir.mkdir(mode=0o700)
    try:
        (tmp_dir / "objects").mkdir(mode=0o700)
        index_path = tmp_dir / "index"
        object_dir = tmp_dir / "objects"
        alt_object_dirs = identity.common_dir / "objects"

        atomic_write(
            tmp_dir / "sealed-pr-package.json",
            package_bytes,
            mode=0o400,
            overwrite=False,
        )
        atomic_write(
            tmp_dir / "sealed-pr-body.md", body_bytes, mode=0o400, overwrite=False
        )

        env = _scoped_git_env(
            git_dir=identity.git_dir,
            work_tree=identity.root,
            index_file=index_path,
            object_dir=object_dir,
            alt_object_dirs=alt_object_dirs,
        )

        read_tree = _run_scoped_git(
            ["read-tree", identity.baseline_commit], env, cwd=identity.root
        )
        if read_tree.returncode != 0:
            raise TransactionError(
                "prospective_tree_invalid", f"git read-tree failed: {read_tree.stderr}"
            )

        add = _run_scoped_git(
            ["--literal-pathspecs", "add", "-A", "--", *allowed_paths],
            env,
            cwd=identity.root,
        )
        if add.returncode != 0:
            raise TransactionError(
                "prospective_tree_invalid", f"git add failed: {add.stderr}"
            )

        ls_files = _run_scoped_git(["ls-files", "-s", "-z"], env, cwd=identity.root)
        for line in ls_files.stdout.split(b"\0"):
            if not line:
                continue
            mode = line.split(b" ", 1)[0].decode("ascii", errors="replace")
            if mode == "160000":
                raise TransactionError(
                    "prospective_tree_invalid", "submodules/gitlinks are not permitted"
                )

        write_tree = _run_scoped_git(["write-tree"], env, cwd=identity.root)
        if write_tree.returncode != 0:
            raise TransactionError(
                "prospective_tree_invalid",
                f"git write-tree failed: {write_tree.stderr}",
            )
        tree_oid = write_tree.stdout.decode("ascii").strip()

        diff_tree = _run_scoped_git(
            [
                "diff-tree",
                "--root",
                "-r",
                "-z",
                "--raw",
                "--no-abbrev",
                "--find-renames=50%",
                identity.baseline_commit,
                tree_oid,
            ],
            env,
            cwd=identity.root,
        )
        if diff_tree.returncode != 0:
            raise TransactionError(
                "git_object_state_mismatch", f"git diff-tree failed: {diff_tree.stderr}"
            )
        entries = _parse_diff_tree_raw_z(diff_tree.stdout, identity.object_format)

        author_ident_raw = _run_scoped_git(
            ["var", "GIT_AUTHOR_IDENT"], env, cwd=identity.root
        )
        committer_ident_raw = _run_scoped_git(
            ["var", "GIT_COMMITTER_IDENT"], env, cwd=identity.root
        )
        if author_ident_raw.returncode != 0 or committer_ident_raw.returncode != 0:
            raise TransactionError(
                "prospective_commit_invalid", "unable to determine identity"
            )
        author_name, author_email = _parse_and_split_ident(
            author_ident_raw.stdout.decode("utf-8").strip()
        )
        committer_name, committer_email = _parse_and_split_ident(
            committer_ident_raw.stdout.decode("utf-8").strip()
        )

        frozen_ts = int(time.time())
        frozen_date = f"{frozen_ts} +0000"
        author_ident = f"{author_name} <{author_email}> {frozen_ts} +0000"
        committer_ident = f"{committer_name} <{committer_email}> {frozen_ts} +0000"

        commit_message = _default_commit_message(package_bytes)
        commit_env = dict(env)
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": author_name,
                "GIT_AUTHOR_EMAIL": author_email,
                "GIT_AUTHOR_DATE": frozen_date,
                "GIT_COMMITTER_NAME": committer_name,
                "GIT_COMMITTER_EMAIL": committer_email,
                "GIT_COMMITTER_DATE": frozen_date,
            }
        )
        commit_message_bytes = commit_message.encode("utf-8")
        if b"\0" in commit_message_bytes:
            raise TransactionError(
                "prospective_commit_invalid", "commit message contains NUL"
            )
        commit_input = commit_message_bytes + b"\n"
        commit_tree = _run_scoped_git(
            ["commit-tree", tree_oid, "-p", identity.baseline_commit],
            commit_env,
            cwd=identity.root,
            input_bytes=commit_input,
        )
        if commit_tree.returncode != 0:
            raise TransactionError(
                "prospective_commit_invalid",
                f"git commit-tree failed: {commit_tree.stderr}",
            )
        commit_oid = commit_tree.stdout.decode("ascii").strip()

        verify = _run_scoped_git(
            ["cat-file", "commit", commit_oid], env, cwd=identity.root
        )
        if verify.returncode != 0:
            raise TransactionError(
                "prospective_commit_invalid",
                f"git cat-file could not verify commit: {verify.stderr}",
            )
        _parse_commit_object(
            verify.stdout,
            object_format=identity.object_format,
            tree_oid=tree_oid,
            parent_oid=identity.baseline_commit,
            author_ident=author_ident,
            committer_ident=committer_ident,
            message=commit_input,
        )

        baseline_tree = _run_scoped_git(
            ["rev-parse", f"{identity.baseline_commit}^{{tree}}"],
            env,
            cwd=identity.root,
        )
        if baseline_tree.returncode != 0:
            raise TransactionError(
                "prospective_tree_invalid",
                f"cannot resolve baseline tree: {baseline_tree.stderr}",
            )
        baseline_tree_oid = baseline_tree.stdout.decode("ascii").strip()

        object_records = _build_object_inventory(
            env, identity.root, tmp_dir, identity.object_format
        )
        try:
            inventory_digest = verify_object_inventory(
                object_records, identity.object_format
            )
        except CanonicalJsonError as exc:
            raise TransactionError("transaction_seal_failed", str(exc)) from exc
        object_count = len(object_records)
        state_digest = object_state_digest(
            identity.object_format, tree_oid, inventory_digest
        )

        title = _extract_package_field(package_bytes, "title")
        # The public builder resolves repository slug/default base internally,
        # so transaction.json is finalized with the complete approval payload.
        # The orchestrator only adds the manifest-level sealed_transaction
        # reference and approval digest/revision around this exact payload.
        payload = {
            "schema": 2,
            "run_id": run_id,
            "package_bytes_digest": package_bytes_digest(package_bytes),
            "body_bytes_digest": body_bytes_digest(body_bytes),
            "title": title,
            "commit_message": commit_message,
            "branch_ref": identity.branch_ref,
            "repository": {
                "worktree_id": identity.worktree_id,
                "root": str(identity.root),
                "git_dir": str(identity.git_dir),
                "common_dir": str(identity.common_dir),
                "object_format": identity.object_format,
                "repository_slug": repository_slug,
            },
            "pr_base_branch": pr_base_branch,
            "baseline_commit": identity.baseline_commit,
            "baseline_tree": baseline_tree_oid,
            "changed_entries": [
                {
                    "status": e["status"],
                    "score": e["score"],
                    "old_mode": e["old_mode"],
                    "new_mode": e["new_mode"],
                    "old_oid": e["old_oid"],
                    "new_oid": e["new_oid"],
                    "old_path": e["old_path"],
                    "new_path": e["new_path"],
                }
                for e in entries
            ],
            "tree_oid": tree_oid,
            "object_inventory_digest": inventory_digest,
            "object_state_digest": state_digest,
            "commit_oid": commit_oid,
            "author_ident": author_ident,
            "committer_ident": committer_ident,
            "timestamp": str(frozen_ts),
        }
        transaction_metadata = {
            "schema": 2,
            "payload": payload,
            "object_count": object_count,
        }
        atomic_write(
            tmp_dir / "transaction.json",
            canonical_json(transaction_metadata),
            mode=0o400,
            overwrite=False,
        )

        _finalize_transaction_permissions_and_fsync(tmp_dir)
        sealed_dir = tx_root / f"{run_id}.sealed"
        os.chmod(tmp_dir, 0o500)
        os.replace(tmp_dir, sealed_dir)
        _fsync_dir_best_effort(tx_root)
    except BaseException:
        with contextlib.suppress(OSError):
            _best_effort_rmtree(tmp_dir)
        raise

    return SealedTransaction(manifest=payload, directory=sealed_dir)


def _extract_package_field(package_bytes: bytes, field: str) -> str:
    try:
        pkg = _strict_json_loads(package_bytes)
        value = pkg.get(field)
        if (
            isinstance(value, str)
            and value
            and "\0" not in value
            and (field != "title" or ("\r" not in value and "\n" not in value))
        ):
            return value
    except (CanonicalJsonError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    raise TransactionError("package_invalid", f"package bytes do not carry a {field}")


def _default_commit_message(package_bytes: bytes) -> str:
    return _extract_package_field(package_bytes, "commit_message")


def _random_run_id() -> str:
    return "".join(f"{random.SystemRandom().randrange(16):x}" for _ in range(32))


_LOOSE_OBJECT_HEX_LEN = {"sha1": 40, "sha256": 64}


def _iter_loose_object_oids(git_dir: Path, object_format: str) -> list[str]:
    objects_dir = git_dir / "objects"
    width = _LOOSE_OBJECT_HEX_LEN.get(object_format)
    if width is None:
        raise TransactionError("transaction_seal_failed", "unsupported object format")
    expected_suffix_len = width - 2
    _lstat_existing_components(objects_dir)
    oids: list[str] = []
    for fan in sorted(objects_dir.iterdir()):
        fan_st = os.lstat(fan)
        if (
            not stat.S_ISDIR(fan_st.st_mode)
            or stat.S_ISLNK(fan_st.st_mode)
            or _is_reparse_stat(fan_st)
            or not re.fullmatch(r"[0-9a-f]{2}", fan.name)
        ):
            raise TransactionError(
                "transaction_seal_failed", f"unexpected loose-object entry: {fan.name}"
            )
        for obj_file in sorted(fan.iterdir()):
            obj_st = os.lstat(obj_file)
            if (
                not stat.S_ISREG(obj_st.st_mode)
                or stat.S_ISLNK(obj_st.st_mode)
                or _is_reparse_stat(obj_st)
                or not re.fullmatch(
                    rf"[0-9a-f]{{{expected_suffix_len}}}", obj_file.name
                )
            ):
                raise TransactionError(
                    "transaction_seal_failed",
                    f"unexpected loose-object file: {fan.name}/{obj_file.name}",
                )
            oids.append(fan.name + obj_file.name)
    return sorted(oids)


def _build_object_inventory(
    env: Mapping[str, str], cwd: Path, git_dir: Path, object_format: str
) -> list[tuple[str, str, bytes]]:
    """Every loose object physically present in the transaction's private
    object directory is, by construction, a new object required by the new
    commit and absent from the baseline reachable-object set: Git plumbing
    only writes a loose object when it does not already resolve via the
    scoped object dir or its alternates (PRP §5.1 step 6)."""
    records: list[tuple[str, str, bytes]] = []
    del env, cwd  # loose objects are verified directly, never through alternates
    for oid in _iter_loose_object_oids(git_dir, object_format):
        object_path = git_dir / "objects" / oid[:2] / oid[2:]
        type_ascii, content, _compressed = _decode_loose_object(
            object_path,
            oid,
            object_format,
            reason="transaction_seal_failed",
        )
        records.append((type_ascii, oid, content))
    return records


def _fsync_file_best_effort(path: Path) -> None:
    # Windows' CRT rejects fsync on a read-only descriptor; opening the
    # transaction-owned file read/write is required for the mandatory flush.
    if os.name == "nt":
        os.chmod(path, 0o600)
    fd = os.open(path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _chmod_required(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except (NotImplementedError, OSError):
        if os.name != "nt":
            raise


def _finalize_transaction_permissions_and_fsync(tmp_dir: Path) -> None:
    """fsync every file, best-effort fsync each directory bottom-up, chmod
    files 0400 and directories 0500 (PRP §5.1 step 6)."""
    for dirpath, dirnames, filenames in os.walk(tmp_dir, topdown=False):
        d = Path(dirpath)
        for name in filenames:
            fp = d / name
            _fsync_file_best_effort(fp)
            _chmod_required(fp, 0o400)
        for name in dirnames:
            sub = d / name
            _fsync_dir_best_effort(sub)
            _chmod_required(sub, 0o500)
        _fsync_dir_best_effort(d)


def _best_effort_rmtree(path: Path) -> None:
    import shutil

    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    for dirpath, dirnames, _filenames in os.walk(path):
        for name in dirnames:
            with contextlib.suppress(OSError):
                os.chmod(Path(dirpath) / name, 0o700)
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# §8/§9 ordered validators, public result type, and PR publication
# ---------------------------------------------------------------------------


class PublicationReason(StrEnum):
    # §8 package state/check table (12 rows)
    ARTIFACT_INVALID = "artifact_invalid"
    REPOSITORY_IDENTITY_MISMATCH = "repository_identity_mismatch"
    BASELINE_REVISION_MISMATCH = "baseline_revision_mismatch"
    PACKAGE_INVALID = "package_invalid"
    CHANGED_PATHS_MISMATCH = "changed_paths_mismatch"
    REGRESSION_STATE_MISMATCH = "regression_state_mismatch"
    PUBLICATION_LOCKED = "publication_locked"
    PROSPECTIVE_TREE_INVALID = "prospective_tree_invalid"
    GIT_OBJECT_STATE_MISMATCH = "git_object_state_mismatch"
    PROSPECTIVE_COMMIT_INVALID = "prospective_commit_invalid"
    TRANSACTION_SEAL_FAILED = "transaction_seal_failed"
    PACKAGE_CHANGED_DURING_SEAL = "package_changed_during_seal"
    # §9 publish state/check table (new tokens)
    APPROVAL_BINDING_INVALID = "approval_binding_invalid"
    LOCAL_REF_DIVERGED = "local_ref_diverged"
    REMOTE_DIVERGED = "remote_diverged"
    APPROVED_CONTENT_CHANGED = "approved_content_changed"
    SEALED_TRANSACTION_INVALID = "sealed_transaction_invalid"
    COMMIT_TRANSITION_INVALID = "commit_transition_invalid"
    PRE_SIDE_EFFECT_REVALIDATION_FAILED = "pre_side_effect_revalidation_failed"
    OBJECT_INSTALL_FAILED = "object_install_failed"
    LOCAL_REF_UPDATE_FAILED = "local_ref_update_failed"
    PUSH_FAILED = "push_failed"
    REMOTE_VERIFICATION_FAILED = "remote_verification_failed"
    REMOTE_PR_CONFLICT = "remote_pr_conflict"
    REMOTE_PR_AMBIGUOUS = "remote_pr_ambiguous"
    PR_CREATE_FAILED = "pr_create_failed"
    REMOTE_PR_BODY_MISMATCH = "remote_pr_body_mismatch"
    PUBLICATION_RESULT_INVALID = "publication_result_invalid"
    # §9 decision-matrix/prose reject reasons
    PR_STATE_IMPOSSIBLE = "pr_state_impossible"
    REMOTE_CREATION_UNSUPPORTED = "remote_creation_unsupported"


@dataclass(frozen=True)
class PublicationResult:
    ok: bool
    reason: PublicationReason | None
    detail: str
    value: object | None = None


def _fail(
    reason: PublicationReason, detail: str, *, value: object | None = None
) -> PublicationResult:
    return PublicationResult(False, reason, detail, value=value)


_APPROVAL_MANIFEST_KEYS = {
    "schema",
    "approval_revision",
    "approval_digest",
    "payload",
    "sealed_transaction",
}
_APPROVAL_PAYLOAD_KEYS = {
    "schema",
    "run_id",
    "package_bytes_digest",
    "body_bytes_digest",
    "title",
    "commit_message",
    "branch_ref",
    "repository",
    "pr_base_branch",
    "baseline_commit",
    "baseline_tree",
    "changed_entries",
    "tree_oid",
    "object_inventory_digest",
    "object_state_digest",
    "commit_oid",
    "author_ident",
    "committer_ident",
    "timestamp",
}
_SEALED_TRANSACTION_KEYS = {"path", "package", "body", "metadata", "object_count"}
_REPOSITORY_PAYLOAD_KEYS = {
    "worktree_id",
    "root",
    "git_dir",
    "common_dir",
    "object_format",
    "repository_slug",
}


def _confined_sealed_transaction_path(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    if chr(92) in raw or ":" in raw:
        return None
    p = PurePosixPath(raw)
    if p.is_absolute() or ".." in p.parts:
        return None
    return raw


def _valid_payload_shape(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != _APPROVAL_PAYLOAD_KEYS:
        return False
    repository = payload.get("repository")
    object_format = (
        repository.get("object_format") if isinstance(repository, dict) else None
    )
    width = {"sha1": 40, "sha256": 64}.get(object_format)
    oid_re = re.compile(rf"[0-9a-f]{{{width}}}") if width else None
    if (
        payload.get("schema") != 2
        or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("run_id", "")))
        or not isinstance(repository, dict)
        or set(repository) != _REPOSITORY_PAYLOAD_KEYS
        or not re.fullmatch(r"[0-9a-f]{64}", str(repository.get("worktree_id", "")))
        or not re.fullmatch(
            r"[^/\s]+/[^/\s]+", str(repository.get("repository_slug", ""))
        )
        or any(
            not Path(str(repository.get(k, ""))).is_absolute()
            for k in ("root", "git_dir", "common_dir")
        )
        or not str(payload.get("branch_ref", "")).startswith("refs/heads/")
        or not isinstance(payload.get("pr_base_branch"), str)
        or not payload["pr_base_branch"]
        or oid_re is None
        or any(
            oid_re.fullmatch(str(payload.get(k, ""))) is None
            for k in ("baseline_commit", "baseline_tree", "tree_oid", "commit_oid")
        )
        or any(
            re.fullmatch(r"[0-9a-f]{64}", str(payload.get(k, ""))) is None
            for k in (
                "package_bytes_digest",
                "body_bytes_digest",
                "object_inventory_digest",
                "object_state_digest",
            )
        )
        or not isinstance(payload.get("timestamp"), str)
        or not payload["timestamp"].isdigit()
        or not isinstance(payload.get("title"), str)
        or not payload["title"]
        or any(c in payload["title"] for c in "\r\n\0")
        or not isinstance(payload.get("commit_message"), str)
        or not payload["commit_message"]
        or "\0" in payload["commit_message"]
        or not isinstance(payload.get("changed_entries"), list)
    ):
        return False
    try:
        reparsed = _parse_diff_tree_raw_z(
            _encode_changed_entries_as_raw(payload["changed_entries"]), object_format
        )
    except (TransactionError, TypeError):
        return False
    return reparsed == payload["changed_entries"]


def _encode_changed_entries_as_raw(entries: Sequence[Mapping[str, object]]) -> str:
    """Round-trip schema validation through the normative diff grammar."""
    tokens: list[str] = []
    for entry in entries:
        status = entry["status"]
        suffix = status + (entry["score"] or "")
        tokens.append(
            ":"
            + " ".join(
                [
                    entry["old_mode"],
                    entry["new_mode"],
                    entry["old_oid"],
                    entry["new_oid"],
                    suffix,
                ]
            )
        )
        if status == "R":
            tokens.extend([entry["old_path"], entry["new_path"]])
        else:
            tokens.append(entry["new_path"])
    return "\0".join(tokens) + ("\0" if tokens else "")


def validate_sealed_transaction(
    identity: RepositoryIdentity,
    artifacts_dir: Path,
    manifest_bytes: bytes,
    package_bytes: bytes,
    body_bytes: bytes,
) -> PublicationResult:
    """Re-verify a schema-2 `approval-manifest.json`'s internal consistency
    and its binding to `package_bytes`/`body_bytes`/`identity`/the sealed
    transaction on disk (PRP §6/§8 row 12, reused at §9 rows 2/5/6/8). Never
    performs a repository publication side effect."""
    artifacts_dir = Path(artifacts_dir)
    try:
        manifest = _strict_json_loads(manifest_bytes)
    except (CanonicalJsonError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            f"manifest is not valid JSON: {exc}",
        )
    if not isinstance(manifest, dict) or set(manifest) != _APPROVAL_MANIFEST_KEYS:
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID, "manifest: unexpected key set"
        )
    if manifest.get("schema") != 2:
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "manifest: schema 1 is rejected outright, no implicit upgrade",
        )
    payload = manifest["payload"]
    if not _valid_payload_shape(payload):
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "payload: exact schema/types/entries are invalid",
        )
    sealed_ref = manifest["sealed_transaction"]
    if not isinstance(sealed_ref, dict) or set(sealed_ref) != _SEALED_TRANSACTION_KEYS:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed_transaction: unexpected key set",
        )
    sealed_path = _confined_sealed_transaction_path(sealed_ref.get("path"))
    if sealed_path is None:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed_transaction: unconfined path",
        )
    for name_key in ("package", "body", "metadata"):
        if (
            sealed_ref.get(name_key)
            != {
                "package": "sealed-pr-package.json",
                "body": "sealed-pr-body.md",
                "metadata": "transaction.json",
            }[name_key]
        ):
            return _fail(
                PublicationReason.SEALED_TRANSACTION_INVALID,
                f"sealed_transaction: unexpected {name_key} name",
            )

    expected_sealed_path = f"publication-transactions/{payload['run_id']}.sealed"
    if sealed_path != expected_sealed_path:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed_transaction path does not bind run_id",
        )

    repository = payload.get("repository")
    if not isinstance(repository, dict) or set(repository) != _REPOSITORY_PAYLOAD_KEYS:
        return _fail(
            PublicationReason.REPOSITORY_IDENTITY_MISMATCH,
            "payload.repository: unexpected key set",
        )
    approved_worktree_id = _compute_worktree_id(
        root=repository["root"],
        git_dir=repository["git_dir"],
        common_dir=repository["common_dir"],
        branch_ref=payload["branch_ref"],
        baseline_commit=payload["baseline_commit"],
        object_format=repository["object_format"],
    )
    if approved_worktree_id != repository["worktree_id"]:
        return _fail(
            PublicationReason.REPOSITORY_IDENTITY_MISMATCH,
            "approved immutable worktree_id does not recompute",
        )
    # Compare live structural state separately from the approved immutable
    # identity. Live HEAD is intentionally classified later as baseline,
    # expected, or other.
    if (
        repository.get("root") != str(identity.root)
        or repository.get("git_dir") != str(identity.git_dir)
        or repository.get("common_dir") != str(identity.common_dir)
        or repository.get("object_format") != identity.object_format
        or payload.get("branch_ref") != identity.branch_ref
    ):
        return _fail(
            PublicationReason.REPOSITORY_IDENTITY_MISMATCH,
            "payload repository identity differs from freshly rediscovered identity",
        )

    recomputed_approval_digest = approval_digest(payload)
    if recomputed_approval_digest != manifest.get("approval_digest"):
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "approval_digest does not recompute",
        )
    expected_revision = f"2:{payload.get('run_id')}:{recomputed_approval_digest[:16]}"
    if manifest.get("approval_revision") != expected_revision:
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "approval_revision does not recompute",
        )

    if payload.get("package_bytes_digest") != package_bytes_digest(package_bytes):
        return _fail(
            PublicationReason.APPROVED_CONTENT_CHANGED,
            "package bytes changed since approval",
        )
    if payload.get("body_bytes_digest") != body_bytes_digest(body_bytes):
        return _fail(
            PublicationReason.APPROVED_CONTENT_CHANGED,
            "body bytes changed since approval",
        )
    try:
        body_bytes.decode("utf-8", errors="strict")
        package_title = _extract_package_field(package_bytes, "title")
        package_message = _extract_package_field(package_bytes, "commit_message")
    except (UnicodeDecodeError, TransactionError) as exc:
        return _fail(PublicationReason.APPROVED_CONTENT_CHANGED, str(exc))
    if (
        not body_bytes
        or b"\0" in body_bytes
        or package_title != payload["title"]
        or package_message != payload["commit_message"]
        or payload["branch_ref"] != identity.branch_ref
    ):
        return _fail(
            PublicationReason.APPROVED_CONTENT_CHANGED,
            "sealed package/body semantic fields differ from approval",
        )

    # NOTE: whether live HEAD equals `baseline_commit` (unchanged) or
    # `commit_oid` (already installed/resumed) or neither (a concurrent
    # writer) is deliberately NOT checked here. `publish_sealed_transaction`
    # performs that classification itself via `_classify_local` immediately
    # after this call, with the PRP §9 row-4-specific reasons
    # (`local_ref_diverged`/`remote_diverged`) -- duplicating it here with a
    # different reason token would make whichever check runs first shadow
    # the other's more specific diagnosis. The package-gate orchestrator
    # (§8 row 3, `baseline_revision_mismatch`) performs the equivalent
    # check on its own side, before ever calling into this module.

    sealed_dir = artifacts_dir / sealed_path
    try:
        _lstat_existing_components(sealed_dir)
        sealed_st = os.lstat(sealed_dir)
    except OSError:
        sealed_st = None
    if (
        sealed_st is None
        or not stat.S_ISDIR(sealed_st.st_mode)
        or _is_reparse_stat(sealed_st)
    ):
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed transaction directory is missing",
        )
    try:
        if {p.name for p in sealed_dir.iterdir()} != {
            "index",
            "objects",
            "sealed-pr-package.json",
            "sealed-pr-body.md",
            "transaction.json",
        }:
            raise OSError("sealed directory has an unexpected entry set")
        _validate_confined_regular_file(sealed_dir / "index", sealed_dir)
        _validate_confined_real_directory(sealed_dir / "objects", sealed_dir)
        sealed_package = read_confined_regular_bytes(
            sealed_dir / "sealed-pr-package.json"
        )
        sealed_body = read_confined_regular_bytes(sealed_dir / "sealed-pr-body.md")
        metadata_bytes = read_confined_regular_bytes(sealed_dir / "transaction.json")
    except OSError as exc:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            f"sealed files unreadable: {exc}",
        )
    if sealed_package != package_bytes or sealed_body != body_bytes:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed package/body bytes differ from the supplied bytes",
        )
    try:
        metadata = _strict_json_loads(metadata_bytes)
    except (CanonicalJsonError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID, f"invalid metadata: {exc}"
        )
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"schema", "payload", "object_count"}
        or metadata.get("schema") != 2
        or not isinstance(metadata.get("payload"), dict)
        or not isinstance(metadata.get("object_count"), int)
        or isinstance(metadata.get("object_count"), bool)
        or metadata["object_count"] < 0
        or sealed_ref.get("object_count") != metadata["object_count"]
        or metadata_bytes != canonical_json(metadata)
        or metadata.get("payload") != payload
    ):
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "transaction metadata mismatch",
        )

    env = _scoped_git_env(
        git_dir=identity.git_dir,
        work_tree=identity.root,
        index_file=sealed_dir / "index",
        object_dir=sealed_dir / "objects",
        alt_object_dirs=identity.common_dir / "objects",
    )
    try:
        records = _build_object_inventory(
            env, identity.root, sealed_dir, identity.object_format
        )
        inventory_digest = verify_object_inventory(records, identity.object_format)
    except (TransactionError, CanonicalJsonError) as exc:
        return _fail(PublicationReason.GIT_OBJECT_STATE_MISMATCH, str(exc))
    if len(records) != metadata["object_count"] or inventory_digest != payload.get(
        "object_inventory_digest"
    ):
        return _fail(
            PublicationReason.GIT_OBJECT_STATE_MISMATCH,
            "object_inventory_digest does not recompute",
        )

    baseline_tree = _run_scoped_git(
        ["rev-parse", f"{payload['baseline_commit']}^{{tree}}"],
        env,
        cwd=identity.root,
    )
    if (
        baseline_tree.returncode != 0
        or baseline_tree.stdout.decode("ascii", errors="replace").strip()
        != payload["baseline_tree"]
    ):
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "baseline tree does not recompute",
        )
    diff_tree = _run_scoped_git(
        [
            "diff-tree",
            "--root",
            "-r",
            "-z",
            "--raw",
            "--no-abbrev",
            "--find-renames=50%",
            payload["baseline_commit"],
            payload["tree_oid"],
        ],
        env,
        cwd=identity.root,
    )
    try:
        observed_entries = _parse_diff_tree_raw_z(
            diff_tree.stdout, identity.object_format
        )
    except TransactionError as exc:
        return _fail(PublicationReason.SEALED_TRANSACTION_INVALID, str(exc))
    if diff_tree.returncode != 0 or observed_entries != payload["changed_entries"]:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "tree-derived changed entries do not exactly recompute",
        )
    state_digest = object_state_digest(
        identity.object_format, payload.get("tree_oid"), inventory_digest
    )
    if state_digest != payload.get("object_state_digest"):
        return _fail(
            PublicationReason.GIT_OBJECT_STATE_MISMATCH,
            "object_state_digest does not recompute",
        )

    verify = _run_scoped_git(
        ["cat-file", "commit", payload.get("commit_oid")], env, cwd=identity.root
    )
    if verify.returncode != 0:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed commit does not rehash/parse",
        )
    commit_content = verify.stdout
    commit_raw = f"commit {len(commit_content)}\0".encode("ascii") + commit_content
    if (
        hashlib.new(identity.object_format, commit_raw).hexdigest()
        != payload["commit_oid"]
    ):
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed commit content does not hash to the approved commit",
        )
    try:
        _parse_commit_object(
            verify.stdout,
            object_format=identity.object_format,
            tree_oid=payload["tree_oid"],
            parent_oid=payload["baseline_commit"],
            author_ident=payload["author_ident"],
            committer_ident=payload["committer_ident"],
            message=payload["commit_message"].encode("utf-8") + b"\n",
        )
    except TransactionError as exc:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            str(exc),
        )

    return PublicationResult(True, None, "", value=payload)


def _decode_loose_object(
    path: Path,
    oid: str,
    object_format: str,
    *,
    reason: str = "object_install_failed",
) -> tuple[str, bytes, bytes]:
    try:
        width = _LOOSE_OBJECT_HEX_LEN.get(object_format)
        if width is None or re.fullmatch(rf"[0-9a-f]{{{width}}}", oid) is None:
            raise ValueError("object name does not match repository format")
        compressed = read_confined_regular_bytes(path, max_bytes=512 * 1024 * 1024)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed)
        raw += decompressor.flush()
        if (
            not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise ValueError("loose object has an incomplete or trailing zlib stream")
        header, content = raw.split(b"\0", 1)
        type_bytes, size_bytes = header.split(b" ", 1)
        type_ascii = type_bytes.decode("ascii", errors="strict")
        if (
            type_ascii not in _SUPPORTED_OBJECT_TYPES
            or not re.fullmatch(rb"0|[1-9][0-9]*", size_bytes)
            or int(size_bytes) != len(content)
        ):
            raise ValueError("invalid object header")
        expected = hashlib.new(object_format, raw).hexdigest()
        if expected != oid:
            raise ValueError("object hash/format mismatch")
        return type_ascii, content, compressed
    except (OSError, ValueError, UnicodeError, zlib.error) as exc:
        raise TransactionError(reason, f"invalid loose object {oid}: {exc}") from exc


def _install_sealed_objects(
    sealed_dir: Path, common_dir: Path, object_format: str
) -> int:
    src_objects = sealed_dir / "objects"
    dst_objects = common_dir / "objects"
    _lstat_existing_components(src_objects)
    _lstat_existing_components(dst_objects)
    installed = 0
    width = _LOOSE_OBJECT_HEX_LEN[object_format]
    for fan in sorted(src_objects.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{2}", fan.name):
            raise TransactionError("object_install_failed", "invalid source fanout")
        fan_st = os.lstat(fan)
        if not stat.S_ISDIR(fan_st.st_mode) or _is_reparse_stat(fan_st):
            raise TransactionError("object_install_failed", "unsafe source fanout")
        dst_fan = dst_objects / fan.name
        try:
            dst_fan.mkdir(mode=0o755)
        except FileExistsError:
            pass
        _lstat_existing_components(dst_fan)
        dst_fan_st = os.lstat(dst_fan)
        if not stat.S_ISDIR(dst_fan_st.st_mode) or _is_reparse_stat(dst_fan_st):
            raise TransactionError("object_install_failed", "unsafe destination fanout")
        for obj_file in sorted(fan.iterdir()):
            if not re.fullmatch(rf"[0-9a-f]{{{width - 2}}}", obj_file.name):
                raise TransactionError(
                    "object_install_failed", "invalid source object name"
                )
            oid = fan.name + obj_file.name
            dst_file = dst_fan / obj_file.name
            src_type, src_content, data = _decode_loose_object(
                obj_file, oid, object_format
            )
            if _destination_stat(dst_file) is not None:
                dst_type, dst_content, _ = _decode_loose_object(
                    dst_file, oid, object_format
                )
                if dst_type != src_type or dst_content != src_content:
                    raise TransactionError(
                        "object_install_failed",
                        f"object collision: {oid}",
                    )
                continue
            try:
                atomic_write(dst_file, data, mode=0o444, overwrite=False)
                installed += 1
            except FileExistsError:
                # A concurrent installer won. Reconcile by fully parsing and
                # rehashing its destination; never overwrite it.
                dst_type, dst_content, _ = _decode_loose_object(
                    dst_file, oid, object_format
                )
                if dst_type != src_type or dst_content != src_content:
                    raise TransactionError(
                        "object_install_failed", f"concurrent object collision: {oid}"
                    )
            dst_type, dst_content, _ = _decode_loose_object(
                dst_file, oid, object_format
            )
            if dst_type != src_type or dst_content != src_content:
                raise TransactionError(
                    "object_install_failed", f"installed object mismatch: {oid}"
                )
            _fsync_dir_best_effort(dst_fan)
    _fsync_dir_best_effort(dst_objects)
    return installed


def _run_plain_git(
    args: Sequence[str],
    cwd: Path,
    *,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_bytes,
        capture_output=True,
        shell=False,
        timeout=timeout,
    )


def _cas_update_ref(
    root: Path, branch_ref: str, new_oid: str, old_oid: str
) -> subprocess.CompletedProcess:
    return _run_plain_git(["update-ref", branch_ref, new_oid, old_oid], root)


def _ls_remote_oid(root: Path, branch_ref: str) -> str | None:
    try:
        result = _run_plain_git(
            ["ls-remote", "--exit-code", "--refs", "origin", branch_ref],
            root,
            timeout=_NETWORK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransactionError(
            "remote_verification_failed", "git ls-remote timed out"
        ) from exc
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors="replace")
        raise TransactionError(
            "remote_verification_failed", f"git ls-remote failed: {stderr_text}"
        )
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    return text.splitlines()[0].split("\t")[0]


def _push_with_lease(
    root: Path, expected_commit: str, branch_ref: str, lease_value: str | None
):
    lease = f"--force-with-lease={branch_ref}:{lease_value or ''}"
    return _run_plain_git(
        [
            "push",
            "--porcelain",
            "--set-upstream",
            "origin",
            f"{expected_commit}:{branch_ref}",
            lease,
        ],
        root,
        timeout=_NETWORK_TIMEOUT_SECONDS,
    )


def _gh_json(args: Sequence[str], cwd: Path) -> tuple[bool, object]:
    try:
        result = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            capture_output=True,
            shell=False,
            timeout=_NETWORK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"gh invocation failed: {exc}"
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", errors="replace")
    try:
        return True, _strict_json_loads(result.stdout)
    except (CanonicalJsonError, json.JSONDecodeError, UnicodeDecodeError):
        return False, "gh returned non-JSON output"


def _derive_origin_repository_slug(root: Path) -> str:
    result = _run_plain_git(["remote", "get-url", "origin"], root)
    if result.returncode != 0:
        raise TransactionError("repository_identity_mismatch", "origin is unavailable")
    try:
        url = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise TransactionError(
            "repository_identity_mismatch", "origin URL is not UTF-8"
        ) from exc
    match = re.fullmatch(
        r"(?:https://|ssh://git@|git@)github\.com(?::|/)([^/\s]+)/([^/\s]+?)(?:\.git)?/?",
        url,
    )
    if not match:
        raise TransactionError(
            "repository_identity_mismatch",
            "origin is not a canonical GitHub repository URL",
        )
    return f"{match.group(1)}/{match.group(2)}"


def _classify_local(identity: RepositoryIdentity, payload: Mapping[str, object]) -> str:
    if identity.baseline_commit == payload["baseline_commit"]:
        return "baseline"
    if identity.baseline_commit == payload["commit_oid"]:
        return "expected"
    return "other"


def _classify_remote(remote_oid: str | None, payload: Mapping[str, object]) -> str:
    if remote_oid is None:
        return "absent"
    if remote_oid == payload["baseline_commit"]:
        return "baseline"
    if remote_oid == payload["commit_oid"]:
        return "expected"
    return "other"


def _classify_pr(
    identity: RepositoryIdentity,
    payload: Mapping[str, object],
    repository_slug: str,
    approved_body_bytes: bytes,
) -> tuple[str, object]:
    head_owner, _, head_repository = repository_slug.partition("/")
    short_branch = identity.branch_ref.removeprefix("refs/heads/")
    ok, data = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repository_slug,
            "--state",
            "open",
            "--head",
            f"{head_owner}:{short_branch}",
            "--base",
            payload["pr_base_branch"],
            "--json",
            "number,url,headRefName,headRefOid,headRepositoryOwner,headRepository,baseRefName,title,body",
        ],
        identity.root,
    )
    if not ok or not isinstance(data, list):
        return "ambiguous", None
    if len(data) == 0:
        return "none", None
    if len(data) > 1:
        return "ambiguous", None
    pr = data[0]
    required = {
        "number",
        "url",
        "headRefName",
        "headRefOid",
        "headRepositoryOwner",
        "headRepository",
        "baseRefName",
        "title",
        "body",
    }
    if (
        not isinstance(pr, dict)
        or set(pr) != required
        or not isinstance(pr.get("number"), int)
        or isinstance(pr.get("number"), bool)
        or pr["number"] <= 0
        or any(
            not isinstance(pr.get(key), str)
            for key in (
                "url",
                "headRefName",
                "headRefOid",
                "baseRefName",
                "title",
                "body",
            )
        )
        or not isinstance(pr.get("headRepositoryOwner"), dict)
        or set(pr["headRepositoryOwner"]) != {"login"}
        or not isinstance(pr["headRepositoryOwner"].get("login"), str)
        or not isinstance(pr.get("headRepository"), dict)
        or set(pr["headRepository"]) != {"name"}
        or not isinstance(pr["headRepository"].get("name"), str)
    ):
        return "ambiguous", pr if isinstance(pr, dict) else None
    try:
        body_bytes_observed = pr["body"].encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "ambiguous", pr
    same_non_body = (
        pr["headRepositoryOwner"].get("login") == head_owner
        and pr["headRepository"].get("name") == head_repository
        and pr.get("url") == f"https://github.com/{repository_slug}/pull/{pr['number']}"
        and pr.get("headRefOid") == payload["commit_oid"]
        and pr.get("baseRefName") == payload["pr_base_branch"]
        and pr.get("headRefName") == short_branch
        and pr.get("title") == payload["title"]
    )
    if not same_non_body:
        return "conflict", pr
    if (
        body_bytes_observed != approved_body_bytes
        or body_bytes_digest(body_bytes_observed) != payload["body_bytes_digest"]
    ):
        return "body_mismatch", pr
    return "exact", pr


@dataclass(frozen=True)
class _FreshObservation:
    identity: RepositoryIdentity
    local_kind: str
    remote_kind: str
    remote_oid: str | None
    pr_kind: str
    pr: Mapping[str, object] | None


def _precheck_approval_binding(manifest_bytes: bytes) -> PublicationResult:
    """Validate publish row 2 without consulting mutable repository state."""
    try:
        manifest = _strict_json_loads(manifest_bytes)
    except (CanonicalJsonError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            f"manifest is not valid JSON: {exc}",
        )
    try:
        canonical_manifest_bytes = canonical_json(manifest)
    except CanonicalJsonError as exc:
        return _fail(PublicationReason.APPROVAL_BINDING_INVALID, str(exc))
    if manifest_bytes != canonical_manifest_bytes:
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "manifest bytes are not canonical JSON",
        )
    if not isinstance(manifest, dict) or set(manifest) != _APPROVAL_MANIFEST_KEYS:
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "manifest: unexpected key set",
        )
    payload = manifest.get("payload")
    if manifest.get("schema") != 2 or not _valid_payload_shape(payload):
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "manifest schema/payload is invalid",
        )
    assert isinstance(payload, dict)
    digest = approval_digest(payload)
    expected_revision = f"2:{payload['run_id']}:{digest[:16]}"
    if (
        manifest.get("approval_digest") != digest
        or manifest.get("approval_revision") != expected_revision
    ):
        return _fail(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "approval digest/revision does not recompute",
        )
    sealed = manifest.get("sealed_transaction")
    if not isinstance(sealed, dict) or set(sealed) != _SEALED_TRANSACTION_KEYS:
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed_transaction: unexpected key set",
        )
    sealed_path = _confined_sealed_transaction_path(sealed.get("path"))
    if (
        sealed_path != f"publication-transactions/{payload['run_id']}.sealed"
        or sealed.get("package") != "sealed-pr-package.json"
        or sealed.get("body") != "sealed-pr-body.md"
        or sealed.get("metadata") != "transaction.json"
        or not isinstance(sealed.get("object_count"), int)
        or isinstance(sealed.get("object_count"), bool)
        or sealed["object_count"] < 0
    ):
        return _fail(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "sealed_transaction names/path/count are invalid",
        )
    return PublicationResult(True, None, "", value=manifest)


def _observe_rows_2_to_7(
    invocation_root: Path,
    artifacts_dir: Path,
    manifest_bytes: bytes,
    package_bytes: bytes,
    body_bytes: bytes,
    payload: Mapping[str, object],
    sealed_dir: Path,
) -> PublicationResult:
    """One reusable ordered observation of rows 2-7 plus qualified PR state."""
    binding = _precheck_approval_binding(manifest_bytes)
    if not binding.ok:
        return binding

    try:
        fresh = discover_repository(invocation_root)
    except RepositoryIdentityError as exc:
        return _fail(PublicationReason.REPOSITORY_IDENTITY_MISMATCH, str(exc))

    repository_slug = payload["repository"]["repository_slug"]
    try:
        observed_slug = _derive_origin_repository_slug(fresh.root)
    except (OSError, TransactionError) as exc:
        return _fail(PublicationReason.REPOSITORY_IDENTITY_MISMATCH, str(exc))
    if observed_slug != repository_slug:
        return _fail(
            PublicationReason.REPOSITORY_IDENTITY_MISMATCH,
            "canonical origin repository slug differs from approval",
        )
    ok, gh_repo = _gh_json(
        [
            "repo",
            "view",
            "--repo",
            repository_slug,
            "--json",
            "nameWithOwner,defaultBranchRef",
        ],
        fresh.root,
    )
    if (
        not ok
        or not isinstance(gh_repo, dict)
        or set(gh_repo) != {"nameWithOwner", "defaultBranchRef"}
        or gh_repo.get("nameWithOwner") != repository_slug
        or not isinstance(gh_repo.get("defaultBranchRef"), dict)
        or set(gh_repo["defaultBranchRef"]) != {"name"}
        or gh_repo["defaultBranchRef"].get("name") != payload["pr_base_branch"]
    ):
        return _fail(
            PublicationReason.REPOSITORY_IDENTITY_MISMATCH,
            "gh repository identity/default branch differs from approval",
        )

    local_kind = _classify_local(fresh, payload)
    if local_kind == "other":
        return _fail(
            PublicationReason.LOCAL_REF_DIVERGED,
            "local ref is neither baseline nor expected",
        )
    try:
        remote_oid = _ls_remote_oid(fresh.root, payload["branch_ref"])
    except TransactionError as exc:
        return _fail(PublicationReason.REMOTE_VERIFICATION_FAILED, str(exc))
    remote_kind = _classify_remote(remote_oid, payload)
    if remote_kind == "other":
        return _fail(
            PublicationReason.REMOTE_DIVERGED,
            "remote ref is neither absent/baseline/expected",
        )

    # Rows 5-6 bind the already-open package/body snapshots to every sealed
    # file and object. This call intentionally occurs once per observation.
    validation = validate_sealed_transaction(
        fresh, artifacts_dir, manifest_bytes, package_bytes, body_bytes
    )
    if not validation.ok:
        return validation

    env = _scoped_git_env(
        git_dir=fresh.git_dir,
        work_tree=fresh.root,
        index_file=sealed_dir / "index",
        object_dir=sealed_dir / "objects",
        alt_object_dirs=fresh.common_dir / "objects",
    )
    try:
        transition = _run_scoped_git(
            [
                "merge-base",
                "--is-ancestor",
                payload["baseline_commit"],
                payload["commit_oid"],
            ],
            env,
            cwd=fresh.root,
        )
    except OSError as exc:
        return _fail(PublicationReason.COMMIT_TRANSITION_INVALID, str(exc))
    if transition.returncode != 0:
        return _fail(
            PublicationReason.COMMIT_TRANSITION_INVALID,
            "approved commit is not a baseline fast-forward",
        )

    # The qualified PR observation is coupled to the same fresh identity/ref
    # snapshot and is never reused after a publication side effect.
    pr_kind, pr = _classify_pr(
        fresh, payload, repository_slug, approved_body_bytes=body_bytes
    )
    observation = _FreshObservation(
        fresh, local_kind, remote_kind, remote_oid, pr_kind, pr
    )
    if pr_kind == "conflict":
        return _fail(
            PublicationReason.REMOTE_PR_CONFLICT,
            "open PR conflicts",
            value=observation,
        )
    if pr_kind == "body_mismatch":
        return _fail(
            PublicationReason.REMOTE_PR_BODY_MISMATCH,
            "open PR body differs; manual reconciliation required",
            value=observation,
        )
    if pr_kind == "ambiguous":
        return _fail(
            PublicationReason.REMOTE_PR_AMBIGUOUS,
            "PR query is ambiguous",
            value=observation,
        )
    if pr_kind == "exact" and remote_kind in {"absent", "baseline"}:
        return _fail(
            PublicationReason.PR_STATE_IMPOSSIBLE,
            "exact PR exists without its expected remote head",
            value=observation,
        )
    return PublicationResult(
        True,
        None,
        "",
        value=_FreshObservation(
            fresh, local_kind, remote_kind, remote_oid, pr_kind, pr
        ),
    )


def publish_sealed_transaction(
    identity: RepositoryIdentity, artifacts_dir: Path
) -> PublicationResult:
    """Revalidate a sealed publication transaction and, only after every
    check passes, install objects/CAS the ref/push with a lease/create the
    PR (PRP §9). Rows 1-8 have zero repository/remote side effects."""
    artifacts_dir = Path(artifacts_dir)
    try:
        with acquire_repository_lock(
            artifacts_dir,
            identity.common_dir,
            identity.branch_ref,
            run_id="publish-pr",
        ):
            return _publish_locked(identity, artifacts_dir)
    except PublicationLockedError as exc:
        state = {
            "schema": 1,
            "approval_digest": None,
            "state": "validating",
            "local_oid": identity.baseline_commit,
            "remote_kind": None,
            "remote_oid": None,
            "pr_kind": None,
            "pr_url": None,
            "last_reason": PublicationReason.PUBLICATION_LOCKED.value,
        }
        _write_publication_state(artifacts_dir, state)
        return _fail(PublicationReason.PUBLICATION_LOCKED, str(exc))


def _publish_locked(
    identity: RepositoryIdentity, artifacts_dir: Path
) -> PublicationResult:
    state: dict[str, object] = {
        "schema": 1,
        "approval_digest": None,
        "state": "validating",
        "local_oid": None,
        "remote_kind": None,
        "remote_oid": None,
        "pr_kind": None,
        "pr_url": None,
        "last_reason": None,
    }

    def terminal(
        reason: PublicationReason,
        detail: str,
        observation: _FreshObservation | None = None,
        pr: Mapping[str, object] | None = None,
        pr_kind: str | None = None,
    ) -> PublicationResult:
        def safe_pr_url(candidate: Mapping[str, object] | None) -> str | None:
            url = candidate.get("url") if candidate is not None else None
            return url if isinstance(url, str) else None

        if observation is not None:
            state.update(
                {
                    "local_oid": observation.identity.baseline_commit,
                    "remote_kind": observation.remote_kind,
                    "remote_oid": observation.remote_oid,
                    "pr_kind": observation.pr_kind,
                    "pr_url": safe_pr_url(observation.pr),
                }
            )
        if pr_kind is not None:
            state["pr_kind"] = pr_kind
        if pr is not None:
            state["pr_url"] = safe_pr_url(pr)
        state["last_reason"] = reason.value
        _write_publication_state(artifacts_dir, state)
        return _fail(reason, detail)

    # The approval manifest pathname is opened exactly once per invocation,
    # only after the repository-scoped publication lock is held. Both ordered
    # validation passes consume this same immutable byte snapshot.
    try:
        manifest_bytes = read_confined_regular_bytes(
            artifacts_dir / "approval-manifest.json"
        )
        manifest = _strict_json_loads(manifest_bytes)
        if manifest_bytes != canonical_json(manifest):
            raise CanonicalJsonError("approval manifest bytes are not canonical JSON")
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        CanonicalJsonError,
    ) as exc:
        return terminal(PublicationReason.APPROVAL_BINDING_INVALID, str(exc))
    if not isinstance(manifest, dict):
        return terminal(
            PublicationReason.APPROVAL_BINDING_INVALID,
            "approval manifest must be a JSON object",
        )
    state["approval_digest"] = manifest.get("approval_digest")
    payload = manifest.get("payload")
    if not isinstance(payload, dict):
        return terminal(
            PublicationReason.APPROVAL_BINDING_INVALID, "manifest has no payload"
        )

    sealed_ref = manifest.get("sealed_transaction", {})
    sealed_path = _confined_sealed_transaction_path(
        sealed_ref.get("path") if isinstance(sealed_ref, dict) else None
    )
    if sealed_path is None:
        return terminal(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            "unconfined sealed_transaction path",
        )
    sealed_dir = artifacts_dir / sealed_path
    try:
        package_bytes = read_confined_regular_bytes(
            sealed_dir / "sealed-pr-package.json"
        )
        body_bytes = read_confined_regular_bytes(sealed_dir / "sealed-pr-body.md")
    except OSError as exc:
        return terminal(
            PublicationReason.SEALED_TRANSACTION_INVALID,
            f"cannot read sealed bytes: {exc}",
        )

    first_pass = _observe_rows_2_to_7(
        identity.root,
        artifacts_dir,
        manifest_bytes,
        package_bytes,
        body_bytes,
        payload,
        sealed_dir,
    )
    if not first_pass.ok:
        observed = (
            first_pass.value
            if isinstance(first_pass.value, _FreshObservation)
            else None
        )
        return terminal(first_pass.reason, first_pass.detail, observed)
    first = first_pass.value
    assert isinstance(first, _FreshObservation)
    initial_state = "validated"
    if first.local_kind == "baseline" and first.remote_kind == "expected":
        initial_state = "remote_ahead_local"
    elif first.local_kind == "expected" and first.remote_kind in {"absent", "baseline"}:
        initial_state = "committed"
    elif first.local_kind == "expected" and first.remote_kind == "expected":
        initial_state = "pr_created" if first.pr_kind == "exact" else "pushed"
    state.update(
        {
            "state": initial_state,
            "local_oid": first.identity.baseline_commit,
            "remote_kind": first.remote_kind,
            "remote_oid": first.remote_oid,
            "pr_kind": first.pr_kind,
            "pr_url": first.pr.get("url") if first.pr else None,
        }
    )
    _write_publication_state(artifacts_dir, state)

    # Row 8 is the same reusable rows-2..7 observation, performed again with
    # fresh identity/local/ref/origin/ls-remote/qualified-PR evidence. Nothing
    # intervenes between it and object installation.
    second_pass = _observe_rows_2_to_7(
        identity.root,
        artifacts_dir,
        manifest_bytes,
        package_bytes,
        body_bytes,
        payload,
        sealed_dir,
    )
    if not second_pass.ok:
        observed = (
            second_pass.value
            if isinstance(second_pass.value, _FreshObservation)
            else None
        )
        return terminal(
            PublicationReason.PRE_SIDE_EFFECT_REVALIDATION_FAILED,
            f"fresh pre-install observation failed: {second_pass.reason.value}: {second_pass.detail}",
            observed,
        )
    current = second_pass.value
    assert isinstance(current, _FreshObservation)
    repository_slug = payload["repository"]["repository_slug"]

    if current.local_kind == "baseline":
        try:
            _install_sealed_objects(
                sealed_dir, current.identity.common_dir, current.identity.object_format
            )
        except (OSError, TransactionError) as exc:
            return terminal(PublicationReason.OBJECT_INSTALL_FAILED, str(exc), current)
        try:
            cas = _cas_update_ref(
                current.identity.root,
                payload["branch_ref"],
                payload["commit_oid"],
                payload["baseline_commit"],
            )
        except OSError as exc:
            return terminal(PublicationReason.LOCAL_REF_UPDATE_FAILED, str(exc))
        if cas.returncode != 0:
            return terminal(
                PublicationReason.LOCAL_REF_UPDATE_FAILED,
                f"compare-and-swap ref update failed: {cas.stderr.decode(errors='replace')}",
                current,
            )
        state["state"] = "committed"
        state["local_oid"] = payload["commit_oid"]
        _write_publication_state(artifacts_dir, state)

    if current.remote_kind in ("absent", "baseline"):
        push_kind = current.remote_kind
        for attempt in range(2):
            lease_value = None if push_kind == "absent" else payload["baseline_commit"]
            try:
                push = _push_with_lease(
                    current.identity.root,
                    payload["commit_oid"],
                    payload["branch_ref"],
                    lease_value,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                push = None
                push_outcome = "ambiguous"
                stderr_text = str(exc)
            else:
                stderr_text = push.stderr.decode(errors="replace")
                if (
                    push_kind == "absent"
                    and push.returncode != 0
                    and "unknown option" in stderr_text.lower()
                ):
                    return terminal(
                        PublicationReason.REMOTE_CREATION_UNSUPPORTED,
                        "installed git rejects the trailing-colon absent-ref lease form",
                    )
                push_outcome = "success" if push.returncode == 0 else "ambiguous"

            if push_outcome == "ambiguous":
                state.update(
                    {
                        "state": "push_outcome_ambiguous",
                        "last_reason": "push_outcome_ambiguous",
                    }
                )
                _write_publication_state(artifacts_dir, state)
            try:
                post_push_remote = _ls_remote_oid(
                    current.identity.root, payload["branch_ref"]
                )
            except TransactionError as exc:
                return terminal(PublicationReason.REMOTE_VERIFICATION_FAILED, str(exc))
            observed_kind = _classify_remote(post_push_remote, payload)
            if post_push_remote == payload["commit_oid"]:
                break
            state["remote_kind"] = observed_kind
            state["remote_oid"] = post_push_remote
            if observed_kind == "other":
                return terminal(
                    PublicationReason.REMOTE_DIVERGED,
                    "remote diverged after push",
                )
            if push_outcome == "success":
                return terminal(
                    PublicationReason.REMOTE_VERIFICATION_FAILED,
                    "successful push did not produce the expected remote ref",
                )
            if observed_kind not in {"absent", "baseline"} or attempt == 1:
                return terminal(
                    PublicationReason.PUSH_FAILED,
                    f"ambiguous push did not converge after one retry: {stderr_text}",
                )
            push_kind = observed_kind
        else:  # pragma: no cover - loop exits through break/terminal
            return terminal(
                PublicationReason.PUSH_FAILED, "push retry exhausted", current
            )
        state["state"] = "pushed"
        state["remote_oid"] = post_push_remote
        state["remote_kind"] = "expected"
        state["last_reason"] = None
        _write_publication_state(artifacts_dir, state)

    # Fresh local/remote reconciliation after object/ref/push effects. No PR
    # observation made before those effects is reused as authority.
    try:
        final_identity = discover_repository(identity.root)
    except RepositoryIdentityError as exc:
        return terminal(PublicationReason.REPOSITORY_IDENTITY_MISMATCH, str(exc))
    approved_repository = payload["repository"]
    if (
        str(final_identity.root) != approved_repository["root"]
        or str(final_identity.git_dir) != approved_repository["git_dir"]
        or str(final_identity.common_dir) != approved_repository["common_dir"]
        or final_identity.object_format != approved_repository["object_format"]
        or final_identity.branch_ref != payload["branch_ref"]
    ):
        return terminal(
            PublicationReason.REPOSITORY_IDENTITY_MISMATCH,
            "fresh post-side-effect repository identity differs from approval",
        )
    try:
        final_remote = _ls_remote_oid(final_identity.root, payload["branch_ref"])
    except TransactionError as exc:
        return terminal(PublicationReason.REMOTE_VERIFICATION_FAILED, str(exc))
    if final_identity.baseline_commit != payload["commit_oid"]:
        return terminal(
            PublicationReason.LOCAL_REF_DIVERGED,
            "local head does not equal the approved commit after CAS",
        )
    if final_remote != payload["commit_oid"]:
        reason = (
            PublicationReason.REMOTE_DIVERGED
            if _classify_remote(final_remote, payload) == "other"
            else PublicationReason.REMOTE_VERIFICATION_FAILED
        )
        return terminal(
            reason,
            "remote head does not equal the approved commit after push",
        )

    observed_pr_kind, observed_pr = _classify_pr(
        final_identity, payload, repository_slug, approved_body_bytes=body_bytes
    )
    state.update(
        {
            "local_oid": final_identity.baseline_commit,
            "remote_kind": "expected",
            "remote_oid": final_remote,
            "pr_kind": observed_pr_kind,
            "pr_url": (
                observed_pr.get("url") if isinstance(observed_pr, dict) else None
            ),
        }
    )
    approved_body_bytes = body_bytes
    if observed_pr_kind == "none":
        try:
            approved_body_bytes = read_confined_regular_bytes(
                sealed_dir / "sealed-pr-body.md"
            )
            approved_body_bytes.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            return terminal(PublicationReason.APPROVED_CONTENT_CHANGED, str(exc))
        if (
            not approved_body_bytes
            or b"\0" in approved_body_bytes
            or body_bytes_digest(approved_body_bytes) != payload["body_bytes_digest"]
        ):
            return terminal(
                PublicationReason.APPROVED_CONTENT_CHANGED,
                "final PR body snapshot is empty/NUL or digest-mismatched",
            )
        create_result = _create_pr_with_retry(
            final_identity,
            payload,
            repository_slug,
            approved_body_bytes,
        )
        if not create_result.ok:
            reason = create_result.reason or PublicationReason.PR_CREATE_FAILED
            failed_pr = (
                create_result.value
                if isinstance(create_result.value, Mapping)
                else None
            )
            failed_kind = {
                PublicationReason.REMOTE_PR_BODY_MISMATCH: "body_mismatch",
                PublicationReason.REMOTE_PR_CONFLICT: "conflict",
                PublicationReason.REMOTE_PR_AMBIGUOUS: "ambiguous",
            }.get(reason, "none")
            return terminal(
                reason,
                create_result.detail,
                pr=failed_pr,
                pr_kind=failed_kind,
            )
    elif observed_pr_kind != "exact":
        reason = {
            "body_mismatch": PublicationReason.REMOTE_PR_BODY_MISMATCH,
            "conflict": PublicationReason.REMOTE_PR_CONFLICT,
            "ambiguous": PublicationReason.REMOTE_PR_AMBIGUOUS,
        }.get(observed_pr_kind, PublicationReason.PR_STATE_IMPOSSIBLE)
        return terminal(
            reason,
            f"post-push PR classification: {observed_pr_kind}",
            pr=observed_pr if isinstance(observed_pr, Mapping) else None,
            pr_kind=observed_pr_kind,
        )

    # Final repository-qualified reconciliation is mandatory even after the
    # create helper reconciled its own outcome. Refresh repository identity,
    # canonical origin/default base, local/remote heads, and the PR together;
    # never reuse the pre-create observation as publication authority.
    try:
        reconciled_identity = discover_repository(identity.root)
        reconciled_slug = _derive_origin_repository_slug(reconciled_identity.root)
    except (OSError, RepositoryIdentityError, TransactionError) as exc:
        return terminal(PublicationReason.REPOSITORY_IDENTITY_MISMATCH, str(exc))
    if (
        reconciled_slug != repository_slug
        or str(reconciled_identity.root) != approved_repository["root"]
        or str(reconciled_identity.git_dir) != approved_repository["git_dir"]
        or str(reconciled_identity.common_dir) != approved_repository["common_dir"]
        or reconciled_identity.object_format != approved_repository["object_format"]
        or reconciled_identity.branch_ref != payload["branch_ref"]
        or reconciled_identity.baseline_commit != payload["commit_oid"]
    ):
        return terminal(
            PublicationReason.REPOSITORY_IDENTITY_MISMATCH,
            "final repository identity/local head differs from approval",
        )
    ok, reconciled_repo = _gh_json(
        [
            "repo",
            "view",
            "--repo",
            repository_slug,
            "--json",
            "nameWithOwner,defaultBranchRef",
        ],
        reconciled_identity.root,
    )
    if (
        not ok
        or not isinstance(reconciled_repo, dict)
        or set(reconciled_repo) != {"nameWithOwner", "defaultBranchRef"}
        or reconciled_repo.get("nameWithOwner") != repository_slug
        or not isinstance(reconciled_repo.get("defaultBranchRef"), dict)
        or set(reconciled_repo["defaultBranchRef"]) != {"name"}
        or reconciled_repo["defaultBranchRef"].get("name") != payload["pr_base_branch"]
    ):
        return terminal(
            PublicationReason.REPOSITORY_IDENTITY_MISMATCH,
            "final gh repository/default base differs from approval",
        )
    try:
        reconciled_remote = _ls_remote_oid(
            reconciled_identity.root, payload["branch_ref"]
        )
    except TransactionError as exc:
        return terminal(PublicationReason.REMOTE_VERIFICATION_FAILED, str(exc))
    if reconciled_remote != payload["commit_oid"]:
        reason = (
            PublicationReason.REMOTE_DIVERGED
            if _classify_remote(reconciled_remote, payload) == "other"
            else PublicationReason.REMOTE_VERIFICATION_FAILED
        )
        return terminal(reason, "final remote head differs from approval")
    final_pr_kind, final_pr = _classify_pr(
        reconciled_identity,
        payload,
        repository_slug,
        approved_body_bytes=approved_body_bytes,
    )
    if final_pr_kind != "exact":
        reason = {
            "body_mismatch": PublicationReason.REMOTE_PR_BODY_MISMATCH,
            "conflict": PublicationReason.REMOTE_PR_CONFLICT,
            "ambiguous": PublicationReason.REMOTE_PR_AMBIGUOUS,
            "none": PublicationReason.PR_CREATE_FAILED,
        }.get(final_pr_kind, PublicationReason.PR_STATE_IMPOSSIBLE)
        return terminal(
            reason,
            f"final PR reconciliation did not produce exact: {final_pr_kind}",
            pr=final_pr if isinstance(final_pr, Mapping) else None,
            pr_kind=final_pr_kind,
        )
    pr = final_pr
    assert isinstance(pr, Mapping)
    state.update(
        {
            "local_oid": reconciled_identity.baseline_commit,
            "remote_kind": "expected",
            "remote_oid": reconciled_remote,
            "state": "pr_created",
            "pr_kind": "exact",
            "pr_url": pr.get("url"),
            "last_reason": None,
        }
    )
    _write_publication_state(artifacts_dir, state)

    qualified_url_pattern = (
        rf"https://github\.com/{re.escape(repository_slug)}/pull/[1-9][0-9]*"
    )
    if not isinstance(pr, dict) or not re.fullmatch(
        qualified_url_pattern, pr.get("url", "")
    ):
        return terminal(
            PublicationReason.PUBLICATION_RESULT_INVALID,
            "PR URL is not a valid GitHub PR URL",
        )

    publication = {
        "schema": 2,
        "status": "published",
        "approval_revision": manifest.get("approval_revision"),
        "approval_digest": manifest.get("approval_digest"),
        "branch": payload["branch_ref"],
        "commit": payload["commit_oid"],
        "tree": payload["tree_oid"],
        "url": pr["url"],
        "remote_state": "published",
    }
    try:
        atomic_write(
            artifacts_dir / "publish.json", canonical_json(publication), mode=0o600
        )
    except (CanonicalJsonError, OSError) as exc:
        return terminal(PublicationReason.PUBLICATION_RESULT_INVALID, str(exc))
    state["state"] = "published"
    _write_publication_state(artifacts_dir, state)
    return PublicationResult(True, None, "", value=publication)


def _write_publication_state(artifacts_dir: Path, state: Mapping[str, object]) -> None:
    atomic_write(
        artifacts_dir / "publication-state.json", canonical_json(state), mode=0o600
    )


def _create_pr_with_retry(
    identity: RepositoryIdentity,
    payload: Mapping[str, object],
    repository_slug: str,
    approved_body_bytes: bytes,
) -> PublicationResult:
    """Create at most twice, reconciling every outcome with one body snapshot."""
    short_branch = identity.branch_ref.removeprefix("refs/heads/")
    head_owner, _, _ = repository_slug.partition("/")

    for attempt in range(2):
        try:
            create = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repository_slug,
                    "--base",
                    payload["pr_base_branch"],
                    "--head",
                    f"{head_owner}:{short_branch}",
                    "--title",
                    payload["title"],
                    "--body-file",
                    "-",
                ],
                input=approved_body_bytes,
                shell=False,
                check=False,
                capture_output=True,
                cwd=identity.root,
                timeout=_NETWORK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            create_outcome = "ambiguous"
        else:
            create_outcome = (
                "success" if create.returncode == 0 else "unambiguous_failure"
            )

        # The create exit code/body/URL is never authority. Every outcome,
        # including success and exceptions, is reconciled by the qualified
        # strict query using the same immutable body snapshot.
        pr_kind, pr = _classify_pr(
            identity,
            payload,
            repository_slug,
            approved_body_bytes=approved_body_bytes,
        )
        if pr_kind == "exact":
            return PublicationResult(True, None, "", value=pr)
        if pr_kind == "body_mismatch":
            return PublicationResult(
                False,
                PublicationReason.REMOTE_PR_BODY_MISMATCH,
                "PR exists with a body that differs from the approved snapshot; "
                "manual reconciliation required",
                value=pr,
            )
        if pr_kind == "conflict":
            return PublicationResult(
                False,
                PublicationReason.REMOTE_PR_CONFLICT,
                "an unrelated open PR occupies this head/base",
                value=pr,
            )
        if pr_kind == "ambiguous":
            return PublicationResult(
                False,
                PublicationReason.REMOTE_PR_AMBIGUOUS,
                "PR query returned an indeterminate result",
                value=pr,
            )
        assert pr_kind == "none"
        if create_outcome == "success":
            return _fail(
                PublicationReason.PR_CREATE_FAILED,
                "gh reported success but qualified reconciliation found no PR",
            )
        if attempt == 0:
            continue
    return _fail(
        PublicationReason.PR_CREATE_FAILED,
        "PR creation did not converge after one retry",
    )


def _build_parser() -> object:
    import argparse

    parser = argparse.ArgumentParser(prog="prp_publication_binding")
    sub = parser.add_subparsers(dest="cmd", required=True)
    vt = sub.add_parser("validate-sealed-transaction")
    vt.add_argument("--identity-root", required=True)
    vt.add_argument("--artifacts-dir", required=True)
    vt.add_argument("--manifest-file", required=True)
    vt.add_argument("--package-file", required=True)
    vt.add_argument("--body-file", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return 0 if code == 0 else 64
    try:
        if args.cmd == "validate-sealed-transaction":
            identity = discover_repository(Path(args.identity_root))
            result = validate_sealed_transaction(
                identity,
                Path(args.artifacts_dir),
                Path(args.manifest_file).read_bytes(),
                Path(args.package_file).read_bytes(),
                Path(args.body_file).read_bytes(),
            )
            print(
                json.dumps(
                    {
                        "ok": result.ok,
                        "reason": result.reason.value if result.reason else None,
                        "detail": result.detail,
                    },
                    sort_keys=True,
                )
            )
            return 0 if result.ok else 2
    except Exception as exc:  # internal error, never a contract verdict
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    return 64


if __name__ == "__main__":
    sys.exit(main())
