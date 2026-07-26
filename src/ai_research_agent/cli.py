from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import shutil
import sys

from .audit import verify_run
from .cache import HttpCache
from .claim_evidence import ClaimEvidenceError, bind_claim_evidence
from .crossmark import CrossrefPublicProvider, assess_crossmark
from .external_review import (
    ExternalReviewError,
    prepare_review_packet,
    verify_external_review,
)
from .fulltext import FulltextDownloadError, SafeFulltextDownloader
from .human_read import (
    HumanReadGateError,
    attest_human_read,
    rescind_human_read,
    verify_chain,
)
from .http import JsonHttpClient, RemoteServiceError
from .license_policy import assess_fulltext_license
from .license_discovery import discover_license_candidates
from .official_sources import (
    OfficialDomainRule,
    OfficialSourcePolicyError,
    OfficialSourceRegistry,
    create_manual_registration,
    load_registration_evidence_bundle,
    save_candidate_evidence,
    save_registration_evidence_bundle,
)
from .pipeline import ResearchPipeline
from .pdf_audit import (
    PdfReferenceAuditError,
    extract_pdf_references,
    lookup_reference,
    write_pdf_audit,
)
from .publication_status import assess_crossref_status
from .reporting import prepare_run_dir, write_run
from .reference_batch import (
    LookupOutcome,
    ReferenceBatchError,
    ReferenceBatchIntegrityError,
    ReferenceItem,
    load_checkpoint,
    result_to_dict,
    run_reference_batch,
)
from .sources import CrossrefSource, EuropePmcSource, PubMedSource


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-agent",
        description=(
            "Search selected open scholarly APIs and emit auditable, "
            "citation-verification artifacts."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search", help="Run discovery and verification")
    search.add_argument("query", help="Database query text")
    search.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum records requested from each discovery source (default: 10)",
    )
    search.add_argument(
        "--all",
        action="store_true",
        help="Explicitly fetch every page reported by each source (may be large)",
    )
    search.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Records requested per page (default: 100)",
    )
    search.add_argument(
        "--no-archive",
        action="store_true",
        help="Do not archive raw API response bytes (not recommended for audits)",
    )
    search.add_argument(
        "--sources",
        default="pubmed,europe_pmc",
        help="Comma-separated discovery sources: pubmed,europe_pmc",
    )
    search.add_argument(
        "--output",
        type=Path,
        default=Path("runs"),
        help="Root directory for immutable run artifacts (default: runs)",
    )
    search.add_argument(
        "--json",
        action="store_true",
        help="Print the compact run result as JSON",
    )
    search.add_argument(
        "--cache-db",
        type=Path,
        help="SQLite HTTP cache used for auditable online capture or offline replay",
    )
    search.add_argument(
        "--offline-replay",
        action="store_true",
        help="Use only previously cached responses; never fall back to the network",
    )

    license_parser = subparsers.add_parser(
        "assess-license",
        help="Conservatively assess one full-text location and license",
    )
    license_parser.add_argument("--url")
    license_parser.add_argument("--license")
    license_parser.add_argument("--version")

    discover_license = subparsers.add_parser(
        "discover-licenses",
        help="Extract unverified license/location candidates from saved public metadata",
    )
    discover_license.add_argument("--europe-pmc", type=Path)
    discover_license.add_argument("--pmc", type=Path)
    discover_license.add_argument("--crossref", type=Path)

    download = subparsers.add_parser(
        "download-fulltext",
        help="Safely download explicitly licensed public PDF/XML full text",
    )
    download.add_argument("--url", required=True)
    download.add_argument("--license", required=True)
    download.add_argument("--version", required=True)
    download.add_argument("--output", type=Path, required=True)
    download.add_argument("--max-bytes", type=int, default=25_000_000)

    status_parser = subparsers.add_parser(
        "check-status",
        help="Check Crossref post-publication update signals for one DOI",
    )
    status_parser.add_argument("doi")
    status_parser.add_argument("--archive-dir", type=Path)

    crossmark_parser = subparsers.add_parser(
        "check-crossmark",
        help="Run conservative Crossref public fallback for Crossmark-related metadata",
    )
    crossmark_parser.add_argument("doi")
    crossmark_parser.add_argument("--archive-dir", type=Path)

    bind = subparsers.add_parser(
        "bind-claim",
        help="Bind a claim to an exact location in a locally held full-text material",
    )
    bind.add_argument("--claim-id", required=True)
    bind.add_argument("--claim", required=True)
    bind.add_argument("--material", type=Path, required=True)
    bind.add_argument("--material-sha256", required=True)
    bind.add_argument(
        "--material-content-type", choices=("text/plain", "application/xml", "text/xml"),
        required=True,
    )
    bind.add_argument("--text", type=Path, required=True)
    bind.add_argument("--evidence-id", required=True)
    bind.add_argument("--anchor-kind", choices=("quote", "paragraph"), required=True)
    bind.add_argument("--anchor", required=True)
    bind.add_argument(
        "--support-status",
        choices=("SUPPORTED", "UNSUPPORTED", "AMBIGUOUS", "UNVERIFIABLE_ACCESS"),
        required=True,
        help="Human-supplied semantic label; location matching does not infer it",
    )
    bind.add_argument("--expected-excerpt-sha256")

    review_packet = subparsers.add_parser(
        "prepare-review-packet",
        help="Prepare an unsigned packet for an external human reviewer",
    )
    review_packet.add_argument("--material", type=Path, required=True)
    review_packet.add_argument("--role", required=True)
    review_packet.add_argument("--statement", required=True)
    review_packet.add_argument("--output", type=Path, required=True)

    verify_review = subparsers.add_parser(
        "verify-review-signature",
        help="Verify packet integrity, material hash, and detached key signature",
    )
    verify_review.add_argument("--packet", type=Path, required=True)
    verify_review.add_argument("--signature", type=Path, required=True)
    verify_review.add_argument("--public-key", type=Path, required=True)
    verify_review.add_argument("--material", type=Path, required=True)

    attest_read = subparsers.add_parser(
        "attest-read",
        help="Record an identity-unverified human-read self-attestation",
    )
    attest_read.add_argument("material", type=Path)
    attest_read.add_argument("--sha256", required=True)
    attest_read.add_argument("--role", required=True)
    attest_read.add_argument("--statement", required=True)
    attest_read.add_argument("--event-log", type=Path, required=True)

    rescind = subparsers.add_parser(
        "rescind-read",
        help="Append a rescission for a prior human-read event",
    )
    rescind.add_argument("event_hash")
    rescind.add_argument("--role", required=True)
    rescind.add_argument("--reason", required=True)
    rescind.add_argument("--event-log", type=Path, required=True)

    verify_log = subparsers.add_parser(
        "verify-read-log",
        help="Verify an append-only human-read event hash chain",
    )
    verify_log.add_argument("event_log", type=Path)

    verify_run_parser = subparsers.add_parser(
        "verify-run",
        help="Verify manifest and raw-response SHA-256 hashes",
    )
    verify_run_parser.add_argument("run_dir", type=Path)

    register_official = subparsers.add_parser(
        "register-official-source",
        help="Freeze a manual official-source allowlist candidate and its evidence",
    )
    register_official.add_argument("--organization-id", required=True)
    register_official.add_argument("--organization-name", required=True)
    register_official.add_argument("--hostname", required=True)
    register_official.add_argument("--path-prefix", action="append", required=True)
    register_official.add_argument("--include-subdomains", action="store_true")
    register_official.add_argument("--registrar-role", required=True)
    register_official.add_argument("--evidence-source-url", required=True)
    register_official.add_argument("--evidence-file", type=Path, required=True)
    register_official.add_argument("--observed-at", required=True)
    register_official.add_argument("--output", type=Path, required=True)

    propose_official = subparsers.add_parser(
        "propose-official-source",
        help="Create a non-network, non-legal official-source URL candidate",
    )
    propose_official.add_argument("--registration-bundle", type=Path, required=True)
    propose_official.add_argument("--organization-id", required=True)
    propose_official.add_argument("--url", required=True)
    propose_official.add_argument("--title", required=True)
    propose_official.add_argument("--output", type=Path, required=True)

    pdf_audit = subparsers.add_parser(
        "audit-pdf-references",
        help="Extract numbered references, map body citations, and optionally verify identities",
    )
    pdf_audit.add_argument("pdf", type=Path)
    pdf_audit.add_argument(
        "--output", type=Path, default=Path("pdf-audits"),
        help="Root directory for immutable audit artifacts (default: pdf-audits)",
    )
    pdf_audit.add_argument(
        "--lookup", action="store_true",
        help="Query selected open APIs for each parsed reference (network is opt-in)",
    )
    pdf_audit.add_argument(
        "--sources", default="pubmed,europe_pmc",
        help="Comma-separated lookup sources: pubmed,europe_pmc",
    )
    pdf_audit.add_argument(
        "--limit-per-reference", type=int, default=5,
        help="Maximum candidates requested per reference (default: 5)",
    )
    pdf_audit.add_argument(
        "--lookup-references",
        help="Optional comma/range selector such as 1,3,10-20 (default: every reference)",
    )
    pdf_audit.add_argument("--cache-db", type=Path)
    pdf_audit.add_argument(
        "--checkpoint", type=Path,
        help="Append-only batch checkpoint path (a deterministic local path is used by default)",
    )
    pdf_audit.add_argument(
        "--max-items", type=int,
        help="Process at most this many unfinished references, then stop resumably",
    )
    pdf_audit.add_argument(
        "--offline-replay", action="store_true",
        help="Use only cached API responses; requires --lookup and --cache-db",
    )
    pdf_audit.add_argument("--max-bytes", type=int, default=100_000_000)
    pdf_audit.add_argument("--max-pages", type=int, default=500)
    pdf_audit.add_argument("--json", action="store_true")
    return parser


def _user_agent() -> str:
    contact = os.getenv("RESEARCH_AGENT_EMAIL")
    return (
        f"AIResearchAgent/0.3.3 (mailto:{contact})"
        if contact
        else "AIResearchAgent/0.3.3"
    )


def run_search(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")
    if args.all and args.limit is not None:
        raise SystemExit("Use either an explicit --limit or --all, not both")
    if args.page_size < 1 or args.page_size > 1000:
        raise SystemExit("--page-size must be between 1 and 1000")
    sources = [value.strip() for value in args.sources.split(",") if value.strip()]
    allowed = {"pubmed", "europe_pmc"}
    unknown = set(sources) - allowed
    if unknown:
        raise SystemExit(f"Unknown source(s): {', '.join(sorted(unknown))}")
    if not sources:
        raise SystemExit("At least one discovery source is required")
    if args.offline_replay and args.cache_db is None:
        raise SystemExit("--offline-replay requires --cache-db")
    if args.offline_replay and not args.cache_db.resolve().is_file():
        raise SystemExit("Offline replay cache does not exist")

    limit = None if args.all else (args.limit if args.limit is not None else 10)
    started_at = datetime.now(timezone.utc).isoformat()
    run_dir = prepare_run_dir(args.output.resolve(), started_at, args.query)
    archive_dir = None if args.no_archive else run_dir / "raw_responses"
    cache = HttpCache(args.cache_db.resolve()) if args.cache_db else None
    client = JsonHttpClient(
        _user_agent(),
        archive_dir=archive_dir,
        cache=cache,
        offline=args.offline_replay,
    )
    pipeline = ResearchPipeline(
        PubMedSource(client),
        EuropePmcSource(client),
        CrossrefSource(client),
    )
    run = pipeline.run(
        args.query,
        limit,
        sources,
        page_size=args.page_size,
        transport_mode="offline_replay" if args.offline_replay else "network",
        cache_enabled=cache is not None,
    )
    run_dir = write_run(
        run,
        args.output.resolve(),
        started_at,
        run_dir=run_dir,
    )
    result = {
        "run_dir": str(run_dir),
        "records": len(run.publications),
        "citation_ready": sum(item.citation_ready for item in run.publications),
        "partial": bool(run.issues)
        or any(
            details.get("state") == "partial"
            for details in run.source_pagination.values()
        ),
        "search_complete": all(
            details.get("state") == "complete"
            for details in run.source_pagination.values()
        ),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Run: {run_dir}")
        print(f"Records: {result['records']}")
        print(f"Citation-ready candidates: {result['citation_ready']}")
        print(f"Partial run: {'yes' if result['partial'] else 'no'}")
        print(f"All pages fetched: {'yes' if result['search_complete'] else 'no'}")
    return 2 if not run.publications and run.issues else 0


def run_assess_license(args: argparse.Namespace) -> int:
    assessment = assess_fulltext_license(
        url=args.url,
        license=args.license,
        version=args.version,
    )
    print(
        json.dumps(
            {
                "decision": assessment.decision.value,
                "version": assessment.version.value,
                "normalized_license": assessment.normalized_license,
                "reason": assessment.reason,
                "license_allows_machine_read": assessment.license_allows_machine_read,
                "may_fetch_full_text": assessment.may_fetch_full_text,
                "fetch_boundary": (
                    "A separate DNS, IP, redirect, size, and content-type safety gate "
                    "must pass before any network fetch."
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _load_json_object(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read metadata JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Metadata JSON {path} must contain an object")
    return value


def run_discover_licenses(args: argparse.Namespace) -> int:
    if not any((args.europe_pmc, args.pmc, args.crossref)):
        print("License discovery blocked: provide at least one metadata JSON file.", file=sys.stderr)
        return 2
    try:
        candidates = discover_license_candidates(
            europe_pmc=_load_json_object(args.europe_pmc),
            pmc=_load_json_object(args.pmc),
            crossref=_load_json_object(args.crossref),
        )
    except ValueError as exc:
        print(f"License discovery blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "candidates": [item.__dict__ for item in candidates],
        "candidate_count": len(candidates),
        "final_legal_judgment": False,
        "download_authorized": False,
        "boundary": "Candidates require separate license/version policy and network safety gates.",
    }, ensure_ascii=False))
    return 0


def run_download_fulltext(args: argparse.Namespace) -> int:
    if args.max_bytes < 1:
        print("Full-text download blocked: --max-bytes must be positive.", file=sys.stderr)
        return 2
    try:
        result = SafeFulltextDownloader(max_bytes=args.max_bytes).download(
            url=args.url,
            license=args.license,
            version=args.version,
            destination=args.output.resolve(),
        )
    except FulltextDownloadError as exc:
        print(f"Full-text download blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "path": str(result.path), "final_url": result.final_url,
        "sha256": result.sha256, "byte_length": result.byte_length,
        "content_type": result.content_type,
        "normalized_license": result.normalized_license,
        "manuscript_version": result.manuscript_version.value,
        "retrieved_at": result.retrieved_at,
        "redirect_chain": list(result.redirect_chain),
        "final_legal_judgment": False,
    }, ensure_ascii=False))
    return 0


def run_check_status(args: argparse.Namespace) -> int:
    from .models import normalize_doi

    normalized = normalize_doi(args.doi)
    if not normalized:
        print("Status check blocked: invalid DOI.", file=sys.stderr)
        return 2
    client = JsonHttpClient(
        _user_agent(),
        archive_dir=args.archive_dir.resolve() if args.archive_dir else None,
    )
    source = CrossrefSource(client)
    try:
        work = source.lookup(normalized)
        if work is None:
            print("Status check failed: DOI did not resolve in Crossref.", file=sys.stderr)
            return 2
        updates = source.lookup_updates(normalized)
    except RemoteServiceError as exc:
        print(f"Status check failed: {exc}", file=sys.stderr)
        return 2
    assessment = assess_crossref_status([work, *updates])
    print(
        json.dumps(
            {
                "doi": normalized,
                "status": assessment.status.value,
                "signals": list(assessment.signals),
                "conclusively_not_retracted": False,
                "reverse_update_lookup_complete": True,
                "boundary": (
                    "unknown or absent signals do not prove that no retraction, "
                    "correction, or expression of concern exists"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_check_crossmark(args: argparse.Namespace) -> int:
    client = JsonHttpClient(
        _user_agent(),
        archive_dir=args.archive_dir.resolve() if args.archive_dir else None,
    )
    result = assess_crossmark(
        args.doi,
        crossref_fallback=CrossrefPublicProvider(client, os.getenv("RESEARCH_AGENT_EMAIL")),
    )
    print(json.dumps({
        "target_doi": result.target_doi, "status": result.status.value,
        "complete": result.complete, "route": result.route.value,
        "provider_name": result.provider_name, "fallback_used": result.fallback_used,
        "crossmark_realtime_verified": result.crossmark_realtime_verified,
        "crossmark_participation_declared": result.crossmark_participation_declared,
        "signals": list(result.signals),
        "evidence": [item.__dict__ for item in result.evidence],
        "errors": list(result.errors),
        "boundary": "Crossref fallback is not an independent live Crossmark dialog verification.",
    }, ensure_ascii=False))
    return 0 if result.complete else 2


def run_bind_claim(args: argparse.Namespace) -> int:
    try:
        material = args.material.read_bytes()
        text_value = args.text.read_text(encoding="utf-8")
        result = bind_claim_evidence(
            claim_id=args.claim_id, claim_text=args.claim, material=material,
            expected_material_sha256=args.material_sha256,
            material_content_type=args.material_content_type,
            evidence_id=args.evidence_id, anchor_kind=args.anchor_kind,
            anchor_value=args.anchor, text=text_value,
            support_status=args.support_status,
            expected_excerpt_sha256=args.expected_excerpt_sha256,
        )
    except (OSError, UnicodeError, ClaimEvidenceError) as exc:
        print(f"Claim binding blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0


def run_prepare_review_packet(args: argparse.Namespace) -> int:
    try:
        packet = prepare_review_packet(
            material_path=args.material, reviewer_role=args.role,
            confirmation_statement=args.statement,
        )
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ExternalReviewError("output already exists")
        output.write_text(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ExternalReviewError) as exc:
        print(f"Review packet blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"packet": str(output), "packet_sha256": packet["packet_sha256"],
                      "identity_verified": False, "human_read_confirmed": False}, ensure_ascii=False))
    return 0


def run_verify_review_signature(args: argparse.Namespace) -> int:
    try:
        packet = _load_json_object(args.packet)
        if packet is None:
            raise ExternalReviewError("packet is required")
        result = verify_external_review(
            packet=packet, signature_path=args.signature,
            public_key_path=args.public_key, material_path=args.material,
        )
    except (ValueError, ExternalReviewError) as exc:
        print(f"External review verification blocked: {exc}", file=sys.stderr)
        return 2
    payload = dict(result.__dict__)
    payload["verification_gate_passed"] = result.verification_gate_passed
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if result.verification_gate_passed else 2


def run_attest_read(args: argparse.Namespace) -> int:
    if not args.material.is_absolute():
        print("Human-read attestation blocked: material path must be absolute.", file=sys.stderr)
        return 2
    try:
        event = attest_human_read(
            event_log=args.event_log.resolve(),
            material_path=args.material,
            expected_sha256=args.sha256,
            reviewer_role=args.role,
            confirmation_statement=args.statement,
            actor_type="human",
            explicit_command=True,
        )
    except HumanReadGateError as exc:
        print(f"Human-read attestation blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(event, ensure_ascii=False))
    return 0


def run_rescind_read(args: argparse.Namespace) -> int:
    try:
        event = rescind_human_read(
            event_log=args.event_log.resolve(),
            target_event_hash=args.event_hash,
            reviewer_role=args.role,
            reason=args.reason,
            actor_type="human",
            explicit_command=True,
        )
    except HumanReadGateError as exc:
        print(f"Human-read rescission blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(event, ensure_ascii=False))
    return 0


def run_verify_read_log(args: argparse.Namespace) -> int:
    result = verify_chain(args.event_log.resolve())
    print(
        json.dumps(
            {
                "valid": result.valid,
                "event_count": result.event_count,
                "error": result.error,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.valid else 2


def run_verify_run(args: argparse.Namespace) -> int:
    result = verify_run(args.run_dir)
    print(
        json.dumps(
            {
                "valid": result.valid,
                "checked_files": result.checked_files,
                "errors": list(result.errors),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.valid else 2


def run_audit_pdf_references(args: argparse.Namespace) -> int:
    if args.max_bytes < 1 or args.max_pages < 1:
        print("PDF audit blocked: size and page limits must be positive.", file=sys.stderr)
        return 2
    if args.limit_per_reference < 1 or args.limit_per_reference > 20:
        print("PDF audit blocked: --limit-per-reference must be between 1 and 20.", file=sys.stderr)
        return 2
    if args.max_items is not None and args.max_items < 0:
        print("PDF audit blocked: --max-items must be non-negative.", file=sys.stderr)
        return 2
    sources = [value.strip() for value in args.sources.split(",") if value.strip()]
    unknown = set(sources) - {"pubmed", "europe_pmc"}
    if not sources or unknown:
        detail = f": {', '.join(sorted(unknown))}" if unknown else ""
        print(f"PDF audit blocked: invalid lookup sources{detail}.", file=sys.stderr)
        return 2
    if args.offline_replay and (not args.lookup or args.cache_db is None):
        print(
            "PDF audit blocked: --offline-replay requires --lookup and --cache-db.",
            file=sys.stderr,
        )
        return 2
    if args.offline_replay and not args.cache_db.resolve().is_file():
        print("PDF audit blocked: offline replay cache does not exist.", file=sys.stderr)
        return 2

    try:
        extraction = extract_pdf_references(
            args.pdf, max_bytes=args.max_bytes, max_pages=args.max_pages
        )
    except PdfReferenceAuditError as exc:
        print(f"PDF audit blocked: {exc}", file=sys.stderr)
        return 2
    try:
        extractor_version = version("pypdf")
    except PackageNotFoundError:
        extractor_version = "unavailable"

    selected_numbers = {item.number for item in extraction.references}
    if args.lookup_references:
        if not args.lookup:
            print("PDF audit blocked: --lookup-references requires --lookup.", file=sys.stderr)
            return 2
        try:
            selected_numbers = set()
            for part in args.lookup_references.split(","):
                match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
                if not match:
                    raise ValueError(part)
                start = int(match.group(1))
                end = int(match.group(2) or start)
                if end < start or end - start > 1000:
                    raise ValueError(part)
                selected_numbers.update(range(start, end + 1))
        except ValueError:
            print("PDF audit blocked: invalid --lookup-references selector.", file=sys.stderr)
            return 2
        unknown_numbers = selected_numbers - {item.number for item in extraction.references}
        if not selected_numbers or unknown_numbers:
            print(
                "PDF audit blocked: lookup selector contains no references or unknown numbers.",
                file=sys.stderr,
            )
            return 2

    started_at = datetime.now(timezone.utc).isoformat()
    output_root = args.output.resolve()
    lookups: dict[int, dict] = {}
    batch_payload: dict | None = None
    if args.lookup:
        output_root.mkdir(parents=True, exist_ok=True)
        transport_mode = "offline_replay" if args.offline_replay else "network"
        checkpoint_identity = json.dumps(
            {
                "pdf_sha256": extraction.source_sha256,
                "selected": sorted(selected_numbers),
                "sources": sources,
                "limit": args.limit_per_reference,
                "lookup_policy_version": "pdf-reference-lookup-0.2",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        checkpoint_key = hashlib.sha256(checkpoint_identity).hexdigest()[:24]
        checkpoint = (
            args.checkpoint.resolve()
            if args.checkpoint
            else output_root / ".checkpoints" / f"{checkpoint_key}.jsonl"
        )
        archive_dir = Path(str(checkpoint) + ".raw_responses")
        cache = HttpCache(args.cache_db.resolve()) if args.cache_db else None
        client = JsonHttpClient(
            _user_agent(), archive_dir=archive_dir, cache=cache, offline=args.offline_replay
        )
        pipeline = ResearchPipeline(
            PubMedSource(client), EuropePmcSource(client), CrossrefSource(client)
        )
        reference_by_id = {
            str(reference.number): reference for reference in extraction.references
        }
        items = [
            ReferenceItem(
                str(reference.number),
                {
                    "reference": asdict(reference),
                    "lookup_policy": {
                        "sources": sources,
                        "limit_per_reference": args.limit_per_reference,
                    },
                },
            )
            for reference in extraction.references
        ]

        def execute_reference(item, context):
            candidates, issues, queries, coverage = lookup_reference(
                reference_by_id[item.reference_id],
                pipeline,
                sources,
                limit=args.limit_per_reference,
                transport_mode=context.transport_mode,
                cache_enabled=context.cache_enabled,
            )
            best_decision = (
                candidates[0]["score"]["decision"] if candidates else "not_matched"
            )
            match_status = (
                "high-confidence"
                if best_decision == "matched_high_confidence"
                else (
                    "review"
                    if best_decision
                    in {"candidate_requires_review", "identifier_metadata_conflict"}
                    else "not-matched"
                )
            )
            execution_status = str(coverage["execution_status"])
            if execution_status == "failed":
                match_status = None
            return LookupOutcome(
                execution_status=execution_status,
                match_status=match_status,
                queries=tuple(queries),
                candidates=tuple(candidates),
                issues=tuple(issues),
                evidence=coverage,
            )

        try:
            batch = run_reference_batch(
                items,
                [str(number) for number in sorted(selected_numbers)],
                checkpoint,
                execute_reference,
                transport_mode=transport_mode,
                cache_enabled=cache is not None,
                max_items=args.max_items,
                batch_context={
                    "source_pdf_sha256": extraction.source_sha256,
                    "page_text_sha256": list(extraction.page_text_sha256),
                    "extractor": "pypdf",
                    "extractor_version": extractor_version,
                    "lookup_policy_version": "pdf-reference-lookup-0.2",
                },
            )
        except (ReferenceBatchError, ReferenceBatchIntegrityError) as exc:
            print(f"PDF audit blocked: {exc}", file=sys.stderr)
            return 2
        batch_payload = result_to_dict(batch)
        latest_events: dict[int, dict] = {}
        for event in load_checkpoint(checkpoint):
            if event.get("event_type") in {"attempt_started", "attempt_finished"}:
                latest_events[int(event["reference_id"])] = event
        for number, event in latest_events.items():
            if event.get("event_type") != "attempt_finished":
                continue
            evidence = dict(event.get("evidence") or {})
            lookups[number] = {
                "query_attempted": True,
                "queries": event.get("queries", []),
                "candidates": event.get("candidates", []),
                "issues": event.get("issues", []),
                "execution_status": event.get("execution_status", "failed"),
                "match_status": event.get("match_status"),
                "search_complete": evidence.get("search_complete", False),
                "identity_resolved": evidence.get("identity_resolved", False),
                "pagination_states": evidence.get("pagination_states", []),
                "candidate_exhaustion_proven": evidence.get(
                    "candidate_exhaustion_proven", False
                ),
                "error": event.get("error"),
            }
        checkpoint_sha256 = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        run_dir = write_pdf_audit(
            extraction,
            output_root,
            lookups=lookups,
            started_at=started_at,
            lookup_policy={
                "lookup_enabled": True,
                "transport_mode": transport_mode,
                "sources": sources,
                "selected_reference_numbers": sorted(selected_numbers),
                "limit_per_reference": args.limit_per_reference,
                "cache_enabled": cache is not None,
                "checkpoint_sha256": checkpoint_sha256,
            },
            lookup_batch={
                "batch_id": batch_payload["batch_id"],
                "batch_status": batch_payload["batch_status"],
                "selected_count": batch_payload["selected_count"],
                "attempted_this_run": batch_payload["attempted_this_run"],
                "completed_count": batch_payload["completed_count"],
                "partial_count": batch_payload["partial_count"],
                "failed_count": batch_payload["failed_count"],
                "pending_count": batch_payload["pending_count"],
                "semantic_support_assessed": False,
            },
        )
        shutil.copy2(checkpoint, run_dir / "batch_checkpoint.jsonl")
        if archive_dir.is_dir() and any(archive_dir.iterdir()):
            shutil.copytree(archive_dir, run_dir / "raw_responses")
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["output_sha256"] = {
            str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(run_dir.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        run_dir = write_pdf_audit(
            extraction,
            output_root,
            started_at=started_at,
            lookup_policy={
                "lookup_enabled": False,
                "transport_mode": "none",
                "sources": [],
                "selected_reference_numbers": [],
                "limit_per_reference": args.limit_per_reference,
                "cache_enabled": False,
            },
        )

    result = {
        "run_dir": str(run_dir),
        "references": len(extraction.references),
        "cited_references": len(extraction.references) - len(extraction.uncited_reference_numbers),
        "dangling_citations": list(extraction.dangling_citation_numbers),
        "lookup_attempted": args.lookup,
        "batch": batch_payload,
        "lookup_complete": batch_payload is None or batch_payload["batch_status"] == "completed",
        "human_adjudicated": False,
        "semantic_support_assessed": False,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Run: {run_dir}")
        print(f"References: {result['references']}")
        print(f"Cited references: {result['cited_references']}")
        print(f"Identity lookup attempted: {'yes' if args.lookup else 'no'}")
        if batch_payload is not None:
            print(f"Lookup batch status: {batch_payload['batch_status']}")
            print(
                "Lookup counts: "
                f"completed={batch_payload['completed_count']}, "
                f"partial={batch_payload['partial_count']}, "
                f"failed={batch_payload['failed_count']}, "
                f"pending={batch_payload['pending_count']}"
            )
        print("Semantic support assessed: no")
    return 0 if result["lookup_complete"] else 3


def run_register_official_source(args: argparse.Namespace) -> int:
    try:
        evidence = args.evidence_file.resolve().read_bytes()
        registration = create_manual_registration(
            organization_id=args.organization_id,
            organization_name=args.organization_name,
            rules=(OfficialDomainRule(
                args.hostname,
                tuple(args.path_prefix),
                args.include_subdomains,
            ),),
            registrar_role=args.registrar_role,
            evidence_source_url=args.evidence_source_url,
            evidence_bytes=evidence,
            evidence_observed_at=args.observed_at,
        )
        saved = save_registration_evidence_bundle(
            registration, evidence, args.output.resolve()
        )
    except (OSError, OfficialSourcePolicyError) as exc:
        print(f"Official-source registration blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "path": str(saved.path),
        "sha256": saved.sha256,
        "registration_sha256": registration.sha256,
        "registrar_identity_verified": False,
        "final_official_status_verified": False,
    }, ensure_ascii=False))
    return 0


def run_propose_official_source(args: argparse.Namespace) -> int:
    try:
        registration = load_registration_evidence_bundle(
            args.registration_bundle.resolve()
        )
        candidate = OfficialSourceRegistry((registration,)).propose(
            organization_id=args.organization_id,
            url=args.url,
            title=args.title,
        )
        saved = save_candidate_evidence(candidate, args.output.resolve())
    except (OSError, OfficialSourcePolicyError) as exc:
        print(f"Official-source candidate blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "path": str(saved.path),
        "sha256": saved.sha256,
        "evidence_id": candidate.evidence_id,
        "candidate_status": candidate.candidate_status,
        "network_fetch_permitted": False,
        "official_identity_verified": False,
        "final_legal_judgment": False,
    }, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "search":
        return run_search(args)
    if args.command == "assess-license":
        return run_assess_license(args)
    if args.command == "discover-licenses":
        return run_discover_licenses(args)
    if args.command == "download-fulltext":
        return run_download_fulltext(args)
    if args.command == "check-status":
        return run_check_status(args)
    if args.command == "check-crossmark":
        return run_check_crossmark(args)
    if args.command == "bind-claim":
        return run_bind_claim(args)
    if args.command == "prepare-review-packet":
        return run_prepare_review_packet(args)
    if args.command == "verify-review-signature":
        return run_verify_review_signature(args)
    if args.command == "attest-read":
        return run_attest_read(args)
    if args.command == "rescind-read":
        return run_rescind_read(args)
    if args.command == "verify-read-log":
        return run_verify_read_log(args)
    if args.command == "verify-run":
        return run_verify_run(args)
    if args.command == "register-official-source":
        return run_register_official_source(args)
    if args.command == "propose-official-source":
        return run_propose_official_source(args)
    if args.command == "audit-pdf-references":
        return run_audit_pdf_references(args)
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
