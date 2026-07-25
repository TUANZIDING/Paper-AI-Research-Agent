from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from .human_read import sha256_file


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROLE_RE = re.compile(r"^anonymous_[a-z0-9]+(?:_[a-z0-9]+)*$")


class ExternalReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ExternalReviewVerification:
    packet_valid: bool
    signature_verified: bool
    key_fingerprint: str
    material_hash_verified: bool
    identity_verified: bool = False
    human_read_confirmed: bool = False
    boundary: str = (
        "A valid detached signature proves control of the configured key only. "
        "It does not establish the signer's real-world identity or that reading occurred."
    )

    @property
    def verification_gate_passed(self) -> bool:
        return self.packet_valid and self.signature_verified and self.material_hash_verified


def canonical_packet_bytes(packet: dict[str, Any]) -> bytes:
    return json.dumps(
        packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def prepare_review_packet(
    *,
    material_path: str | Path,
    reviewer_role: str,
    confirmation_statement: str,
    claim_evidence_ids: list[str] | None = None,
    issued_at: str | None = None,
) -> dict[str, Any]:
    material = Path(material_path).resolve()
    if not material.is_file():
        raise ExternalReviewError("material_path must identify an existing file")
    role = reviewer_role.strip().casefold()
    if _ROLE_RE.fullmatch(role) is None:
        raise ExternalReviewError("reviewer_role must be an anonymous role label")
    statement = " ".join(confirmation_statement.split())
    if len(statement) < 20:
        raise ExternalReviewError("confirmation_statement is too short")
    evidence_ids = sorted(set(claim_evidence_ids or []))
    if any(re.fullmatch(r"ce-[0-9a-f]{24}", item) is None for item in evidence_ids):
        raise ExternalReviewError("claim_evidence_ids contain an invalid identifier")
    if evidence_ids:
        raise ExternalReviewError(
            "claim-evidence references are disabled until binding-record hashes are implemented"
        )
    packet = {
        "schema_version": 1,
        "event_type": "HUMAN_READ_EXTERNAL_SIGNATURE_REQUEST",
        "issued_at": issued_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reviewer_role": role,
        "material_sha256": sha256_file(material),
        "claim_evidence_ids": evidence_ids,
        "confirmation_statement": statement,
        "identity_claim": "unverified_key_holder",
    }
    packet["packet_sha256"] = hashlib.sha256(canonical_packet_bytes(packet)).hexdigest()
    return packet


def verify_external_review(
    *,
    packet: dict[str, Any],
    signature_path: str | Path,
    public_key_path: str | Path,
    material_path: str | Path,
    openssl_binary: str = "openssl",
) -> ExternalReviewVerification:
    _validate_packet(packet)
    signature = Path(signature_path)
    public_key = Path(public_key_path)
    material = Path(material_path)
    if not signature.is_file() or not public_key.is_file() or not material.is_file():
        raise ExternalReviewError("signature, public key, and material must be files")
    material_matches = sha256_file(material) == packet["material_sha256"]
    if not material_matches:
        raise ExternalReviewError("material SHA-256 does not match the signed packet")
    public_key_bytes = public_key.read_bytes()
    signature_bytes = signature.read_bytes()
    if len(public_key_bytes) > 64_000 or len(signature_bytes) > 64_000:
        raise ExternalReviewError("public key or signature exceeds the size limit")
    fingerprint = hashlib.sha256(public_key_bytes).hexdigest()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        payload_file = root / "packet.json"
        key_file = root / "public.pem"
        signature_file = root / "packet.sig"
        payload_file.write_bytes(canonical_packet_bytes(packet))
        key_file.write_bytes(public_key_bytes)
        signature_file.write_bytes(signature_bytes)
        key_info = subprocess.run(
            [openssl_binary, "pkey", "-pubin", "-in", str(key_file), "-text_pub", "-noout"],
            capture_output=True, check=False, timeout=15,
        )
        if key_info.returncode != 0 or b"ED25519" not in key_info.stdout.upper():
            raise ExternalReviewError("public key must be Ed25519")
        result = subprocess.run(
            [
                openssl_binary,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(key_file),
                "-rawin",
                "-in",
                str(payload_file),
                "-sigfile",
                str(signature_file),
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
    verified = result.returncode == 0
    return ExternalReviewVerification(
        packet_valid=True,
        signature_verified=verified,
        key_fingerprint=fingerprint,
        material_hash_verified=material_matches,
        identity_verified=False,
        human_read_confirmed=False,
    )


def _validate_packet(packet: dict[str, Any]) -> None:
    required = {
        "schema_version", "event_type", "issued_at", "reviewer_role",
        "material_sha256", "claim_evidence_ids", "confirmation_statement",
        "identity_claim", "packet_sha256",
    }
    if set(packet) != required:
        raise ExternalReviewError("review packet keys do not match schema")
    if packet["schema_version"] != 1 or packet["event_type"] != "HUMAN_READ_EXTERNAL_SIGNATURE_REQUEST":
        raise ExternalReviewError("unsupported review packet version or type")
    if packet["identity_claim"] != "unverified_key_holder":
        raise ExternalReviewError("review packet cannot assert verified identity")
    if _ROLE_RE.fullmatch(str(packet["reviewer_role"])) is None:
        raise ExternalReviewError("invalid reviewer role")
    if _SHA256_RE.fullmatch(str(packet["material_sha256"])) is None:
        raise ExternalReviewError("invalid material SHA-256")
    issued_at = str(packet["issued_at"])
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        issued_at,
    ) is None:
        raise ExternalReviewError("issued_at must be an RFC 3339 date-time with timezone")
    try:
        parsed_time = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalReviewError("issued_at must be an ISO date-time") from exc
    if parsed_time.tzinfo is None:
        raise ExternalReviewError("issued_at must include a timezone")
    evidence_ids = packet["claim_evidence_ids"]
    if not isinstance(evidence_ids, list) or any(
        not isinstance(item, str) or re.fullmatch(r"ce-[0-9a-f]{24}", item) is None
        for item in evidence_ids
    ):
        raise ExternalReviewError("claim_evidence_ids are invalid")
    if evidence_ids:
        raise ExternalReviewError(
            "claim-evidence references are disabled until binding-record hashes are implemented"
        )
    statement = packet["confirmation_statement"]
    if (
        not isinstance(statement, str)
        or len(statement) < 20
        or statement != " ".join(statement.split())
    ):
        raise ExternalReviewError("confirmation_statement is invalid")
    if _SHA256_RE.fullmatch(str(packet["packet_sha256"])) is None:
        raise ExternalReviewError("invalid packet SHA-256")
    unsigned = dict(packet)
    supplied = unsigned.pop("packet_sha256")
    calculated = hashlib.sha256(canonical_packet_bytes(unsigned)).hexdigest()
    if supplied != calculated:
        raise ExternalReviewError("review packet SHA-256 mismatch")
