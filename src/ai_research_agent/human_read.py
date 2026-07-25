from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - the project currently targets Unix/macOS
    fcntl = None  # type: ignore[assignment]


SELF_ATTESTED_EVENT = "HUMAN_READ_SELF_ATTESTED"
RESCINDED_EVENT = "HUMAN_READ_ATTESTATION_RESCINDED"
GENESIS_HASH = "0" * 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ANONYMOUS_ROLE_RE = re.compile(r"^anonymous_[a-z0-9]+(?:_[a-z0-9]+)*$")
_MACHINE_ACTORS = {
    "agent",
    "ai",
    "automation",
    "bot",
    "machine",
    "model",
    "pipeline",
    "system",
}


class HumanReadGateError(ValueError):
    """Raised when a human-read event cannot safely be recorded."""


@dataclass(frozen=True)
class ChainVerification:
    valid: bool
    event_count: int
    error: str | None = None


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Return the SHA-256 digest of a regular file."""
    material = Path(path)
    digest = hashlib.sha256()
    with material.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def attest_human_read(
    *,
    event_log: str | os.PathLike[str],
    material_path: str | os.PathLike[str],
    expected_sha256: str,
    reviewer_role: str,
    confirmation_statement: str,
    actor_type: str = "human",
    explicit_command: bool = False,
) -> dict[str, Any]:
    """Append an identity-unverified human-read self-attestation.

    ``explicit_command`` deliberately defaults to false so ordinary pipelines
    cannot silently create the claim. This event is not proof of human identity
    and must never promote a record to HUMAN_READ_CONFIRMED.
    """
    if not explicit_command:
        raise HumanReadGateError(
            "HUMAN_READ_SELF_ATTESTED requires an explicit attestation command."
        )
    _validate_actor(actor_type)
    role = _validate_reviewer_role(reviewer_role)
    statement = _validate_text(
        confirmation_statement,
        field="confirmation_statement",
        minimum_length=10,
    )
    material = Path(material_path)
    if not material.is_absolute():
        raise HumanReadGateError("material_path must be an absolute path.")
    if not material.is_file():
        raise HumanReadGateError("material_path must identify an existing regular file.")

    expected = expected_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(expected):
        raise HumanReadGateError("expected_sha256 must be a 64-character SHA-256.")
    actual = sha256_file(material)
    if actual != expected:
        raise HumanReadGateError(
            f"Material SHA-256 mismatch: expected {expected}, calculated {actual}."
        )

    event = {
        "schema_version": 1,
        "event_type": SELF_ATTESTED_EVENT,
        "recorded_at": _utc_now(),
        "actor_type": "claimed_human",
        "attestation_method": "unverified_cli_claim",
        "identity_verified": False,
        "reviewer_role": role,
        "material_path": str(material),
        "material_sha256": actual,
        "confirmation_statement": statement,
    }
    return _append_event(Path(event_log), event)


def rescind_human_read(
    *,
    event_log: str | os.PathLike[str],
    target_event_hash: str,
    reviewer_role: str,
    reason: str,
    actor_type: str = "human",
    explicit_command: bool = False,
) -> dict[str, Any]:
    """Append a rescission without modifying the original self-attestation."""
    if not explicit_command:
        raise HumanReadGateError("Rescission requires an explicit command.")
    _validate_actor(actor_type)
    role = _validate_reviewer_role(reviewer_role)
    rescission_reason = _validate_text(reason, field="reason", minimum_length=5)
    target_hash = target_event_hash.strip().lower()
    if not _SHA256_RE.fullmatch(target_hash):
        raise HumanReadGateError("target_event_hash must be a SHA-256 event hash.")

    log_path = Path(event_log)
    events, verification = _read_and_verify(log_path)
    if not verification.valid:
        raise HumanReadGateError(f"Cannot append to invalid event chain: {verification.error}")
    matching = [
        event
        for event in events
        if event.get("event_hash") == target_hash
        and event.get("event_type") == SELF_ATTESTED_EVENT
    ]
    if not matching:
        raise HumanReadGateError(
            "target_event_hash does not identify a HUMAN_READ_SELF_ATTESTED event."
        )
    if any(
        event.get("event_type") == RESCINDED_EVENT
        and event.get("target_event_hash") == target_hash
        for event in events
    ):
        raise HumanReadGateError("The confirmation has already been rescinded.")

    event = {
        "schema_version": 1,
        "event_type": RESCINDED_EVENT,
        "recorded_at": _utc_now(),
        "actor_type": "claimed_human",
        "attestation_method": "unverified_cli_claim",
        "identity_verified": False,
        "reviewer_role": role,
        "target_event_hash": target_hash,
        "reason": rescission_reason,
    }
    return _append_event(log_path, event)


def verify_chain(event_log: str | os.PathLike[str]) -> ChainVerification:
    """Verify JSONL syntax, event hashes, and the previous-event hash chain."""
    _, result = _read_and_verify(Path(event_log))
    return result


def _validate_actor(actor_type: str) -> None:
    normalized = actor_type.strip().casefold()
    if normalized in _MACHINE_ACTORS or normalized != "human":
        raise HumanReadGateError(
            "actor_type must be the unverified 'human' self-claim; machine/system labels are blocked."
        )


def _validate_reviewer_role(reviewer_role: str) -> str:
    role = reviewer_role.strip().casefold()
    if not _ANONYMOUS_ROLE_RE.fullmatch(role):
        raise HumanReadGateError(
            "reviewer_role must be an anonymous role label such as "
            "'anonymous_senior_domain_reviewer'; names and signatures are not accepted."
        )
    if len(role) > 80:
        raise HumanReadGateError("reviewer_role is too long.")
    return role


def _validate_text(value: str, *, field: str, minimum_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) < minimum_length:
        raise HumanReadGateError(f"{field} must contain an explicit statement.")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_hash(event_without_hash: dict[str, Any]) -> str:
    canonical = json.dumps(
        event_without_hash,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _append_event(log_path: Path, event: dict[str, Any]) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a+", encoding="utf-8") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.seek(0)
            events, verification = _verify_lines(stream.readlines())
            if not verification.valid:
                raise HumanReadGateError(
                    f"Cannot append to invalid event chain: {verification.error}"
                )
            complete = dict(event)
            complete["previous_event_hash"] = (
                events[-1]["event_hash"] if events else GENESIS_HASH
            )
            complete["event_hash"] = _event_hash(complete)
            stream.seek(0, os.SEEK_END)
            stream.write(
                json.dumps(complete, ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
            return complete
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_and_verify(
    log_path: Path,
) -> tuple[list[dict[str, Any]], ChainVerification]:
    if not log_path.exists():
        return [], ChainVerification(valid=True, event_count=0)
    if not log_path.is_file():
        return [], ChainVerification(
            valid=False, event_count=0, error="Event log is not a regular file."
        )
    try:
        with log_path.open("r", encoding="utf-8") as stream:
            return _verify_lines(stream.readlines())
    except (OSError, UnicodeError) as exc:
        return [], ChainVerification(valid=False, event_count=0, error=str(exc))


def _verify_lines(
    lines: list[str],
) -> tuple[list[dict[str, Any]], ChainVerification]:
    events: list[dict[str, Any]] = []
    previous = GENESIS_HASH
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            return events, ChainVerification(
                valid=False,
                event_count=len(events),
                error=f"Blank line at JSONL line {number}.",
            )
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return events, ChainVerification(
                valid=False,
                event_count=len(events),
                error=f"Invalid JSON at line {number}: {exc.msg}.",
            )
        if not isinstance(event, dict):
            return events, ChainVerification(
                valid=False,
                event_count=len(events),
                error=f"Event at line {number} is not an object.",
            )
        supplied_hash = event.get("event_hash")
        supplied_previous = event.get("previous_event_hash")
        if supplied_previous != previous:
            return events, ChainVerification(
                valid=False,
                event_count=len(events),
                error=f"Previous-event hash mismatch at line {number}.",
            )
        unhashed = dict(event)
        unhashed.pop("event_hash", None)
        calculated_hash = _event_hash(unhashed)
        if supplied_hash != calculated_hash:
            return events, ChainVerification(
                valid=False,
                event_count=len(events),
                error=f"Event hash mismatch at line {number}.",
            )
        if event.get("event_type") not in {SELF_ATTESTED_EVENT, RESCINDED_EVENT}:
            return events, ChainVerification(
                valid=False,
                event_count=len(events),
                error=f"Unsupported event type at line {number}.",
            )
        if event.get("actor_type") != "claimed_human":
            return events, ChainVerification(
                valid=False,
                event_count=len(events),
                error=f"Non-human actor at line {number}.",
            )
        if (
            event.get("attestation_method") != "unverified_cli_claim"
            or event.get("identity_verified") is not False
        ):
            return events, ChainVerification(
                valid=False,
                event_count=len(events),
                error=f"Unsupported identity claim at line {number}.",
            )
        events.append(event)
        previous = supplied_hash
    return events, ChainVerification(valid=True, event_count=len(events))
