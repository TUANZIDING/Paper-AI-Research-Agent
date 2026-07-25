from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import os
import re
from typing import Any
from urllib.parse import quote, urlencode
import xml.etree.ElementTree as ET

from .http import JsonHttpClient, RemoteServiceError
from .models import (
    AccessStatus,
    Publication,
    RetractionStatus,
    SourceEvidence,
    normalize_doi,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _year(value: Any) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group()) if match else None


def _clean_xml_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _relation_doi(relation: ET.Element) -> str | None:
    for element in relation.findall(".//ArticleId"):
        if str(element.get("IdType") or "").casefold() == "doi":
            doi = normalize_doi(_clean_xml_text(element.text))
            if doi:
                return doi
    direct = normalize_doi(_clean_xml_text(relation.findtext("DOI")))
    if direct:
        return direct
    for field in (relation.findtext("RefSource"), relation.findtext("Note")):
        if not isinstance(field, str):
            continue
        match = re.search(r"10\.\d{4,9}/[^\s<>\]\[\"']+", field, re.I)
        if match:
            doi = normalize_doi(match.group(0))
            if doi:
                return doi
    return None


_NLM_PUBMED_DOCTYPE = re.compile(
    rb'<!DOCTYPE\s+PubmedArticleSet\s+PUBLIC\s+'
    rb'"-//NLM//DTD PubMedArticle,\s*[^"<>]+//EN"\s+'
    rb'"https://dtd\.nlm\.nih\.gov/ncbi/pubmed/out/pubmed_[0-9]+\.dtd"\s*>',
    re.I,
)


def _safe_pubmed_xml(payload: bytes) -> bytes:
    """Remove only NLM's known PubMed DTD declaration; reject all others."""

    upper = payload.upper()
    if b"<!ENTITY" in upper:
        raise RemoteServiceError("Unsafe PubMed XML declaration (ENTITY)")
    doctypes = list(re.finditer(rb"<!DOCTYPE\b[^>]*>", payload, re.I))
    if not doctypes:
        return payload
    if len(doctypes) != 1 or _NLM_PUBMED_DOCTYPE.fullmatch(doctypes[0].group(0)) is None:
        raise RemoteServiceError("Unsafe PubMed XML declaration (DOCTYPE)")
    match = doctypes[0]
    return payload[: match.start()] + payload[match.end() :]


class PaginationState(str, Enum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    PARTIAL = "partial"


@dataclass
class PaginationInfo:
    state: PaginationState
    total_available: int
    records_returned: int
    pages_fetched: int
    max_records: int | None
    errors: list[str] = field(default_factory=list)
    history_used: bool = False
    history_query_key: str | None = None
    history_failure: str | None = None


def _validate_paging(limit: int | None, page_size: int) -> None:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive or None for complete pagination")
    if page_size < 1:
        raise ValueError("page_size must be positive")


class PubMedSource:
    name = "pubmed"
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, client: JsonHttpClient):
        self.client = client
        self.last_pagination: PaginationInfo | None = None
        self.last_status_evidence: dict[str, str | None] | None = None

    def _common(self) -> dict[str, str]:
        params = {
            "tool": os.getenv("NCBI_TOOL", "ai_research_agent"),
        }
        email = os.getenv("NCBI_EMAIL") or os.getenv("RESEARCH_AGENT_EMAIL")
        if email:
            params["email"] = email
        api_key = os.getenv("NCBI_API_KEY")
        if api_key:
            params["api_key"] = api_key
        return params

    def search(
        self,
        query: str,
        limit: int | None,
        *,
        page_size: int = 100,
    ) -> tuple[list[Publication], int]:
        """Search a fixed PubMed UID set through the NCBI History Server.

        A positive ``limit`` is an explicit max-records cap. ``None`` requests
        complete pagination. ``last_pagination`` records whether the outcome
        was complete, deliberately truncated, or partial.
        """
        _validate_paging(limit, page_size)
        self.last_pagination = None
        if not hasattr(self.client, "get_bytes"):
            return self._legacy_search(query, limit, page_size=page_size)

        publications: list[Publication] = []
        errors: list[str] = []
        total = 0
        pages = 0
        processed_ids = 0
        search_params = {
            **self._common(),
            "db": "pubmed",
            "retmode": "json",
            "retmax": "0",
            "term": query,
            "usehistory": "y",
        }
        search_url = f"{self.base}/esearch.fcgi?{urlencode(search_params)}"
        try:
            search_data = self.client.get_json(search_url)
            result = search_data.get("esearchresult")
            if not isinstance(result, dict):
                raise RemoteServiceError(
                    "PubMed esearch response lacks esearchresult"
                )
            try:
                total = int(result.get("count", 0))
            except (TypeError, ValueError) as exc:
                raise RemoteServiceError(
                    "PubMed esearch returned an invalid count"
                ) from exc
            query_key = result.get("querykey")
            webenv = result.get("webenv")
            if not isinstance(query_key, str) or not query_key.strip():
                raise RemoteServiceError(
                    "PubMed History response lacks querykey"
                )
            if not isinstance(webenv, str) or not webenv.strip():
                raise RemoteServiceError(
                    "PubMed History response lacks WebEnv"
                )
        except RemoteServiceError as exc:
            self.last_pagination = PaginationInfo(
                PaginationState.PARTIAL,
                total,
                0,
                0,
                limit,
                [str(exc)],
                history_used=False,
                history_failure=str(exc),
            )
            raise

        target = min(total, limit) if limit is not None else total
        while processed_ids < target:
            remaining = page_size
            if limit is not None:
                remaining = min(remaining, limit - processed_ids)
            remaining = min(remaining, target - processed_ids)
            summary_params = {
                **self._common(),
                "db": "pubmed",
                "retmode": "json",
                "query_key": query_key,
                "WebEnv": webenv,
                "retstart": str(processed_ids),
                "retmax": str(remaining),
            }
            summary_url = f"{self.base}/esummary.fcgi?{urlencode(summary_params)}"
            try:
                summary_data = self.client.get_json(summary_url)
            except RemoteServiceError as exc:
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL,
                    total,
                    len(publications),
                    pages,
                    limit,
                    [str(exc)],
                    history_used=True,
                    history_query_key=query_key,
                    history_failure=str(exc),
                )
                raise
            summary = summary_data.get("result")
            if not isinstance(summary, dict):
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL,
                    total,
                    len(publications),
                    pages,
                    limit,
                    ["PubMed esummary response lacks result"],
                    history_used=True,
                    history_query_key=query_key,
                    history_failure="PubMed esummary response lacks result",
                )
                raise RemoteServiceError("PubMed esummary response lacks result")
            raw_ids = summary.get("uids")
            if not isinstance(raw_ids, list):
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL,
                    total,
                    len(publications),
                    pages,
                    limit,
                    ["PubMed History esummary response lacks uids"],
                    history_used=True,
                    history_query_key=query_key,
                    history_failure="PubMed History esummary response lacks uids",
                )
                raise RemoteServiceError(
                    "PubMed History esummary response lacks uids"
                )
            ids = [str(value) for value in raw_ids]
            pages += 1
            if not ids:
                errors.append(
                    "PubMed History returned an empty page before the target was reached"
                )
                break
            retrieved_at = getattr(self.client, "last_fetched_at", None) or utc_now()
            for pmid in ids:
                item = summary.get(pmid)
                if not isinstance(item, dict):
                    errors.append(f"PubMed summary missing PMID {pmid}")
                    continue
                publication = self._publication(pmid, item, retrieved_at)
                if publication is None:
                    errors.append(f"PubMed summary missing title for PMID {pmid}")
                    continue
                publications.append(publication)
            processed_ids += len(ids)

        if errors:
            state = PaginationState.PARTIAL
        elif limit is not None and total > limit:
            state = PaginationState.TRUNCATED
        else:
            state = PaginationState.COMPLETE
        self.last_pagination = PaginationInfo(
            state,
            total,
            len(publications),
            pages,
            limit,
            errors,
            history_used=True,
            history_query_key=query_key,
            history_failure="; ".join(errors) if errors else None,
        )
        return publications, total

    def _legacy_search(
        self, query: str, limit: int | None, *, page_size: int
    ) -> tuple[list[Publication], int]:
        """Compatibility path for pre-byte-transport test doubles only."""

        publications: list[Publication] = []
        errors: list[str] = []
        total: int | None = None
        retstart = 0
        pages = 0
        processed_ids = 0
        while total is None or processed_ids < min(total, limit or total):
            remaining = (
                page_size
                if limit is None
                else min(page_size, limit - processed_ids)
            )
            params = {
                **self._common(),
                "db": "pubmed",
                "retmode": "json",
                "retstart": str(retstart),
                "retmax": str(remaining),
                "term": query,
            }
            data = self.client.get_json(
                f"{self.base}/esearch.fcgi?{urlencode(params)}"
            )
            result = data.get("esearchresult")
            if not isinstance(result, dict):
                error = "PubMed esearch response lacks esearchresult"
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL, total or 0, len(publications), pages,
                    limit, [error], history_failure=error,
                )
                raise RemoteServiceError(error)
            total = int(result.get("count", 0)) if total is None else total
            ids = [str(value) for value in result.get("idlist", [])]
            pages += 1
            if not ids:
                errors.append("PubMed returned an empty page before the target was reached")
                break
            summary_params = {
                **self._common(), "db": "pubmed", "retmode": "json",
                "id": ",".join(ids),
            }
            summary = self.client.get_json(
                f"{self.base}/esummary.fcgi?{urlencode(summary_params)}"
            ).get("result", {})
            retrieved_at = getattr(self.client, "last_fetched_at", None) or utc_now()
            for pmid in ids:
                item = summary.get(pmid) if isinstance(summary, dict) else None
                publication = (
                    self._publication(pmid, item, retrieved_at)
                    if isinstance(item, dict) else None
                )
                if publication is None:
                    errors.append(f"PubMed summary missing PMID {pmid}")
                else:
                    publications.append(publication)
            processed_ids += len(ids)
            retstart += len(ids)
        total = total or 0
        state = (
            PaginationState.PARTIAL if errors else
            PaginationState.TRUNCATED if limit is not None and total > limit else
            PaginationState.COMPLETE
        )
        warning = "Legacy client lacks byte/XML transport; History snapshot was not used"
        self.last_pagination = PaginationInfo(
            state, total, len(publications), pages, limit, errors,
            history_used=False, history_failure=warning,
        )
        return publications, total

    def fetch_status_relations(self, pmid: str) -> list[dict[str, str | None]]:
        """Fetch structured PubMed post-publication relation metadata."""

        if not isinstance(pmid, str) or not re.fullmatch(r"[1-9][0-9]*", pmid):
            raise ValueError("A numeric PMID is required")
        params = {
            **self._common(),
            "db": "pubmed",
            "retmode": "xml",
            "id": pmid,
        }
        url = f"{self.base}/efetch.fcgi?{urlencode(params)}"
        payload = self.client.get_bytes(
            url, accept="application/xml,text/xml", max_bytes=5_000_000
        )
        if len(payload) > 5_000_000:
            raise RemoteServiceError("PubMed XML exceeds 5000000 byte limit")
        payload = _safe_pubmed_xml(payload)
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise RemoteServiceError("Invalid PubMed XML response") from exc
        if root.tag.casefold() == "error" or root.findall(".//ERROR"):
            raise RemoteServiceError("PubMed XML contains an explicit error")
        matching_articles = []
        for article in root.findall(".//PubmedArticle"):
            returned_pmid = _clean_xml_text(
                article.findtext("./MedlineCitation/PMID")
            )
            if returned_pmid == pmid:
                matching_articles.append(article)
        if len(matching_articles) != 1:
            raise RemoteServiceError(
                f"PubMed XML target mismatch for PMID {pmid}"
            )
        self.last_status_evidence = {
            "retrieved_at": getattr(self.client, "last_fetched_at", None),
            "response_sha256": getattr(self.client, "last_response_sha256", None),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
        relations: list[dict[str, str | None]] = []
        for relation in matching_articles[0].findall(
            ".//CommentsCorrectionsList/CommentsCorrections"
        ):
            ref_type = relation.get("RefType")
            if not ref_type:
                continue
            related_pmid = _clean_xml_text(relation.findtext("PMID"))
            doi = _relation_doi(relation)
            note = _clean_xml_text(relation.findtext("Note"))
            relations.append(
                {
                    "ref_type": ref_type,
                    "pmid": related_pmid,
                    "doi": doi,
                    "note": note,
                }
            )
        return relations

    def _publication(
        self, pmid: str, item: dict[str, Any], retrieved_at: str
    ) -> Publication | None:
        identifiers = {
            entry.get("idtype"): entry.get("value")
            for entry in item.get("articleids", [])
            if isinstance(entry, dict)
        }
        doi = normalize_doi(identifiers.get("doi"))
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        return Publication(
            title=title,
            authors=[
                str(author.get("name"))
                for author in item.get("authors", [])
                if isinstance(author, dict) and author.get("name")
            ],
            journal=item.get("fulljournalname") or item.get("source"),
            year=_year(item.get("pubdate")),
            doi=doi,
            pmid=pmid,
            publication_types=list(item.get("pubtype", [])),
            retraction_status=(
                RetractionStatus.FLAGGED
                if any(
                    "retracted publication" in str(kind).casefold()
                    for kind in item.get("pubtype", [])
                )
                else RetractionStatus.UNKNOWN
            ),
            evidence=[
                SourceEvidence(
                    source=self.name,
                    record_id=pmid,
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    retrieved_at=retrieved_at,
                    title=title,
                    doi=doi,
                    pmid=pmid,
                    note=str(item.get("recordstatus") or ""),
                    response_sha256=getattr(self.client, "last_response_sha256", None),
                )
            ],
        )


class EuropePmcSource:
    name = "europe_pmc"
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def __init__(self, client: JsonHttpClient):
        self.client = client
        self.last_pagination: PaginationInfo | None = None

    @staticmethod
    def translate_query(query: str) -> str:
        doi_match = re.fullmatch(r"\s*(10\.\S+?/\S+?)\[doi\]\s*", query, re.I)
        if doi_match:
            return f"DOI:{doi_match.group(1).rstrip(' .;,')}"
        return query

    def search(
        self,
        query: str,
        limit: int | None,
        *,
        page_size: int = 100,
    ) -> tuple[list[Publication], int]:
        """Search Europe PMC with cursor pagination.

        A positive ``limit`` is an explicit max-records cap. ``None`` follows
        cursors until all reported hits have been fetched.
        """
        _validate_paging(limit, page_size)
        self.last_pagination = None
        translated_query = self.translate_query(query)
        publications: list[Publication] = []
        errors: list[str] = []
        cursor = "*"
        total: int | None = None
        pages = 0
        while total is None or len(publications) < min(total, limit or total):
            requested = page_size
            if limit is not None:
                requested = min(requested, limit - len(publications))
            params = {
                "query": translated_query,
                "format": "json",
                "resultType": "lite",
                "pageSize": str(requested),
                "cursorMark": cursor,
            }
            url = f"{self.endpoint}?{urlencode(params)}"
            try:
                data = self.client.get_json(url)
            except RemoteServiceError as exc:
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL,
                    total or 0,
                    len(publications),
                    pages,
                    limit,
                    [str(exc)],
                )
                raise
            try:
                page_total = int(data.get("hitCount", 0))
            except (TypeError, ValueError) as exc:
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL,
                    total or 0,
                    len(publications),
                    pages,
                    limit,
                    ["Europe PMC returned an invalid hitCount"],
                )
                raise RemoteServiceError("Europe PMC returned an invalid hitCount") from exc
            if total is None:
                total = page_total
            result_list = data.get("resultList")
            if not isinstance(result_list, dict):
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL,
                    total,
                    len(publications),
                    pages,
                    limit,
                    ["Europe PMC response lacks resultList"],
                )
                raise RemoteServiceError("Europe PMC response lacks resultList")
            items = result_list.get("result", [])
            if not isinstance(items, list):
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL,
                    total,
                    len(publications),
                    pages,
                    limit,
                    ["Europe PMC resultList.result is not a list"],
                )
                raise RemoteServiceError("Europe PMC resultList.result is not a list")
            pages += 1
            before = len(publications)
            retrieved_at = getattr(self.client, "last_fetched_at", None) or utc_now()
            for item in items:
                if limit is not None and len(publications) >= limit:
                    break
                publication = self._publication(item, retrieved_at)
                if publication is None:
                    errors.append("Europe PMC result missing an object or title")
                    continue
                publications.append(publication)
            if len(publications) >= min(total, limit or total):
                break
            next_cursor = data.get("nextCursorMark")
            if not items:
                errors.append("Europe PMC returned an empty page before the target was reached")
                break
            if not next_cursor or str(next_cursor) == cursor:
                errors.append("Europe PMC cursor did not advance before target was reached")
                break
            if len(publications) == before and items:
                errors.append("Europe PMC page contained no usable records")
                break
            cursor = str(next_cursor)

        total = total or 0
        if errors:
            state = PaginationState.PARTIAL
        elif limit is not None and total > limit:
            state = PaginationState.TRUNCATED
        else:
            state = PaginationState.COMPLETE
        self.last_pagination = PaginationInfo(
            state, total, len(publications), pages, limit, errors
        )
        return publications, total

    def _publication(
        self, item: Any, retrieved_at: str
    ) -> Publication | None:
        if not isinstance(item, dict) or not item.get("title"):
            return None
        source_id = str(item.get("id") or "")
        pmid = str(item.get("pmid")) if item.get("pmid") else None
        pmcid = str(item.get("pmcid")) if item.get("pmcid") else None
        doi = normalize_doi(item.get("doi"))
        is_open = str(item.get("isOpenAccess", "")).upper()
        if is_open == "Y":
            access_status = AccessStatus.OPEN
        elif is_open == "N":
            access_status = AccessStatus.CLOSED
        else:
            access_status = AccessStatus.UNKNOWN
        access_url = (
            f"https://europepmc.org/articles/{pmcid}"
            if pmcid
            else f"https://europepmc.org/article/{item.get('source', 'MED')}/{source_id}"
        )
        retracted = str(item.get("isRetracted", "")).upper()
        return Publication(
            title=str(item["title"]).strip(),
            authors=[
                value.strip()
                for value in str(item.get("authorString") or "").rstrip(".").split(",")
                if value.strip()
            ],
            journal=item.get("journalTitle"),
            year=_year(item.get("pubYear")),
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            publication_types=[
                value.strip()
                for value in str(item.get("pubType") or "").split(";")
                if value.strip()
            ],
            access_status=access_status,
            access_url=access_url,
            retraction_status=(
                RetractionStatus.FLAGGED
                if retracted == "Y"
                else (
                    RetractionStatus.NOT_FLAGGED
                    if retracted == "N"
                    else RetractionStatus.UNKNOWN
                )
            ),
            evidence=[
                SourceEvidence(
                    source=self.name,
                    record_id=source_id,
                    url=access_url,
                    retrieved_at=retrieved_at,
                    title=str(item["title"]).strip(),
                    doi=doi,
                    pmid=pmid,
                    response_sha256=getattr(self.client, "last_response_sha256", None),
                )
            ],
        )


class CrossrefSource:
    name = "crossref"
    endpoint = "https://api.crossref.org/works"

    def __init__(self, client: JsonHttpClient, mailto: str | None = None):
        self.client = client
        self.mailto = mailto or os.getenv("CROSSREF_MAILTO")
        self.last_update_evidence: dict[str, str | None] | None = None

    def lookup(self, doi: str) -> dict[str, Any] | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        params = {"mailto": self.mailto} if self.mailto else {}
        suffix = f"?{urlencode(params)}" if params else ""
        url = f"{self.endpoint}/{quote(normalized, safe='')}{suffix}"
        data = self.client.get_json(url)
        message = data.get("message")
        return message if isinstance(message, dict) else None

    def lookup_updates(self, doi: str) -> list[dict[str, Any]]:
        """Reverse-query notices that declare they update the target DOI."""
        normalized = normalize_doi(doi)
        if not normalized:
            raise ValueError("A valid DOI is required for an update lookup")
        params = {
            "filter": f"updates:{normalized}",
            "rows": "100",
        }
        if self.mailto:
            params["mailto"] = self.mailto
        data = self.client.get_json(f"{self.endpoint}?{urlencode(params)}")
        message = data.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("items"), list):
            raise RemoteServiceError("Crossref update lookup lacks message.items")
        items = [item for item in message["items"] if isinstance(item, dict)]
        try:
            total = int(message.get("total-results", len(items)))
        except (TypeError, ValueError) as exc:
            raise RemoteServiceError(
                "Crossref update lookup returned an invalid total-results"
            ) from exc
        if total > len(items):
            raise RemoteServiceError(
                f"Crossref update lookup truncated: returned {len(items)} of {total}"
            )
        bound_items: list[dict[str, Any]] = []
        for item in items:
            updates = item.get("update-to")
            if not isinstance(updates, list) or not any(
                isinstance(update, dict)
                and normalize_doi(update.get("DOI")) == normalized
                for update in updates
            ):
                raise RemoteServiceError(
                    "Crossref update item is not bound to the requested DOI"
                )
            projected = dict(item)
            projected["update-to"] = [
                update
                for update in updates
                if isinstance(update, dict)
                and normalize_doi(update.get("DOI")) == normalized
            ]
            bound_items.append(projected)
        self.last_update_evidence = {
            "retrieved_at": getattr(self.client, "last_fetched_at", None),
            "response_sha256": getattr(self.client, "last_response_sha256", None),
            "url": f"https://api.crossref.org/works?filter=updates:{normalized}",
        }
        return bound_items

    @staticmethod
    def evidence(
        item: dict[str, Any], retrieved_at: str | None = None
    ) -> SourceEvidence:
        doi = normalize_doi(item.get("DOI"))
        titles = item.get("title") or []
        title = str(titles[0]).strip() if titles else None
        return SourceEvidence(
            source="crossref",
            record_id=doi,
            url=f"https://doi.org/{doi}" if doi else "https://www.crossref.org/",
            retrieved_at=retrieved_at or utc_now(),
            title=title,
            doi=doi,
        )
