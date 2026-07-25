from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class LicenseCandidate:
    """Unverified candidate extracted from public metadata.

    No field in this record is a legal conclusion or download authorization.
    """

    source: str
    url: str
    license_text: str | None
    version: str | None
    raw_version: str | None
    source_record: str | None
    raw_fragment_json: str


def discover_license_candidates(
    *,
    europe_pmc: Mapping[str, Any] | None = None,
    pmc: Mapping[str, Any] | None = None,
    crossref: Mapping[str, Any] | None = None,
) -> list[LicenseCandidate]:
    """Extract candidates from caller-supplied public metadata only.

    The function performs no network request and never infers a license merely
    from an open-access flag or repository hostname.
    """

    candidates: list[LicenseCandidate] = []
    if isinstance(europe_pmc, Mapping):
        candidates.extend(_from_europe_pmc(europe_pmc))
    if isinstance(pmc, Mapping):
        candidates.extend(_from_pmc(pmc))
    if isinstance(crossref, Mapping):
        candidates.extend(_from_crossref(crossref))
    unique: dict[tuple[str, str, str | None, str | None], LicenseCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.source,
            candidate.url,
            candidate.license_text,
            candidate.version,
        )
        unique.setdefault(key, candidate)
    return sorted(
        unique.values(),
        key=lambda item: (
            item.source,
            item.url,
            item.license_text or "",
            item.version or "",
        ),
    )


def _from_europe_pmc(metadata: Mapping[str, Any]) -> list[LicenseCandidate]:
    record_id = _text(metadata.get("pmcid") or metadata.get("id"))
    root_license = _license_text(metadata.get("license"))
    root_version_raw = _text(metadata.get("version") or metadata.get("manuscriptVersion"))
    entries = _mapping_list(metadata.get("fullTextUrlList"), "fullTextUrl")
    if not entries:
        entries = _as_mapping_list(metadata.get("fullTextUrl"))
    candidates: list[LicenseCandidate] = []
    for entry in entries:
        url = _text(entry.get("url") or entry.get("href"))
        if not url:
            continue
        raw_version = _text(
            entry.get("version") or entry.get("manuscriptVersion")
        ) or root_version_raw
        candidates.append(
            _candidate(
                source="europe_pmc",
                url=url,
                license_text=_license_text(entry.get("license")) or root_license,
                raw_version=raw_version,
                source_record=record_id,
                fragment={"record": record_id, "full_text": dict(entry), "license": metadata.get("license")},
            )
        )
    return candidates


def _from_pmc(metadata: Mapping[str, Any]) -> list[LicenseCandidate]:
    record_id = _text(metadata.get("pmcid") or metadata.get("id"))
    root_license = _license_text(
        metadata.get("license") or metadata.get("license_url") or metadata.get("license_text")
    )
    raw_version = _text(metadata.get("version") or metadata.get("manuscript_version"))
    entries: list[Mapping[str, Any]] = []
    for key in ("full_text", "fullTextUrl", "files", "links"):
        entries.extend(_as_mapping_list(metadata.get(key)))
    for key in ("pdf_url", "xml_url", "full_text_url", "href"):
        url = _text(metadata.get(key))
        if url:
            entries.append({"url": url})
    candidates: list[LicenseCandidate] = []
    for entry in entries:
        url = _text(entry.get("url") or entry.get("href"))
        if not url:
            continue
        entry_version = _text(entry.get("version") or entry.get("manuscript_version"))
        candidates.append(
            _candidate(
                source="pmc",
                url=url,
                license_text=_license_text(entry.get("license")) or root_license,
                raw_version=entry_version or raw_version,
                source_record=record_id,
                fragment={"record": record_id, "full_text": dict(entry), "license": metadata.get("license")},
            )
        )
    return candidates


def _from_crossref(metadata: Mapping[str, Any]) -> list[LicenseCandidate]:
    record_id = _text(metadata.get("DOI") or metadata.get("doi"))
    links = _as_mapping_list(metadata.get("link"))
    licenses = _as_mapping_list(metadata.get("license"))
    candidates: list[LicenseCandidate] = []
    for link in links:
        url = _text(link.get("URL") or link.get("url"))
        if not url:
            continue
        link_raw_version = _text(
            link.get("content-version") or link.get("version")
        )
        matching = [
            item for item in licenses
            if (
                (not link_raw_version and not _text(item.get("content-version")))
                or _text(item.get("content-version")) == link_raw_version
            )
        ]
        if not matching:
            matching = [None]
        for license_entry in matching:
            license_text = (
                _license_text(
                    license_entry.get("URL")
                    or license_entry.get("url")
                    or license_entry.get("license")
                )
                if license_entry is not None
                else None
            )
            license_raw_version = (
                _text(license_entry.get("content-version"))
                if license_entry is not None
                else None
            )
            raw_version = link_raw_version or license_raw_version
            candidates.append(
                _candidate(
                    source="crossref",
                    url=url,
                    license_text=license_text,
                    raw_version=raw_version,
                    source_record=record_id,
                    fragment={
                        "record": record_id,
                        "link": dict(link),
                        "license": dict(license_entry) if license_entry else None,
                    },
                )
            )
    return candidates


def _candidate(
    *,
    source: str,
    url: str,
    license_text: str | None,
    raw_version: str | None,
    source_record: str | None,
    fragment: Mapping[str, Any],
) -> LicenseCandidate:
    return LicenseCandidate(
        source=source,
        url=url,
        license_text=license_text,
        version=_normalize_version_candidate(raw_version),
        raw_version=raw_version,
        source_record=source_record,
        raw_fragment_json=json.dumps(
            fragment, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ),
    )


def _normalize_version_candidate(value: str | None) -> str | None:
    if value is None:
        return None
    compact = "".join(character for character in value.casefold() if character.isalnum())
    return {
        "vor": "publishedVersion",
        "versionofrecord": "publishedVersion",
        "published": "publishedVersion",
        "publishedversion": "publishedVersion",
        "am": "acceptedVersion",
        "accepted": "acceptedVersion",
        "acceptedmanuscript": "acceptedVersion",
        "acceptedversion": "acceptedVersion",
        "sm": "submittedVersion",
        "submitted": "submittedVersion",
        "submittedversion": "submittedVersion",
        "preprint": "submittedVersion",
    }.get(compact)


def _mapping_list(value: Any, key: str) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return _as_mapping_list(value.get(key))
    return []


def _as_mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _license_text(value: Any) -> str | None:
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, Mapping):
        return _text(value.get("URL") or value.get("url") or value.get("text"))
    if isinstance(value, list):
        return next((text for item in value if (text := _license_text(item))), None)
    return None
