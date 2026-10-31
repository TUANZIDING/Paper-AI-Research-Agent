"""Reference-manager, screening and PRISMA exports (v0.4).

* RIS and BibTeX import into Zotero, EndNote and most managers.
* The ASReview CSV uses the column names ASReview LAB recognises
  (``title``, ``abstract``, ``authors``, ``doi``, ``url``, ``keywords``) and
  leaves ``included`` empty: labels are for a human screener, never prefilled.
* PRISMA 2020 counts are reported as numbers for the flow diagram; automated
  keyword pre-screen exclusions are reported separately from human screening.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Iterable

from .models import Publication, normalize_doi


def _authors(publication: Publication) -> list[str]:
    return [author for author in publication.authors if author and author.strip()]


def to_ris(rows: Iterable[tuple[Publication, str, str]]) -> str:
    """``rows`` are (publication, abstract, origin note)."""

    lines: list[str] = []
    for publication, abstract, note in rows:
        lines.append("TY  - JOUR")
        lines.append(f"TI  - {publication.title}")
        for author in _authors(publication):
            lines.append(f"AU  - {author}")
        if publication.year:
            lines.append(f"PY  - {publication.year}")
        if publication.journal:
            lines.append(f"JO  - {publication.journal}")
        doi = normalize_doi(publication.doi)
        if doi:
            lines.append(f"DO  - {doi}")
            lines.append(f"UR  - https://doi.org/{doi}")
        if publication.pmid:
            lines.append(f"AN  - PMID:{publication.pmid}")
        if publication.pmcid:
            lines.append(f"C2  - {publication.pmcid}")
        if abstract:
            lines.append(f"AB  - {abstract}")
        if note:
            lines.append(f"N1  - {note}")
        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)


def _bib_escape(value: str) -> str:
    value = value.replace("\\", "\\textbackslash{}")
    return re.sub(r"([{}&%$#_])", r"\\\1", value)


def _bib_key(publication: Publication, used: set[str]) -> str:
    first = (publication.authors[0] if publication.authors else "anon").split()[0]
    base = re.sub(r"[^A-Za-z0-9]", "", first).lower() or "anon"
    word = next(iter(re.findall(r"[A-Za-z]{4,}", publication.title)), "work").lower()
    key = f"{base}{publication.year or 'nd'}{word}"
    candidate, index = key, 1
    while candidate in used:
        index += 1
        candidate = f"{key}{index}"
    used.add(candidate)
    return candidate


def to_bibtex(rows: Iterable[tuple[Publication, str, str]]) -> str:
    used: set[str] = set()
    entries: list[str] = []
    for publication, abstract, note in rows:
        fields = [("title", "{" + _bib_escape(publication.title) + "}")]
        if publication.authors:
            fields.append(("author", " and ".join(_bib_escape(a) for a in _authors(publication))))
        if publication.journal:
            fields.append(("journal", _bib_escape(publication.journal)))
        if publication.year:
            fields.append(("year", str(publication.year)))
        doi = normalize_doi(publication.doi)
        if doi:
            fields.append(("doi", doi))
        if publication.pmid:
            fields.append(("pmid", publication.pmid))
        if abstract:
            fields.append(("abstract", _bib_escape(abstract)))
        if note:
            fields.append(("note", _bib_escape(note)))
        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
        entries.append(f"@article{{{_bib_key(publication, used)},\n{body}\n}}\n")
    return "\n".join(entries)


ASREVIEW_COLUMNS = [
    "record_id", "title", "abstract", "authors", "year", "journal", "doi", "pmid",
    "url", "keywords", "origin", "auto_prescreen", "included",
]


def to_asreview_csv(rows: Iterable[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=ASREVIEW_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for index, row in enumerate(rows, 1):
        writer.writerow({**row, "record_id": index, "included": ""})
    return buffer.getvalue()


def prisma_counts(
    *,
    database_records: dict[str, int],
    citation_records: int,
    other_records: int,
    after_dedup: int,
    auto_excluded: int,
    to_human_screening: int,
    oa_available: int,
    needs_access: int,
) -> dict[str, Any]:
    identified = sum(database_records.values()) + citation_records + other_records
    return {
        "identification": {
            "records_from_databases": database_records,
            "records_from_citation_searching": citation_records,
            "records_from_other_methods": other_records,
            "total_identified": identified,
            "duplicates_removed": identified - after_dedup,
        },
        "screening": {
            "records_after_deduplication": after_dedup,
            "excluded_by_automated_keyword_prescreen": auto_excluded,
            "records_for_human_title_abstract_screening": to_human_screening,
            "human_screening_completed": False,
        },
        "retrieval": {
            "reports_with_lawful_open_access": oa_available,
            "reports_needing_institutional_access": needs_access,
        },
        "note": (
            "Counts for a PRISMA 2020 flow diagram. Human screening, eligibility "
            "assessment and inclusion are not performed by this tool."
        ),
    }
