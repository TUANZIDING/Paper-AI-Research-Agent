from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import re
import unicodedata
from typing import Any

from .publication_status import PublicationStatus


class VerificationStatus(str, Enum):
    DISCOVERED = "discovered"
    AUTHORITATIVE_ID_VERIFIED = "authoritative_id_verified"
    CROSS_SOURCE_VERIFIED = "cross_source_verified"
    METADATA_CONFLICT = "metadata_conflict"
    UNVERIFIED = "unverified"


class AccessStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed_or_not_open"
    UNKNOWN = "unknown"


class RetractionStatus(str, Enum):
    FLAGGED = "flagged"
    NOT_FLAGGED = "not_flagged"
    UNKNOWN = "unknown"


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    doi = value.strip().lower()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi)
    doi = doi.rstrip(" .;,)")
    return doi if doi.startswith("10.") and "/" in doi else None


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


@dataclass
class SourceEvidence:
    source: str
    record_id: str | None
    url: str
    retrieved_at: str
    title: str | None = None
    doi: str | None = None
    pmid: str | None = None
    note: str | None = None
    response_sha256: str | None = None
    evidence_id: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_id:
            identity = "\x1f".join(
                [
                    self.source,
                    self.record_id or "",
                    self.url,
                    self.title or "",
                    self.doi or "",
                    self.pmid or "",
                    self.note or "",
                    self.response_sha256 or "",
                ]
            )
            self.evidence_id = "ev-" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:20]


@dataclass
class Publication:
    title: str
    authors: list[str] = field(default_factory=list)
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    publication_types: list[str] = field(default_factory=list)
    access_status: AccessStatus = AccessStatus.UNKNOWN
    access_url: str | None = None
    retraction_status: RetractionStatus = RetractionStatus.UNKNOWN
    publication_status: PublicationStatus = PublicationStatus.UNKNOWN
    status_signals: list[str] = field(default_factory=list)
    status_relations: list[dict[str, Any]] = field(default_factory=list)
    publication_status_sources_checked: list[str] = field(default_factory=list)
    status_evidence_ids: list[str] = field(default_factory=list)
    publication_status_check_complete: bool = False
    verification_status: VerificationStatus = VerificationStatus.DISCOVERED
    verification_reasons: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    evidence: list[SourceEvidence] = field(default_factory=list)
    field_provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        if self.pmid:
            return f"pmid:{self.pmid}"
        return f"title:{normalize_title(self.title)}"

    @property
    def citation_ready(self) -> bool:
        return (
            self.verification_status
            in {
                VerificationStatus.AUTHORITATIVE_ID_VERIFIED,
                VerificationStatus.CROSS_SOURCE_VERIFIED,
            }
            and not self.conflicts
            and self.retraction_status is not RetractionStatus.FLAGGED
            and self.publication_status
            not in {
                PublicationStatus.RETRACTED,
                PublicationStatus.EXPRESSION_OF_CONCERN,
                PublicationStatus.CORRECTION,
            }
            and self.publication_status_check_complete
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["access_status"] = self.access_status.value
        result["retraction_status"] = self.retraction_status.value
        result["publication_status"] = self.publication_status.value
        result["verification_status"] = self.verification_status.value
        result["citation_ready"] = self.citation_ready
        return result


@dataclass
class SourceIssue:
    source: str
    stage: str
    message: str
    fatal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
