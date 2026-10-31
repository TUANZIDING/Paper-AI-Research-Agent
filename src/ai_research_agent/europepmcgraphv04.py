"""Europe PMC citation graph and core-metadata enrichment (v0.4).

Europe PMC exposes ``/{source}/{id}/references`` (backward) and
``/{source}/{id}/citations`` (forward) without an API key. Its citation graph
covers what Europe PMC has text-mined or received from publishers; it is not a
complete citation index, so every snowball run states which graph it used.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from .http import JsonHttpClient, RemoteServiceError
from .models import Publication
from .sources import EuropePmcSource, utc_now

REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"


class EuropePmcGraph:
    name = "europe_pmc_graph"

    def __init__(self, client: JsonHttpClient, page_size: int = 1000):
        self.client = client
        self.page_size = max(1, min(page_size, 1000))
        self.abstracts: dict[str, str] = {}
        self.licenses: dict[str, str] = {}
        self._mapper = EuropePmcSource(client)

    def _links(self, pmid: str, kind: str, limit: int) -> tuple[list[dict[str, Any]], int]:
        if kind not in {"references", "citations"}:
            raise ValueError("kind must be references or citations")
        container, key = (
            ("referenceList", "reference") if kind == "references" else ("citationList", "citation")
        )
        items: list[dict[str, Any]] = []
        total = 0
        page = 1
        while len(items) < limit:
            params = {"format": "json", "pageSize": str(self.page_size), "page": str(page)}
            data = self.client.get_json(f"{REST}/MED/{pmid}/{kind}?{urlencode(params)}")
            total = int(data.get("hitCount", 0) or 0)
            block = data.get(container) if isinstance(data.get(container), dict) else {}
            page_items = block.get(key) if isinstance(block.get(key), list) else []
            items.extend(item for item in page_items if isinstance(item, dict))
            if not page_items or len(items) >= total:
                break
            page += 1
        return items[:limit], total

    def backward(self, pmid: str, limit: int) -> tuple[list[str], int, int]:
        """Return (linked PMIDs, references reported, references without a PMID)."""

        items, total = self._links(pmid, "references", limit)
        pmids = [str(item["id"]) for item in items if item.get("source") == "MED" and item.get("id")]
        return list(dict.fromkeys(pmids)), total, len(items) - len(pmids)

    def forward(self, pmid: str, limit: int) -> tuple[list[str], int, int]:
        """Return (citing PMIDs, citations reported, citing records without a PMID)."""

        items, total = self._links(pmid, "citations", limit)
        pmids = [str(item["id"]) for item in items if item.get("source") == "MED" and item.get("id")]
        return list(dict.fromkeys(pmids)), total, len(items) - len(pmids)

    def resolve_pmid(self, doi: str) -> str | None:
        params = {"query": f'DOI:"{doi}"', "format": "json", "resultType": "lite", "pageSize": "1"}
        data = self.client.get_json(f"{REST}/search?{urlencode(params)}")
        results = ((data.get("resultList") or {}).get("result")) or []
        for item in results:
            if isinstance(item, dict) and item.get("pmid"):
                return str(item["pmid"])
        return None

    def fetch_core(self, pmids: list[str], batch: int = 100) -> list[Publication]:
        """Resolve PMIDs to full records with abstracts and OA flags."""

        publications: list[Publication] = []
        unique = list(dict.fromkeys(value for value in pmids if str(value).isdigit()))
        for start in range(0, len(unique), batch):
            chunk = unique[start : start + batch]
            query = "(" + " OR ".join(f"EXT_ID:{value}" for value in chunk) + ") AND SRC:MED"
            params = {
                "query": query, "format": "json", "resultType": "core",
                "pageSize": str(len(chunk)),
            }
            data = self.client.get_json(f"{REST}/search?{urlencode(params)}")
            results = ((data.get("resultList") or {}).get("result"))
            if not isinstance(results, list):
                raise RemoteServiceError("Europe PMC core lookup lacks resultList.result")
            retrieved_at = getattr(self.client, "last_fetched_at", None) or utc_now()
            for item in results:
                publication = self._mapper._publication(item, retrieved_at)
                if publication is None:
                    continue
                abstract = item.get("abstractText") if isinstance(item, dict) else None
                if isinstance(abstract, str) and abstract.strip():
                    self.abstracts[publication.dedupe_key] = " ".join(abstract.split())
                license_value = item.get("license") if isinstance(item, dict) else None
                if isinstance(license_value, str) and license_value.strip():
                    self.licenses[publication.dedupe_key] = license_value.strip()
                publications.append(publication)
        return publications
