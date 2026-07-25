"""Conservative publication-status assessment from untrusted metadata.

The functions in this module only compare external strings with a closed set of
known status labels.  They never evaluate, import, interpolate, or execute
external values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping


class PublicationStatus(str, Enum):
    RETRACTED = "retracted"
    CORRECTION = "correction"
    EXPRESSION_OF_CONCERN = "expression_of_concern"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PublicationStatusAssessment:
    status: PublicationStatus
    signals: tuple[str, ...] = ()

    @property
    def conclusively_not_retracted(self) -> bool:
        """Always false: absence of a flag is not proof that no retraction exists."""

        return False


_RETRACTED = {
    "retracted publication",
    "retraction of publication",
    "retraction",
    "retracted",
    "retractionin",
    "retractionof",
    "is retracted by",
    "is retraction of",
}
_CORRECTION = {
    "published erratum",
    "corrected and republished article",
    "correction",
    "corrigendum",
    "erratum",
    "erratumin",
    "erratumfor",
    "republishedin",
    "republishedfrom",
    "update",
    "is corrected by",
    "is correction of",
}
_EXPRESSION_OF_CONCERN = {
    "expression of concern",
    "expressionofconcernin",
    "expressionofconcernfor",
    "is expression of concern of",
    "is concerned by",
}


def _normalized(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"[_\-\s]+", " ", value.strip().casefold())
    return value or None


def _compact(value: Any) -> str | None:
    normalized = _normalized(value)
    return normalized.replace(" ", "") if normalized else None


def _classify(value: Any) -> PublicationStatus | None:
    normalized = _normalized(value)
    compact = _compact(value)
    if normalized in _RETRACTED or compact in _RETRACTED:
        return PublicationStatus.RETRACTED
    if normalized in _EXPRESSION_OF_CONCERN or compact in _EXPRESSION_OF_CONCERN:
        return PublicationStatus.EXPRESSION_OF_CONCERN
    if normalized in _CORRECTION or compact in _CORRECTION:
        return PublicationStatus.CORRECTION
    return None


def _controlled_signal(source: str, field: str, value: Any) -> tuple[
    PublicationStatus, str
] | None:
    status = _classify(value)
    if status is None:
        return None
    return status, f"{source}:{field}:{status.value}"


def _crossref_signals(work: Any) -> list[tuple[PublicationStatus, str]]:
    if isinstance(work, (list, tuple)):
        signals: list[tuple[PublicationStatus, str]] = []
        for item in work:
            signals.extend(_crossref_signals(item))
        return signals
    if not isinstance(work, Mapping):
        return []
    signals: list[tuple[PublicationStatus, str]] = []

    # Crossmark updates may be represented directly or within update-to.
    for field in ("type", "subtype"):
        signal = _controlled_signal("crossref", field, work.get(field))
        if signal:
            signals.append(signal)

    updates = work.get("update-to")
    if isinstance(updates, (list, tuple)):
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            for field in ("type", "label"):
                signal = _controlled_signal(
                    "crossref", f"update-to.{field}", update.get(field)
                )
                if signal:
                    signals.append(signal)

    relation = work.get("relation")
    if isinstance(relation, Mapping):
        for relation_type, entries in relation.items():
            signal = _controlled_signal(
                "crossref", "relation", relation_type
            )
            if signal:
                signals.append(signal)
            if isinstance(entries, Mapping):
                entries = (entries,)
            if not isinstance(entries, (list, tuple)):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                for field in ("type", "label"):
                    signal = _controlled_signal(
                        "crossref", f"relation.{field}", entry.get(field)
                    )
                    if signal:
                        signals.append(signal)
    return signals


def _pubmed_signals(
    publication_types: Any, related_articles: Any
) -> list[tuple[PublicationStatus, str]]:
    signals: list[tuple[PublicationStatus, str]] = []
    if isinstance(publication_types, (list, tuple)):
        for publication_type in publication_types:
            signal = _controlled_signal(
                "pubmed", "publication_type", publication_type
            )
            if signal:
                signals.append(signal)

    if isinstance(related_articles, Mapping):
        related_articles = (related_articles,)
    if isinstance(related_articles, (list, tuple)):
        for related in related_articles:
            if not isinstance(related, Mapping):
                continue
            for field in ("ref_type", "type", "relation"):
                signal = _controlled_signal(
                    "pubmed", f"related_article.{field}", related.get(field)
                )
                if signal:
                    signals.append(signal)
    return signals


def assess_publication_status(
    *,
    crossref_work: Any = None,
    pubmed_publication_types: Any = None,
    pubmed_related_articles: Any = None,
) -> PublicationStatusAssessment:
    """Assess status without treating missing flags as a clean bill of health."""

    signals = _crossref_signals(crossref_work)
    signals.extend(
        _pubmed_signals(pubmed_publication_types, pubmed_related_articles)
    )
    statuses = {status for status, _ in signals}
    for status in (
        PublicationStatus.RETRACTED,
        PublicationStatus.EXPRESSION_OF_CONCERN,
        PublicationStatus.CORRECTION,
    ):
        if status in statuses:
            controlled = tuple(
                dict.fromkeys(
                    signal for signal_status, signal in signals
                    if signal_status is status
                )
            )
            return PublicationStatusAssessment(status, controlled)
    return PublicationStatusAssessment(PublicationStatus.UNKNOWN)


def assess_crossref_status(work: Any) -> PublicationStatusAssessment:
    return assess_publication_status(crossref_work=work)


def assess_pubmed_status(
    publication_types: Any = None, related_articles: Any = None
) -> PublicationStatusAssessment:
    return assess_publication_status(
        pubmed_publication_types=publication_types,
        pubmed_related_articles=related_articles,
    )
