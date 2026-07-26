"""Recoverable, append-only execution for batches of reference lookups.

This module records *identity lookup* progress only.  A completed lookup or a
high-confidence bibliographic match never means that a cited paper supports a
claim.  Semantic support remains explicitly unassessed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


EXECUTION_STATUSES = frozenset({"pending", "completed", "partial", "failed"})
MATCH_STATUSES = frozenset({"high-confidence", "review", "not-matched"})
TERMINAL_LOOKUP_STATUSES = frozenset({"completed", "partial", "failed"})
TRANSPORT_MODES = frozenset({"network", "offline_replay"})
SEMANTIC_SUPPORT_STATUS = "NOT_ASSESSED"
LEDGER_SCHEMA = "reference-batch-checkpoint-0.1"


class ReferenceBatchError(RuntimeError):
    """Raised when a batch contract or checkpoint ledger is invalid."""


class ReferenceBatchIntegrityError(ReferenceBatchError):
    """Raised when an append-only checkpoint ledger fails verification."""


@dataclass(frozen=True)
class ReferenceItem:
    """One selected reference and its stable, JSON-serializable input payload."""

    reference_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class BatchExecutionContext:
    """Transport context forwarded unchanged to the lookup implementation."""

    transport_mode: str = "network"
    cache_enabled: bool = False


@dataclass(frozen=True)
class LookupOutcome:
    """Explicit result returned by a single-reference lookup implementation."""

    execution_status: str
    match_status: str | None
    queries: tuple[Mapping[str, Any], ...] = ()
    candidates: tuple[Mapping[str, Any], ...] = ()
    issues: tuple[Mapping[str, Any] | str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReferenceState:
    reference_id: str
    execution_status: str
    match_status: str | None
    attempt_count: int
    transport_mode: str | None
    cache_enabled: bool | None
    semantic_support_status: str = SEMANTIC_SUPPORT_STATUS
    result_event_hash: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class BatchRunResult:
    batch_id: str
    batch_status: str
    selected_count: int
    attempted_this_run: int
    completed_count: int
    partial_count: int
    failed_count: int
    pending_count: int
    states: tuple[ReferenceState, ...]
    checkpoint_path: str
    semantic_support_assessed: bool = False
    boundary: str = (
        "Lookup completion and bibliographic match confidence do not establish "
        "semantic support for any claim."
    )


LookupExecutor = Callable[
    [ReferenceItem, BatchExecutionContext], LookupOutcome | Mapping[str, Any]
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReferenceBatchError("Batch data must be finite JSON values.") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_reference_items(
    references: Iterable[ReferenceItem], selected_reference_ids: Iterable[str | int]
) -> tuple[tuple[ReferenceItem, ...], tuple[str, ...], str]:
    by_id: dict[str, ReferenceItem] = {}
    for item in references:
        if not isinstance(item, ReferenceItem):
            raise ReferenceBatchError("references must contain ReferenceItem values")
        reference_id = item.reference_id.strip()
        if not reference_id or reference_id in by_id:
            raise ReferenceBatchError("reference IDs must be non-empty and unique")
        payload = dict(item.payload)
        _canonical_json(payload)
        by_id[reference_id] = ReferenceItem(reference_id, payload)

    selected = tuple(str(value).strip() for value in selected_reference_ids)
    if not selected:
        raise ReferenceBatchError("at least one selected reference is required")
    if any(not value for value in selected) or len(set(selected)) != len(selected):
        raise ReferenceBatchError("selected reference IDs must be non-empty and unique")
    missing = [value for value in selected if value not in by_id]
    if missing:
        raise ReferenceBatchError(f"selected references are missing: {missing}")
    ordered = tuple(by_id[value] for value in selected)
    fingerprint_input = [
        {"reference_id": item.reference_id, "payload": dict(item.payload)}
        for item in ordered
    ]
    return ordered, selected, _sha256_json(fingerprint_input)


def _event_with_hash(event: Mapping[str, Any], previous_hash: str | None) -> dict[str, Any]:
    payload = dict(event)
    payload["previous_event_hash"] = previous_hash
    payload["event_hash"] = _sha256_json(payload)
    return payload


def _verify_events(events: list[dict[str, Any]]) -> None:
    previous: str | None = None
    for line_number, event in enumerate(events, 1):
        if event.get("schema_version") != LEDGER_SCHEMA:
            raise ReferenceBatchIntegrityError(
                f"checkpoint line {line_number} has an unsupported schema"
            )
        claimed = event.get("event_hash")
        if not isinstance(claimed, str) or len(claimed) != 64:
            raise ReferenceBatchIntegrityError(
                f"checkpoint line {line_number} has an invalid event hash"
            )
        if event.get("previous_event_hash") != previous:
            raise ReferenceBatchIntegrityError(
                f"checkpoint line {line_number} breaks the hash chain"
            )
        unhashed = dict(event)
        unhashed.pop("event_hash", None)
        if _sha256_json(unhashed) != claimed:
            raise ReferenceBatchIntegrityError(
                f"checkpoint line {line_number} does not match its event hash"
            )
        previous = claimed


def _read_locked(handle: Any) -> list[dict[str, Any]]:
    handle.seek(0)
    payload = handle.read()
    if not payload:
        return []
    if not payload.endswith("\n"):
        raise ReferenceBatchIntegrityError("checkpoint has a truncated final line")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReferenceBatchIntegrityError(
                f"checkpoint line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(event, dict):
            raise ReferenceBatchIntegrityError(
                f"checkpoint line {line_number} is not an object"
            )
        events.append(event)
    _verify_events(events)
    return events


def _open_checkpoint(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a+", encoding="utf-8")


def _append_event(path: Path, event: Mapping[str, Any]) -> dict[str, Any]:
    with _open_checkpoint(path) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        events = _read_locked(handle)
        previous = events[-1]["event_hash"] if events else None
        finalized = _event_with_hash(event, previous)
        handle.seek(0, os.SEEK_END)
        handle.write(_canonical_json(finalized).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return finalized


def load_checkpoint(path: str | Path) -> tuple[dict[str, Any], ...]:
    """Load and verify the entire checkpoint hash chain."""

    checkpoint = Path(path).expanduser().resolve()
    if not checkpoint.exists():
        return ()
    with _open_checkpoint(checkpoint) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        events = _read_locked(handle)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return tuple(events)


def _initialize_or_validate(
    path: Path,
    selected: tuple[str, ...],
    references_sha256: str,
    batch_context_sha256: str,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    batch_id = hashlib.sha256(
        f"{references_sha256}\x1f{batch_context_sha256}\x1f{'|'.join(selected)}".encode("utf-8")
    ).hexdigest()
    with _open_checkpoint(path) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        events = _read_locked(handle)
        if not events:
            header = _event_with_hash(
                {
                    "schema_version": LEDGER_SCHEMA,
                    "event_type": "batch_initialized",
                    "recorded_at": utc_now(),
                    "batch_id": batch_id,
                    "selected_reference_ids": list(selected),
                    "references_sha256": references_sha256,
                    "batch_context_sha256": batch_context_sha256,
                    "semantic_support_assessed": False,
                },
                None,
            )
            handle.seek(0, os.SEEK_END)
            handle.write(_canonical_json(header).decode("utf-8") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            events = [header]
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    header = events[0]
    if (
        header.get("event_type") != "batch_initialized"
        or header.get("batch_id") != batch_id
        or header.get("selected_reference_ids") != list(selected)
        or header.get("references_sha256") != references_sha256
        or header.get("batch_context_sha256") != batch_context_sha256
        or header.get("semantic_support_assessed") is not False
    ):
        raise ReferenceBatchIntegrityError(
            "checkpoint belongs to different selected references or inputs"
        )
    if any(event.get("batch_id") != batch_id for event in events):
        raise ReferenceBatchIntegrityError("checkpoint contains a mismatched batch ID")
    return batch_id, events


def _attempt_counts(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if event.get("event_type") == "attempt_started":
            reference_id = event.get("reference_id")
            if isinstance(reference_id, str):
                counts[reference_id] = counts.get(reference_id, 0) + 1
    return counts


def _states(
    selected: tuple[str, ...], events: Iterable[Mapping[str, Any]]
) -> tuple[ReferenceState, ...]:
    latest: dict[str, Mapping[str, Any]] = {}
    counts = _attempt_counts(events)
    for event in events:
        if event.get("event_type") in {"attempt_started", "attempt_finished"}:
            reference_id = event.get("reference_id")
            if reference_id in selected:
                latest[str(reference_id)] = event
    states: list[ReferenceState] = []
    for reference_id in selected:
        event = latest.get(reference_id)
        if event is None:
            states.append(
                ReferenceState(reference_id, "pending", None, counts.get(reference_id, 0), None, None)
            )
            continue
        if event.get("event_type") == "attempt_started":
            states.append(
                ReferenceState(
                    reference_id=reference_id,
                    execution_status="pending",
                    match_status=None,
                    attempt_count=counts.get(reference_id, 0),
                    transport_mode=event.get("transport_mode"),
                    cache_enabled=event.get("cache_enabled"),
                    result_event_hash=event.get("event_hash"),
                )
            )
            continue
        states.append(
            ReferenceState(
                reference_id=reference_id,
                execution_status=str(event["execution_status"]),
                match_status=event.get("match_status"),
                attempt_count=counts.get(reference_id, 0),
                transport_mode=event.get("transport_mode"),
                cache_enabled=event.get("cache_enabled"),
                result_event_hash=event.get("event_hash"),
                error=event.get("error"),
            )
        )
    return tuple(states)


def _coerce_outcome(value: LookupOutcome | Mapping[str, Any]) -> LookupOutcome:
    if isinstance(value, LookupOutcome):
        outcome = value
    elif isinstance(value, Mapping):
        outcome = LookupOutcome(
            execution_status=str(value.get("execution_status", "")),
            match_status=value.get("match_status"),
            queries=tuple(value.get("queries", ())),
            candidates=tuple(value.get("candidates", ())),
            issues=tuple(value.get("issues", ())),
            evidence=dict(value.get("evidence", {})),
        )
    else:
        raise ReferenceBatchError("lookup executor returned an unsupported result")
    if outcome.execution_status not in TERMINAL_LOOKUP_STATUSES:
        raise ReferenceBatchError(
            "lookup execution_status must be completed, partial, or failed"
        )
    if outcome.execution_status == "failed":
        if outcome.match_status is not None:
            raise ReferenceBatchError("failed lookups cannot claim a match status")
    elif outcome.match_status not in MATCH_STATUSES:
        raise ReferenceBatchError(
            "completed or partial lookups require an explicit match status"
        )
    _canonical_json(
        {
            "queries": outcome.queries,
            "candidates": outcome.candidates,
            "issues": outcome.issues,
            "evidence": outcome.evidence,
        }
    )
    return outcome


def _finish_event(
    *,
    batch_id: str,
    item: ReferenceItem,
    attempt_number: int,
    context: BatchExecutionContext,
    outcome: LookupOutcome,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA,
        "event_type": "attempt_finished",
        "recorded_at": utc_now(),
        "batch_id": batch_id,
        "reference_id": item.reference_id,
        "reference_payload_sha256": _sha256_json(dict(item.payload)),
        "attempt_number": attempt_number,
        "execution_status": outcome.execution_status,
        "match_status": outcome.match_status,
        "transport_mode": context.transport_mode,
        "cache_enabled": context.cache_enabled,
        "queries": list(outcome.queries),
        "candidates": list(outcome.candidates),
        "issues": list(outcome.issues),
        "evidence": dict(outcome.evidence),
        "error": error,
        "semantic_support_status": SEMANTIC_SUPPORT_STATUS,
        "semantic_support_assessed": False,
    }


def _summarize(
    batch_id: str,
    checkpoint: Path,
    selected: tuple[str, ...],
    events: tuple[dict[str, Any], ...],
    attempted_this_run: int,
) -> BatchRunResult:
    states = _states(selected, events)
    counts = {
        status: sum(item.execution_status == status for item in states)
        for status in EXECUTION_STATUSES
    }
    if counts["completed"] == len(states):
        batch_status = "completed"
    elif counts["pending"] == len(states):
        batch_status = "pending"
    elif counts["failed"] == len(states):
        batch_status = "failed"
    else:
        batch_status = "partial"
    return BatchRunResult(
        batch_id=batch_id,
        batch_status=batch_status,
        selected_count=len(states),
        attempted_this_run=attempted_this_run,
        completed_count=counts["completed"],
        partial_count=counts["partial"],
        failed_count=counts["failed"],
        pending_count=counts["pending"],
        states=states,
        checkpoint_path=str(checkpoint),
    )


def run_reference_batch(
    references: Iterable[ReferenceItem],
    selected_reference_ids: Iterable[str | int],
    checkpoint_path: str | Path,
    executor: LookupExecutor,
    *,
    transport_mode: str = "network",
    cache_enabled: bool = False,
    max_items: int | None = None,
    batch_context: Mapping[str, Any] | None = None,
) -> BatchRunResult:
    """Execute or resume selected reference lookups.

    Completed references are never queried again.  Pending, partial, and failed
    references remain eligible for a later attempt.  Every attempt is appended
    to a hash-chained JSONL checkpoint, preserving earlier failures.
    """

    if transport_mode not in TRANSPORT_MODES:
        raise ReferenceBatchError(
            f"transport_mode must be one of {sorted(TRANSPORT_MODES)}"
        )
    if transport_mode == "offline_replay" and not cache_enabled:
        raise ReferenceBatchError("offline replay requires cache_enabled=true")
    if max_items is not None and max_items < 0:
        raise ReferenceBatchError("max_items must be non-negative or None")
    ordered, selected, references_sha256 = _validate_reference_items(
        references, selected_reference_ids
    )
    batch_context_sha256 = _sha256_json(dict(batch_context or {}))
    checkpoint = Path(checkpoint_path).expanduser().resolve()
    batch_id, events = _initialize_or_validate(
        checkpoint, selected, references_sha256, batch_context_sha256
    )
    states = {item.reference_id: item for item in _states(selected, events)}
    context = BatchExecutionContext(transport_mode, cache_enabled)
    attempted = 0

    for item in ordered:
        prior = states[item.reference_id]
        if prior.execution_status == "completed":
            continue
        if max_items is not None and attempted >= max_items:
            break
        attempt_number = prior.attempt_count + 1
        _append_event(
            checkpoint,
            {
                "schema_version": LEDGER_SCHEMA,
                "event_type": "attempt_started",
                "recorded_at": utc_now(),
                "batch_id": batch_id,
                "reference_id": item.reference_id,
                "reference_payload_sha256": _sha256_json(dict(item.payload)),
                "attempt_number": attempt_number,
                "execution_status": "pending",
                "match_status": None,
                "transport_mode": transport_mode,
                "cache_enabled": cache_enabled,
                "semantic_support_status": SEMANTIC_SUPPORT_STATUS,
                "semantic_support_assessed": False,
            },
        )
        attempted += 1
        try:
            outcome = _coerce_outcome(executor(item, context))
            _append_event(
                checkpoint,
                _finish_event(
                    batch_id=batch_id,
                    item=item,
                    attempt_number=attempt_number,
                    context=context,
                    outcome=outcome,
                ),
            )
        except Exception as exc:  # Each reference failure must not abort the batch.
            message = " ".join(str(exc).split())[:1000]
            failed = LookupOutcome("failed", None)
            _append_event(
                checkpoint,
                _finish_event(
                    batch_id=batch_id,
                    item=item,
                    attempt_number=attempt_number,
                    context=context,
                    outcome=failed,
                    error=f"{type(exc).__name__}: {message}",
                ),
            )

    final_events = load_checkpoint(checkpoint)
    return _summarize(batch_id, checkpoint, selected, final_events, attempted)


def result_to_dict(result: BatchRunResult) -> dict[str, Any]:
    """Return a JSON-ready result without changing any evidence status."""

    return asdict(result)
