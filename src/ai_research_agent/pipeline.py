from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
import json
import re
from typing import Iterable

from .http import RemoteServiceError
from .models import (
    AccessStatus,
    Publication,
    RetractionStatus,
    SourceEvidence,
    SourceIssue,
    VerificationStatus,
    normalize_doi,
    normalize_title,
)
from .publication_status import PublicationStatus, assess_publication_status
from .provenance import FieldObservation, decide_field
from .sources import CrossrefSource, EuropePmcSource, PubMedSource


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def _validated_status_metadata(source: object, attribute: str) -> dict[str, str]:
    metadata = getattr(source, attribute, None)
    if not isinstance(metadata, dict):
        raise RemoteServiceError("Status lookup lacks response evidence metadata")
    retrieved_at = metadata.get("retrieved_at")
    response_sha256 = metadata.get("response_sha256")
    url = metadata.get("url")
    if not isinstance(retrieved_at, str) or not retrieved_at:
        raise RemoteServiceError("Status lookup lacks retrieval time")
    if not isinstance(response_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", response_sha256
    ):
        raise RemoteServiceError("Status lookup lacks a valid response SHA-256")
    if not isinstance(url, str) or not url:
        raise RemoteServiceError("Status lookup lacks an evidence URL")
    return {
        "retrieved_at": retrieved_at,
        "response_sha256": response_sha256,
        "url": url,
    }


def _copy_missing(target: Publication, source: Publication) -> None:
    for name in ("journal", "year", "doi", "pmid", "pmcid", "access_url"):
        if getattr(target, name) in (None, "") and getattr(source, name) not in (None, ""):
            setattr(target, name, getattr(source, name))
    if not target.authors and source.authors:
        target.authors = list(source.authors)
    target.publication_types = sorted(
        set(target.publication_types) | set(source.publication_types)
    )
    if source.access_status is AccessStatus.OPEN:
        target.access_status = AccessStatus.OPEN
        target.access_url = source.access_url
    elif target.access_status is AccessStatus.UNKNOWN:
        target.access_status = source.access_status
    if source.retraction_status is RetractionStatus.FLAGGED:
        target.retraction_status = RetractionStatus.FLAGGED
    elif target.retraction_status is RetractionStatus.UNKNOWN:
        target.retraction_status = source.retraction_status
    target.evidence.extend(source.evidence)


def merge_publications(items: Iterable[Publication]) -> list[Publication]:
    merged: list[Publication] = []
    by_key: dict[str, Publication] = {}
    by_pmid: dict[str, Publication] = {}
    by_title: dict[str, Publication] = {}
    for item in items:
        keys = [item.dedupe_key]
        if item.pmid:
            keys.append(f"pmid:{item.pmid}")
        title_key = f"title:{normalize_title(item.title)}"
        keys.append(title_key)
        existing = next(
            (
                candidate
                for key in keys
                if (candidate := by_key.get(key) or by_pmid.get(key) or by_title.get(key))
            ),
            None,
        )
        if existing:
            if item.doi and existing.doi and normalize_doi(item.doi) != normalize_doi(existing.doi):
                existing.conflicts.append(
                    f"DOI conflict: {existing.doi} vs {item.doi}"
                )
            if title_similarity(existing.title, item.title) < 0.86:
                existing.conflicts.append(
                    f"Title conflict across records: {existing.title!r} vs {item.title!r}"
                )
            _copy_missing(existing, item)
        else:
            existing = item
            merged.append(existing)
        by_key[existing.dedupe_key] = existing
        if existing.pmid:
            by_pmid[f"pmid:{existing.pmid}"] = existing
        by_title[f"title:{normalize_title(existing.title)}"] = existing
    return merged


@dataclass
class SearchRun:
    query: str
    requested_limit_per_source: int | None
    publications: list[Publication]
    source_totals: dict[str, int] = field(default_factory=dict)
    source_queries: dict[str, str] = field(default_factory=dict)
    source_pagination: dict[str, dict[str, object]] = field(default_factory=dict)
    issues: list[SourceIssue] = field(default_factory=list)
    transport_mode: str = "network"
    cache_enabled: bool = False


class ResearchPipeline:
    def __init__(
        self,
        pubmed: PubMedSource,
        europe_pmc: EuropePmcSource,
        crossref: CrossrefSource,
    ):
        self.pubmed = pubmed
        self.europe_pmc = europe_pmc
        self.crossref = crossref

    def run(
        self,
        query: str,
        limit: int | None,
        sources: list[str],
        *,
        page_size: int = 100,
        transport_mode: str = "network",
        cache_enabled: bool = False,
    ) -> SearchRun:
        discovered: list[Publication] = []
        totals: dict[str, int] = {}
        source_queries: dict[str, str] = {}
        source_pagination: dict[str, dict[str, object]] = {}
        issues: list[SourceIssue] = []
        source_map = {
            "pubmed": self.pubmed,
            "europe_pmc": self.europe_pmc,
        }
        for source_name in sources:
            source = source_map[source_name]
            source_queries[source_name] = (
                source.translate_query(query)
                if isinstance(source, EuropePmcSource)
                else query
            )
            try:
                records, total = source.search(query, limit, page_size=page_size)
                discovered.extend(records)
                totals[source_name] = total
            except RemoteServiceError as exc:
                issues.append(
                    SourceIssue(
                        source=source_name,
                        stage="discovery",
                        message=str(exc),
                    )
                )
            pagination = getattr(source, "last_pagination", None)
            if pagination is not None:
                pagination_data = asdict(pagination)
                pagination_data["state"] = pagination.state.value
                source_pagination[source_name] = pagination_data

        publications = merge_publications(discovered)
        for publication in publications:
            self._verify(publication, issues)
        publications.sort(
            key=lambda item: (
                not item.citation_ready,
                -(item.year or 0),
                normalize_title(item.title),
            )
        )
        return SearchRun(
            query=query,
            requested_limit_per_source=limit,
            publications=publications,
            source_totals=totals,
            source_queries=source_queries,
            source_pagination=source_pagination,
            issues=issues,
            transport_mode=transport_mode,
            cache_enabled=cache_enabled,
        )

    def _verify(self, publication: Publication, issues: list[SourceIssue]) -> None:
        source_names = {item.source for item in publication.evidence}
        crossref_item = None
        crossref_updates: list[dict[str, object]] = []
        status_sources_checked: list[str] = []
        crossref_status_complete = not bool(publication.doi)
        pubmed_status_complete = not bool(publication.pmid)
        pubmed_relations: list[dict[str, object]] = []
        if publication.pmid and "pubmed" in source_names:
            publication.verification_status = (
                VerificationStatus.AUTHORITATIVE_ID_VERIFIED
            )
            publication.verification_reasons.append(
                "PMID resolved in the authoritative NCBI PubMed API."
            )

        if publication.doi:
            try:
                crossref_item = self.crossref.lookup(publication.doi)
            except RemoteServiceError as exc:
                issues.append(
                    SourceIssue(
                        source="crossref",
                        stage="verification",
                        message=f"{publication.doi}: {exc}",
                    )
                )
                crossref_item = None
            if crossref_item:
                evidence = self.crossref.evidence(crossref_item)
                fetched_at = (
                    getattr(self.crossref.client, "last_fetched_at", None)
                    if hasattr(self.crossref, "client") else None
                )
                if fetched_at:
                    evidence.retrieved_at = fetched_at
                response_sha256 = (
                    getattr(self.crossref.client, "last_response_sha256", None)
                    if hasattr(self.crossref, "client") else None
                )
                if response_sha256:
                    evidence.response_sha256 = response_sha256
                publication.evidence.append(evidence)
                crossref_doi = normalize_doi(crossref_item.get("DOI"))
                similarity = title_similarity(publication.title, evidence.title or "")
                if crossref_doi != normalize_doi(publication.doi):
                    publication.conflicts.append(
                        f"Crossref DOI mismatch: {publication.doi} vs {crossref_doi}"
                    )
                if similarity < 0.90:
                    publication.conflicts.append(
                        f"Crossref title similarity below threshold: {similarity:.3f}"
                    )
                if not publication.conflicts:
                    publication.verification_status = (
                        VerificationStatus.CROSS_SOURCE_VERIFIED
                    )
                    publication.verification_reasons.append(
                        f"DOI and title matched Crossref (title similarity {similarity:.3f})."
                    )
                try:
                    crossref_updates = self.crossref.lookup_updates(publication.doi)
                    metadata = _validated_status_metadata(
                        self.crossref, "last_update_evidence"
                    )
                    status_evidence = SourceEvidence(
                        source="crossref_status",
                        record_id=publication.doi,
                        url=metadata["url"],
                        retrieved_at=metadata["retrieved_at"],
                        doi=publication.doi,
                        note=f"reverse update lookup; matched_items={len(crossref_updates)}",
                        response_sha256=metadata["response_sha256"],
                    )
                    publication.evidence.append(status_evidence)
                    publication.status_evidence_ids.append(status_evidence.evidence_id)
                    crossref_status_complete = True
                    status_sources_checked.append("crossref_reverse_updates")
                except (RemoteServiceError, ValueError) as exc:
                    issues.append(
                        SourceIssue(
                            source="crossref",
                            stage="publication_status",
                            message=f"{publication.doi}: {exc}",
                        )
                    )
            elif publication.verification_status is VerificationStatus.DISCOVERED:
                publication.verification_status = VerificationStatus.UNVERIFIED
                publication.verification_reasons.append(
                    "DOI did not resolve through Crossref during this run."
                )

        if publication.pmid:
            try:
                pubmed_relations = self.pubmed.fetch_status_relations(publication.pmid)
                metadata = _validated_status_metadata(
                    self.pubmed, "last_status_evidence"
                )
                status_evidence = SourceEvidence(
                    source="pubmed_status",
                    record_id=publication.pmid,
                    url=metadata["url"],
                    retrieved_at=metadata["retrieved_at"],
                    doi=publication.doi,
                    pmid=publication.pmid,
                    note=json.dumps(
                        {"relation_count": len(pubmed_relations)},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    response_sha256=metadata["response_sha256"],
                )
                publication.evidence.append(status_evidence)
                publication.status_evidence_ids.append(status_evidence.evidence_id)
                pubmed_status_complete = True
                status_sources_checked.append("pubmed_comments_corrections")
            except (RemoteServiceError, ValueError, AttributeError) as exc:
                issues.append(
                    SourceIssue(
                        source="pubmed",
                        stage="publication_status",
                        message=f"PMID {publication.pmid}: {exc}",
                    )
                )

        status = assess_publication_status(
            crossref_work=[crossref_item, *crossref_updates],
            pubmed_publication_types=publication.publication_types,
            pubmed_related_articles=pubmed_relations,
        )
        publication.publication_status = status.status
        publication.status_signals = list(status.signals)
        publication.status_relations = list(pubmed_relations)
        publication.publication_status_sources_checked = sorted(status_sources_checked)
        publication.publication_status_check_complete = (
            crossref_status_complete and pubmed_status_complete
        )
        if status.status is PublicationStatus.RETRACTED:
            publication.retraction_status = RetractionStatus.FLAGGED
            publication.verification_reasons.append(
                "Retraction signal found; citation-ready status is blocked."
            )
        elif status.status is PublicationStatus.EXPRESSION_OF_CONCERN:
            publication.verification_reasons.append(
                "Expression-of-concern signal found; citation-ready status is blocked."
            )
        elif status.status is PublicationStatus.CORRECTION:
            publication.verification_reasons.append(
                "Correction signal found; citation-ready status is blocked until the corrected version is reviewed."
            )
        if not publication.publication_status_check_complete:
            publication.verification_reasons.append(
                "One or more required post-publication status lookups were not completed; citation-ready status is blocked."
            )

        if publication.conflicts:
            publication.verification_status = VerificationStatus.METADATA_CONFLICT
            publication.verification_reasons.append(
                "Metadata conflict blocks citation-ready status."
            )
        elif (
            publication.verification_status is VerificationStatus.DISCOVERED
            and publication.pmid
            and "europe_pmc" in source_names
        ):
            publication.verification_status = (
                VerificationStatus.AUTHORITATIVE_ID_VERIFIED
            )
            publication.verification_reasons.append(
                "PMID resolved through Europe PMC's MED record."
            )
        elif publication.verification_status is VerificationStatus.DISCOVERED:
            publication.verification_status = VerificationStatus.UNVERIFIED
            publication.verification_reasons.append(
                "No authoritative identifier or independent DOI match was confirmed."
            )
        self._record_field_provenance(publication)

    @staticmethod
    def _record_field_provenance(publication: Publication) -> None:
        priorities = ("pubmed", "crossref", "europe_pmc")
        for field_name in ("title", "doi", "pmid"):
            observations = [
                FieldObservation(
                    source=evidence.source,
                    evidence_id=evidence.evidence_id,
                    value=getattr(evidence, field_name),
                    retrieved_at=evidence.retrieved_at,
                )
                for evidence in publication.evidence
                if getattr(evidence, field_name) not in (None, "")
            ]
            if observations:
                publication.field_provenance[field_name] = decide_field(
                    field_name,
                    observations,
                    source_priority=priorities,
                ).to_dict()
