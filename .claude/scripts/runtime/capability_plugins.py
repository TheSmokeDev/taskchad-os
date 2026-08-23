"""Synchronous, process-local transactional lifecycle for strict capability plugins.

Callers apply requested changes at an explicit turn boundary and consume only
lease-bound contribution snapshots.
"""

from __future__ import annotations

import asyncio
import builtins as python_builtins
import importlib
import inspect
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from importlib import abc as importlib_abc
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import config
from runtime.capability_plugin_journal import (
    CommandIdentityConflictError,
    JournalCommitAmbiguousError,
    JournalOwnershipError,
    LockedLifecycleJournal,
)
from runtime.capability_plugin_journal import ReceiptPersistenceError as ReceiptPersistenceError
from runtime.capability_plugin_manifest import (
    CapabilityPluginArtifact,
    CapabilityPluginCandidate,
    CapabilityPluginManifest,
    ContributionDeclaration,
    ContributionType,
    ManifestSource,
    contribution_topological_order,
    core_version_satisfies,
    redact_detail,
)

RECEIPT_FILENAME = "capability_plugin_lifecycle.jsonl"
RECEIPT_SCHEMA_VERSION = 2
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PLUGIN_ID_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)*$"
)
_PLUGIN_VERSION_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MODULE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DETAIL_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROVENANCE_ID_RE = re.compile(r"^[a-z_]+:[a-f0-9]{20}$")


class CapabilityPluginError(RuntimeError):
    """A stable lifecycle error that does not expose plugin exception detail."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = _safe_detail_code(code)
        self.detail = redact_detail(detail)
        super().__init__(f"{self.code}: {self.detail}")


class CapabilityNotFoundError(KeyError):
    pass


class _OwnershipCleanupInterrupted(KeyboardInterrupt):
    pass


class PluginDesiredState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class PluginEffectiveState(StrEnum):
    LOADED = "loaded"
    DRAINING = "draining"
    UNLOADED = "unloaded"
    RESTART_REQUIRED = "restart_required"


class PluginLifecycleState(StrEnum):
    DISCOVERED = "discovered"
    ENABLED = "enabled"
    LOADING = "loading"
    LOADED = "loaded"
    DEGRADED = "degraded"
    UNLOAD_REQUESTED = "unload_requested"
    DRAINING = "draining"
    UNLOADED = "unloaded"
    FAILED = "failed"
    RESTART_REQUIRED = "restart_required"


class LifecycleTransition(StrEnum):
    ENABLE = "enable"
    LOAD = "load"
    DISABLE = "disable"
    UNLOAD = "unload"


class LifecycleEvent(StrEnum):
    ENABLED = "enabled"
    LOADED = "loaded"
    UNLOAD_REQUESTED = "unload_requested"
    DRAINING = "draining"
    UNLOADED = "unloaded"
    FAILED = "failed"
    RESTART_REQUIRED = "restart_required"
    NO_OP = "no_op"
    SUPERSEDED = "superseded"


class LifecyclePhase(StrEnum):
    REQUEST = "request"
    PROGRESS = "progress"
    TERMINAL = "terminal"


class LifecycleOutcome(StrEnum):
    ACCEPTED = "accepted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NO_OP = "no_op"


class DisposalOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class _SnapshotReleaseState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    RELEASE_FAILED = "release_failed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class PluginIdentity:
    id: str
    version: str


@dataclass(frozen=True, slots=True)
class ContributionBinding:
    id: str
    type: ContributionType
    plugin_id: str
    plugin_version: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistrationReceipt:
    plugin_id: str
    contribution_id: str
    depends_on: tuple[str, ...]
    disposer_registered: bool


@dataclass(frozen=True, slots=True)
class DisposalReceipt:
    contribution_id: str
    outcome: DisposalOutcome
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "contribution_id": self.contribution_id,
            "outcome": self.outcome.value,
            "detail": self.detail,
        }


class _SnapshotLease:
    """Revocable lease state shared by a snapshot and every derived handle."""

    def __init__(
        self,
        lease_id: str,
        values: Mapping[str, Any],
        release: Callable[[str], tuple[PluginLifecycleReceipt, ...]],
    ) -> None:
        self.lease_id = lease_id
        self._values = values
        self._release = release
        self._condition = threading.Condition(threading.Lock())
        self._active_invocations = 0
        self._invocation_threads: dict[int, int] = {}
        self._release_state = _SnapshotReleaseState.OPEN
        self._release_attempt = 0
        self._release_receipts: tuple[PluginLifecycleReceipt, ...] = ()
        self._release_error_code = ""
        self._attempt_outcomes: dict[
            int,
            tuple[tuple[PluginLifecycleReceipt, ...] | None, BaseException | None],
        ] = {}
        self._attempt_waiters: dict[int, int] = {}

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._release_state is not _SnapshotReleaseState.OPEN

    def diagnostic_state(self) -> tuple[int, bool, str, str]:
        with self._condition:
            return (
                self._active_invocations,
                self._release_state
                in {_SnapshotReleaseState.CLOSING, _SnapshotReleaseState.RELEASE_FAILED},
                self._release_state.value,
                self._release_error_code,
            )

    def invoke(self, contribution_id: str, *args: Any, **kwargs: Any) -> Any:
        value = self._begin_use(contribution_id)
        try:
            if not callable(value):
                raise CapabilityPluginError(
                    "contribution_not_callable",
                    "This contribution exposes inert data; use handle.read()",
                )
            try:
                result = value(*args, **kwargs)
            except KeyboardInterrupt:
                execution_interrupted = True
                execution_failed = False
            except BaseException:
                execution_interrupted = False
                execution_failed = True
            else:
                execution_interrupted = False
                execution_failed = False
            if execution_interrupted:
                raise KeyboardInterrupt() from None
            if execution_failed:
                raise CapabilityPluginError(
                    "contribution_execution_failed",
                    "Contribution execution failed without exposing plugin detail",
                )
            try:
                detached = _detach_invocation_result(result)
            except KeyboardInterrupt:
                detach_interrupted = True
                detach_failed = False
            except CapabilityPluginError:
                raise
            except BaseException:
                detach_interrupted = False
                detach_failed = True
            else:
                detach_interrupted = False
                detach_failed = False
            if detach_interrupted:
                raise KeyboardInterrupt() from None
            if detach_failed:
                raise CapabilityPluginError(
                    "non_inert_contribution_result",
                    "Contribution calls must return closed built-in inert data",
                )
            return detached
        finally:
            self._end_use()

    def read(self, contribution_id: str) -> Any:
        value = self._begin_use(contribution_id)
        try:
            if callable(value):
                raise CapabilityPluginError(
                    "contribution_is_callable",
                    "Callable contribution data cannot be extracted as raw authority",
                )
            try:
                frozen = _freeze_inert(value)
            except KeyboardInterrupt:
                freeze_interrupted = True
                freeze_failed = False
            except CapabilityPluginError:
                raise
            except BaseException:
                freeze_interrupted = False
                freeze_failed = True
            else:
                freeze_interrupted = False
                freeze_failed = False
            if freeze_interrupted:
                raise KeyboardInterrupt() from None
            if freeze_failed:
                raise CapabilityPluginError(
                    "non_inert_contribution",
                    "Non-callable contributions must be closed built-in inert data",
                )
            return frozen
        finally:
            self._end_use()

    def close(self) -> tuple[PluginLifecycleReceipt, ...]:
        owner_thread = threading.get_ident()
        with self._condition:
            if self._invocation_threads.get(owner_thread, 0):
                raise CapabilityPluginError(
                    "snapshot_close_from_active_invocation",
                    "A contribution cannot close the snapshot that authorizes its call",
                )
            if self._release_state is _SnapshotReleaseState.CLOSED:
                return self._release_receipts
            if self._release_state is _SnapshotReleaseState.CLOSING:
                observed_attempt = self._release_attempt
                self._attempt_waiters[observed_attempt] = (
                    self._attempt_waiters.get(observed_attempt, 0) + 1
                )
                try:
                    while observed_attempt not in self._attempt_outcomes:
                        self._condition.wait()
                    receipts, error = self._attempt_outcomes[observed_attempt]
                finally:
                    remaining = self._attempt_waiters[observed_attempt] - 1
                    if remaining:
                        self._attempt_waiters[observed_attempt] = remaining
                    else:
                        self._attempt_waiters.pop(observed_attempt, None)
                        self._attempt_outcomes.pop(observed_attempt, None)
                if error is not None:
                    raise error
                assert receipts is not None
                return receipts

            for completed_attempt in tuple(self._attempt_outcomes):
                if not self._attempt_waiters.get(completed_attempt, 0):
                    self._attempt_outcomes.pop(completed_attempt, None)
            self._release_state = _SnapshotReleaseState.CLOSING
            self._release_error_code = ""
            self._release_attempt += 1
            release_attempt = self._release_attempt
            try:
                while self._active_invocations:
                    self._condition.wait()
            except BaseException as exc:
                self._release_state = _SnapshotReleaseState.RELEASE_FAILED
                self._release_error_code = (
                    exc.code
                    if isinstance(exc, CapabilityPluginError)
                    else "snapshot_release_failed"
                )
                self._attempt_outcomes[release_attempt] = (None, exc)
                if not self._attempt_waiters.get(release_attempt, 0):
                    self._attempt_outcomes.pop(release_attempt, None)
                self._condition.notify_all()
                raise
        try:
            receipts = self._release(self.lease_id)
        except BaseException as exc:
            interrupted = self._publish_until_committed(
                lambda failure=exc: self._publish_release_failure(
                    release_attempt, failure
                )
            )
            if interrupted:
                raise KeyboardInterrupt() from None
            raise
        interrupted = self._publish_until_committed(
            lambda: self._publish_release_success(release_attempt, receipts)
        )
        if interrupted:
            raise KeyboardInterrupt() from None
        return receipts

    @staticmethod
    def _publish_until_committed(
        publish: Callable[[], None],
    ) -> bool:
        interrupted = False
        while True:
            try:
                publish()
            except KeyboardInterrupt:
                interrupted = True
                continue
            return interrupted

    def _publish_release_failure(
        self,
        release_attempt: int,
        exc: BaseException,
    ) -> None:
        with self._condition:
            self._release_state = _SnapshotReleaseState.RELEASE_FAILED
            self._release_error_code = (
                exc.code
                if isinstance(exc, CapabilityPluginError)
                else "snapshot_release_failed"
            )
            self._attempt_outcomes[release_attempt] = (None, exc)
            if not self._attempt_waiters.get(release_attempt, 0):
                self._attempt_outcomes.pop(release_attempt, None)
            self._condition.notify_all()

    def _publish_release_success(
        self,
        release_attempt: int,
        receipts: tuple[PluginLifecycleReceipt, ...],
    ) -> None:
        with self._condition:
            self._release_state = _SnapshotReleaseState.CLOSED
            self._release_receipts = receipts
            self._attempt_outcomes[release_attempt] = (receipts, None)
            if not self._attempt_waiters.get(release_attempt, 0):
                self._attempt_outcomes.pop(release_attempt, None)
            self._condition.notify_all()

    def _begin_use(self, contribution_id: str) -> Any:
        with self._condition:
            if self._release_state is not _SnapshotReleaseState.OPEN:
                raise CapabilityPluginError(
                    "snapshot_released",
                    "Released capability handles cannot be invoked or read",
                )
            try:
                value = self._values[contribution_id]
            except KeyError as exc:
                raise CapabilityNotFoundError(contribution_id) from exc
            self._active_invocations += 1
            owner_thread = threading.get_ident()
            self._invocation_threads[owner_thread] = (
                self._invocation_threads.get(owner_thread, 0) + 1
            )
            return value

    def _end_use(self) -> None:
        with self._condition:
            self._active_invocations -= 1
            owner_thread = threading.get_ident()
            remaining = self._invocation_threads[owner_thread] - 1
            if remaining:
                self._invocation_threads[owner_thread] = remaining
            else:
                self._invocation_threads.pop(owner_thread, None)
            if self._active_invocations == 0:
                self._condition.notify_all()


@dataclass(frozen=True, slots=True)
class ContributionHandle:
    """Lease-bound contribution metadata plus callable/inert access."""

    id: str
    type: ContributionType
    plugin_id: str
    plugin_version: str
    depends_on: tuple[str, ...]
    _lease: _SnapshotLease = field(repr=False, compare=False)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._lease.invoke(self.id, *args, **kwargs)

    def read(self) -> Any:
        """Return a detached immutable copy of closed, inert built-in data."""

        return self._lease.read(self.id)


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    lease_id: str
    generation: int
    plugins: tuple[PluginIdentity, ...]
    contributions: Mapping[str, ContributionHandle]
    _lease: _SnapshotLease = field(repr=False, compare=False)

    def resolve(self, contribution_id: str) -> ContributionHandle:
        if self.closed:
            raise CapabilityPluginError(
                "snapshot_released", "Released capability snapshot cannot be resolved"
            )
        try:
            return self.contributions[contribution_id]
        except KeyError as exc:
            raise CapabilityNotFoundError(contribution_id) from exc

    @property
    def closed(self) -> bool:
        return self._lease.closed

    def close(self) -> tuple[PluginLifecycleReceipt, ...]:
        """Stop new invocations, await active calls, then release the turn lease."""

        return self._lease.close()

    def __enter__(self) -> PluginSnapshot:
        if self.closed:
            raise CapabilityPluginError(
                "snapshot_released", "Released capability snapshot cannot be reacquired"
            )
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def _freeze_inert(value: Any, *, _depth: int = 0) -> Any:
    """Copy only exact built-in inert data; never return arbitrary objects raw."""

    if _depth > 32:
        raise CapabilityPluginError(
            "inert_data_too_deep", "Contribution data exceeds the safe read depth"
        )
    if value is None or type(value) in {bool, int, float, str, bytes}:
        return value
    if type(value) in {list, tuple}:
        return tuple(_freeze_inert(item, _depth=_depth + 1) for item in value)
    if type(value) in {set, frozenset}:
        return frozenset(_freeze_inert(item, _depth=_depth + 1) for item in value)
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            raise CapabilityPluginError(
                "non_inert_contribution",
                "Contribution mappings must use exact string keys",
            )
        return MappingProxyType(
            {
                key: _freeze_inert(item, _depth=_depth + 1)
                for key, item in value.items()
            }
        )
    raise CapabilityPluginError(
        "non_inert_contribution",
        "Non-callable contributions must be closed built-in inert data",
    )


def _is_deferred_callable(value: object) -> bool:
    """Detect function and callable-object forms that defer execution."""

    if (
        inspect.iscoroutinefunction(value)
        or inspect.isgeneratorfunction(value)
        or inspect.isasyncgenfunction(value)
    ):
        return True
    call_method = getattr(type(value), "__call__", None)
    return bool(
        call_method is not None
        and (
            inspect.iscoroutinefunction(call_method)
            or inspect.isgeneratorfunction(call_method)
            or inspect.isasyncgenfunction(call_method)
        )
    )


def _discard_deferred_result(value: object) -> None:
    """Best-effort close a rejected deferred result without returning authority."""

    interrupted = False
    try:
        if inspect.iscoroutine(value) or inspect.isgenerator(value):
            value.close()
            return
        if inspect.isasyncgen(value):
            close_result = value.aclose()
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(close_result)
                return
            iterator = close_result.__await__()
            try:
                next(iterator)
            except StopIteration:
                return
            close_iterator = getattr(iterator, "close", None)
            if callable(close_iterator):
                close_iterator()
            return
        close = getattr(value, "close", None)
        if callable(close):
            close()
    except KeyboardInterrupt:
        interrupted = True
    except BaseException:
        # Rejection is the authority boundary. Cleanup is best effort and a
        # hostile deferred finalizer cannot turn rejection into process exit.
        return
    if interrupted:
        raise KeyboardInterrupt() from None


def _detach_invocation_result(value: Any) -> Any:
    """Return only immutable detached built-ins from a synchronous invocation."""

    if (
        inspect.isawaitable(value)
        or inspect.isgenerator(value)
        or inspect.isasyncgen(value)
    ):
        _discard_deferred_result(value)
        raise CapabilityPluginError(
            "deferred_contribution_result",
            "Contribution calls must complete synchronously",
        )
    if callable(value):
        raise CapabilityPluginError(
            "authority_contribution_result",
            "Contribution calls cannot return executable authority",
        )
    try:
        return _freeze_inert(value)
    except CapabilityPluginError as exc:
        if exc.code != "non_inert_contribution":
            raise
        raise CapabilityPluginError(
            "non_inert_contribution_result",
            "Contribution calls must return closed built-in inert data",
        ) from exc


@dataclass(frozen=True, slots=True)
class PluginLifecycleReceipt:
    command_id: str
    event_id: int
    plugin_id: str
    plugin_version: str
    plugin_provenance_id: str
    source: ManifestSource
    command_transition: LifecycleTransition
    requested_transition: LifecycleTransition
    phase: LifecyclePhase
    event: LifecycleEvent
    desired_state: PluginDesiredState
    effective_state: PluginEffectiveState
    lifecycle_state: PluginLifecycleState
    generation_before: int
    generation_after: int
    contribution_ids: tuple[str, ...]
    outcome: LifecycleOutcome
    restart_required: bool
    timestamp: datetime
    detail_code: str = ""
    detail: str = ""
    disposals: tuple[DisposalReceipt, ...] = ()
    journal_owner_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "command_id": self.command_id,
            "event_id": self.event_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "plugin_provenance_id": self.plugin_provenance_id,
            "source": self.source.value,
            "command_transition": self.command_transition.value,
            "requested_transition": self.requested_transition.value,
            "phase": self.phase.value,
            "event": self.event.value,
            "desired_state": self.desired_state.value,
            "effective_state": self.effective_state.value,
            "lifecycle_state": self.lifecycle_state.value,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "contribution_ids": list(self.contribution_ids),
            "outcome": self.outcome.value,
            "restart_required": self.restart_required,
            "timestamp": self.timestamp.isoformat(),
            "detail_code": self.detail_code,
            "detail": self.detail,
            "disposals": [item.to_dict() for item in self.disposals],
            "journal_owner_id": self.journal_owner_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PluginLifecycleReceipt:
        required_fields = {
            "schema_version",
            "command_id",
            "event_id",
            "plugin_id",
            "plugin_version",
            "plugin_provenance_id",
            "source",
            "command_transition",
            "requested_transition",
            "phase",
            "event",
            "desired_state",
            "effective_state",
            "lifecycle_state",
            "generation_before",
            "generation_after",
            "contribution_ids",
            "outcome",
            "restart_required",
            "timestamp",
            "detail_code",
            "detail",
            "disposals",
        }

        def require_text(key: str, *, allow_empty: bool = False) -> str:
            value = raw[key]
            if type(value) is not str or (not allow_empty and not value):
                raise ValueError(f"{key} must be an exact string")
            return value

        def require_identifier(key: str, pattern: re.Pattern[str]) -> str:
            value = require_text(key)
            if not pattern.fullmatch(value) or redact_detail(value) != value:
                raise ValueError(f"{key} is not a safe bounded identifier")
            return value

        receipt: PluginLifecycleReceipt | None = None
        invalid = False
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("receipt must be a mapping")
            required_fields.add("journal_owner_id")
            unknown = set(raw) - required_fields
            missing = required_fields - set(raw)
            if unknown or missing:
                raise ValueError("receipt fields do not match schema")
            if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
                raise ValueError("receipt schema version is invalid")
            event_id = raw["event_id"]
            generation_before = raw["generation_before"]
            generation_after = raw["generation_after"]
            if type(event_id) is not int or event_id <= 0:
                raise ValueError("event_id must be a positive integer")
            if type(generation_before) is not int or generation_before < 0:
                raise ValueError("generation_before must be a non-negative integer")
            if type(generation_after) is not int or generation_after < 0:
                raise ValueError("generation_after must be a non-negative integer")
            contribution_ids_raw = raw["contribution_ids"]
            if type(contribution_ids_raw) is not list or not all(
                type(item) is str
                and _PLUGIN_ID_RE.fullmatch(item)
                and redact_detail(item) == item
                for item in contribution_ids_raw
            ):
                raise ValueError("contribution_ids must contain safe bounded identifiers")
            if type(raw["restart_required"]) is not bool:
                raise ValueError("restart_required must be a boolean")
            detail_code = require_text("detail_code", allow_empty=True)
            if detail_code and _safe_detail_code(detail_code) != detail_code:
                raise ValueError("detail_code is invalid")
            detail = require_text("detail", allow_empty=True)
            if len(detail) > 400 or redact_detail(detail) != detail:
                raise ValueError("detail is not bounded and redacted")
            timestamp_raw = require_text("timestamp")
            timestamp = datetime.fromisoformat(timestamp_raw)
            if timestamp.tzinfo is None:
                raise ValueError("timestamp must be timezone-aware")
            disposals_raw = raw["disposals"]
            if type(disposals_raw) is not list:
                raise ValueError("disposals must be a list")
            disposals: list[DisposalReceipt] = []
            for item in disposals_raw:
                if type(item) is not dict or set(item) != {
                    "contribution_id",
                    "outcome",
                    "detail",
                }:
                    raise ValueError("disposal receipt fields do not match schema")
                contribution_id = item["contribution_id"]
                disposal_detail = item["detail"]
                if (
                    type(contribution_id) is not str
                    or not _PLUGIN_ID_RE.fullmatch(contribution_id)
                    or redact_detail(contribution_id) != contribution_id
                ):
                    raise ValueError(
                        "disposal contribution_id must be a safe bounded identifier"
                    )
                if type(disposal_detail) is not str or len(disposal_detail) > 400:
                    raise ValueError("disposal detail must be a bounded string")
                if redact_detail(disposal_detail) != disposal_detail:
                    raise ValueError("disposal detail must already be redacted")
                disposals.append(
                    DisposalReceipt(
                        contribution_id=contribution_id,
                        outcome=DisposalOutcome(item["outcome"]),
                        detail=disposal_detail,
                    )
                )
            journal_owner_id = require_identifier("journal_owner_id", _OPERATION_ID_RE)
            plugin_provenance_id = require_text("plugin_provenance_id")
            if not _PROVENANCE_ID_RE.fullmatch(plugin_provenance_id):
                raise ValueError("plugin_provenance_id is invalid")
            receipt = cls(
                command_id=require_identifier("command_id", _OPERATION_ID_RE),
                event_id=event_id,
                plugin_id=require_identifier("plugin_id", _PLUGIN_ID_RE),
                plugin_version=require_identifier(
                    "plugin_version", _PLUGIN_VERSION_RE
                ),
                plugin_provenance_id=plugin_provenance_id,
                source=ManifestSource(require_text("source")),
                command_transition=LifecycleTransition(require_text("command_transition")),
                requested_transition=LifecycleTransition(
                    require_text("requested_transition")
                ),
                phase=LifecyclePhase(require_text("phase")),
                event=LifecycleEvent(require_text("event")),
                desired_state=PluginDesiredState(require_text("desired_state")),
                effective_state=PluginEffectiveState(require_text("effective_state")),
                lifecycle_state=PluginLifecycleState(require_text("lifecycle_state")),
                generation_before=generation_before,
                generation_after=generation_after,
                contribution_ids=tuple(contribution_ids_raw),
                outcome=LifecycleOutcome(require_text("outcome")),
                restart_required=raw["restart_required"],
                timestamp=timestamp.astimezone(UTC),
                detail_code=detail_code,
                detail=detail,
                disposals=tuple(disposals),
                journal_owner_id=journal_owner_id,
            )
            allowed_requested = {
                LifecycleTransition.ENABLE: {
                    LifecycleTransition.ENABLE,
                    LifecycleTransition.LOAD,
                },
                LifecycleTransition.DISABLE: {
                    LifecycleTransition.DISABLE,
                    LifecycleTransition.UNLOAD,
                },
            }
            if receipt.command_transition not in allowed_requested or (
                receipt.requested_transition
                not in allowed_requested[receipt.command_transition]
            ):
                raise ValueError("receipt transition lineage is invalid")
            event_contract = {
                LifecycleEvent.ENABLED: (
                    LifecyclePhase.REQUEST,
                    LifecycleOutcome.ACCEPTED,
                    LifecycleTransition.ENABLE,
                    LifecycleTransition.ENABLE,
                ),
                LifecycleEvent.UNLOAD_REQUESTED: (
                    LifecyclePhase.REQUEST,
                    LifecycleOutcome.ACCEPTED,
                    LifecycleTransition.DISABLE,
                    LifecycleTransition.DISABLE,
                ),
                LifecycleEvent.DRAINING: (
                    LifecyclePhase.PROGRESS,
                    LifecycleOutcome.ACCEPTED,
                    LifecycleTransition.DISABLE,
                    LifecycleTransition.UNLOAD,
                ),
                LifecycleEvent.LOADED: (
                    LifecyclePhase.TERMINAL,
                    LifecycleOutcome.SUCCEEDED,
                    LifecycleTransition.ENABLE,
                    LifecycleTransition.LOAD,
                ),
                LifecycleEvent.UNLOADED: (
                    LifecyclePhase.TERMINAL,
                    LifecycleOutcome.SUCCEEDED,
                    LifecycleTransition.DISABLE,
                    LifecycleTransition.UNLOAD,
                ),
                LifecycleEvent.FAILED: (
                    LifecyclePhase.TERMINAL,
                    LifecycleOutcome.FAILED,
                    None,
                    None,
                ),
                LifecycleEvent.RESTART_REQUIRED: (
                    LifecyclePhase.TERMINAL,
                    LifecycleOutcome.FAILED,
                    None,
                    None,
                ),
                LifecycleEvent.NO_OP: (
                    LifecyclePhase.TERMINAL,
                    LifecycleOutcome.NO_OP,
                    None,
                    None,
                ),
                LifecycleEvent.SUPERSEDED: (
                    LifecyclePhase.TERMINAL,
                    LifecycleOutcome.NO_OP,
                    None,
                    None,
                ),
            }
            expected_phase, expected_outcome, expected_command, expected_request = (
                event_contract[receipt.event]
            )
            if (
                receipt.phase is not expected_phase
                or receipt.outcome is not expected_outcome
                or (
                    expected_command is not None
                    and receipt.command_transition is not expected_command
                )
                or (
                    expected_request is not None
                    and receipt.requested_transition is not expected_request
                )
            ):
                raise ValueError("receipt event contract is invalid")
            if receipt.generation_after < receipt.generation_before:
                raise ValueError("receipt generation transition is invalid")
            if (
                receipt.phase is LifecyclePhase.REQUEST
                and receipt.generation_after != receipt.generation_before
            ):
                raise ValueError("request receipts cannot change generation")
            if len(set(receipt.contribution_ids)) != len(receipt.contribution_ids):
                raise ValueError("receipt contribution IDs must be unique")
            if any(
                disposal.contribution_id not in receipt.contribution_ids
                for disposal in receipt.disposals
            ):
                raise ValueError("receipt disposal is outside the contribution contract")
            state_requires_restart = (
                receipt.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
                or receipt.effective_state is PluginEffectiveState.RESTART_REQUIRED
            )
            if receipt.restart_required is not state_requires_restart:
                raise ValueError("receipt restart-required state is inconsistent")
            if receipt.event is LifecycleEvent.RESTART_REQUIRED and not receipt.restart_required:
                raise ValueError("restart-required event must require restart")
            exact_event_states = {
                LifecycleEvent.DRAINING: (
                    PluginDesiredState.DISABLED,
                    PluginEffectiveState.DRAINING,
                    PluginLifecycleState.DRAINING,
                ),
                LifecycleEvent.LOADED: (
                    PluginDesiredState.ENABLED,
                    PluginEffectiveState.LOADED,
                    PluginLifecycleState.LOADED,
                ),
                LifecycleEvent.UNLOADED: (
                    PluginDesiredState.DISABLED,
                    PluginEffectiveState.UNLOADED,
                    PluginLifecycleState.UNLOADED,
                ),
            }
            expected_state = exact_event_states.get(receipt.event)
            if expected_state is not None and (
                receipt.desired_state,
                receipt.effective_state,
                receipt.lifecycle_state,
            ) != expected_state:
                raise ValueError("receipt event state is inconsistent")
            if receipt.event is LifecycleEvent.ENABLED:
                enabled_states = {
                    (
                        PluginDesiredState.ENABLED,
                        PluginEffectiveState.UNLOADED,
                        PluginLifecycleState.ENABLED,
                    ),
                    (
                        PluginDesiredState.ENABLED,
                        PluginEffectiveState.LOADED,
                        PluginLifecycleState.LOADED,
                    ),
                }
                if (
                    receipt.desired_state,
                    receipt.effective_state,
                    receipt.lifecycle_state,
                ) not in enabled_states:
                    raise ValueError("enabled request state is inconsistent")
            if receipt.event is LifecycleEvent.UNLOAD_REQUESTED:
                unload_request_states = {
                    (
                        PluginDesiredState.DISABLED,
                        PluginEffectiveState.LOADED,
                        PluginLifecycleState.UNLOAD_REQUESTED,
                    ),
                    (
                        PluginDesiredState.DISABLED,
                        PluginEffectiveState.UNLOADED,
                        PluginLifecycleState.UNLOADED,
                    ),
                }
                if (
                    receipt.desired_state,
                    receipt.effective_state,
                    receipt.lifecycle_state,
                ) not in unload_request_states:
                    raise ValueError("unload request state is inconsistent")
        except (KeyError, TypeError, ValueError):
            invalid = True
        if invalid:
            raise ReceiptPersistenceError(
                "receipt journal contains an invalid lifecycle receipt"
            ) from None
        assert receipt is not None
        return receipt


def _prevalidated_receipt_payload(receipt: PluginLifecycleReceipt) -> dict[str, object]:
    """Validate a generated receipt before any journal commit can make it authority."""

    payload = receipt.to_dict()
    payload["event_id"] = max(1, receipt.event_id)
    payload["journal_owner_id"] = "precommit"
    PluginLifecycleReceipt.from_dict(payload)
    return payload


@dataclass(frozen=True, slots=True)
class PluginInstanceView:
    id: str
    version: str
    source: ManifestSource
    desired_state: PluginDesiredState
    effective_state: PluginEffectiveState
    lifecycle_state: PluginLifecycleState
    contribution_ids: tuple[str, ...]
    residual_contribution_ids: tuple[str, ...]
    error_code: str
    detail: str


@dataclass(frozen=True, slots=True)
class _PendingTransition:
    transition: LifecycleTransition
    command_id: str


@dataclass(frozen=True, slots=True)
class _FrozenModuleSource:
    source: bytes | None
    filename: str
    is_package: bool


class _FrozenArtifactLoader(importlib_abc.Loader):
    def __init__(
        self,
        module_source: _FrozenModuleSource,
        finder: _FrozenArtifactFinder,
    ) -> None:
        self._module_source = module_source
        self._finder = finder

    def create_module(self, _spec: object) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        plugin_builtins = dict(vars(python_builtins))
        plugin_builtins["__import__"] = self._finder.plugin_import
        module.__dict__["__builtins__"] = plugin_builtins
        source = self._module_source.source
        if source is None:
            return
        code = compile(
            source,
            self._module_source.filename,
            "exec",
            dont_inherit=True,
        )
        exec(code, module.__dict__)


class _FrozenArtifactFinder(importlib_abc.MetaPathFinder):
    """Resolve exactly one unique plugin namespace from discovery-captured bytes."""

    def __init__(
        self,
        modules: Mapping[str, _FrozenModuleSource],
        *,
        namespace: str,
        public_root: str,
    ) -> None:
        self._modules = MappingProxyType(dict(modules))
        self._namespace = namespace
        self._public_root = public_root
        self._importlib_proxy: ModuleType | None = None

    def _is_plugin_absolute_name(self, name: str) -> bool:
        return name == self._public_root or name.startswith(f"{self._public_root}.")

    def _rewrite_plugin_name(self, name: str) -> str:
        return f"{self._namespace}.{name}"

    def _plugin_import_module(
        self,
        name: str,
        package: str | None = None,
    ) -> ModuleType:
        if name.startswith("."):
            if package and self._is_plugin_absolute_name(package):
                package = self._rewrite_plugin_name(package)
            return importlib.import_module(name, package)
        if self._is_plugin_absolute_name(name):
            return importlib.import_module(self._rewrite_plugin_name(name))
        return importlib.import_module(name, package)

    def _plugin_importlib(self) -> ModuleType:
        if self._importlib_proxy is None:
            proxy = ModuleType("importlib")
            proxy.__dict__.update(vars(importlib))
            proxy.import_module = self._plugin_import_module  # type: ignore[attr-defined]
            self._importlib_proxy = proxy
        return self._importlib_proxy

    def plugin_import(
        self,
        name: str,
        globals: Mapping[str, Any] | None = None,
        locals: Mapping[str, Any] | None = None,
        fromlist: tuple[str, ...] | list[str] = (),
        level: int = 0,
    ) -> Any:
        """Redirect static and dynamic absolute self-imports to frozen modules."""

        if level == 0 and self._is_plugin_absolute_name(name):
            rewritten = self._rewrite_plugin_name(name)
            if fromlist:
                return python_builtins.__import__(
                    rewritten,
                    globals,
                    locals,
                    fromlist,
                    0,
                )
            importlib.import_module(rewritten)
            return sys.modules[f"{self._namespace}.{self._public_root}"]
        if level == 0 and name == "importlib":
            return self._plugin_importlib()
        if level == 0 and name.startswith("importlib.") and not fromlist:
            python_builtins.__import__(name, globals, locals, fromlist, level)
            return self._plugin_importlib()
        return python_builtins.__import__(name, globals, locals, fromlist, level)

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> ModuleSpec | None:
        module_source = self._modules.get(fullname)
        if module_source is None:
            return None
        loader = _FrozenArtifactLoader(module_source, self)
        return importlib_util.spec_from_loader(
            fullname,
            loader,
            origin=module_source.filename,
            is_package=module_source.is_package,
        )


@dataclass(frozen=True, slots=True)
class _LoaderOwnership:
    namespace: str | None
    cleanup_proven: bool
    finder: _FrozenArtifactFinder | None = None

    def owned_module_names(self) -> tuple[str, ...]:
        if self.namespace is None:
            return ()
        prefix = f"{self.namespace}."
        return tuple(
            sorted(
                (
                    name
                    for name in tuple(sys.modules)
                    if name == self.namespace or name.startswith(prefix)
                ),
                key=lambda name: (name.count("."), name),
            )
        )

    def cleanup(self) -> bool:
        if not self.cleanup_proven:
            return False
        if self.finder is not None:
            for index, item in enumerate(tuple(sys.meta_path)):
                if item is self.finder:
                    if index >= len(sys.meta_path) or sys.meta_path[index] is not self.finder:
                        return False
                    sys.meta_path.pop(index)
                    break
            if any(item is self.finder for item in tuple(sys.meta_path)):
                return False
        for module_name in reversed(self.owned_module_names()):
            sys.modules.pop(module_name, None)
        return not self.owned_module_names()


@dataclass(frozen=True, slots=True)
class _LoadedEntrypoint:
    register: Callable[[StagedRegistrar], Any]
    ownership: _LoaderOwnership


@dataclass(frozen=True, slots=True)
class _RegisteredContribution:
    binding: ContributionBinding
    value: Any = field(repr=False, compare=False)
    disposer: Callable[[], object]


@dataclass(slots=True)
class _PluginInstance:
    candidate: CapabilityPluginCandidate
    desired_state: PluginDesiredState
    effective_state: PluginEffectiveState
    lifecycle_state: PluginLifecycleState
    registrations: dict[str, _RegisteredContribution] = field(default_factory=dict)
    ownership: _LoaderOwnership | None = None
    lease_count: int = 0
    deferred_unload: _DeferredUnload | None = None
    residual_contribution_ids: tuple[str, ...] = ()
    error_code: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _DeferredUnload:
    pending: _PendingTransition
    generation_before: int


@dataclass(frozen=True, slots=True)
class _DisposalBatch:
    receipts: tuple[DisposalReceipt, ...]
    failed_ids: tuple[str, ...]
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class _CleanupAttempt:
    proven: bool
    interrupted: bool = False


def _attempt_ownership_cleanup(
    ownership: _LoaderOwnership | None,
) -> _CleanupAttempt:
    if ownership is None:
        return _CleanupAttempt(proven=True)
    try:
        return _CleanupAttempt(proven=ownership.cleanup())
    except KeyboardInterrupt:
        return _CleanupAttempt(proven=False, interrupted=True)
    except BaseException:
        return _CleanupAttempt(proven=False)


def _persistence_interrupted(exc: BaseException) -> bool:
    if isinstance(exc, KeyboardInterrupt):
        return True
    if isinstance(exc, JournalCommitAmbiguousError):
        return exc.interrupted
    return False


@dataclass(frozen=True, slots=True)
class PluginLeaseView:
    lease_id: str
    generation: int
    plugin_ids: tuple[str, ...]
    active_invocations: int
    closing: bool
    release_state: str
    release_error_code: str
    created_at: datetime


@dataclass(slots=True)
class _ActiveLease:
    lease: _SnapshotLease
    generation: int
    plugin_ids: tuple[str, ...]
    created_at: datetime
    accounting_released: bool = False
    completed_receipts: list[PluginLifecycleReceipt] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _LifecycleTransaction:
    token: str
    epoch: int
    owner_thread: int


class StagedRegistrar:
    """Manifest-scoped registrar that publishes nothing outside its transaction."""

    def __init__(self, manifest: CapabilityPluginManifest) -> None:
        self._manifest = manifest
        self._declarations: dict[str, ContributionDeclaration] = {
            item.id: item for item in manifest.contributions
        }
        self._staged: dict[str, _RegisteredContribution] = {}

    @property
    def contribution_ids(self) -> tuple[str, ...]:
        return tuple(self._staged)

    def publish(
        self,
        contribution_id: str,
        value: Any,
        *,
        disposer: Callable[[], object],
        depends_on: Iterable[str] = (),
    ) -> RegistrationReceipt:
        declaration = self._declarations.get(contribution_id)
        if declaration is None:
            raise CapabilityPluginError(
                "undeclared_contribution", "Plugin attempted to publish an undeclared contribution"
            )
        if contribution_id in self._staged:
            raise CapabilityPluginError(
                "duplicate_registration", "Contribution was registered more than once"
            )
        dependencies = tuple(depends_on)
        if dependencies != declaration.depends_on:
            raise CapabilityPluginError(
                "registration_dependency_mismatch",
                "Published dependency edges do not match the manifest",
            )
        if callable(value) and _is_deferred_callable(value):
            raise CapabilityPluginError(
                "deferred_contribution_not_supported",
                "Published contribution functions must be synchronous",
            )
        if not callable(disposer) or _is_deferred_callable(disposer):
            raise CapabilityPluginError(
                "invalid_disposer", "Every contribution requires a synchronous disposer"
            )

        binding = ContributionBinding(
            id=contribution_id,
            type=declaration.type,
            plugin_id=self._manifest.id,
            plugin_version=self._manifest.version,
            depends_on=declaration.depends_on,
        )
        self._staged[contribution_id] = _RegisteredContribution(
            binding=binding,
            value=value,
            disposer=disposer,
        )
        return RegistrationReceipt(
            plugin_id=self._manifest.id,
            contribution_id=contribution_id,
            depends_on=declaration.depends_on,
            disposer_registered=True,
        )

    def exact_registrations(self) -> dict[str, _RegisteredContribution]:
        declared = set(self._declarations)
        registered = set(self._staged)
        if declared != registered:
            raise CapabilityPluginError(
                "registration_set_mismatch",
                "Registered contributions do not exactly match the manifest",
            )
        return dict(self._staged)


class CapabilityPluginKernel:
    """One locked authority for generic capability lifecycle and snapshots."""

    def __init__(
        self,
        candidates: Iterable[CapabilityPluginCandidate],
        *,
        receipt_path: Path | None = None,
        receipt_writer: Callable[[PluginLifecycleReceipt], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        command_id_factory: Callable[[], str] | None = None,
        environ: Mapping[str, str] | None = None,
        core_version: str | None = None,
    ) -> None:
        ordered = _validate_and_order_candidates(tuple(candidates))
        self._lock = threading.RLock()
        self._lease_condition = threading.Condition(self._lock)
        self._candidates = ordered
        self._instances: dict[str, _PluginInstance] = {}
        self._bindings: dict[str, _RegisteredContribution] = {}
        self._pending: dict[str, _PendingTransition] = {}
        self._journaled_pending: set[str] = set()
        self._generation = 0
        self._active_leases: dict[str, _ActiveLease] = {}
        self._receipt_path = Path(receipt_path) if receipt_path is not None else None
        self._receipt_writer = receipt_writer
        self._clock = clock
        self._command_id_factory = command_id_factory
        self._environ = environ
        self._core_version = core_version
        self._recovered_journal_path: Path | None = None
        self._journal: LockedLifecycleJournal | None = None
        self._writer_records: dict[str, PluginLifecycleReceipt] = {}
        self._writer_event_id = 0
        self._transaction_epoch = 0
        self._active_transaction: _LifecycleTransaction | None = None
        self._closing = False
        self._closed = False

        for candidate in ordered:
            enabled = candidate.manifest.enabled_by_default
            instance = _PluginInstance(
                candidate=candidate,
                desired_state=(
                    PluginDesiredState.ENABLED if enabled else PluginDesiredState.DISABLED
                ),
                effective_state=PluginEffectiveState.UNLOADED,
                lifecycle_state=(
                    PluginLifecycleState.ENABLED if enabled else PluginLifecycleState.DISCOVERED
                ),
            )
            self._instances[candidate.manifest.id] = instance
            if enabled:
                self._pending[candidate.manifest.id] = _PendingTransition(
                    LifecycleTransition.ENABLE,
                    self._new_command_id(),
                )

        if self._receipt_path is not None and self._receipt_writer is None:
            recovery_interrupted = False
            cleanup_failed = False
            recovery_failed = False
            capability_failure: tuple[str, str] | None = None
            try:
                self._ensure_journal_recovered()
            except BaseException as exc:
                recovery_interrupted = isinstance(exc, KeyboardInterrupt)
                recovery_failed = True
                if type(exc) is CapabilityPluginError:
                    capability_failure = (exc.code, exc.detail)
                journal = self._journal
                self._journal = None
                self._closed = True
                if journal is not None:
                    try:
                        journal.close()
                    except BaseException as cleanup_exc:
                        recovery_interrupted = recovery_interrupted or isinstance(
                            cleanup_exc, KeyboardInterrupt
                        )
                        cleanup_failed = not isinstance(cleanup_exc, KeyboardInterrupt)
            if recovery_interrupted:
                raise KeyboardInterrupt() from None
            if cleanup_failed:
                raise ReceiptPersistenceError(
                    "journal cleanup failed after initial recovery failure"
                ) from None
            if capability_failure is not None:
                raise CapabilityPluginError(*capability_failure) from None
            if recovery_failed:
                raise ReceiptPersistenceError("initial lifecycle recovery failed") from None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        return tuple(candidate.manifest.id for candidate in self._candidates)

    def state(self, plugin_id: str) -> PluginInstanceView:
        with self._lock:
            instance = self._get_instance(plugin_id)
            return PluginInstanceView(
                id=instance.candidate.manifest.id,
                version=instance.candidate.manifest.version,
                source=instance.candidate.manifest.source,
                desired_state=instance.desired_state,
                effective_state=instance.effective_state,
                lifecycle_state=instance.lifecycle_state,
                contribution_ids=tuple(instance.registrations),
                residual_contribution_ids=instance.residual_contribution_ids,
                error_code=instance.error_code,
                detail=instance.detail,
            )

    def outstanding_leases(self) -> tuple[PluginLeaseView, ...]:
        """Return bounded diagnostics for snapshots the kernel still owns."""

        with self._lock:
            views: list[PluginLeaseView] = []
            for lease_id, active in sorted(self._active_leases.items()):
                invocation_count, closing, release_state, release_error_code = (
                    active.lease.diagnostic_state()
                )
                views.append(
                    PluginLeaseView(
                        lease_id=lease_id,
                        generation=active.generation,
                        plugin_ids=active.plugin_ids,
                        active_invocations=invocation_count,
                        closing=closing,
                        release_state=release_state,
                        release_error_code=release_error_code,
                        created_at=active.created_at,
                    )
                )
            return tuple(views)

    def close(self, *, timeout: float = 0.0) -> None:
        """Release journal ownership only after callers prove a clean drain.

        Close never invokes plugin callbacks implicitly.  It waits up to
        ``timeout`` for snapshot leases, then refuses with the owner intact if
        a forgotten lease or any loaded/residual physical state remains.
        """

        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        journal: LockedLifecycleJournal | None = None
        with self._lease_condition:
            if self._closed:
                return
            if self._closing:
                raise CapabilityPluginError(
                    "kernel_closing", "Another kernel close is already active"
                )
            if self._active_transaction is not None:
                raise CapabilityPluginError(
                    "lifecycle_transaction_active",
                    "Kernel close is refused while a lifecycle callback is active",
                )
            self._closing = True
            try:
                deadline = time.monotonic() + timeout
                while self._active_leases:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        outstanding = ",".join(sorted(self._active_leases))
                        raise CapabilityPluginError(
                            "outstanding_snapshot_leases",
                            f"Kernel close refused; outstanding lease IDs: {outstanding}",
                        )
                    self._lease_condition.wait(timeout=remaining)

                physically_active = tuple(
                    sorted(
                        plugin_id
                        for plugin_id, instance in self._instances.items()
                        if instance.registrations
                        or instance.ownership is not None
                        or instance.deferred_unload is not None
                        or instance.effective_state
                        in {
                            PluginEffectiveState.LOADED,
                            PluginEffectiveState.DRAINING,
                            PluginEffectiveState.RESTART_REQUIRED,
                        }
                        or instance.lifecycle_state
                        not in {
                            PluginLifecycleState.DISCOVERED,
                            PluginLifecycleState.UNLOADED,
                            PluginLifecycleState.FAILED,
                        }
                    )
                )
                if physically_active:
                    raise CapabilityPluginError(
                        "plugins_not_drained",
                        "Kernel close refused; disable and drain plugins first: "
                        + ",".join(physically_active),
                    )
                journal = self._journal
            except BaseException:
                self._closing = False
                raise
        try:
            if journal is not None:
                journal.close()
        except BaseException:
            with self._lease_condition:
                if journal is not None and bool(getattr(journal, "closed", False)):
                    self._closed = True
                    self._journal = None
                self._closing = False
            raise
        with self._lease_condition:
            self._closed = True
            self._journal = None
            self._closing = False

    def snapshot(self) -> PluginSnapshot:
        with self._lock:
            self._require_open("snapshot")
            registrations = dict(self._bindings)
            active_plugin_ids = {
                registered.binding.plugin_id for registered in registrations.values()
            }
            plugins = tuple(
                PluginIdentity(candidate.manifest.id, candidate.manifest.version)
                for candidate in self._candidates
                if candidate.manifest.id in active_plugin_ids
            )
            for identity in plugins:
                self._instances[identity.id].lease_count += 1
            lease_id = uuid.uuid4().hex
            lease = _SnapshotLease(
                lease_id,
                MappingProxyType(
                    {
                        contribution_id: registered.value
                        for contribution_id, registered in registrations.items()
                    }
                ),
                self._release_snapshot,
            )
            handles = MappingProxyType(
                {
                    contribution_id: ContributionHandle(
                        id=registered.binding.id,
                        type=registered.binding.type,
                        plugin_id=registered.binding.plugin_id,
                        plugin_version=registered.binding.plugin_version,
                        depends_on=registered.binding.depends_on,
                        _lease=lease,
                    )
                    for contribution_id, registered in registrations.items()
                }
            )
            plugin_ids = tuple(identity.id for identity in plugins)
            self._active_leases[lease_id] = _ActiveLease(
                lease=lease,
                generation=self._generation,
                plugin_ids=plugin_ids,
                created_at=self._now(),
            )
            return PluginSnapshot(
                lease_id=lease_id,
                generation=self._generation,
                plugins=plugins,
                contributions=handles,
                _lease=lease,
            )

    def _release_snapshot(
        self,
        lease_id: str,
    ) -> tuple[PluginLifecycleReceipt, ...]:
        with self._lock:
            active = self._active_leases.get(lease_id)
            if active is None:
                raise CapabilityPluginError(
                    "snapshot_lease_unknown", "Capability snapshot lease is not outstanding"
                )
            if not active.accounting_released:
                for plugin_id in active.plugin_ids:
                    instance = self._instances[plugin_id]
                    if instance.lease_count <= 0:
                        raise CapabilityPluginError(
                            "snapshot_lease_underflow",
                            "Capability snapshot lease accounting failed",
                        )
                    instance.lease_count -= 1
                active.accounting_released = True
            ready = self._ready_deferred_unloads()
            if self._active_transaction is not None:
                if ready:
                    raise CapabilityPluginError(
                        "snapshot_release_deferred",
                        "Another lifecycle transaction is active; retry snapshot close",
                    )
                receipts = tuple(active.completed_receipts)
                self._active_leases.pop(lease_id, None)
                self._lease_condition.notify_all()
                return receipts
            if not ready:
                receipts = tuple(active.completed_receipts)
                self._active_leases.pop(lease_id, None)
                self._lease_condition.notify_all()
                return receipts
            transaction = self._begin_transaction()

        try:
            for instance, deferred in ready:
                receipt = self._complete_unload(
                    instance,
                    deferred.pending,
                    generation_before=deferred.generation_before,
                    transaction=transaction,
                )
                with self._lock:
                    current = self._active_leases.get(lease_id)
                    if current is not active:
                        raise CapabilityPluginError(
                            "snapshot_lease_lost",
                            "Capability snapshot release lost lease authority",
                        )
                    active.completed_receipts.append(receipt)
        except BaseException:
            with self._lock:
                for instance, deferred in ready:
                    if instance.deferred_unload is deferred:
                        # The callback never entered its state transition and
                        # the same physical attempt is safe to retry.
                        continue
                    if (
                        instance.effective_state is PluginEffectiveState.UNLOADED
                        and not instance.registrations
                        and instance.ownership is None
                    ):
                        # This unload completed before a later release failed.
                        continue
                    instance.deferred_unload = None
                    instance.residual_contribution_ids = tuple(instance.registrations)
                    instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
                    instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
                    instance.error_code = "snapshot_release_interrupted"
                    instance.detail = (
                        "Snapshot release interrupted physical disposal; restart is required"
                    )
            raise
        finally:
            with self._lock:
                self._end_transaction(transaction)
        with self._lock:
            current = self._active_leases.get(lease_id)
            if current is not active:
                raise CapabilityPluginError(
                    "snapshot_lease_lost", "Capability snapshot release lost lease authority"
                )
            completed = tuple(active.completed_receipts)
            self._active_leases.pop(lease_id, None)
            self._lease_condition.notify_all()
        return completed

    def request_enable(
        self,
        plugin_id: str,
        *,
        command_id: str | None = None,
    ) -> PluginLifecycleReceipt:
        with self._lock:
            self._require_mutation_allowed()
            self._ensure_journal_recovered()
            instance = self._get_instance(plugin_id)
            resolved_command_id = self._resolve_command_id(command_id)
            replay = self._existing_command_receipt(
                resolved_command_id,
                plugin_id,
                LifecycleTransition.ENABLE,
            )
            if replay is not None:
                if instance.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED:
                    return replace(
                        replay,
                        event_id=0,
                        event=LifecycleEvent.RESTART_REQUIRED,
                        desired_state=instance.desired_state,
                        effective_state=instance.effective_state,
                        lifecycle_state=instance.lifecycle_state,
                        outcome=LifecycleOutcome.FAILED,
                        restart_required=True,
                        detail_code="restart_required",
                        detail="Historical receipt cannot replace restart-required local state",
                    )
                self._reconcile_replayed_receipt(instance, replay)
                return replay
            pending = self._pending.get(plugin_id)
            if instance.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED:
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.ENABLE,
                        event=LifecycleEvent.FAILED,
                        outcome=LifecycleOutcome.FAILED,
                        desired_state=instance.desired_state,
                        lifecycle_state=instance.lifecycle_state,
                        detail_code="restart_required",
                        detail="Process restart is required before this plugin can be enabled",
                    ),
                )[0]
            if instance.effective_state is PluginEffectiveState.DRAINING:
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.ENABLE,
                        event=LifecycleEvent.FAILED,
                        outcome=LifecycleOutcome.FAILED,
                        desired_state=instance.desired_state,
                        lifecycle_state=instance.lifecycle_state,
                        detail_code="snapshot_leases_draining",
                        detail="Prior turn snapshots must release before this plugin can reload",
                    ),
                )[0]
            if instance.effective_state is PluginEffectiveState.LOADED and (
                instance.desired_state is PluginDesiredState.ENABLED
                and (pending is None or pending.transition is LifecycleTransition.ENABLE)
            ):
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.ENABLE,
                        event=LifecycleEvent.NO_OP,
                        outcome=LifecycleOutcome.NO_OP,
                        desired_state=PluginDesiredState.ENABLED,
                        lifecycle_state=PluginLifecycleState.LOADED,
                        detail_code="already_enabled",
                    ),
                )[0]
            if (
                pending is not None
                and pending.transition is LifecycleTransition.ENABLE
                and instance.desired_state is PluginDesiredState.ENABLED
            ):
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.ENABLE,
                        event=LifecycleEvent.NO_OP,
                        outcome=LifecycleOutcome.NO_OP,
                        desired_state=PluginDesiredState.ENABLED,
                        lifecycle_state=instance.lifecycle_state,
                        detail_code="enable_already_requested",
                    ),
                )[0]

            next_lifecycle = (
                PluginLifecycleState.LOADED
                if instance.effective_state is PluginEffectiveState.LOADED
                else PluginLifecycleState.ENABLED
            )
            receipt = self._make_receipt(
                instance,
                command_id=resolved_command_id,
                transition=LifecycleTransition.ENABLE,
                event=LifecycleEvent.ENABLED,
                outcome=LifecycleOutcome.ACCEPTED,
                desired_state=PluginDesiredState.ENABLED,
                lifecycle_state=next_lifecycle,
            )
            if pending is not None and pending.command_id in self._journaled_pending:
                persisted, replayed = self._persist_superseding_request(
                    instance, pending, receipt
                )
            else:
                persisted, replayed = self._persist_request_receipt(instance, receipt)
            if replayed:
                self._reconcile_replayed_receipt(instance, persisted)
                return persisted
            if persisted.outcome is LifecycleOutcome.FAILED:
                return persisted
            instance.desired_state = PluginDesiredState.ENABLED
            instance.lifecycle_state = next_lifecycle
            if pending is not None:
                self._journaled_pending.discard(pending.command_id)
            self._journaled_pending.add(resolved_command_id)
            if instance.effective_state is PluginEffectiveState.LOADED:
                self._pending.pop(plugin_id, None)
                if pending is not None:
                    return self._persist_immediate_supersession_terminal(
                        instance,
                        persisted,
                        transition=LifecycleTransition.LOAD,
                        event=LifecycleEvent.LOADED,
                    )
            else:
                self._pending[plugin_id] = _PendingTransition(
                    LifecycleTransition.ENABLE,
                    resolved_command_id,
                )
            return persisted

    def request_disable(
        self,
        plugin_id: str,
        *,
        command_id: str | None = None,
    ) -> PluginLifecycleReceipt:
        with self._lock:
            self._require_mutation_allowed()
            self._ensure_journal_recovered()
            instance = self._get_instance(plugin_id)
            resolved_command_id = self._resolve_command_id(command_id)
            replay = self._existing_command_receipt(
                resolved_command_id,
                plugin_id,
                LifecycleTransition.DISABLE,
            )
            if replay is not None:
                if instance.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED:
                    return replace(
                        replay,
                        event_id=0,
                        event=LifecycleEvent.RESTART_REQUIRED,
                        desired_state=instance.desired_state,
                        effective_state=instance.effective_state,
                        lifecycle_state=instance.lifecycle_state,
                        outcome=LifecycleOutcome.FAILED,
                        restart_required=True,
                        detail_code="restart_required_disable_refused",
                        detail="Historical receipt cannot replace restart-required local state",
                    )
                self._reconcile_replayed_receipt(instance, replay)
                return replay
            pending = self._pending.get(plugin_id)
            if instance.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED:
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.DISABLE,
                        event=LifecycleEvent.FAILED,
                        outcome=LifecycleOutcome.FAILED,
                        desired_state=instance.desired_state,
                        lifecycle_state=PluginLifecycleState.RESTART_REQUIRED,
                        detail_code="restart_required_disable_refused",
                        detail="Disable intent was not changed because restart is already required",
                        restart_required=True,
                    ),
                )[0]
            undrained_dependents = tuple(
                sorted(
                    candidate.manifest.id
                    for candidate in self._candidates
                    if plugin_id in candidate.manifest.requirements.plugins
                    and (
                        self._instances[candidate.manifest.id].registrations
                        or self._instances[candidate.manifest.id].ownership is not None
                        or self._instances[candidate.manifest.id].deferred_unload is not None
                        or self._instances[candidate.manifest.id].effective_state
                        in {
                            PluginEffectiveState.LOADED,
                            PluginEffectiveState.DRAINING,
                            PluginEffectiveState.RESTART_REQUIRED,
                        }
                    )
                )
            )
            if undrained_dependents:
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.DISABLE,
                        event=LifecycleEvent.FAILED,
                        outcome=LifecycleOutcome.FAILED,
                        desired_state=instance.desired_state,
                        lifecycle_state=instance.lifecycle_state,
                        detail_code="loaded_dependents_present",
                        detail=(
                            "Dependent plugins must be fully drained first: "
                            + ",".join(undrained_dependents)
                        ),
                    ),
                )[0]
            if (
                instance.effective_state is PluginEffectiveState.UNLOADED
                and (pending is None or pending.transition is not LifecycleTransition.ENABLE)
                and instance.desired_state is PluginDesiredState.DISABLED
            ):
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.DISABLE,
                        event=LifecycleEvent.NO_OP,
                        outcome=LifecycleOutcome.NO_OP,
                        desired_state=PluginDesiredState.DISABLED,
                        lifecycle_state=instance.lifecycle_state,
                        detail_code="already_disabled",
                    ),
                )[0]
            if (
                pending is not None
                and pending.transition is LifecycleTransition.DISABLE
                and instance.desired_state is PluginDesiredState.DISABLED
            ):
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.DISABLE,
                        event=LifecycleEvent.NO_OP,
                        outcome=LifecycleOutcome.NO_OP,
                        desired_state=PluginDesiredState.DISABLED,
                        lifecycle_state=PluginLifecycleState.UNLOAD_REQUESTED,
                        detail_code="disable_already_requested",
                    ),
                )[0]
            if (
                instance.effective_state is PluginEffectiveState.DRAINING
                and instance.desired_state is PluginDesiredState.DISABLED
            ):
                return self._persist_request_receipt(
                    instance,
                    self._make_receipt(
                        instance,
                        command_id=resolved_command_id,
                        transition=LifecycleTransition.DISABLE,
                        event=LifecycleEvent.NO_OP,
                        outcome=LifecycleOutcome.NO_OP,
                        desired_state=PluginDesiredState.DISABLED,
                        lifecycle_state=PluginLifecycleState.DRAINING,
                        detail_code="snapshot_leases_draining",
                    ),
                )[0]

            active = instance.effective_state is PluginEffectiveState.LOADED
            next_lifecycle = (
                PluginLifecycleState.UNLOAD_REQUESTED if active else PluginLifecycleState.UNLOADED
            )
            receipt = self._make_receipt(
                instance,
                command_id=resolved_command_id,
                transition=LifecycleTransition.DISABLE,
                event=LifecycleEvent.UNLOAD_REQUESTED,
                outcome=LifecycleOutcome.ACCEPTED,
                desired_state=PluginDesiredState.DISABLED,
                lifecycle_state=next_lifecycle,
            )
            if pending is not None and pending.command_id in self._journaled_pending:
                persisted, replayed = self._persist_superseding_request(
                    instance, pending, receipt
                )
            else:
                persisted, replayed = self._persist_request_receipt(instance, receipt)
            if replayed:
                self._reconcile_replayed_receipt(instance, persisted)
                return persisted
            if persisted.outcome is LifecycleOutcome.FAILED:
                return persisted
            instance.desired_state = PluginDesiredState.DISABLED
            instance.lifecycle_state = next_lifecycle
            if pending is not None:
                self._journaled_pending.discard(pending.command_id)
            self._journaled_pending.add(resolved_command_id)
            if active:
                self._pending[plugin_id] = _PendingTransition(
                    LifecycleTransition.DISABLE,
                    resolved_command_id,
                )
            else:
                self._pending.pop(plugin_id, None)
                if pending is not None:
                    return self._persist_immediate_supersession_terminal(
                        instance,
                        persisted,
                        transition=LifecycleTransition.UNLOAD,
                        event=LifecycleEvent.UNLOADED,
                    )
            return persisted

    def apply_turn_boundary(self) -> tuple[PluginLifecycleReceipt, ...]:
        """Apply queued lifecycle changes atomically with respect to snapshots."""

        with self._lock:
            self._require_mutation_allowed()
            self._ensure_journal_recovered()
            transaction = self._begin_transaction()

        receipts: list[PluginLifecycleReceipt] = []
        try:
            for candidate in reversed(self._candidates):
                plugin_id = candidate.manifest.id
                with self._lock:
                    self._assert_transaction(transaction)
                    pending = self._pending.get(plugin_id)
                    if (
                        pending is None
                        or pending.transition is not LifecycleTransition.DISABLE
                    ):
                        continue
                    instance = self._instances[plugin_id]
                    bootstrap = self._persist_pending_request_if_needed(instance, pending)
                if bootstrap is not None:
                    receipts.append(bootstrap)
                    if bootstrap.outcome is LifecycleOutcome.FAILED:
                        continue
                receipts.append(self._unload_instance(instance, pending, transaction))
                with self._lock:
                    self._assert_transaction(transaction)
                    if self._pending.get(plugin_id) is pending:
                        self._pending.pop(plugin_id, None)

            for candidate in self._candidates:
                plugin_id = candidate.manifest.id
                with self._lock:
                    self._assert_transaction(transaction)
                    pending = self._pending.get(plugin_id)
                    if (
                        pending is None
                        or pending.transition is not LifecycleTransition.ENABLE
                    ):
                        continue
                    instance = self._instances[plugin_id]
                    bootstrap = self._persist_pending_request_if_needed(instance, pending)
                if bootstrap is not None:
                    receipts.append(bootstrap)
                    if bootstrap.outcome is LifecycleOutcome.FAILED:
                        continue
                receipts.append(self._load_instance(instance, pending, transaction))
                with self._lock:
                    self._assert_transaction(transaction)
                    if self._pending.get(plugin_id) is pending:
                        self._pending.pop(plugin_id, None)

            while True:
                with self._lock:
                    self._assert_transaction(transaction)
                    ready = self._ready_deferred_unloads()
                if not ready:
                    break
                for instance, deferred in ready:
                    receipts.append(
                        self._complete_unload(
                            instance,
                            deferred.pending,
                            generation_before=deferred.generation_before,
                            transaction=transaction,
                        )
                    )
            return tuple(receipts)
        finally:
            with self._lock:
                self._end_transaction(transaction)

    def _load_instance(
        self,
        instance: _PluginInstance,
        pending: _PendingTransition,
        transaction: _LifecycleTransaction,
    ) -> PluginLifecycleReceipt:
        with self._lock:
            self._assert_transaction(transaction)
            manifest = instance.candidate.manifest
            if self._pending.get(manifest.id) is pending:
                self._pending.pop(manifest.id, None)
            generation_before = self._generation
            try:
                self._enforce_runtime_requirements(manifest)
            except CapabilityPluginError as exc:
                instance.lifecycle_state = PluginLifecycleState.FAILED
                instance.effective_state = PluginEffectiveState.UNLOADED
                instance.error_code = exc.code
                instance.detail = exc.detail
                receipt = self._make_receipt(
                    instance,
                    command_id=pending.command_id,
                    transition=LifecycleTransition.LOAD,
                    event=LifecycleEvent.FAILED,
                    outcome=LifecycleOutcome.FAILED,
                    generation_before=generation_before,
                    detail_code=instance.error_code,
                    detail=instance.detail,
                )
                return self._persist_terminal_receipt(
                    instance, receipt, effects_ran=False
                )
            instance.lifecycle_state = PluginLifecycleState.LOADING

        registrar = StagedRegistrar(manifest)
        loaded: _LoadedEntrypoint | None = None
        registrations: dict[str, _RegisteredContribution] = {}
        load_failure_receipt: PluginLifecycleReceipt | None = None
        propagate_clean_interrupt = False
        try:
            loaded = _load_frozen_entrypoint(instance.candidate)
            if _is_deferred_callable(loaded.register):
                raise CapabilityPluginError(
                    "deferred_register_not_supported",
                    "Plugin register must be a synchronous function",
                )
            result = loaded.register(registrar)
            if (
                inspect.isawaitable(result)
                or inspect.isgenerator(result)
                or inspect.isasyncgen(result)
            ):
                _discard_deferred_result(result)
                raise CapabilityPluginError(
                    "deferred_register_not_supported",
                    "Plugin register must complete synchronously",
                )
            registrations = registrar.exact_registrations()
            with self._lock:
                self._assert_transaction(transaction)
                conflicting = set(registrations).intersection(self._bindings)
                if conflicting:
                    raise CapabilityPluginError(
                        "runtime_contribution_conflict",
                        "Contribution conflict reached load stage",
                    )
        except BaseException as exc:  # plugin-controlled process-exit types are isolated
            staged = dict(registrar._staged)
            disposal_batch = self._dispose_registrations(manifest, staged)
            disposals = disposal_batch.receipts
            failed_ids = disposal_batch.failed_ids
            secret_values = self._secret_values(manifest)
            load_error_code = _error_code(exc, secret_values=secret_values)
            cleanup_attempt = (
                _CleanupAttempt(
                    proven=(
                        load_error_code != "module_cleanup_unproven"
                        and not isinstance(exc, _OwnershipCleanupInterrupted)
                    )
                )
                if loaded is None
                else _CleanupAttempt(proven=False)
                if failed_ids
                else _attempt_ownership_cleanup(loaded.ownership)
            )
            cleanup_proven = cleanup_attempt.proven
            interrupted = (
                disposal_batch.interrupted
                or cleanup_attempt.interrupted
                or isinstance(exc, KeyboardInterrupt)
            )
            restart_required = bool(failed_ids) or not cleanup_proven
            with self._lock:
                self._assert_transaction(transaction)
                instance.registrations = {
                    item: staged[item] for item in failed_ids if item in staged
                }
                instance.ownership = (
                    loaded.ownership if loaded is not None and restart_required else None
                )
                instance.residual_contribution_ids = failed_ids
                instance.effective_state = (
                    PluginEffectiveState.RESTART_REQUIRED
                    if restart_required
                    else PluginEffectiveState.UNLOADED
                )
                instance.lifecycle_state = (
                    PluginLifecycleState.RESTART_REQUIRED
                    if restart_required
                    else PluginLifecycleState.FAILED
                )
                instance.error_code = (
                    "rollback_failed"
                    if failed_ids
                    else "module_cleanup_unproven"
                    if not cleanup_proven
                    else "operator_interrupted"
                    if isinstance(exc, KeyboardInterrupt)
                    else _error_code(exc, secret_values=secret_values)
                )
                instance.detail = self._redacted_exception_detail(exc, manifest)
                receipt = self._make_receipt(
                    instance,
                    command_id=pending.command_id,
                    transition=LifecycleTransition.LOAD,
                    event=(
                        LifecycleEvent.RESTART_REQUIRED
                        if restart_required
                        else LifecycleEvent.FAILED
                    ),
                    outcome=LifecycleOutcome.FAILED,
                    generation_before=generation_before,
                    restart_required=restart_required,
                    detail_code=instance.error_code,
                    detail=instance.detail,
                    disposals=disposals,
                )
                persisted = self._persist_terminal_receipt(
                    instance,
                    receipt,
                    effects_ran=True,
                )
                if interrupted:
                    if self._pending.get(manifest.id) is pending:
                        self._pending.pop(manifest.id, None)
                    propagate_clean_interrupt = True
                load_failure_receipt = persisted

        if load_failure_receipt is not None:
            if propagate_clean_interrupt:
                raise KeyboardInterrupt() from None
            return load_failure_receipt

        receipt_interrupted = False
        with self._lock:
            self._assert_transaction(transaction)
            self._bindings.update(registrations)
            instance.registrations = registrations
            instance.ownership = loaded.ownership
            instance.residual_contribution_ids = ()
            instance.error_code = ""
            instance.detail = ""
            instance.effective_state = PluginEffectiveState.LOADED
            instance.lifecycle_state = PluginLifecycleState.LOADED
            self._generation += 1
            receipt = self._make_receipt(
                instance,
                command_id=pending.command_id,
                transition=LifecycleTransition.LOAD,
                event=LifecycleEvent.LOADED,
                outcome=LifecycleOutcome.SUCCEEDED,
                generation_before=generation_before,
            )
            try:
                persisted, _replayed = self._write_receipt(
                    receipt, unique_terminal=True
                )
            except BaseException as exc:  # callbacks and cleanup run below, unlocked
                for contribution_id in registrations:
                    self._bindings.pop(contribution_id, None)
                instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
                instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
                instance.error_code = "receipt_write_failed"
                instance.detail = "Lifecycle receipt could not be durably persisted"
                receipt_interrupted = _persistence_interrupted(exc)
            else:
                return persisted

        disposal_batch = self._dispose_registrations(manifest, registrations)
        disposals = disposal_batch.receipts
        failed_ids = disposal_batch.failed_ids
        cleanup_attempt = (
            _CleanupAttempt(proven=False)
            if failed_ids
            else _attempt_ownership_cleanup(loaded.ownership)
        )
        cleanup_proven = cleanup_attempt.proven
        interrupted = (
            disposal_batch.interrupted
            or cleanup_attempt.interrupted
            or receipt_interrupted
        )
        with self._lock:
            self._assert_transaction(transaction)
            instance.registrations = {
                item: registrations[item] for item in failed_ids if item in registrations
            }
            instance.ownership = loaded.ownership if not cleanup_proven else None
            instance.residual_contribution_ids = failed_ids
            instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
            instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
            failed = replace(
                receipt,
                event=LifecycleEvent.RESTART_REQUIRED,
                effective_state=PluginEffectiveState.RESTART_REQUIRED,
                lifecycle_state=PluginLifecycleState.RESTART_REQUIRED,
                outcome=LifecycleOutcome.FAILED,
                restart_required=True,
                detail_code=instance.error_code,
                detail=instance.detail,
                disposals=disposals,
            )
            if interrupted:
                if self._pending.get(manifest.id) is pending:
                    self._pending.pop(manifest.id, None)
                raise KeyboardInterrupt() from None
            return failed

    def _unload_instance(
        self,
        instance: _PluginInstance,
        pending: _PendingTransition,
        transaction: _LifecycleTransaction,
    ) -> PluginLifecycleReceipt:
        with self._lock:
            self._assert_transaction(transaction)
            manifest = instance.candidate.manifest
            if self._pending.get(manifest.id) is pending:
                self._pending.pop(manifest.id, None)
            generation_before = self._generation
            registrations = dict(instance.registrations)
            for contribution_id in registrations:
                self._bindings.pop(contribution_id, None)
            if registrations:
                self._generation += 1
            if instance.lease_count:
                instance.effective_state = PluginEffectiveState.DRAINING
                instance.lifecycle_state = PluginLifecycleState.DRAINING
                instance.deferred_unload = _DeferredUnload(
                    pending=pending,
                    generation_before=generation_before,
                )
                receipt = self._make_receipt(
                    instance,
                    command_id=pending.command_id,
                    transition=LifecycleTransition.UNLOAD,
                    event=LifecycleEvent.DRAINING,
                    outcome=LifecycleOutcome.ACCEPTED,
                    generation_before=generation_before,
                    detail_code="snapshot_leases_draining",
                    detail="Physical disposal is deferred until prior turn snapshots release",
                )
                try:
                    persisted, _replayed = self._write_receipt(receipt)
                    return persisted
                except BaseException as exc:  # authority changed without durable proof
                    instance.deferred_unload = None
                    instance.residual_contribution_ids = tuple(registrations)
                    instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
                    instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
                    instance.error_code = "receipt_write_failed"
                    instance.detail = "Lifecycle receipt could not be durably persisted"
                    failed = replace(
                        receipt,
                        phase=LifecyclePhase.TERMINAL,
                        event=LifecycleEvent.RESTART_REQUIRED,
                        effective_state=PluginEffectiveState.RESTART_REQUIRED,
                        lifecycle_state=PluginLifecycleState.RESTART_REQUIRED,
                        outcome=LifecycleOutcome.FAILED,
                        restart_required=True,
                        detail_code=instance.error_code,
                        detail=instance.detail,
                    )
                    persistence_interrupted = _persistence_interrupted(exc)
                if persistence_interrupted:
                    if self._pending.get(manifest.id) is pending:
                        self._pending.pop(manifest.id, None)
                    raise KeyboardInterrupt() from None
                return failed

        return self._complete_unload(
            instance,
            pending,
            generation_before=generation_before,
            transaction=transaction,
        )

    def _complete_unload(
        self,
        instance: _PluginInstance,
        pending: _PendingTransition,
        *,
        generation_before: int,
        transaction: _LifecycleTransaction,
    ) -> PluginLifecycleReceipt:
        with self._lock:
            self._assert_transaction(transaction)
            manifest = instance.candidate.manifest
            registrations = dict(instance.registrations)
            instance.deferred_unload = None
            instance.effective_state = PluginEffectiveState.DRAINING
            instance.lifecycle_state = PluginLifecycleState.DRAINING

        disposal_batch = self._dispose_registrations(manifest, registrations)
        disposals = disposal_batch.receipts
        failed_ids = disposal_batch.failed_ids
        if failed_ids:
            with self._lock:
                self._assert_transaction(transaction)
                instance.registrations = {
                    item: registrations[item] for item in failed_ids if item in registrations
                }
                instance.residual_contribution_ids = failed_ids
                instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
                instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
                instance.error_code = "disposal_unproven"
                instance.detail = "One or more contribution disposers did not prove success"
                receipt = self._make_receipt(
                    instance,
                    command_id=pending.command_id,
                    transition=LifecycleTransition.UNLOAD,
                    event=LifecycleEvent.RESTART_REQUIRED,
                    outcome=LifecycleOutcome.FAILED,
                    generation_before=generation_before,
                    restart_required=True,
                    detail_code=instance.error_code,
                    detail=instance.detail,
                    disposals=disposals,
                )
                persisted = self._persist_terminal_receipt(
                    instance, receipt, effects_ran=True
                )
                if disposal_batch.interrupted:
                    if self._pending.get(manifest.id) is pending:
                        self._pending.pop(manifest.id, None)
                    raise KeyboardInterrupt() from None
                return persisted

        cleanup_attempt = _attempt_ownership_cleanup(instance.ownership)
        cleanup_proven = cleanup_attempt.proven
        if not cleanup_proven:
            with self._lock:
                self._assert_transaction(transaction)
                instance.registrations = {}
                instance.residual_contribution_ids = ()
                instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
                instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
                instance.error_code = "module_cleanup_unproven"
                instance.detail = "Loader-owned module cleanup could not be proven"
                receipt = self._make_receipt(
                    instance,
                    command_id=pending.command_id,
                    transition=LifecycleTransition.UNLOAD,
                    event=LifecycleEvent.RESTART_REQUIRED,
                    outcome=LifecycleOutcome.FAILED,
                    generation_before=generation_before,
                    restart_required=True,
                    detail_code=instance.error_code,
                    detail=instance.detail,
                    disposals=disposals,
                )
                persisted = self._persist_terminal_receipt(
                    instance, receipt, effects_ran=True
                )
                if cleanup_attempt.interrupted:
                    if self._pending.get(manifest.id) is pending:
                        self._pending.pop(manifest.id, None)
                    raise KeyboardInterrupt() from None
                return persisted

        with self._lock:
            self._assert_transaction(transaction)
            instance.registrations = {}
            instance.residual_contribution_ids = ()
            instance.ownership = None
            instance.effective_state = PluginEffectiveState.UNLOADED
            instance.lifecycle_state = PluginLifecycleState.UNLOADED
            instance.error_code = ""
            instance.detail = ""
            receipt = self._make_receipt(
                instance,
                command_id=pending.command_id,
                transition=LifecycleTransition.UNLOAD,
                event=LifecycleEvent.UNLOADED,
                outcome=LifecycleOutcome.SUCCEEDED,
                generation_before=generation_before,
                disposals=disposals,
            )
            try:
                persisted, _replayed = self._write_receipt(
                    receipt, unique_terminal=True
                )
                return persisted
            except BaseException as exc:  # durable failure becomes restart-required
                instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
                instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
                instance.error_code = "receipt_write_failed"
                instance.detail = "Lifecycle receipt could not be durably persisted"
                failed = replace(
                    receipt,
                    event=LifecycleEvent.RESTART_REQUIRED,
                    effective_state=PluginEffectiveState.RESTART_REQUIRED,
                    lifecycle_state=PluginLifecycleState.RESTART_REQUIRED,
                    outcome=LifecycleOutcome.FAILED,
                    restart_required=True,
                    detail_code=instance.error_code,
                    detail=instance.detail,
                )
                persistence_interrupted = _persistence_interrupted(exc)
            if persistence_interrupted:
                if self._pending.get(manifest.id) is pending:
                    self._pending.pop(manifest.id, None)
                raise KeyboardInterrupt() from None
            return failed

    def _require_open(self, operation: str) -> None:
        if self._closed:
            raise CapabilityPluginError(
                "kernel_closed", f"Capability kernel is closed; cannot {operation}"
            )
        if self._closing:
            raise CapabilityPluginError(
                "kernel_closing", f"Capability kernel is closing; cannot {operation}"
            )

    def _require_mutation_allowed(self) -> None:
        self._require_open("mutate lifecycle state")
        if self._active_transaction is not None:
            raise CapabilityPluginError(
                "lifecycle_callback_active",
                "Lifecycle mutation is refused while plugin callbacks are active",
            )

    def _begin_transaction(self) -> _LifecycleTransaction:
        if self._active_transaction is not None:
            raise CapabilityPluginError(
                "lifecycle_transaction_active", "Another lifecycle transaction is active"
            )
        self._transaction_epoch += 1
        transaction = _LifecycleTransaction(
            token=uuid.uuid4().hex,
            epoch=self._transaction_epoch,
            owner_thread=threading.get_ident(),
        )
        self._active_transaction = transaction
        return transaction

    def _assert_transaction(self, transaction: _LifecycleTransaction) -> None:
        if (
            self._active_transaction is not transaction
            or self._transaction_epoch != transaction.epoch
            or transaction.owner_thread != threading.get_ident()
        ):
            raise CapabilityPluginError(
                "lifecycle_transaction_lost",
                "Lifecycle callback transaction token is no longer authoritative",
            )

    def _end_transaction(self, transaction: _LifecycleTransaction) -> None:
        self._assert_transaction(transaction)
        self._active_transaction = None
        self._transaction_epoch += 1

    def _ready_deferred_unloads(
        self,
    ) -> tuple[tuple[_PluginInstance, _DeferredUnload], ...]:
        return tuple(
            (instance, instance.deferred_unload)
            for instance in self._instances.values()
            if instance.lease_count == 0 and instance.deferred_unload is not None
        )

    def _persist_request_receipt(
        self,
        instance: _PluginInstance,
        receipt: PluginLifecycleReceipt,
    ) -> tuple[PluginLifecycleReceipt, bool]:
        try:
            return self._write_receipt(receipt, replay_request=True)
        except CommandIdentityConflictError as exc:
            raise CapabilityPluginError(
                "command_identity_conflict",
                "Command ID was already used for a different lifecycle command",
            ) from exc
        except JournalCommitAmbiguousError as exc:
            self._quarantine_ambiguous_journal(instance)
            if exc.interrupted:
                raise KeyboardInterrupt() from None
            return (
                replace(
                    receipt,
                    phase=LifecyclePhase.TERMINAL,
                    event=LifecycleEvent.RESTART_REQUIRED,
                    desired_state=instance.desired_state,
                    effective_state=instance.effective_state,
                    lifecycle_state=instance.lifecycle_state,
                    outcome=LifecycleOutcome.FAILED,
                    restart_required=True,
                    detail_code=instance.error_code,
                    detail=instance.detail,
                ),
                False,
            )
        except Exception:  # noqa: BLE001 - no effects ran; preserve prior physical state
            return (
                replace(
                    receipt,
                    phase=LifecyclePhase.TERMINAL,
                    event=LifecycleEvent.FAILED,
                    desired_state=instance.desired_state,
                    effective_state=instance.effective_state,
                    lifecycle_state=instance.lifecycle_state,
                    outcome=LifecycleOutcome.FAILED,
                    restart_required=(
                        instance.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
                    ),
                    detail_code="receipt_write_failed",
                    detail="Lifecycle receipt could not be durably persisted",
                ),
                False,
            )

    def _persist_superseding_request(
        self,
        instance: _PluginInstance,
        pending: _PendingTransition,
        receipt: PluginLifecycleReceipt,
    ) -> tuple[PluginLifecycleReceipt, bool]:
        """Durably replace pending work without a terminal/request split brain."""

        if self._receipt_writer is not None:
            return (
                replace(
                    receipt,
                    phase=LifecyclePhase.TERMINAL,
                    event=LifecycleEvent.FAILED,
                    desired_state=instance.desired_state,
                    effective_state=instance.effective_state,
                    lifecycle_state=instance.lifecycle_state,
                    outcome=LifecycleOutcome.FAILED,
                    restart_required=(
                        instance.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
                    ),
                    detail_code="atomic_supersession_unavailable",
                    detail="Injected receipt writers cannot atomically supersede commands",
                ),
                False,
            )

        event_transition = (
            LifecycleTransition.LOAD
            if pending.transition is LifecycleTransition.ENABLE
            else LifecycleTransition.UNLOAD
        )
        superseded = self._make_receipt(
            instance,
            command_id=pending.command_id,
            command_transition=pending.transition,
            transition=event_transition,
            phase=LifecyclePhase.TERMINAL,
            event=LifecycleEvent.SUPERSEDED,
            outcome=LifecycleOutcome.NO_OP,
            detail_code="superseded_by_new_command",
            detail="A newer lifecycle command superseded this pending request",
        )
        replacement = replace(
            receipt,
            detail_code="supersession_replacement",
            detail="Atomic replacement request; incomplete recovery must fail closed",
        )
        superseded_payload = _prevalidated_receipt_payload(superseded)
        replacement_payload = _prevalidated_receipt_payload(replacement)
        try:
            result = self._journal_for_current_path().append_supersession(
                superseded_payload, replacement_payload
            )
        except CommandIdentityConflictError as exc:
            raise CapabilityPluginError(
                "command_identity_conflict",
                "Command ID was already used for a different lifecycle command",
            ) from exc
        except JournalCommitAmbiguousError as exc:
            self._quarantine_ambiguous_journal(instance)
            if exc.interrupted:
                raise KeyboardInterrupt() from None
            return (
                replace(
                    replacement,
                    phase=LifecyclePhase.TERMINAL,
                    event=LifecycleEvent.RESTART_REQUIRED,
                    desired_state=instance.desired_state,
                    effective_state=instance.effective_state,
                    lifecycle_state=instance.lifecycle_state,
                    outcome=LifecycleOutcome.FAILED,
                    restart_required=True,
                    detail_code=instance.error_code,
                    detail=instance.detail,
                ),
                False,
            )
        except Exception:  # noqa: BLE001 - neither side of the batch became durable
            return (
                replace(
                    receipt,
                    phase=LifecyclePhase.TERMINAL,
                    event=LifecycleEvent.FAILED,
                    desired_state=instance.desired_state,
                    effective_state=instance.effective_state,
                    lifecycle_state=instance.lifecycle_state,
                    outcome=LifecycleOutcome.FAILED,
                    restart_required=(
                        instance.lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
                    ),
                    detail_code="receipt_write_failed",
                    detail="Lifecycle replacement could not be durably persisted",
                ),
                False,
            )
        try:
            parsed = PluginLifecycleReceipt.from_dict(result.request)
        except ReceiptPersistenceError:
            self._quarantine_ambiguous_journal(instance)
            return (
                replace(
                    receipt,
                    phase=LifecyclePhase.TERMINAL,
                    event=LifecycleEvent.RESTART_REQUIRED,
                    desired_state=instance.desired_state,
                    effective_state=instance.effective_state,
                    lifecycle_state=instance.lifecycle_state,
                    outcome=LifecycleOutcome.FAILED,
                    restart_required=True,
                    detail_code=instance.error_code,
                    detail=instance.detail,
                ),
                False,
            )
        return parsed, result.replayed

    def _persist_immediate_supersession_terminal(
        self,
        instance: _PluginInstance,
        request: PluginLifecycleReceipt,
        *,
        transition: LifecycleTransition,
        event: LifecycleEvent,
    ) -> PluginLifecycleReceipt:
        """Terminalize a replacement already satisfied by current physical state."""

        terminal = self._make_receipt(
            instance,
            command_id=request.command_id,
            command_transition=request.command_transition,
            transition=transition,
            event=event,
            outcome=LifecycleOutcome.SUCCEEDED,
            desired_state=request.desired_state,
            lifecycle_state=instance.lifecycle_state,
        )
        self._journaled_pending.discard(request.command_id)
        persistence_interrupted = False
        try:
            self._write_receipt(terminal, unique_terminal=True)
        except BaseException as exc:
            persistence_interrupted = _persistence_interrupted(exc)
            self._quarantine_ambiguous_journal(instance)
            failed = replace(
                request,
                phase=LifecyclePhase.TERMINAL,
                event=LifecycleEvent.RESTART_REQUIRED,
                desired_state=instance.desired_state,
                effective_state=instance.effective_state,
                lifecycle_state=instance.lifecycle_state,
                outcome=LifecycleOutcome.FAILED,
                restart_required=True,
                detail_code=instance.error_code,
                detail=instance.detail,
            )
        else:
            return request
        if persistence_interrupted:
            raise KeyboardInterrupt() from None
        return failed

    def _quarantine_ambiguous_journal(self, instance: _PluginInstance) -> None:
        """Revoke new authority when durable command state cannot be identified."""

        plugin_id = instance.candidate.manifest.id
        revoked_binding = False
        for contribution_id in instance.registrations:
            removed = self._bindings.pop(contribution_id, None)
            revoked_binding = removed is not None or revoked_binding
        if revoked_binding:
            self._generation += 1
        pending = self._pending.pop(plugin_id, None)
        if pending is not None:
            self._journaled_pending.discard(pending.command_id)
        instance.deferred_unload = None
        instance.desired_state = PluginDesiredState.DISABLED
        instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
        instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
        instance.error_code = "journal_commit_ambiguous"
        instance.detail = "Lifecycle journal image is ambiguous; process restart is required"

    def _persist_pending_request_if_needed(
        self,
        instance: _PluginInstance,
        pending: _PendingTransition,
    ) -> PluginLifecycleReceipt | None:
        if pending.command_id in self._journaled_pending:
            return None
        enabling = pending.transition is LifecycleTransition.ENABLE
        receipt = self._make_receipt(
            instance,
            command_id=pending.command_id,
            transition=pending.transition,
            event=LifecycleEvent.ENABLED if enabling else LifecycleEvent.UNLOAD_REQUESTED,
            outcome=LifecycleOutcome.ACCEPTED,
            desired_state=(
                PluginDesiredState.ENABLED if enabling else PluginDesiredState.DISABLED
            ),
            lifecycle_state=(
                PluginLifecycleState.ENABLED
                if enabling
                else PluginLifecycleState.UNLOAD_REQUESTED
            ),
            phase=LifecyclePhase.REQUEST,
        )
        persisted, replayed = self._persist_request_receipt(instance, receipt)
        if persisted.outcome is not LifecycleOutcome.FAILED:
            self._journaled_pending.add(pending.command_id)
        if replayed and persisted.phase is LifecyclePhase.TERMINAL:
            return persisted
        return persisted

    def _persist_terminal_receipt(
        self,
        instance: _PluginInstance,
        receipt: PluginLifecycleReceipt,
        *,
        effects_ran: bool,
    ) -> PluginLifecycleReceipt:
        persistence_interrupted = False
        failed: PluginLifecycleReceipt | None = None
        try:
            persisted, _replayed = self._write_receipt(receipt, unique_terminal=True)
            return persisted
        except BaseException as exc:  # never convert persistence failure to success
            instance.error_code = "receipt_write_failed"
            instance.detail = "Lifecycle receipt could not be durably persisted"
            if effects_ran:
                instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
                instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
            failed = replace(
                receipt,
                event=(
                    LifecycleEvent.RESTART_REQUIRED if effects_ran else LifecycleEvent.FAILED
                ),
                effective_state=instance.effective_state,
                lifecycle_state=instance.lifecycle_state,
                outcome=LifecycleOutcome.FAILED,
                restart_required=effects_ran,
                detail_code="receipt_write_failed",
                detail="Lifecycle receipt could not be durably persisted",
            )
            persistence_interrupted = _persistence_interrupted(exc)
            if persistence_interrupted:
                pending = self._pending.get(instance.candidate.manifest.id)
                if pending is not None and pending.command_id == receipt.command_id:
                    self._pending.pop(instance.candidate.manifest.id, None)
        if persistence_interrupted:
            raise KeyboardInterrupt() from None
        assert failed is not None
        return failed

    def _make_receipt(
        self,
        instance: _PluginInstance,
        *,
        command_id: str,
        transition: LifecycleTransition,
        event: LifecycleEvent,
        outcome: LifecycleOutcome,
        command_transition: LifecycleTransition | None = None,
        phase: LifecyclePhase | None = None,
        desired_state: PluginDesiredState | None = None,
        lifecycle_state: PluginLifecycleState | None = None,
        generation_before: int | None = None,
        restart_required: bool | None = None,
        detail_code: str = "",
        detail: str = "",
        disposals: tuple[DisposalReceipt, ...] = (),
    ) -> PluginLifecycleReceipt:
        manifest = instance.candidate.manifest
        secret_values = self._secret_values(manifest)
        if generation_before is None:
            generation_before = self._generation
        if desired_state is None:
            desired_state = instance.desired_state
        if lifecycle_state is None:
            lifecycle_state = instance.lifecycle_state
        if restart_required is None:
            restart_required = lifecycle_state is PluginLifecycleState.RESTART_REQUIRED
        if command_transition is None:
            command_transition = {
                LifecycleTransition.LOAD: LifecycleTransition.ENABLE,
                LifecycleTransition.UNLOAD: LifecycleTransition.DISABLE,
            }.get(transition, transition)
        if phase is None:
            if outcome is LifecycleOutcome.ACCEPTED and transition in {
                LifecycleTransition.ENABLE,
                LifecycleTransition.DISABLE,
            }:
                phase = LifecyclePhase.REQUEST
            elif event is LifecycleEvent.DRAINING:
                phase = LifecyclePhase.PROGRESS
            else:
                phase = LifecyclePhase.TERMINAL
        return PluginLifecycleReceipt(
            command_id=command_id,
            event_id=0,
            plugin_id=manifest.id,
            plugin_version=manifest.version,
            plugin_provenance_id=instance.candidate.provenance_id,
            source=manifest.source,
            command_transition=command_transition,
            requested_transition=transition,
            phase=phase,
            event=event,
            desired_state=desired_state,
            effective_state=instance.effective_state,
            lifecycle_state=lifecycle_state,
            generation_before=generation_before,
            generation_after=self._generation,
            contribution_ids=manifest.contribution_ids,
            outcome=outcome,
            restart_required=restart_required,
            timestamp=self._now(),
            detail_code=_safe_detail_code(
                detail_code,
                secret_values=secret_values,
                allow_empty=True,
            ),
            detail=redact_detail(detail, secret_values=secret_values),
            disposals=disposals,
        )

    def _write_receipt(
        self,
        receipt: PluginLifecycleReceipt,
        *,
        replay_request: bool = False,
        unique_terminal: bool = False,
    ) -> tuple[PluginLifecycleReceipt, bool]:
        payload = _prevalidated_receipt_payload(receipt)
        if self._receipt_writer is not None:
            existing = self._writer_records.get(receipt.command_id)
            if existing is not None:
                if (
                    existing.plugin_id != receipt.plugin_id
                    or existing.command_transition != receipt.command_transition
                ):
                    raise CommandIdentityConflictError("command identity conflict")
                if replay_request or (
                    unique_terminal and existing.phase is LifecyclePhase.TERMINAL
                ):
                    return existing, True
            self._writer_event_id += 1
            persisted = replace(receipt, event_id=self._writer_event_id)
            writer_interrupted = False
            try:
                self._receipt_writer(persisted)
            except KeyboardInterrupt:
                writer_interrupted = True
            if writer_interrupted:
                raise KeyboardInterrupt() from None
            self._writer_records[persisted.command_id] = persisted
            return persisted, False

        journal = self._journal_for_current_path()
        result = (
            journal.append_request(payload)
            if replay_request
            else journal.append_event(
                payload,
                unique_terminal=unique_terminal,
            )
        )
        return PluginLifecycleReceipt.from_dict(result.record), result.replayed

    def _journal_for_current_path(self) -> LockedLifecycleJournal:
        if self._receipt_writer is not None:
            raise CapabilityPluginError(
                "journal_unavailable", "Injected receipt writer has no lifecycle journal"
            )
        if self._journal is not None:
            return self._journal
        path = self._resolved_receipt_path()
        try:
            self._journal = LockedLifecycleJournal(path)
        except JournalOwnershipError as exc:
            raise CapabilityPluginError(
                "journal_owner_unavailable",
                "Another live kernel owns this lifecycle journal",
            ) from exc
        return self._journal

    def _resolved_receipt_path(self) -> Path:
        if self._journal is not None:
            return self._journal.path
        return (
            self._receipt_path
            if self._receipt_path is not None
            else Path(config.DATA_DIR) / RECEIPT_FILENAME
        )

    def _existing_command_receipt(
        self,
        command_id: str,
        plugin_id: str,
        command_transition: LifecycleTransition,
    ) -> PluginLifecycleReceipt | None:
        if self._receipt_writer is not None:
            existing = self._writer_records.get(command_id)
            records = (existing,) if existing is not None else ()
        else:
            journal = self._journal_for_current_path()
            parsed_records = tuple(
                (raw, PluginLifecycleReceipt.from_dict(raw)) for raw in journal.records()
            )
            records = tuple(
                receipt
                for raw, receipt in parsed_records
                if receipt.command_id == command_id
                and raw["journal_owner_id"] == journal.owner_id
            )
        if not records:
            return None
        for existing in records:
            if (
                existing.plugin_id != plugin_id
                or existing.command_transition is not command_transition
            ):
                raise CapabilityPluginError(
                    "command_identity_conflict",
                    "Command ID was already used for a different lifecycle command",
                )
        return records[-1]

    def _ensure_journal_recovered(self) -> None:
        if self._receipt_writer is not None:
            return
        journal = self._journal_for_current_path()
        path = journal.path
        if self._recovered_journal_path != path:
            self._recover_journal(path)

    def _recover_journal(self, path: Path) -> None:
        """Reconcile accepted request events lacking a physical terminal event."""

        journal = self._journal_for_current_path()
        if journal.path != path:
            raise CapabilityPluginError(
                "journal_path_changed", "Lifecycle journal path changed after ownership"
            )
        records = tuple(
            (raw, PluginLifecycleReceipt.from_dict(raw)) for raw in journal.records()
        )
        by_command: dict[str, list[PluginLifecycleReceipt]] = {}
        for _raw, receipt in records:
            by_command.setdefault(receipt.command_id, []).append(receipt)

        incomplete: list[PluginLifecycleReceipt] = []
        for events in by_command.values():
            identity = {
                (
                    item.plugin_id,
                    item.plugin_version,
                    item.plugin_provenance_id,
                    item.source,
                    item.command_transition,
                    item.contribution_ids,
                )
                for item in events
            }
            if len(identity) != 1:
                raise ReceiptPersistenceError(
                    "receipt journal command history changes lifecycle identity"
                )
            by_owner: dict[str, list[PluginLifecycleReceipt]] = {}
            for item in events:
                by_owner.setdefault(item.journal_owner_id, []).append(item)
            for owner_events in by_owner.values():
                request_seen = False
                progress_seen = False
                terminal_seen = False
                for item in owner_events:
                    if terminal_seen:
                        raise ReceiptPersistenceError(
                            "receipt journal owner appended after terminal state"
                        )
                    if item.phase is LifecyclePhase.REQUEST:
                        if request_seen or progress_seen:
                            raise ReceiptPersistenceError(
                                "receipt journal owner repeated or reordered a request"
                            )
                        request_seen = True
                    elif item.phase is LifecyclePhase.PROGRESS:
                        if not request_seen:
                            raise ReceiptPersistenceError(
                                "receipt journal owner progressed without a request"
                            )
                        progress_seen = True
                    else:
                        terminal_seen = True
            requests = tuple(
                item
                for item in events
                if item.phase is LifecyclePhase.REQUEST
                and item.outcome is LifecycleOutcome.ACCEPTED
            )
            if not requests:
                continue
            request = max(requests, key=lambda item: item.event_id)
            if any(
                item.phase is LifecyclePhase.TERMINAL
                and item.event_id > request.event_id
                for item in events
            ):
                continue
            incomplete.append(request)

        latest_by_plugin: dict[str, PluginLifecycleReceipt] = {}
        currently_marked_plugins = {
            request.plugin_id
            for request in incomplete
            if request.plugin_id in self._instances
            and request.detail_code == "supersession_replacement"
        }
        latest_fence_event_by_plugin: dict[str, int] = {}
        for _raw, receipt in records:
            if (
                receipt.plugin_id in self._instances
                and receipt.detail_code == "recovered_supersession_fence"
            ):
                latest_fence_event_by_plugin[receipt.plugin_id] = max(
                    receipt.event_id,
                    latest_fence_event_by_plugin.get(receipt.plugin_id, 0),
                )
        for request in sorted(incomplete, key=lambda item: item.event_id):
            if request.plugin_id not in self._instances:
                continue
            manifest = self._instances[request.plugin_id].candidate.manifest
            if (
                request.plugin_version != manifest.version
                or request.plugin_provenance_id
                != self._instances[request.plugin_id].candidate.provenance_id
                or request.source is not manifest.source
                or request.contribution_ids != manifest.contribution_ids
            ):
                raise ReceiptPersistenceError(
                    "incomplete lifecycle request does not match the active manifest"
                )
            if (
                request.plugin_id in currently_marked_plugins
                or request.event_id
                < latest_fence_event_by_plugin.get(request.plugin_id, 0)
            ):
                instance = self._instances[request.plugin_id]
                instance.desired_state = PluginDesiredState.DISABLED
                instance.effective_state = PluginEffectiveState.RESTART_REQUIRED
                instance.lifecycle_state = PluginLifecycleState.RESTART_REQUIRED
                instance.error_code = "recovered_supersession_fence"
                instance.detail = (
                    "Incomplete atomic replacement is fenced; process restart is required"
                )
                self._pending.pop(request.plugin_id, None)
                latest_by_plugin.pop(request.plugin_id, None)
                self._append_recovery_terminal(
                    instance,
                    request,
                    event=LifecycleEvent.RESTART_REQUIRED,
                    outcome=LifecycleOutcome.FAILED,
                    detail_code=instance.error_code,
                )
                continue
            prior = latest_by_plugin.get(request.plugin_id)
            if prior is not None:
                self._append_recovery_terminal(
                    self._instances[prior.plugin_id],
                    prior,
                    event=LifecycleEvent.SUPERSEDED,
                    outcome=LifecycleOutcome.NO_OP,
                    detail_code="recovery_superseded",
                )
            latest_by_plugin[request.plugin_id] = request

        for plugin_id, request in latest_by_plugin.items():
            instance = self._instances[plugin_id]
            enabling = request.command_transition is LifecycleTransition.ENABLE
            adopted, replayed = self._write_receipt(
                self._make_receipt(
                    instance,
                    command_id=request.command_id,
                    transition=request.command_transition,
                    event=(
                        LifecycleEvent.ENABLED
                        if enabling
                        else LifecycleEvent.UNLOAD_REQUESTED
                    ),
                    outcome=LifecycleOutcome.ACCEPTED,
                    phase=LifecyclePhase.REQUEST,
                    desired_state=(
                        PluginDesiredState.ENABLED
                        if enabling
                        else PluginDesiredState.DISABLED
                    ),
                    lifecycle_state=(
                        PluginLifecycleState.ENABLED
                        if enabling
                        else PluginLifecycleState.UNLOADED
                    ),
                    detail_code="recovered_owner_claim",
                    detail="New journal owner claimed this incomplete lifecycle request",
                ),
                replay_request=True,
            )
            if replayed and adopted.phase is LifecyclePhase.TERMINAL:
                continue
            request = adopted
            if request.command_transition is LifecycleTransition.ENABLE:
                instance.desired_state = PluginDesiredState.ENABLED
                instance.lifecycle_state = PluginLifecycleState.ENABLED
                self._pending[plugin_id] = _PendingTransition(
                    LifecycleTransition.ENABLE,
                    request.command_id,
                )
                self._journaled_pending.add(request.command_id)
            else:
                instance.desired_state = PluginDesiredState.DISABLED
                instance.effective_state = PluginEffectiveState.UNLOADED
                instance.lifecycle_state = PluginLifecycleState.UNLOADED
                self._pending.pop(plugin_id, None)
                self._append_recovery_terminal(
                    instance,
                    request,
                    event=LifecycleEvent.UNLOADED,
                    outcome=LifecycleOutcome.SUCCEEDED,
                    detail_code="recovered_after_restart",
                )
        self._recovered_journal_path = path

    def _append_recovery_terminal(
        self,
        instance: _PluginInstance,
        request: PluginLifecycleReceipt,
        *,
        event: LifecycleEvent,
        outcome: LifecycleOutcome,
        detail_code: str,
    ) -> PluginLifecycleReceipt:
        event_transition = (
            LifecycleTransition.LOAD
            if request.command_transition is LifecycleTransition.ENABLE
            else LifecycleTransition.UNLOAD
        )
        receipt = self._make_receipt(
            instance,
            command_id=request.command_id,
            command_transition=request.command_transition,
            transition=event_transition,
            phase=LifecyclePhase.TERMINAL,
            event=event,
            outcome=outcome,
            detail_code=detail_code,
            detail="Incomplete lifecycle command was reconciled from the durable journal",
        )
        persisted, _replayed = self._write_receipt(receipt, unique_terminal=True)
        return persisted

    def _reconcile_replayed_receipt(
        self,
        instance: _PluginInstance,
        receipt: PluginLifecycleReceipt,
    ) -> None:
        if (
            receipt.phase is not LifecyclePhase.REQUEST
            or receipt.outcome is not LifecycleOutcome.ACCEPTED
        ):
            return
        if receipt.command_transition is LifecycleTransition.ENABLE:
            instance.desired_state = PluginDesiredState.ENABLED
            instance.lifecycle_state = (
                PluginLifecycleState.LOADED
                if instance.effective_state is PluginEffectiveState.LOADED
                else PluginLifecycleState.ENABLED
            )
            if instance.effective_state is not PluginEffectiveState.LOADED:
                self._pending[instance.candidate.manifest.id] = _PendingTransition(
                    LifecycleTransition.ENABLE,
                    receipt.command_id,
                )
            self._journaled_pending.add(receipt.command_id)
        else:
            instance.desired_state = PluginDesiredState.DISABLED
            if instance.effective_state is PluginEffectiveState.LOADED:
                instance.lifecycle_state = PluginLifecycleState.UNLOAD_REQUESTED
                self._pending[instance.candidate.manifest.id] = _PendingTransition(
                    LifecycleTransition.DISABLE,
                    receipt.command_id,
                )
            self._journaled_pending.add(receipt.command_id)

    def _dispose_registrations(
        self,
        manifest: CapabilityPluginManifest,
        registrations: Mapping[str, _RegisteredContribution],
    ) -> _DisposalBatch:
        receipts: list[DisposalReceipt] = []
        failed_ids: list[str] = []
        ordered_ids = tuple(
            contribution_id
            for contribution_id in reversed(contribution_topological_order(manifest))
            if contribution_id in registrations
        )
        for index, contribution_id in enumerate(ordered_ids):
            registered = registrations[contribution_id]
            try:
                result = registered.disposer()
                if (
                    inspect.isawaitable(result)
                    or inspect.isgenerator(result)
                    or inspect.isasyncgen(result)
                ):
                    _discard_deferred_result(result)
                    raise CapabilityPluginError(
                        "deferred_disposer_not_supported",
                        "Disposer must prove success synchronously",
                    )
                if result is not True:
                    raise CapabilityPluginError(
                        "disposer_returned_false", "Disposer did not return true"
                    )
            except KeyboardInterrupt:
                interrupted_ids = ordered_ids[index:]
                failed_ids.extend(interrupted_ids)
                receipts.append(
                    DisposalReceipt(
                        contribution_id=contribution_id,
                        outcome=DisposalOutcome.FAILED,
                        detail="Operator interrupted disposal; physical cleanup is unproven",
                    )
                )
                receipts.extend(
                    DisposalReceipt(
                        contribution_id=unattempted_id,
                        outcome=DisposalOutcome.FAILED,
                        detail="Disposal was not attempted after operator interruption",
                    )
                    for unattempted_id in interrupted_ids[1:]
                )
                return _DisposalBatch(
                    receipts=tuple(receipts),
                    failed_ids=tuple(failed_ids),
                    interrupted=True,
                )
            except BaseException as exc:  # plugin-controlled process-exit types are isolated
                failed_ids.append(contribution_id)
                receipts.append(
                    DisposalReceipt(
                        contribution_id=contribution_id,
                        outcome=DisposalOutcome.FAILED,
                        detail=self._redacted_exception_detail(exc, manifest),
                    )
                )
            else:
                receipts.append(
                    DisposalReceipt(
                        contribution_id=contribution_id,
                        outcome=DisposalOutcome.SUCCEEDED,
                    )
                )
        return _DisposalBatch(
            receipts=tuple(receipts),
            failed_ids=tuple(failed_ids),
        )

    def _enforce_runtime_requirements(self, manifest: CapabilityPluginManifest) -> None:
        missing_dependencies = tuple(
            dependency
            for dependency in manifest.requirements.plugins
            if self._instances[dependency].effective_state is not PluginEffectiveState.LOADED
        )
        if missing_dependencies:
            raise CapabilityPluginError(
                "plugin_dependency_not_loaded", "Required plugin dependency is not loaded"
            )

        core_version = self._core_version
        if core_version is None:
            try:
                core_version = importlib_metadata.version("thehomie")
            except importlib_metadata.PackageNotFoundError as exc:
                raise CapabilityPluginError(
                    "core_version_unavailable", "Runtime core version could not be verified"
                ) from exc
        constraint = manifest.requirements.core_version
        try:
            compatible = core_version_satisfies(core_version, constraint)
        except Exception as exc:
            raise CapabilityPluginError(
                "core_version_unavailable", "Runtime core version could not be verified"
            ) from exc
        if not compatible:
            raise CapabilityPluginError(
                "core_version_incompatible", "Runtime core version does not satisfy the manifest"
            )

        environ = self._environ if self._environ is not None else os.environ
        missing_env = tuple(
            name for name in manifest.requirements.env if not environ.get(name)
        )
        if missing_env:
            raise CapabilityPluginError(
                "environment_requirement_missing",
                "One or more required environment variables are unavailable",
            )

    def _secret_values(self, manifest: CapabilityPluginManifest) -> tuple[str, ...]:
        environ = self._environ if self._environ is not None else os.environ
        return tuple(
            value
            for name in manifest.requirements.env
            if (value := environ.get(name, ""))
        )

    def _redacted_exception_detail(
        self,
        exc: BaseException,
        manifest: CapabilityPluginManifest,
    ) -> str:
        message = redact_detail(exc, secret_values=self._secret_values(manifest))
        return redact_detail(
            f"Plugin callback failure: {message}",
            secret_values=self._secret_values(manifest),
        )

    def _now(self) -> datetime:
        value = self._clock() if self._clock is not None else datetime.now(UTC)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _new_command_id(self) -> str:
        value = (
            self._command_id_factory()
            if self._command_id_factory is not None
            else uuid.uuid4().hex
        )
        return self._resolve_command_id(value)

    def _resolve_command_id(self, command_id: str | None) -> str:
        value = self._new_command_id() if command_id is None else command_id
        if (
            type(value) is not str
            or not _OPERATION_ID_RE.fullmatch(value)
            or redact_detail(value) != value
        ):
            raise CapabilityPluginError("invalid_command_id", "Command ID is invalid")
        return value

    def _get_instance(self, plugin_id: str) -> _PluginInstance:
        try:
            return self._instances[plugin_id]
        except KeyError as exc:
            raise CapabilityNotFoundError(plugin_id) from exc


def _frozen_module_sources(
    candidate: CapabilityPluginCandidate,
) -> tuple[str, Mapping[str, _FrozenModuleSource]]:
    artifacts = candidate.artifacts
    if not artifacts:
        raise CapabilityPluginError(
            "plugin_artifacts_missing",
            "Capability candidate has no verified executable artifacts",
        )
    if not all(type(item) is CapabilityPluginArtifact for item in artifacts):
        raise CapabilityPluginError(
            "plugin_artifacts_invalid",
            "Capability candidate artifacts are not closed immutable values",
        )
    safe_plugin_id = candidate.manifest.id.replace(".", "_").replace("-", "_")
    namespace = f"_homie_capability_{safe_plugin_id}_{uuid.uuid4().hex}"
    modules: dict[str, _FrozenModuleSource] = {
        namespace: _FrozenModuleSource(
            source=None,
            filename=f"<capability:{candidate.provenance_id}:root>",
            is_package=True,
        )
    }
    for artifact in artifacts:
        relative_path = artifact.relative_path
        if relative_path.endswith("/__init__.py"):
            logical_name = relative_path[: -len("/__init__.py")].replace("/", ".")
            is_package = True
        else:
            logical_name = relative_path[:-3].replace("/", ".")
            is_package = False
        module_name = f"{namespace}.{logical_name}"
        if module_name in modules:
            raise CapabilityPluginError(
                "plugin_artifacts_ambiguous",
                "Verified artifacts contain an ambiguous Python module",
            )
        modules[module_name] = _FrozenModuleSource(
            source=artifact.source,
            filename=(
                f"<capability:{candidate.provenance_id}:{artifact.relative_path}>"
            ),
            is_package=is_package,
        )
        parts = logical_name.split(".")
        for depth in range(1, len(parts)):
            package_name = f"{namespace}.{'.'.join(parts[:depth])}"
            modules.setdefault(
                package_name,
                _FrozenModuleSource(
                    source=None,
                    filename=(
                        f"<capability:{candidate.provenance_id}:"
                        f"{'.'.join(parts[:depth])}>"
                    ),
                    is_package=True,
                ),
            )
    return namespace, MappingProxyType(modules)


def _load_frozen_entrypoint(
    candidate: CapabilityPluginCandidate,
) -> _LoadedEntrypoint:
    module_ref, function_name = candidate.manifest.entrypoint.rsplit(":", 1)
    module_parts = module_ref.split(".")
    if not all(_MODULE_PART_RE.fullmatch(item) for item in module_parts):
        raise CapabilityPluginError("invalid_entrypoint", "Entrypoint module is invalid")
    namespace, modules = _frozen_module_sources(candidate)
    module_name = f"{namespace}.{module_ref}"
    if module_name not in modules:
        raise CapabilityPluginError(
            "entrypoint_module_missing",
            "Entrypoint is missing from the verified artifact set",
        )
    finder = _FrozenArtifactFinder(
        modules,
        namespace=namespace,
        public_root=module_parts[0],
    )
    ownership = _LoaderOwnership(
        namespace=namespace,
        cleanup_proven=True,
        finder=finder,
    )
    sys.meta_path.insert(0, finder)
    try:
        module = importlib.import_module(module_name)
        register = getattr(module, function_name, None)
        if not callable(register):
            raise CapabilityPluginError(
                "entrypoint_not_callable", "Entrypoint function is missing"
            )
    except BaseException as exc:
        source_interrupted = isinstance(exc, KeyboardInterrupt)
        cleanup_attempt = _attempt_ownership_cleanup(ownership)
        if source_interrupted or cleanup_attempt.interrupted:
            raise _OwnershipCleanupInterrupted(
                "Operator interrupted loader-owned module cleanup"
            ) from None
        if not cleanup_attempt.proven:
            raise CapabilityPluginError(
                "module_cleanup_unproven",
                "Loader-owned module cleanup could not be proven",
            ) from exc
        raise
    return _LoadedEntrypoint(register=register, ownership=ownership)


def _validate_and_order_candidates(
    candidates: tuple[CapabilityPluginCandidate, ...],
) -> tuple[CapabilityPluginCandidate, ...]:
    by_id: dict[str, CapabilityPluginCandidate] = {}
    contribution_owner: dict[str, str] = {}
    for candidate in candidates:
        manifest = candidate.manifest
        if manifest.id in by_id:
            raise CapabilityPluginError(
                "duplicate_plugin_id", "Kernel candidates contain a duplicate plugin ID"
            )
        for contribution_id in manifest.contribution_ids:
            if contribution_id in contribution_owner:
                raise CapabilityPluginError(
                    "duplicate_contribution_id",
                    "Kernel candidates contain a duplicate contribution ID",
                )
            contribution_owner[contribution_id] = manifest.id
        by_id[manifest.id] = candidate
    for candidate in candidates:
        for dependency in candidate.manifest.requirements.plugins:
            if dependency not in by_id:
                raise CapabilityPluginError(
                    "plugin_dependency_missing", "Kernel candidate dependency is missing"
                )

    ordered: list[CapabilityPluginCandidate] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    for root in sorted(candidates, key=lambda item: item.sort_key):
        if root.manifest.id in visited:
            continue
        stack: list[tuple[str, bool]] = [(root.manifest.id, False)]
        while stack:
            plugin_id, expanded = stack.pop()
            if expanded:
                visiting.remove(plugin_id)
                visited.add(plugin_id)
                ordered.append(by_id[plugin_id])
                continue
            if plugin_id in visited:
                continue
            if plugin_id in visiting:
                raise CapabilityPluginError(
                    "plugin_dependency_cycle", "Kernel plugin dependencies must be acyclic"
                )
            visiting.add(plugin_id)
            stack.append((plugin_id, True))
            for dependency in reversed(by_id[plugin_id].manifest.requirements.plugins):
                if dependency in visiting:
                    raise CapabilityPluginError(
                        "plugin_dependency_cycle",
                        "Kernel plugin dependencies must be acyclic",
                    )
                if dependency not in visited:
                    stack.append((dependency, False))
    return tuple(ordered)


def _safe_detail_code(
    detail_code: object,
    *,
    secret_values: Iterable[str] = (),
    allow_empty: bool = False,
) -> str:
    if allow_empty and detail_code == "":
        return ""
    if type(detail_code) is not str or not _DETAIL_CODE_RE.fullmatch(detail_code):
        return "plugin_error"
    if redact_detail(detail_code, secret_values=secret_values) != detail_code:
        return "plugin_error"
    return detail_code


def _error_code(
    exc: BaseException,
    *,
    secret_values: Iterable[str] = (),
) -> str:
    if type(exc) is CapabilityPluginError:
        return _safe_detail_code(exc.code, secret_values=secret_values)
    return "plugin_registration_failed"


__all__ = [
    "CapabilityNotFoundError",
    "CapabilityPluginError",
    "CapabilityPluginKernel",
    "ContributionBinding",
    "ContributionHandle",
    "DisposalOutcome",
    "DisposalReceipt",
    "LifecycleEvent",
    "LifecycleOutcome",
    "LifecyclePhase",
    "LifecycleTransition",
    "PluginDesiredState",
    "PluginEffectiveState",
    "PluginIdentity",
    "PluginInstanceView",
    "PluginLeaseView",
    "PluginLifecycleReceipt",
    "PluginLifecycleState",
    "PluginSnapshot",
    "RECEIPT_FILENAME",
    "ReceiptPersistenceError",
    "RegistrationReceipt",
    "StagedRegistrar",
]
