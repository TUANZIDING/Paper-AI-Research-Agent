from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Iterable

from pypdf import PdfReader

from .models import Publication, normalize_doi, normalize_title
from .pipeline import ResearchPipeline


class PdfReferenceAuditError(RuntimeError):
    """Raised when an input cannot be audited without guessing."""


@dataclass(frozen=True)
class ParsedReference:
    number: int
    raw: str
    title: str | None
    first_author: str | None
    journal: str | None
    year: int | None
    doi: str | None
    reference_page: int


@dataclass(frozen=True)
class CitationOccurrence:
    reference_number: int
    page: int
    context: str
    context_sha256: str


@dataclass(frozen=True)
class CandidateScore:
    score: float
    title_score: float
    author_score: float | None
    year_score: float | None
    journal_score: float | None
    doi_exact: bool
    decision: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PdfExtraction:
    source_path: str
    source_sha256: str
    byte_length: int
    page_count: int
    reference_heading_page: int
    references: tuple[ParsedReference, ...]
    citations: tuple[CitationOccurrence, ...]
    dangling_citation_numbers: tuple[int, ...]
    uncited_reference_numbers: tuple[int, ...]


_REFERENCE_MARKER = re.compile(r"(?m)^\s*\[(\d{1,4})\]\s+")
_REFERENCE_HEADING = re.compile(r"(?im)^\s*(?:references|bibliography|参考文献)\s*$")
_CITATION_GROUP = re.compile(r"\[\s*(\d{1,4}(?:\s*[-–—]\s*\d{1,4})?(?:\s*[,;]\s*\d{1,4}(?:\s*[-–—]\s*\d{1,4})?)*)\s*\]")
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s<>\]\[\"']+)", re.I)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _clean_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("ﬁ", "fi").replace("ﬂ", "fl")
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s*\n\s*", " ", value)
    return " ".join(value.split())


def _safe_context(text: str, start: int, end: int, radius: int = 120) -> str:
    excerpt = _clean_text(text[max(0, start - radius) : min(len(text), end + radius)])
    return excerpt[:320]


def _expand_group(value: str) -> set[int]:
    numbers: set[int] = set()
    for item in re.split(r"\s*[,;]\s*", value):
        match = re.fullmatch(r"(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?", item)
        if not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start or end - start > 250:
            continue
        numbers.update(range(start, end + 1))
    return numbers


def _strip_page_noise(text: str) -> str:
    text = re.sub(r"(?im)^\s*please cite this article as:.*$", "", text)
    text = re.sub(r"(?im)^\s*https?://doi\.org/\S+\s*$", "", text)
    text = re.sub(r"(?im)^\s*article in press\s*$", "", text)
    text = re.sub(r"(?im)^\s*jid:.*$", "", text)
    return text


def _parse_reference(number: int, raw: str, page: int) -> ParsedReference:
    cleaned = _clean_text(raw).strip(" .")
    doi_match = _DOI.search(cleaned)
    doi = normalize_doi(doi_match.group(1)) if doi_match else None
    year_matches = list(_YEAR.finditer(cleaned))
    year = int(year_matches[-1].group(1)) if year_matches else None

    first_period = cleaned.find(".")
    authors_part = cleaned[:first_period] if first_period >= 0 else cleaned
    first_author_match = re.match(r"([A-Za-zÀ-ÖØ-öø-ÿ'’-]+)", authors_part)
    first_author = first_author_match.group(1) if first_author_match else None

    title = None
    journal = None
    if first_period >= 0:
        remainder = cleaned[first_period + 1 :].strip()
        boundary = None
        if year_matches:
            year_start = year_matches[-1].start()
            before_year = cleaned[:year_start]
            boundary = before_year.rfind(".")
        if boundary is not None and boundary > first_period:
            title = cleaned[first_period + 1 : boundary].strip(" .") or None
            journal = cleaned[boundary + 1 : (year_matches[-1].start() if year_matches else len(cleaned))].strip(" .;,") or None
        else:
            second_period = remainder.find(".")
            if second_period >= 0:
                title = remainder[:second_period].strip(" .") or None
                journal = remainder[second_period + 1 :].strip(" .;,") or None

    return ParsedReference(
        number=number,
        raw=cleaned,
        title=title,
        first_author=first_author,
        journal=journal,
        year=year,
        doi=doi,
        reference_page=page,
    )


def extract_pdf_references(
    pdf_path: str | Path,
    *,
    max_bytes: int = 100_000_000,
    max_pages: int = 500,
) -> PdfExtraction:
    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file():
        raise PdfReferenceAuditError(f"PDF does not exist: {path}")
    size = path.stat().st_size
    if size < 1 or size > max_bytes:
        raise PdfReferenceAuditError(f"PDF byte length {size} is outside the allowed range")
    payload = path.read_bytes()
    if not payload.startswith(b"%PDF-"):
        raise PdfReferenceAuditError("Input is not a PDF file")
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise PdfReferenceAuditError("Encrypted PDFs are not supported")
        if len(reader.pages) < 1 or len(reader.pages) > max_pages:
            raise PdfReferenceAuditError("PDF page count is outside the allowed range")
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReferenceAuditError:
        raise
    except Exception as exc:  # pypdf exposes several parser-specific exceptions
        raise PdfReferenceAuditError(f"PDF text extraction failed: {exc}") from exc
    if not any(text.strip() for text in pages):
        raise PdfReferenceAuditError("PDF has no extractable text; OCR is required")

    heading_page = -1
    heading_end = -1
    for index, text in enumerate(pages):
        match = _REFERENCE_HEADING.search(text)
        if match:
            heading_page = index
            heading_end = match.end()
            break
    if heading_page < 0:
        raise PdfReferenceAuditError(
            "Numbered reference section not found; this V1 supports explicit References/Bibliography headings"
        )

    reference_chunks: list[tuple[int, str]] = []
    for page_index in range(heading_page, len(pages)):
        page_text = pages[page_index]
        if page_index == heading_page:
            page_text = page_text[heading_end:]
        else:
            first_marker = _REFERENCE_MARKER.search(page_text)
            if first_marker:
                page_text = page_text[first_marker.start() :]
        reference_chunks.append((page_index + 1, _strip_page_noise(page_text)))

    marker_records: list[tuple[int, int, int, str]] = []
    for page_number, text in reference_chunks:
        matches = list(_REFERENCE_MARKER.finditer(text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            marker_records.append((int(match.group(1)), page_number, index, text[match.end() : end]))
    if not marker_records:
        raise PdfReferenceAuditError("No numbered [n] references found")

    merged: list[ParsedReference] = []
    for number, page_number, marker_index, raw in marker_records:
        # A page-ending reference can continue before the first marker on the next page.
        if marker_index == len(list(_REFERENCE_MARKER.finditer(reference_chunks[page_number - heading_page - 1][1]))) - 1:
            next_page_offset = page_number - heading_page
            if next_page_offset < len(reference_chunks):
                next_text = reference_chunks[next_page_offset][1]
                next_marker = _REFERENCE_MARKER.search(next_text)
                if next_marker and next_marker.start() > 0:
                    raw += " " + next_text[: next_marker.start()]
        merged.append(_parse_reference(number, raw, page_number))

    numbers = [item.number for item in merged]
    if len(numbers) != len(set(numbers)):
        raise PdfReferenceAuditError("Duplicate reference numbers found")
    expected = list(range(min(numbers), max(numbers) + 1))
    if numbers != expected or numbers[0] != 1:
        raise PdfReferenceAuditError(
            "Reference numbering is not a continuous [1]..[n] sequence; audit blocked"
        )

    citations: list[CitationOccurrence] = []
    for page_index, text in enumerate(pages[: heading_page + 1]):
        body = text[:heading_end] if page_index == heading_page else text
        for match in _CITATION_GROUP.finditer(body):
            context = _safe_context(body, match.start(), match.end())
            for number in sorted(_expand_group(match.group(1))):
                citations.append(
                    CitationOccurrence(
                        reference_number=number,
                        page=page_index + 1,
                        context=context,
                        context_sha256=_sha256_bytes(context.encode("utf-8")),
                    )
                )

    reference_numbers = set(numbers)
    citation_numbers = {item.reference_number for item in citations}
    return PdfExtraction(
        source_path=str(path),
        source_sha256=_sha256_bytes(payload),
        byte_length=size,
        page_count=len(pages),
        reference_heading_page=heading_page + 1,
        references=tuple(merged),
        citations=tuple(citations),
        dangling_citation_numbers=tuple(sorted(citation_numbers - reference_numbers)),
        uncited_reference_numbers=tuple(sorted(reference_numbers - citation_numbers)),
    )


def _name_token(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").casefold()
    return "".join(re.findall(r"[a-z0-9]+", normalized))


def _similarity(left: str | None, right: str | None) -> float | None:
    if not left or not right:
        return None
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def score_candidate(reference: ParsedReference, candidate: Publication) -> CandidateScore:
    title_score = _similarity(reference.title, candidate.title) or 0.0
    candidate_author = candidate.authors[0] if candidate.authors else None
    author_score = None
    if reference.first_author and candidate_author:
        left = _name_token(reference.first_author)
        right = _name_token(candidate_author)
        author_score = 1.0 if left and (left in right or right in left) else 0.0
    year_score = None
    if reference.year is not None and candidate.year is not None:
        year_score = 1.0 if reference.year == candidate.year else 0.0
    journal_score = _similarity(reference.journal, candidate.journal)
    doi_exact = bool(
        reference.doi
        and candidate.doi
        and normalize_doi(reference.doi) == normalize_doi(candidate.doi)
    )

    components: list[tuple[float, float]] = [(0.65, title_score)]
    if author_score is not None:
        components.append((0.15, author_score))
    if year_score is not None:
        components.append((0.12, year_score))
    if journal_score is not None:
        components.append((0.08, journal_score))
    weighted = sum(weight * value for weight, value in components) / sum(
        weight for weight, _ in components
    )
    score = 1.0 if doi_exact else weighted
    reasons = [f"title={title_score:.3f}"]
    if author_score is not None:
        reasons.append(f"first_author={author_score:.3f}")
    if year_score is not None:
        reasons.append(f"year={year_score:.3f}")
    if journal_score is not None:
        reasons.append(f"journal={journal_score:.3f}")
    if doi_exact:
        reasons.append("doi=exact")
    corroborating = sum(
        (
            author_score == 1.0,
            year_score == 1.0,
            journal_score is not None and journal_score >= 0.80,
        )
    )
    has_conflict = (
        author_score == 0.0
        or year_score == 0.0
        or (journal_score is not None and journal_score < 0.50)
    )
    if doi_exact or (
        score >= 0.86
        and title_score >= 0.88
        and corroborating >= 2
        and not has_conflict
    ):
        decision = "matched_high_confidence"
    elif score >= 0.72 and title_score >= 0.72:
        decision = "candidate_requires_review"
    else:
        decision = "not_matched"
    return CandidateScore(
        score=round(score, 6),
        title_score=round(title_score, 6),
        author_score=author_score,
        year_score=year_score,
        journal_score=round(journal_score, 6) if journal_score is not None else None,
        doi_exact=doi_exact,
        decision=decision,
        reasons=tuple(reasons),
    )


def lookup_reference(
    reference: ParsedReference,
    pipeline: ResearchPipeline,
    sources: list[str],
    *,
    limit: int = 5,
    transport_mode: str = "network",
    cache_enabled: bool = False,
) -> tuple[list[dict], list[dict], list[dict[str, str]]]:
    query_title = _clean_text(reference.title or reference.raw)
    query_title = " ".join(re.sub(r'[\[\]"]', " ", query_title).split())[:500]
    publications: dict[str, Publication] = {}
    issues: list[dict] = []
    queries: list[dict[str, str]] = []
    for source in sources:
        query = (
            f"{query_title}[Title]"
            if source == "pubmed"
            else f'TITLE:"{query_title}"'
        )
        queries.append({"source": source, "query": query})
        run = pipeline.run(
            query,
            limit,
            [source],
            page_size=min(limit, 100),
            transport_mode=transport_mode,
            cache_enabled=cache_enabled,
        )
        issues.extend(issue.to_dict() for issue in run.issues)
        for publication in run.publications:
            publications.setdefault(publication.dedupe_key, publication)
    ranked = sorted(
        ((score_candidate(reference, item), item) for item in publications.values()),
        key=lambda pair: pair[0].score,
        reverse=True,
    )
    candidates = [
        {"score": asdict(score), "publication": item.to_dict()}
        for score, item in ranked[:limit]
    ]
    return candidates, issues, queries


def write_pdf_audit(
    extraction: PdfExtraction,
    output_root: str | Path,
    *,
    lookups: dict[int, dict] | None = None,
    started_at: str | None = None,
) -> Path:
    started_at = started_at or datetime.now(timezone.utc).isoformat()
    timestamp = started_at.replace(":", "").replace("+", "-").split(".")[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", Path(extraction.source_path).stem).strip("-")[:40] or "pdf"
    run_dir = Path(output_root).expanduser().resolve() / f"{timestamp}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=False)

    citation_by_reference: dict[int, list[dict]] = {}
    for occurrence in extraction.citations:
        citation_by_reference.setdefault(occurrence.reference_number, []).append(asdict(occurrence))
    records_path = run_dir / "reference_results.jsonl"
    with records_path.open("w", encoding="utf-8") as handle:
        for reference in extraction.references:
            lookup = (lookups or {}).get(reference.number, {})
            if not lookup:
                lookup = {
                    "query_attempted": False,
                    "queries": [],
                    "candidates": [],
                    "issues": [],
                }
            payload = {
                "reference": asdict(reference),
                "citation_occurrences": citation_by_reference.get(reference.number, []),
                "lookup": lookup,
                "semantic_support_status": "NOT_ASSESSED",
                "human_review_status": "NOT_ATTESTED",
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    summary = {
        "reference_count": len(extraction.references),
        "cited_reference_count": len({item.reference_number for item in extraction.citations} & {item.number for item in extraction.references}),
        "citation_occurrence_count": len(extraction.citations),
        "dangling_citation_numbers": list(extraction.dangling_citation_numbers),
        "uncited_reference_numbers": list(extraction.uncited_reference_numbers),
        "lookup_attempted_count": len(lookups or {}),
        "high_confidence_match_count": sum(
            1 for value in (lookups or {}).values()
            if value.get("candidates") and value["candidates"][0]["score"]["decision"] == "matched_high_confidence"
        ),
    }
    citation_map = {
        "source_pdf_sha256": extraction.source_sha256,
        "reference_heading_page": extraction.reference_heading_page,
        "summary": summary,
        "occurrences": [asdict(item) for item in extraction.citations],
    }
    (run_dir / "citation_map.json").write_text(
        json.dumps(citation_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    report = [
        "# PDF 参考文献逆向审计报告",
        "",
        f"- PDF SHA-256：`{extraction.source_sha256}`",
        f"- 页数：{extraction.page_count}",
        f"- 参考文献：{summary['reference_count']}",
        f"- 正文已引用的参考文献：{summary['cited_reference_count']}",
        f"- 引用位置记录：{summary['citation_occurrence_count']}",
        f"- 高置信身份匹配：{summary['high_confidence_match_count']}（已尝试 {summary['lookup_attempted_count']}）",
        "",
        "## 结论边界",
        "",
        "- 该结果是机器生成的单篇论文审计记录，不是黄金集，也未经过独立人工裁决。",
        "- 文献存在或元数据匹配，不等于该文献支持正文中的具体论断。",
        "- `semantic_support_status=NOT_ASSESSED` 表示尚未进行全文语义核读。",
        "- PDF 和远端元数据均按不可信数据处理；系统不执行其中任何指令。",
        "- V1 仅支持带明确 References/Bibliography 标题的连续 `[1]..[n]` 编号格式。",
        "- 开放数据库无法保证等同 Embase、Scopus 或 Web of Science 的覆盖率。",
        "",
        f"- 正文悬空编号：{list(extraction.dangling_citation_numbers) or '无'}",
        f"- 未在正文出现的参考文献：{list(extraction.uncited_reference_numbers) or '无'}",
    ]
    (run_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    hashes = {
        str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    manifest = {
        "schema_version": "pdf-reference-audit-0.1",
        "run_type": "pdf_reference_audit",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source_pdf": {
            "path": extraction.source_path,
            "sha256": extraction.source_sha256,
            "byte_length": extraction.byte_length,
            "page_count": extraction.page_count,
        },
        "summary": summary,
        "output_sha256": hashes,
        "human_adjudicated": False,
        "semantic_support_assessed": False,
        "coverage_equivalent_to_subscription_indexes": False,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return run_dir
