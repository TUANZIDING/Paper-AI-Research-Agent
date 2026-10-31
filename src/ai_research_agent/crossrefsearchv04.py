"""Crossref bibliographic discovery and open reference lists (v0.4).

Crossref ``query.bibliographic`` is relevance-ranked free text, not Boolean
retrieval. Its hit counts are therefore huge and not a measure of topical
recall; every Crossref discovery run is capped and recorded as truncated.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote, urlencode

from .http import JsonHttpClient, RemoteServiceError
from .models import Publication, SourceEvidence, normalize_doi
from .sources import PaginationInfo, PaginationState, _validate_paging, utc_now


def _strip_jats(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", value).split())


class CrossrefSearchSource:
    name = "crossref"
    endpoint = "https://api.crossref.org/works"
    select = "DOI,title,author,container-title,issued,type,abstract,references-count"

    def __init__(self, client: JsonHttpClient, mailto: str | None = None):
        self.client = client
        self.mailto = mailto or os.getenv("CROSSREF_MAILTO") or os.getenv("RESEARCH_AGENT_EMAIL")
        self.last_pagination: PaginationInfo | None = None
        self.abstracts: dict[str, str] = {}

    def translate_query(self, query: str) -> str:
        """Drop PubMed field tags and Boolean operators; Crossref ranks free text."""

        text = re.sub(r"\[[^\]]*\]", " ", query)
        text = re.sub(r"\b(?:AND|OR|NOT)\b", " ", text)
        text = re.sub(r"[()\"*]", " ", text)
        return " ".join(text.split())

    def search(
        self, query: str, limit: int | None, *, page_size: int = 100
    ) -> tuple[list[Publication], int]:
        _validate_paging(limit, page_size)
        if limit is None:
            raise ValueError("Crossref free-text discovery requires an explicit limit")
        page_size = min(page_size, 1000)
        if limit > 10000:
            raise ValueError("Crossref ranked discovery is limited to 10,000 records (offset cap)")
        self.last_pagination = None
        publications: list[Publication] = []
        # Offset paging keeps Crossref's relevance order; cursor deep paging does
        # not (verified 2026-10-31: cursor=* returned unrelated records first).
        # Crossref caps offset at 10,000, so ranked discovery stops there.
        offset = 0
        total = 0
        pages = 0
        errors: list[str] = []
        while len(publications) < limit:
            params = {
                "query.bibliographic": self.translate_query(query),
                "rows": str(min(page_size, limit - len(publications))),
                "offset": str(offset),
                "sort": "relevance",
                "order": "desc",
                "select": self.select,
            }
            if self.mailto:
                params["mailto"] = self.mailto
            try:
                data = self.client.get_json(f"{self.endpoint}?{urlencode(params)}")
            except RemoteServiceError as exc:
                self.last_pagination = PaginationInfo(
                    PaginationState.PARTIAL, total, len(publications), pages, limit, [str(exc)]
                )
                raise
            message = data.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("items"), list):
                raise RemoteServiceError("Crossref search lacks message.items")
            total = int(message.get("total-results", 0) or 0)
            items = message["items"]
            pages += 1
            retrieved_at = getattr(self.client, "last_fetched_at", None) or utc_now()
            for item in items:
                publication = self.publication(item, retrieved_at)
                if publication is not None and len(publications) < limit:
                    publications.append(publication)
            offset += len(items)
            if not items or len(publications) >= total:
                break
            if offset >= 10000:
                errors.append("Crossref offset limit (10,000) reached before the cap")
                break
        if errors:
            state = PaginationState.PARTIAL
        elif total > len(publications):
            state = PaginationState.TRUNCATED
        else:
            state = PaginationState.COMPLETE
        self.last_pagination = PaginationInfo(state, total, len(publications), pages, limit, errors)
        return publications, total

    def reference_dois(self, doi: str) -> tuple[list[str], int]:
        """Backward links from the publisher-deposited open reference list.

        Returns (DOIs found, references deposited). References without a DOI
        are counted but cannot be resolved automatically.
        """

        normalized = normalize_doi(doi)
        if not normalized:
            raise ValueError("A valid DOI is required")
        params = {"mailto": self.mailto} if self.mailto else {}
        suffix = f"?{urlencode(params)}" if params else ""
        data = self.client.get_json(f"{self.endpoint}/{quote(normalized, safe='')}{suffix}")
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        references = message.get("reference") if isinstance(message.get("reference"), list) else []
        dois = [
            value
            for value in (normalize_doi(ref.get("DOI")) for ref in references if isinstance(ref, dict))
            if value
        ]
        return list(dict.fromkeys(dois)), len(references)

    def publication(self, item: Any, retrieved_at: str) -> Publication | None:
        if not isinstance(item, dict):
            return None
        titles = item.get("title") or []
        title = str(titles[0]).strip() if titles else ""
        if not title:
            return None
        doi = normalize_doi(item.get("DOI"))
        issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
        year = issued[0] if issued and isinstance(issued[0], int) else None
        authors = [
            " ".join(part for part in (author.get("family"), author.get("given")) if part)
            for author in item.get("author") or []
            if isinstance(author, dict) and (author.get("family") or author.get("given"))
        ]
        containers = item.get("container-title") or []
        publication = Publication(
            title=title,
            authors=authors,
            journal=str(containers[0]) if containers else None,
            year=year,
            doi=doi,
            publication_types=[str(item["type"])] if item.get("type") else [],
            evidence=[
                SourceEvidence(
                    source=self.name,
                    record_id=doi,
                    url=f"https://doi.org/{doi}" if doi else "https://www.crossref.org/",
                    retrieved_at=retrieved_at,
                    title=title,
                    doi=doi,
                    response_sha256=getattr(self.client, "last_response_sha256", None),
                )
            ],
        )
        if isinstance(item.get("abstract"), str) and item["abstract"].strip():
            self.abstracts[publication.dedupe_key] = _strip_jats(item["abstract"])
        return publication
