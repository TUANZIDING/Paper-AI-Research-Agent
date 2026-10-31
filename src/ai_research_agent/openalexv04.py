"""OpenAlex discovery and citation-graph access (v0.4).

OpenAlex list endpoints now require a free API key for reliable use. The key is
read from ``OPENALEX_API_KEY`` and is never written to archives (the archive
layer strips ``api_key`` from stored URLs). Without a key, requests may be
refused with HTTP 429; callers must record that as a source issue rather than
silently treating the source as empty.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote, urlencode

from .http import JsonHttpClient, RemoteServiceError
from .models import AccessStatus, Publication, RetractionStatus, SourceEvidence, normalize_doi
from .sources import PaginationInfo, PaginationState, _validate_paging, utc_now

OPENALEX_ID = re.compile(r"^(?:https://openalex\.org/)?(W\d+)$", re.I)


def short_openalex_id(value: Any) -> str | None:
    match = OPENALEX_ID.match(str(value or "").strip())
    return match.group(1).upper() if match else None


def abstract_from_inverted_index(index: Any) -> str | None:
    """Rebuild plain text from OpenAlex's ``abstract_inverted_index``."""

    if not isinstance(index, dict) or not index:
        return None
    positions: dict[int, str] = {}
    for word, places in index.items():
        if not isinstance(places, list):
            continue
        for place in places:
            if isinstance(place, int) and place >= 0:
                positions[place] = str(word)
    if not positions:
        return None
    return " ".join(positions[key] for key in sorted(positions))


class OpenAlexSource:
    name = "openalex"
    endpoint = "https://api.openalex.org/works"

    def __init__(self, client: JsonHttpClient, api_key: str | None = None):
        self.client = client
        self.api_key = api_key if api_key is not None else os.getenv("OPENALEX_API_KEY")
        self.mailto = os.getenv("RESEARCH_AGENT_EMAIL")
        self.last_pagination: PaginationInfo | None = None
        self.abstracts: dict[str, str] = {}
        self.references: dict[str, list[str]] = {}

    def _auth(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.mailto:
            params["mailto"] = self.mailto
        return params

    def translate_query(self, query: str) -> str:
        return query

    def search(
        self, query: str, limit: int | None, *, page_size: int = 100
    ) -> tuple[list[Publication], int]:
        """Cursor-paginated full-text search; truncation is always explicit."""

        _validate_paging(limit, page_size)
        page_size = min(page_size, 200)
        self.last_pagination = None
        publications: list[Publication] = []
        errors: list[str] = []
        cursor = "*"
        total: int | None = None
        pages = 0
        while total is None or len(publications) < min(total, limit or total):
            requested = page_size if limit is None else min(page_size, limit - len(publications))
            params = {**self._auth(), "search": query, "per-page": str(requested), "cursor": cursor}
            try:
                data = self.client.get_json(f"{self.endpoint}?{urlencode(params)}")
            except RemoteServiceError as exc:
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL, total or 0, len(publications), pages, limit, [str(exc)]
                )
                raise
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            if total is None:
                try:
                    total = int(meta.get("count", 0))
                except (TypeError, ValueError) as exc:
                    raise RemoteServiceError("OpenAlex returned an invalid meta.count") from exc
            results = data.get("results")
            if not isinstance(results, list):
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL, total, len(publications), pages, limit,
                    ["OpenAlex response lacks results"],
                )
                raise RemoteServiceError("OpenAlex response lacks results")
            pages += 1
            retrieved_at = getattr(self.client, "last_fetched_at", None) or utc_now()
            for item in results:
                if limit is not None and len(publications) >= limit:
                    break
                publication = self.publication(item, retrieved_at)
                if publication is not None:
                    publications.append(publication)
            if len(publications) >= min(total, limit or total):
                break
            next_cursor = meta.get("next_cursor")
            if not results or not next_cursor or str(next_cursor) == cursor:
                errors.append("OpenAlex cursor ended before the target was reached")
                break
            cursor = str(next_cursor)
        total = total or 0
        if errors:
            state = PaginationState.PARTIAL
        elif limit is not None and total > limit:
            state = PaginationState.TRUNCATED
        else:
            state = PaginationState.COMPLETE
        self.last_pagination = PaginationInfo(state, total, len(publications), pages, limit, errors)
        return publications, total

    def work(self, identifier: str) -> Publication | None:
        """Resolve one work by DOI, PMID (``pmid:123``) or OpenAlex ID."""

        short = short_openalex_id(identifier)
        doi = normalize_doi(identifier)
        if short:
            path = short
        elif doi:
            path = f"doi:{quote(doi, safe='/')}"
        elif re.fullmatch(r"(?:pmid:)?\d+", identifier.strip(), re.I):
            path = f"pmid:{identifier.split(':')[-1].strip()}"
        else:
            raise ValueError(f"Unsupported OpenAlex identifier: {identifier!r}")
        suffix = f"?{urlencode(self._auth())}" if self._auth() else ""
        data = self.client.get_json(f"{self.endpoint}/{path}{suffix}")
        return self.publication(data, getattr(self.client, "last_fetched_at", None) or utc_now())

    def cited_by(self, openalex_id: str, limit: int) -> tuple[list[Publication], int]:
        """Forward citations: works whose reference list contains ``openalex_id``."""

        short = short_openalex_id(openalex_id)
        if not short:
            raise ValueError("An OpenAlex work ID is required")
        publications: list[Publication] = []
        cursor = "*"
        total = 0
        while len(publications) < limit:
            params = {
                **self._auth(), "filter": f"cites:{short}",
                "per-page": str(min(200, limit - len(publications))), "cursor": cursor,
            }
            data = self.client.get_json(f"{self.endpoint}?{urlencode(params)}")
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            total = int(meta.get("count", 0) or 0)
            results = data.get("results") if isinstance(data.get("results"), list) else []
            retrieved_at = getattr(self.client, "last_fetched_at", None) or utc_now()
            for item in results:
                publication = self.publication(item, retrieved_at)
                if publication is not None:
                    publications.append(publication)
            next_cursor = meta.get("next_cursor")
            if not results or not next_cursor or len(publications) >= total:
                break
            cursor = str(next_cursor)
        return publications[:limit], total

    def fetch_many(self, openalex_ids: list[str]) -> list[Publication]:
        """Batch-resolve OpenAlex IDs (50 per request) for backward snowballing."""

        shorts = [value for value in (short_openalex_id(item) for item in openalex_ids) if value]
        publications: list[Publication] = []
        for start in range(0, len(shorts), 50):
            chunk = shorts[start : start + 50]
            params = {**self._auth(), "filter": "openalex:" + "|".join(chunk), "per-page": "50"}
            data = self.client.get_json(f"{self.endpoint}?{urlencode(params)}")
            results = data.get("results") if isinstance(data.get("results"), list) else []
            retrieved_at = getattr(self.client, "last_fetched_at", None) or utc_now()
            for item in results:
                publication = self.publication(item, retrieved_at)
                if publication is not None:
                    publications.append(publication)
        return publications

    def publication(self, item: Any, retrieved_at: str) -> Publication | None:
        if not isinstance(item, dict):
            return None
        title = str(item.get("title") or item.get("display_name") or "").strip()
        if not title:
            return None
        short = short_openalex_id(item.get("id"))
        ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
        doi = normalize_doi(item.get("doi") or ids.get("doi"))
        pmid_raw = str(ids.get("pmid") or "")
        pmid_match = re.search(r"(\d+)$", pmid_raw)
        pmid = pmid_match.group(1) if pmid_match else None
        pmcid_match = re.search(r"(PMC\d+)", str(ids.get("pmcid") or ""), re.I)
        source = (item.get("primary_location") or {}).get("source") or {}
        open_access = item.get("open_access") if isinstance(item.get("open_access"), dict) else {}
        is_oa = open_access.get("is_oa")
        authors = [
            str((entry.get("author") or {}).get("display_name"))
            for entry in item.get("authorships") or []
            if isinstance(entry, dict) and (entry.get("author") or {}).get("display_name")
        ]
        record_url = f"https://openalex.org/{short}" if short else "https://openalex.org/"
        publication = Publication(
            title=title,
            authors=authors,
            journal=source.get("display_name") if isinstance(source, dict) else None,
            year=item.get("publication_year") if isinstance(item.get("publication_year"), int) else None,
            doi=doi,
            pmid=pmid,
            pmcid=pmcid_match.group(1).upper() if pmcid_match else None,
            publication_types=[str(item["type"])] if item.get("type") else [],
            access_status=(
                AccessStatus.OPEN if is_oa is True
                else AccessStatus.CLOSED if is_oa is False
                else AccessStatus.UNKNOWN
            ),
            access_url=open_access.get("oa_url") if is_oa else None,
            retraction_status=(
                RetractionStatus.FLAGGED if item.get("is_retracted") is True
                else RetractionStatus.NOT_FLAGGED if item.get("is_retracted") is False
                else RetractionStatus.UNKNOWN
            ),
            evidence=[
                SourceEvidence(
                    source=self.name,
                    record_id=short,
                    url=record_url,
                    retrieved_at=retrieved_at,
                    title=title,
                    doi=doi,
                    pmid=pmid,
                    response_sha256=getattr(self.client, "last_response_sha256", None),
                )
            ],
        )
        abstract = abstract_from_inverted_index(item.get("abstract_inverted_index"))
        if abstract:
            self.abstracts[publication.dedupe_key] = abstract
        if short:
            refs = [value for value in (short_openalex_id(ref) for ref in item.get("referenced_works") or []) if value]
            self.references[short] = refs
        return publication


class OpenAlexGraph:
    """Citation graph adapter: OpenAlex links, Europe PMC core metadata.

    Links are mapped to PMIDs so that both graphs feed the same dedupe and
    pre-screen path. Linked works without a PMID are counted, not dropped
    silently.
    """

    name = "openalex_graph"

    def __init__(self, source: OpenAlexSource, metadata):
        self.source = source
        self.metadata = metadata  # an EuropePmcGraph for core records
        self.abstracts = metadata.abstracts

    def _work_id(self, pmid: str) -> str | None:
        work = self.source.work(f"pmid:{pmid}")
        if work is None:
            return None
        return work.evidence[0].record_id if work.evidence else None

    def backward(self, pmid: str, limit: int) -> tuple[list[str], int, int]:
        work_id = self._work_id(pmid)
        refs = self.source.references.get(work_id or "", [])
        works = self.source.fetch_many(refs[:limit])
        pmids = [item.pmid for item in works if item.pmid]
        return list(dict.fromkeys(pmids)), len(refs), len(refs[:limit]) - len(pmids)

    def forward(self, pmid: str, limit: int) -> tuple[list[str], int, int]:
        work_id = self._work_id(pmid)
        if not work_id:
            return [], 0, 0
        works, total = self.source.cited_by(work_id, limit)
        pmids = [item.pmid for item in works if item.pmid]
        return list(dict.fromkeys(pmids)), total, len(works) - len(pmids)

    def resolve_pmid(self, doi: str) -> str | None:
        work = self.source.work(doi)
        return work.pmid if work else None

    def fetch_core(self, pmids: list[str], batch: int = 100) -> list[Publication]:
        return self.metadata.fetch_core(pmids, batch)
