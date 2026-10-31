"""Lawful open-access full-text triage (v0.4).

Only two lawful routes are queried: Unpaywall (publisher/repository OA
locations) and the Europe PMC open-access subset. Each record is sorted into:

* ``oa_machine_readable``  - OA location with an allowlisted licence
  (CC BY / CC BY-SA / CC0 / public domain); eligible for automated download
  through the existing fail-closed ``SafeFulltextDownloader``.
* ``oa_link_only``          - an OA location exists but the licence is missing,
  NC/ND or otherwise restricted; the link is listed for a human to open, never
  downloaded automatically.
* ``needs_institutional_access`` - no lawful OA location found; use library
  access, interlibrary loan, or ask the authors.
* ``no_identifier``         - no DOI or PMCID to check.

Shadow libraries and paywall circumvention are out of scope by design.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlencode

from .fulltext import FulltextDownloadError, SafeFulltextDownloader
from .http import JsonHttpClient, RemoteServiceError
from .license_policy import LicenseDecision, assess_fulltext_license
from .models import Publication, normalize_doi

EPMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"


@dataclass
class AccessDecision:
    key: str
    title: str
    doi: str | None
    pmid: str | None
    pmcid: str | None
    category: str
    route: str | None = None
    url: str | None = None
    landing_url: str | None = None
    license: str | None = None
    version: str | None = None
    downloaded_path: str | None = None
    downloaded_sha256: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OpenAccessResolver:
    def __init__(
        self,
        client: JsonHttpClient,
        *,
        email: str | None = None,
        epmc_licenses: dict[str, str] | None = None,
    ):
        self.client = client
        self.email = email or os.getenv("UNPAYWALL_EMAIL") or os.getenv("RESEARCH_AGENT_EMAIL")
        self.epmc_licenses = epmc_licenses or {}
        self.issues: list[str] = []

    def unpaywall(self, doi: str) -> dict[str, Any] | None:
        if not self.email:
            return None
        url = f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}?{urlencode({'email': self.email})}"
        try:
            return self.client.get_json(url)
        except RemoteServiceError as exc:
            if "HTTP 404" in str(exc):
                return None
            self.issues.append(f"Unpaywall {doi}: {exc}")
            return None

    def decide(self, publication: Publication) -> AccessDecision:
        doi = normalize_doi(publication.doi)
        decision = AccessDecision(
            key=publication.dedupe_key,
            title=publication.title,
            doi=doi,
            pmid=publication.pmid,
            pmcid=publication.pmcid,
            category="no_identifier",
        )
        # Route 1: Europe PMC open-access subset (JATS XML with stated licence).
        epmc_license = self.epmc_licenses.get(publication.dedupe_key)
        if publication.pmcid and str(publication.access_status.value) == "open":
            url = EPMC_FULLTEXT.format(pmcid=publication.pmcid)
            assessment = assess_fulltext_license(url=url, license=epmc_license, version="publishedVersion")
            decision.route = "europe_pmc_oa"
            decision.url = url
            decision.landing_url = f"https://europepmc.org/articles/{publication.pmcid}"
            decision.license = epmc_license
            decision.version = "publishedVersion"
            if assessment.decision is LicenseDecision.ALLOWED_MACHINE_READ:
                decision.category = "oa_machine_readable"
                return decision
            decision.category = "oa_link_only"
        # Route 2: Unpaywall best OA location.
        if doi:
            data = self.unpaywall(doi)
            location = (data or {}).get("best_oa_location")
            if isinstance(location, dict) and (location.get("url_for_pdf") or location.get("url")):
                assessment = assess_fulltext_license(location=location)
                if assessment.decision is LicenseDecision.ALLOWED_MACHINE_READ:
                    decision.category = "oa_machine_readable"
                    decision.route = "unpaywall"
                    decision.url = location.get("url_for_pdf") or location.get("url")
                    decision.landing_url = location.get("url_for_landing_page")
                    decision.license = location.get("license")
                    decision.version = location.get("version")
                    return decision
                if decision.category != "oa_link_only":
                    decision.category = "oa_link_only"
                    decision.route = "unpaywall"
                    decision.url = location.get("url_for_pdf") or location.get("url")
                    decision.landing_url = location.get("url_for_landing_page")
                    decision.license = location.get("license")
                    decision.version = location.get("version")
                return decision
            if decision.category == "no_identifier":
                decision.category = "needs_institutional_access"
                decision.landing_url = f"https://doi.org/{doi}"
                decision.note = (
                    "No lawful OA location found"
                    + ("" if self.email else " (Unpaywall skipped: no contact email configured)")
                )
        elif decision.category == "no_identifier" and publication.pmid:
            decision.category = "needs_institutional_access"
            decision.landing_url = f"https://pubmed.ncbi.nlm.nih.gov/{publication.pmid}/"
            decision.note = "No DOI; Europe PMC OA subset did not contain it"
        return decision

    def download(
        self,
        decisions: list[AccessDecision],
        directory: Path,
        *,
        max_files: int = 20,
        downloader: SafeFulltextDownloader | None = None,
    ) -> int:
        """Download at most ``max_files`` machine-readable OA documents."""

        directory.mkdir(parents=True, exist_ok=True)
        downloader = downloader or SafeFulltextDownloader()
        count = 0
        for item in decisions:
            if count >= max_files:
                break
            if item.category != "oa_machine_readable" or not item.url:
                continue
            stem = re.sub(r"[^A-Za-z0-9.-]+", "-", item.pmcid or item.doi or item.key).strip("-")
            suffix = ".xml" if item.route == "europe_pmc_oa" else ".pdf"
            target = (directory / f"{stem[:80]}{suffix}").resolve()
            try:
                result = downloader.download(
                    url=item.url, license=item.license, version=item.version,
                    destination=target, overwrite=True,
                )
            except FulltextDownloadError as exc:
                item.note = f"download blocked: {exc}"
                continue
            item.downloaded_path = str(result.path.name)
            item.downloaded_sha256 = result.sha256
            count += 1
        return count
