"""Crash-safe, lifetime-owned lifecycle accounting for capability plugins.

The lifecycle kernel owns process-local physical plugin state.  Consequently,
one kernel must exclusively own a journal for its entire lifetime: sharing only
an append lock would let another process replay receipts for effects it never
performed.  This module therefore holds a dedicated byte-0 OS lock until
``close()`` (or process exit) and persists the logical JSONL stream by atomic
full-file replacement.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO


class ReceiptPersistenceError(OSError):
    """The physical lifecycle journal could not be trusted or persisted."""


class JournalCommitAmbiguousError(ReceiptPersistenceError):
    """A failed atomic update did not prove one durable authoritative image."""

    def __init__(
        self,
        message: str,
        *,
        interrupted: bool = False,
    ) -> None:
        self.interrupted = interrupted
        super().__init__(message)


class JournalOwnershipError(ReceiptPersistenceError):
    """Another live kernel already owns this process-local lifecycle journal."""


class CommandIdentityConflictError(ValueError):
    """A command ID was replayed for a different plugin or transition."""


@dataclass(frozen=True, slots=True)
class JournalAppendResult:
    record: Mapping[str, Any]
    replayed: bool


@dataclass(frozen=True, slots=True)
class JournalSupersessionResult:
    superseded: Mapping[str, Any] | None
    request: Mapping[str, Any]
    replayed: bool


_PROCESS_OWNERS_GUARD = threading.Lock()
_PROCESS_OWNERS: dict[str, str] = {}


def _path_identity(path: Path) -> str:
    value = str(path.resolve(strict=False))
    return os.path.normcase(value)


def _open_private_text_file(path: Path) -> TextIO:
    """Create one exclusive 0600 text file without exposing descriptor transfer state."""

    def private_opener(raw_path: str, flags: int) -> int:
        return os.open(raw_path, flags, 0o600)

    return open(
        path,
        "x",
        encoding="utf-8",
        newline="\n",
        opener=private_opener,
    )


class LockedLifecycleJournal:
    """Exclusively own and atomically update one lifecycle JSONL journal."""

    def __init__(self, path: Path, *, owner_timeout: float = 0.0) -> None:
        self.path = Path(path).resolve(strict=False)
        self.owner_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._identity = _path_identity(self.path)
        self._thread_lock = threading.RLock()
        self._owner_handle: BinaryIO | None = None
        self._closed = False
        self._acquire_owner(max(0.0, owner_timeout))

    @property
    def closed(self) -> bool:
        with self._thread_lock:
            return self._closed

    def close(self) -> None:
        """Release the lifetime owner lock.  Safe to call more than once."""

        with self._thread_lock:
            if self._closed:
                return
            handle = self._owner_handle
            try:
                if handle is not None:
                    # Closing the descriptor is the ownership transition.  An
                    # explicit unlock would create a window where this object
                    # still accepts writes after another process acquired the
                    # journal.  If close raises after releasing the OS lock,
                    # the outcome is unknowable, so the journal must remain
                    # permanently fail-closed rather than become retryable.
                    handle.close()
            except BaseException:
                self._owner_handle = None
                with _PROCESS_OWNERS_GUARD:
                    if _PROCESS_OWNERS.get(self._identity) == self.owner_id:
                        _PROCESS_OWNERS.pop(self._identity, None)
                self._closed = True
                raise
            else:
                self._owner_handle = None
                with _PROCESS_OWNERS_GUARD:
                    if _PROCESS_OWNERS.get(self._identity) == self.owner_id:
                        _PROCESS_OWNERS.pop(self._identity, None)
                self._closed = True

    def records(self) -> tuple[Mapping[str, Any], ...]:
        with self._thread_lock:
            self._require_owner()
            try:
                return self._read_unlocked()
            except ReceiptPersistenceError:
                raise
            except Exception as exc:
                raise ReceiptPersistenceError("receipt journal read failed") from exc

    def append_request(self, payload: Mapping[str, Any]) -> JournalAppendResult:
        """Append one command request, or return its physically recorded replay."""

        return self._append(payload, replay_request=True, unique_terminal=False)

    def append_event(
        self,
        payload: Mapping[str, Any],
        *,
        unique_terminal: bool,
    ) -> JournalAppendResult:
        """Append a progress/terminal event with optional terminal de-duplication."""

        return self._append(
            payload,
            replay_request=False,
            unique_terminal=unique_terminal,
        )

    def append_supersession(
        self,
        superseded_payload: Mapping[str, Any],
        request_payload: Mapping[str, Any],
    ) -> JournalSupersessionResult:
        """Atomically terminalize old pending work and persist its replacement."""

        with self._thread_lock:
            self._require_owner()
            try:
                records = self._read_unlocked()
                old_matches = self._command_matches(records, superseded_payload)
                new_matches = self._command_matches(records, request_payload)
                owned_new_matches = self._owned_matches(new_matches)
                if owned_new_matches:
                    return JournalSupersessionResult(
                        superseded=None,
                        request=owned_new_matches[-1],
                        replayed=True,
                    )
                owned_old_matches = self._owned_matches(old_matches)
                if not owned_old_matches:
                    raise ReceiptPersistenceError(
                        "superseded command has no durable request"
                    )
                old_terminals = [
                    item
                    for item in owned_old_matches
                    if item.get("phase") == "terminal"
                ]
                if old_terminals:
                    raise ReceiptPersistenceError(
                        "superseded command is already terminal"
                    )

                mutable = list(records)
                superseded = self._next_record(mutable, superseded_payload)
                mutable.append(superseded)
                request = self._next_record(mutable, request_payload)
                mutable.append(request)
                self._commit_records_unlocked(records, tuple(mutable))
                return JournalSupersessionResult(
                    superseded=superseded,
                    request=request,
                    replayed=False,
                )
            except (CommandIdentityConflictError, ReceiptPersistenceError):
                raise
            except Exception as exc:
                raise ReceiptPersistenceError("receipt persistence failed") from exc

    def _append(
        self,
        payload: Mapping[str, Any],
        *,
        replay_request: bool,
        unique_terminal: bool,
    ) -> JournalAppendResult:
        with self._thread_lock:
            self._require_owner()
            try:
                records = self._read_unlocked()
                matches = self._command_matches(records, payload)
                owned_matches = self._owned_matches(matches)
                if replay_request and owned_matches:
                    return JournalAppendResult(record=owned_matches[-1], replayed=True)
                if unique_terminal:
                    terminals = [
                        item
                        for item in owned_matches
                        if item.get("phase") == "terminal"
                    ]
                    if terminals:
                        return JournalAppendResult(record=terminals[-1], replayed=True)

                record = self._next_record(records, payload)
                self._commit_records_unlocked(records, (*records, record))
                return JournalAppendResult(record=record, replayed=False)
            except (CommandIdentityConflictError, ReceiptPersistenceError):
                raise
            except Exception as exc:
                raise ReceiptPersistenceError("receipt persistence failed") from exc

    def _next_record(
        self,
        records: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        event_id = max((int(item["event_id"]) for item in records), default=0) + 1
        record = dict(payload)
        record["event_id"] = event_id
        record["journal_owner_id"] = self.owner_id
        return record

    def _owned_matches(
        self, records: list[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        return [
            item for item in records if item.get("journal_owner_id") == self.owner_id
        ]

    def _command_matches(
        self,
        records: tuple[Mapping[str, Any], ...],
        payload: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        command_id = payload.get("command_id")
        matches = [item for item in records if item.get("command_id") == command_id]
        for existing in matches:
            identity_fields = (
                "plugin_id",
                "plugin_version",
                "plugin_provenance_id",
                "source",
                "command_transition",
                "contribution_ids",
            )
            if any(existing.get(field) != payload.get(field) for field in identity_fields):
                raise CommandIdentityConflictError("command identity conflict")
        return matches

    def _read_unlocked(self) -> tuple[Mapping[str, Any], ...]:
        if not self.path.exists():
            return ()
        try:
            payload = self.path.read_bytes()
        except Exception as exc:
            raise ReceiptPersistenceError("receipt journal read failed") from exc

        # Every committed atomic image ends with a newline.  An unterminated
        # tail could be a truncated terminal receipt, so it must fail closed;
        # discarding it could replay effects that actually completed.
        if not payload:
            return ()

        parts = payload.split(b"\n")
        if not payload.endswith(b"\n"):
            raise ReceiptPersistenceError(
                "receipt journal contains an unterminated committed frame"
            )
        if parts and parts[-1] == b"":
            parts.pop()

        records: list[Mapping[str, Any]] = []
        prior_event_id = 0
        for raw_line in parts:
            if not raw_line.strip():
                raise ReceiptPersistenceError(
                    "receipt journal contains a blank committed frame"
                )
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReceiptPersistenceError(
                    "receipt journal contains invalid committed JSON"
                ) from exc
            if not isinstance(record, dict):
                raise ReceiptPersistenceError("receipt journal contains a non-object event")
            event_id = record.get("event_id")
            if type(event_id) is not int or event_id != prior_event_id + 1:
                raise ReceiptPersistenceError(
                    "receipt journal event IDs are not contiguous from one"
                )
            prior_event_id = event_id
            records.append(record)
        return tuple(records)

    def _commit_records_unlocked(
        self,
        original: tuple[Mapping[str, Any], ...],
        expected: tuple[Mapping[str, Any], ...],
    ) -> None:
        """Commit an exact image and reconcile failures after replacement.

        An exception after ``os.replace`` is not proof that the old image is
        still durable.  While lifetime ownership is held, re-read the journal
        and compare the complete owner-scoped image before reporting an
        outcome.  The exact old image proves failure without replacement; the
        expected image is visible but not proven durable, and any other image
        is corrupt.  Both latter cases are fail-closed ambiguities for the
        lifecycle kernel to quarantine.
        """

        interrupted = False
        clean_interrupt = False
        ambiguous_message = ""
        persistence_failed = False
        try:
            self._atomic_replace_unlocked(expected)
        except BaseException as exc:
            interrupted = isinstance(exc, KeyboardInterrupt)
            try:
                visible = self._read_unlocked()
            except BaseException as read_exc:
                interrupted = interrupted or isinstance(read_exc, KeyboardInterrupt)
                ambiguous_message = "receipt journal commit outcome is ambiguous"
            else:
                if visible == expected:
                    ambiguous_message = (
                        "receipt journal image is visible but durability is unproven"
                    )
                elif visible == original:
                    clean_interrupt = interrupted
                    persistence_failed = not interrupted
                else:
                    ambiguous_message = (
                        "receipt journal commit produced an unexpected image"
                    )

        if clean_interrupt:
            raise KeyboardInterrupt() from None
        if ambiguous_message:
            raise JournalCommitAmbiguousError(
                ambiguous_message,
                interrupted=interrupted,
            ) from None
        if persistence_failed:
            raise ReceiptPersistenceError("receipt persistence failed") from None

    def _atomic_replace_unlocked(
        self,
        records: tuple[Mapping[str, Any], ...],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        handle: TextIO | None = None
        interrupted = False
        try:
            try:
                handle = _open_private_text_file(temporary)
                for record in records:
                    handle.write(
                        json.dumps(
                            record,
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            except KeyboardInterrupt:
                interrupted = True
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except KeyboardInterrupt:
                        interrupted = True
                    except BaseException:
                        if not interrupted:
                            raise
            if interrupted:
                raise KeyboardInterrupt() from None
            os.replace(temporary, self.path)
            self._fsync_parent_directory()
        except KeyboardInterrupt:
            interrupted = True
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except KeyboardInterrupt:
                interrupted = True
            except OSError:
                pass
            except BaseException:
                if not interrupted:
                    raise
            if interrupted:
                raise KeyboardInterrupt() from None

    def _fsync_parent_directory(self) -> None:
        if os.name == "nt":
            return
        descriptor: int | None = None
        interrupted = False
        try:
            try:
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                os.fsync(descriptor)
            except KeyboardInterrupt:
                interrupted = True
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except KeyboardInterrupt:
                    interrupted = True
                except BaseException:
                    if not interrupted:
                        raise
            if interrupted:
                raise KeyboardInterrupt() from None

    def _acquire_owner(self, timeout: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _PROCESS_OWNERS_GUARD:
            if self._identity in _PROCESS_OWNERS:
                raise JournalOwnershipError(
                    "lifecycle journal already has an in-process owner"
                )
            _PROCESS_OWNERS[self._identity] = self.owner_id

        lock_path = self.path.with_suffix(self.path.suffix + ".owner")
        handle: BinaryIO | None = None
        acquisition_interrupted = False
        acquisition_failed = False
        cleanup_failed = False
        try:
            # a+b neither truncates nor replaces the lock inode.  The shared
            # helper intentionally uses w-mode and is unsuitable for a
            # lifetime ownership lock.
            handle = open(lock_path, "a+b")  # noqa: SIM115
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + timeout
            while True:
                handle.seek(0)
                try:
                    self._lock_byte_zero(handle)
                    break
                except (OSError, BlockingIOError) as exc:
                    if time.monotonic() >= deadline:
                        raise JournalOwnershipError(
                            "lifecycle journal is owned by another process"
                        ) from exc
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            self._owner_handle = handle
        except BaseException as exc:
            acquisition_interrupted = isinstance(exc, KeyboardInterrupt)
            acquisition_failed = not acquisition_interrupted
            with _PROCESS_OWNERS_GUARD:
                if _PROCESS_OWNERS.get(self._identity) == self.owner_id:
                    _PROCESS_OWNERS.pop(self._identity, None)
            if handle is not None:
                try:
                    handle.close()
                except BaseException as cleanup_exc:
                    acquisition_interrupted = acquisition_interrupted or isinstance(
                        cleanup_exc, KeyboardInterrupt
                    )
                    cleanup_failed = not isinstance(cleanup_exc, KeyboardInterrupt)

        if acquisition_interrupted:
            raise KeyboardInterrupt() from None
        if cleanup_failed:
            raise JournalOwnershipError(
                "lifecycle journal owner handle cleanup failed"
            ) from None
        if acquisition_failed:
            raise JournalOwnershipError("lifecycle journal owner acquisition failed") from None

    @staticmethod
    def _lock_byte_zero(handle: BinaryIO) -> None:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0, os.SEEK_SET)

    @staticmethod
    def _unlock_byte_zero(handle: BinaryIO) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.lockf(handle.fileno(), fcntl.LOCK_UN, 1, 0, os.SEEK_SET)

    def _require_owner(self) -> None:
        if self._closed or self._owner_handle is None:
            raise JournalOwnershipError("lifecycle journal owner is closed")

    def __enter__(self) -> LockedLifecycleJournal:
        self._require_owner()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        # The OS releases this lock at process exit.  This best-effort path
        # also avoids retaining temporary-directory handles after ordinary GC.
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "CommandIdentityConflictError",
    "JournalAppendResult",
    "JournalCommitAmbiguousError",
    "JournalOwnershipError",
    "JournalSupersessionResult",
    "LockedLifecycleJournal",
    "ReceiptPersistenceError",
]
