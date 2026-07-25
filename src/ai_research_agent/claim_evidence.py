"""Fail-closed claim-to-evidence location binding.

All supplied claim text, extracted text, excerpts, section labels, and event-log
content are untrusted data.  This module compares and hashes those values; it
never interprets them as instructions and never infers scientific truth.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .human_read import RESCINDED_EVENT, SELF_ATTESTED_EVENT, verify_chain


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVIDENCE_ID_RE = re.compile(r"^ev-[0-9a-f]{20}$")
_RANGE_RE = re.compile(r"^([1-9][0-9]*)(?:-([1-9][0-9]*))?$")


class ClaimEvidenceError(ValueError):
    """Raised when a binding cannot be made without weakening its guarantees."""


class AnchorKind(str, Enum):
    PAGE = "page"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    QUOTE = "quote"


class LocatorStatus(str, Enum):
    LOCATED = "located"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    OUT_OF_BOUNDS = "out_of_bounds"
    INVALID_ANCHOR = "invalid_anchor"


class SupportStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    AMBIGUOUS = "AMBIGUOUS"
    UNVERIFIABLE_ACCESS = "UNVERIFIABLE_ACCESS"


class HumanReviewStatus(str, Enum):
    NOT_ATTESTED = "not_attested"
    SELF_ATTESTED = "self_attested"


@dataclass(frozen=True)
class LocatorResult:
    status: LocatorStatus
    excerpt: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ClaimEvidenceBinding:
    schema_version: int
    claim_id: str
    claim_text_hash: str
    material_sha256: str
    evidence_id: str
    anchor_kind: str
    anchor_value: str
    excerpt_sha256: str
    locator_status: str
    requested_support_status: str
    support_status: str
    component_support_statuses: tuple[str, ...] = field(default_factory=tuple)
    human_review_status: str = HumanReviewStatus.NOT_ATTESTED.value
    attestation_event_hash: str | None = None
    identity_verified: bool = False
    boundary_statement: str = (
        "Software verified hashes and location only; semantic support and human "
        "identity were not independently verified."
    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["component_support_statuses"] = list(self.component_support_statuses)
        return value


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _range(value: str, length: int) -> tuple[int, int] | LocatorResult:
    match = _RANGE_RE.fullmatch(value)
    if match is None:
        return LocatorResult(
            LocatorStatus.INVALID_ANCHOR,
            message="Range must be a 1-based integer or inclusive start-end range.",
        )
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if end < start:
        return LocatorResult(
            LocatorStatus.INVALID_ANCHOR,
            message="Range end must not precede its start.",
        )
    if start > length or end > length:
        return LocatorResult(
            LocatorStatus.OUT_OF_BOUNDS,
            message=f"Requested range {start}-{end} exceeds available count {length}.",
        )
    return start, end


def locate_anchor(
    *,
    anchor_kind: AnchorKind | str,
    anchor_value: str,
    text: str,
    pages: Sequence[str] | None = None,
    sections: Mapping[str, str] | None = None,
) -> LocatorResult:
    """Locate an anchor without assigning any semantic support status."""
    try:
        kind = AnchorKind(anchor_kind)
    except ValueError:
        return LocatorResult(
            LocatorStatus.INVALID_ANCHOR, message="Unsupported anchor kind."
        )
    if not isinstance(anchor_value, str) or not anchor_value:
        return LocatorResult(
            LocatorStatus.INVALID_ANCHOR, message="Anchor value is required."
        )
    if not isinstance(text, str):
        return LocatorResult(
            LocatorStatus.INVALID_ANCHOR, message="Extracted text must be a string."
        )

    if kind is AnchorKind.QUOTE:
        count = text.count(anchor_value)
        if count == 0:
            return LocatorResult(
                LocatorStatus.NOT_FOUND, message="Exact quote was not found."
            )
        if count != 1:
            return LocatorResult(
                LocatorStatus.AMBIGUOUS,
                message=f"Exact quote occurs {count} times.",
            )
        return LocatorResult(LocatorStatus.LOCATED, excerpt=anchor_value)

    if kind is AnchorKind.SECTION:
        if not isinstance(sections, Mapping):
            return LocatorResult(
                LocatorStatus.INVALID_ANCHOR,
                message="Section anchors require a controlled section mapping.",
            )
        excerpt = sections.get(anchor_value)
        if not isinstance(excerpt, str):
            return LocatorResult(
                LocatorStatus.NOT_FOUND, message="Section label was not found."
            )
        return LocatorResult(LocatorStatus.LOCATED, excerpt=excerpt)

    if kind is AnchorKind.PAGE:
        if pages is None or isinstance(pages, (str, bytes)):
            return LocatorResult(
                LocatorStatus.INVALID_ANCHOR,
                message="Page anchors require an ordered page-text sequence.",
            )
        values = list(pages)
        if not all(isinstance(item, str) for item in values):
            return LocatorResult(
                LocatorStatus.INVALID_ANCHOR,
                message="Every page value must be text.",
            )
        bounds = _range(anchor_value, len(values))
        if isinstance(bounds, LocatorResult):
            return bounds
        start, end = bounds
        return LocatorResult(
            LocatorStatus.LOCATED,
            excerpt="\n\f\n".join(values[start - 1 : end]),
        )

    paragraphs = [
        value for value in re.split(r"\n\s*\n", text) if value.strip()
    ]
    bounds = _range(anchor_value, len(paragraphs))
    if isinstance(bounds, LocatorResult):
        return bounds
    start, end = bounds
    return LocatorResult(
        LocatorStatus.LOCATED,
        excerpt="\n\n".join(paragraphs[start - 1 : end]),
    )


def _effective_support(
    requested: SupportStatus,
    components: Sequence[SupportStatus],
) -> SupportStatus:
    # Location matching cannot prove claim atomicity, completeness, or semantic
    # entailment. Until an externally governed semantic adjudication is bound,
    # an input request for SUPPORTED must remain ambiguous.
    if requested is SupportStatus.SUPPORTED:
        return SupportStatus.AMBIGUOUS
    return requested


def _controlled_attestation(
    *, event_log: Path, event_hash: str, material_sha256: str
) -> None:
    if _SHA256_RE.fullmatch(event_hash) is None:
        raise ClaimEvidenceError("Attestation event hash must be a SHA-256 value.")
    found: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    try:
        snapshot = event_log.read_bytes()
        with tempfile.NamedTemporaryFile("wb") as stream:
            stream.write(snapshot)
            stream.flush()
            verification = verify_chain(Path(stream.name))
        if not verification.valid:
            raise ClaimEvidenceError(
                f"Attestation event chain is invalid: {verification.error}"
            )
        for line in snapshot.decode("utf-8").splitlines():
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
                if event.get("event_hash") == event_hash:
                    found = event
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClaimEvidenceError("Attestation event log is unreadable.") from exc
    if found is None or found.get("event_type") != SELF_ATTESTED_EVENT:
        raise ClaimEvidenceError(
            "Attestation hash does not reference a controlled self-attestation event."
        )
    if any(
        event.get("event_type") == RESCINDED_EVENT
        and event.get("target_event_hash") == event_hash
        for event in events
    ):
        raise ClaimEvidenceError("Attestation event has been rescinded.")
    if found.get("material_sha256") != material_sha256:
        raise ClaimEvidenceError(
            "Attestation material hash does not match the bound material."
        )
    if (
        found.get("actor_type") != "claimed_human"
        or found.get("attestation_method") != "unverified_cli_claim"
        or found.get("identity_verified") is not False
    ):
        raise ClaimEvidenceError("Unsupported attestation identity claim.")


def _derive_text(material: bytes, content_type: str) -> str:
    base = content_type.split(";", 1)[0].strip().casefold()
    if base == "text/plain":
        try:
            return material.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClaimEvidenceError("Plain-text material must be UTF-8.") from exc
    if base in {"application/xml", "text/xml"} or base.endswith("+xml"):
        upper = material.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise ClaimEvidenceError("DTD and entity declarations are forbidden.")
        try:
            root = ET.fromstring(material)
        except ET.ParseError as exc:
            raise ClaimEvidenceError("XML material is not well formed.") from exc
        local_name = root.tag.rsplit("}", 1)[-1].casefold()
        if local_name not in {"article", "pmc-articleset"}:
            raise ClaimEvidenceError("XML is not a supported full-text article container.")
        namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
        if namespace:
            parsed_namespace = urlsplit(namespace)
            if (
                parsed_namespace.scheme not in {"http", "https"}
                or parsed_namespace.hostname != "jats.nlm.nih.gov"
            ):
                raise ClaimEvidenceError("XML namespace is not an allowlisted JATS namespace.")
        names = [node.tag.rsplit("}", 1)[-1].casefold() for node in root.iter()]
        if not {"front", "article-meta", "title-group", "article-title", "body"}.issubset(names):
            raise ClaimEvidenceError("XML lacks required JATS article structure.")
        return "\n".join(value.strip() for value in root.itertext() if value.strip())
    raise ClaimEvidenceError(
        "Automatic material-to-text binding currently supports UTF-8 text and article XML only."
    )


def bind_claim_evidence(
    *,
    claim_id: str,
    claim_text: str,
    material: bytes,
    material_content_type: str,
    expected_material_sha256: str,
    evidence_id: str,
    anchor_kind: AnchorKind | str,
    anchor_value: str,
    text: str,
    support_status: SupportStatus | str,
    pages: Sequence[str] | None = None,
    sections: Mapping[str, str] | None = None,
    expected_excerpt_sha256: str | None = None,
    component_support_statuses: Sequence[SupportStatus | str] = (),
    attestation_event_log: str | Path | None = None,
    attestation_event_hash: str | None = None,
) -> ClaimEvidenceBinding:
    """Create a binding after location and material-integrity checks pass.

    ``support_status`` is recorded input, not a status inferred from the text.
    A location match never upgrades human-review state.
    """
    if _CLAIM_ID_RE.fullmatch(claim_id) is None:
        raise ClaimEvidenceError("Invalid claim_id.")
    if not isinstance(claim_text, str) or not claim_text.strip():
        raise ClaimEvidenceError("claim_text is required.")
    if not isinstance(material, bytes):
        raise ClaimEvidenceError("material must be bytes.")
    expected_material = expected_material_sha256.strip().casefold()
    if _SHA256_RE.fullmatch(expected_material) is None:
        raise ClaimEvidenceError("Expected material hash must be SHA-256.")
    actual_material = sha256_bytes(material)
    if actual_material != expected_material:
        raise ClaimEvidenceError("Material SHA-256 mismatch.")
    derived_text = _derive_text(material, material_content_type)
    if text != derived_text:
        raise ClaimEvidenceError(
            "Locator text is not the deterministic extraction of the hashed material."
        )
    if _EVIDENCE_ID_RE.fullmatch(evidence_id) is None:
        raise ClaimEvidenceError("Invalid evidence_id.")
    try:
        bind_kind = AnchorKind(anchor_kind)
    except ValueError as exc:
        raise ClaimEvidenceError("Unsupported anchor kind.") from exc
    if bind_kind not in {AnchorKind.QUOTE, AnchorKind.PARAGRAPH}:
        raise ClaimEvidenceError(
            "Page and section binding require a controlled extraction artifact and are not enabled."
        )
    try:
        requested = SupportStatus(support_status)
        components = tuple(SupportStatus(value) for value in component_support_statuses)
    except ValueError as exc:
        raise ClaimEvidenceError("Unsupported semantic support status.") from exc

    result = locate_anchor(
        anchor_kind=anchor_kind,
        anchor_value=anchor_value,
        text=text,
        pages=pages,
        sections=sections,
    )
    if result.status is not LocatorStatus.LOCATED or result.excerpt is None:
        raise ClaimEvidenceError(
            f"Evidence locator is not uniquely resolved: {result.status.value}."
        )
    excerpt_hash = sha256_text(result.excerpt)
    if expected_excerpt_sha256 is not None:
        expected_excerpt = expected_excerpt_sha256.strip().casefold()
        if _SHA256_RE.fullmatch(expected_excerpt) is None:
            raise ClaimEvidenceError("Expected excerpt hash must be SHA-256.")
        if excerpt_hash != expected_excerpt:
            raise ClaimEvidenceError("Excerpt SHA-256 mismatch.")

    if (attestation_event_log is None) != (attestation_event_hash is None):
        raise ClaimEvidenceError(
            "Attestation event log and event hash must be supplied together."
        )
    review = HumanReviewStatus.NOT_ATTESTED
    event_hash: str | None = None
    if attestation_event_log is not None and attestation_event_hash is not None:
        event_hash = attestation_event_hash.strip().casefold()
        _controlled_attestation(
            event_log=Path(attestation_event_log),
            event_hash=event_hash,
            material_sha256=actual_material,
        )
        review = HumanReviewStatus.SELF_ATTESTED

    kind = bind_kind
    effective = _effective_support(requested, components)
    return ClaimEvidenceBinding(
        schema_version=1,
        claim_id=claim_id,
        claim_text_hash=sha256_text(claim_text),
        material_sha256=actual_material,
        evidence_id=evidence_id,
        anchor_kind=kind.value,
        anchor_value=anchor_value,
        excerpt_sha256=excerpt_hash,
        locator_status=LocatorStatus.LOCATED.value,
        requested_support_status=requested.value,
        support_status=effective.value,
        component_support_statuses=tuple(item.value for item in components),
        human_review_status=review.value,
        attestation_event_hash=event_hash,
        identity_verified=False,
    )
