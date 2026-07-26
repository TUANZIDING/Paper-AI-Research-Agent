from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_research_agent.audit import verify_run
from ai_research_agent.cli import main
from ai_research_agent.models import Publication
from ai_research_agent.pipeline import SearchRun
from ai_research_agent.pdf_audit import (
    ParsedReference,
    PdfReferenceAuditError,
    extract_pdf_references,
    lookup_reference,
    score_candidate,
    write_pdf_audit,
    _parse_reference,
)
from ai_research_agent.reference_batch import _append_event, load_checkpoint
from scripts.verify_release import load_json, validate_json_schema


class _FakePage:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self) -> str:
        return self.text


class _FakeReader:
    is_encrypted = False

    def __init__(self, _path, strict=False):
        self.pages = [
            _FakePage(
                "Title\nEvidence statement [1]. Related studies [2-3].\n"
            ),
            _FakePage(
                "References\n"
                "[1] Alpha A. First finding. Journal A 2020;1:1-2.\n"
                "[2] Beta B. Second finding. Journal B 2021;2:3-4.\n"
                "[3] Gamma C. Third finding. Journal C 2022;3:5-6.\n"
            ),
        ]


class _CrossPageReader:
    is_encrypted = False

    def __init__(self, _path, strict=False):
        self.pages = [
            _FakePage(
                "Body statement [1].\nReferences\n"
                "[1] Alpha A. A title that\n"
            ),
            _FakePage(
                "2 Journal / Article\nARTICLE IN PRESS\n"
                "continues across pages. Journal A 2020;1:1-2.\n"
                "[2] Beta B. Second title. Journal B 2021;2:3-4.\n"
            ),
        ]


class _TocReader:
    is_encrypted = False

    def __init__(self, _path, strict=False):
        self.pages = [
            _FakePage("Contents\nReferences\nMethods ........ 2\n"),
            _FakePage("Body statement [1].\n"),
            _FakePage("References\n[1] Alpha A. Actual title. Journal A 2020;1:1-2.\n"),
        ]


class PdfAuditTests(unittest.TestCase):
    def _fake_pdf(self, root: Path) -> Path:
        path = root / "sample.pdf"
        path.write_bytes(b"%PDF-1.7\ncontrolled-test-fixture")
        return path

    def test_extracts_numbered_references_and_maps_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._fake_pdf(Path(directory))
            with patch("ai_research_agent.pdf_audit.PdfReader", _FakeReader):
                result = extract_pdf_references(path)
        self.assertEqual(len(result.references), 3)
        self.assertEqual({item.reference_number for item in result.citations}, {1, 2, 3})
        self.assertEqual(result.uncited_reference_numbers, ())
        self.assertEqual(result.dangling_citation_numbers, ())
        self.assertEqual(result.references[0].title, "First finding")

    def test_missing_pdf_dependency_fails_only_when_extraction_is_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._fake_pdf(Path(directory))
            with patch("ai_research_agent.pdf_audit.PdfReader", None):
                with self.assertRaisesRegex(PdfReferenceAuditError, "requires pypdf"):
                    extract_pdf_references(path)

    def test_candidate_scoring_rejects_same_title_wrong_author_and_year(self):
        reference = ParsedReference(
            1, "raw", "Generic clinical update", "Alpha", "Journal A", 2020, None, 2
        )
        candidate = Publication(
            title="Generic clinical update",
            authors=["Beta B"],
            journal="Journal A",
            year=1998,
        )
        score = score_candidate(reference, candidate)
        self.assertNotEqual(score.decision, "matched_high_confidence")
        self.assertEqual(score.author_score, 0.0)
        self.assertEqual(score.year_score, 0.0)

    def test_exact_doi_is_high_confidence(self):
        reference = ParsedReference(
            1, "raw", "A title", "Alpha", "Journal", 2020, "10.1000/xyz", 2
        )
        candidate = Publication(title="A title", doi="https://doi.org/10.1000/XYZ")
        score = score_candidate(reference, candidate)
        self.assertEqual(score.decision, "matched_high_confidence")
        self.assertTrue(score.doi_exact)

    def test_exact_doi_with_conflicting_metadata_requires_review(self):
        reference = ParsedReference(
            1, "raw", "Expected title", "Li", "Journal", 2020, "10.1000/xyz", 2
        )
        candidate = Publication(
            title="Unrelated work", authors=["Williams AB"], year=1999,
            doi="10.1000/xyz",
        )
        score = score_candidate(reference, candidate)
        self.assertEqual(score.decision, "identifier_metadata_conflict")

    def test_short_surname_is_not_a_substring_match(self):
        reference = ParsedReference(
            1, "raw", "Same title", "Li", "Journal", 2020, None, 2
        )
        candidate = Publication(
            title="Same title", authors=["Williams AB"], journal="Journal", year=2020
        )
        score = score_candidate(reference, candidate)
        self.assertEqual(score.author_score, 0.0)
        self.assertNotEqual(score.decision, "matched_high_confidence")

    def test_reference_continuation_across_pages_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._fake_pdf(Path(directory))
            with patch("ai_research_agent.pdf_audit.PdfReader", _CrossPageReader):
                result = extract_pdf_references(path)
        self.assertEqual(len(result.references), 2)
        self.assertEqual(result.references[0].title, "A title that continues across pages")

    def test_table_of_contents_references_heading_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._fake_pdf(Path(directory))
            with patch("ai_research_agent.pdf_audit.PdfReader", _TocReader):
                result = extract_pdf_references(path)
        self.assertEqual(result.reference_heading_page, 3)
        self.assertEqual(result.references[0].title, "Actual title")
        self.assertEqual({item.reference_number for item in result.citations}, {1})

    def test_title_only_candidate_still_requires_review(self):
        reference = ParsedReference(
            1, "raw", "A distinctive title", "Alpha", "Journal", 2020, None, 2
        )
        score = score_candidate(reference, Publication(title="A distinctive title"))
        self.assertEqual(score.decision, "candidate_requires_review")

    def test_abbreviated_journal_does_not_override_matching_author_and_year(self):
        reference = ParsedReference(
            1, "raw", "A title", "Alpha", "J Am Coll Surg", 2020, None, 2
        )
        candidate = Publication(
            title="A title.", authors=["Alpha AB"],
            journal="Journal of the American College of Surgeons", year=2020,
        )
        score = score_candidate(reference, candidate)
        self.assertEqual(score.decision, "matched_high_confidence")

    def test_publication_year_is_not_confused_with_first_page(self):
        parsed = _parse_reference(
            12,
            "Fu CY. A title. Am J Emerg Med 2018;36(11):1937-42.",
            7,
        )
        self.assertEqual(parsed.year, 2018)
        self.assertEqual(parsed.journal, "Am J Emerg Med")

    def test_spaced_pdf_year_is_repaired(self):
        parsed = _parse_reference(
            5,
            "Tucker MC. Simple fixation. J Trauma 20 0 0;49(6):989-94.",
            7,
        )
        self.assertEqual(parsed.year, 2000)

    def test_high_confidence_pubmed_match_skips_fallback_queries(self):
        class FakePipeline:
            def __init__(self):
                self.calls = []

            def run(self, query, limit, sources, **kwargs):
                self.calls.append((query, tuple(sources)))
                return SearchRun(
                    query=query,
                    requested_limit_per_source=limit,
                    publications=[Publication(
                        title="A title", authors=["Alpha AB"],
                        journal="Journal of Trauma", year=2020,
                    )],
                )

        reference = ParsedReference(
            1, "raw", "A title", "Alpha", "J Trauma", 2020, None, 2
        )
        pipeline = FakePipeline()
        candidates, issues, queries, coverage = lookup_reference(
            reference, pipeline, ["pubmed", "europe_pmc"]
        )
        self.assertEqual(len(pipeline.calls), 1)
        self.assertEqual(queries[0]["strategy"], "title")
        self.assertEqual(candidates[0]["score"]["decision"], "matched_high_confidence")
        self.assertEqual(issues, [])
        self.assertTrue(coverage["identity_resolved"])
        self.assertFalse(coverage["search_complete"])
        self.assertFalse(coverage["planned_queries_executed"])
        self.assertFalse(coverage["candidate_exhaustion_proven"])

    def test_written_audit_is_hash_verifiable_and_keeps_semantic_gate_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._fake_pdf(root)
            with patch("ai_research_agent.pdf_audit.PdfReader", _FakeReader):
                extraction = extract_pdf_references(path)
            run_dir = write_pdf_audit(extraction, root / "runs")
            verification = verify_run(run_dir)
            self.assertTrue(verification.valid, verification.errors)
            row = json.loads((run_dir / "reference_results.jsonl").read_text().splitlines()[0])
            self.assertEqual(row["semantic_support_status"], "NOT_ASSESSED")
            self.assertFalse(row["lookup"]["query_attempted"])
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["schema_version"], "pdf-reference-audit-0.1")
            self.assertIn("artifact_completed_at", manifest)
            self.assertNotIn("completed_at", manifest)
            self.assertIsNone(manifest["lookup_batch"])
            schema = load_json(
                Path(__file__).resolve().parents[1]
                / "schemas/pdf-reference-audit-0.1.schema.json"
            )
            validate_json_schema(manifest, schema)
            self.assertFalse(manifest["human_adjudicated"])
            self.assertFalse(manifest["semantic_support_assessed"])

    def test_cli_extract_only_reports_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._fake_pdf(root)
            output = io.StringIO()
            with patch("ai_research_agent.pdf_audit.PdfReader", _FakeReader), redirect_stdout(output):
                status = main([
                    "audit-pdf-references", str(path),
                    "--output", str(root / "runs"), "--json",
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["references"], 3)
            self.assertFalse(payload["human_adjudicated"])
            self.assertFalse(payload["semantic_support_assessed"])

    def test_cli_lookup_checkpoint_resumes_only_unfinished_references(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._fake_pdf(root)
            checkpoint = root / "checkpoint.jsonl"
            calls = []
            output = io.StringIO()

            def fake_lookup(reference, *_args, **_kwargs):
                calls.append(reference.number)
                candidate = {
                    "score": {"decision": "matched_high_confidence", "score": 1.0},
                    "publication": {"title": reference.title},
                }
                return [candidate], [], [{
                    "source": "pubmed", "strategy": "title", "query": "test",
                    "pagination_state": "complete",
                }], {
                    "execution_status": "completed", "search_complete": True,
                    "identity_resolved": True, "pagination_states": ["complete"],
                    "candidate_exhaustion_proven": True,
                }

            with (
                patch("ai_research_agent.pdf_audit.PdfReader", _FakeReader),
                patch("ai_research_agent.cli.lookup_reference", fake_lookup),
                redirect_stdout(output),
            ):
                first = main([
                    "audit-pdf-references", str(path), "--lookup",
                    "--lookup-references", "1-3", "--max-items", "1",
                    "--checkpoint", str(checkpoint), "--output", str(root / "runs"),
                    "--json",
                ])
                second = main([
                    "audit-pdf-references", str(path), "--lookup",
                    "--lookup-references", "1-3", "--checkpoint", str(checkpoint),
                    "--output", str(root / "runs"), "--json",
                ])
            self.assertEqual((first, second), (3, 0))
            self.assertEqual(calls, [1, 2, 3])
            payloads = [json.loads(line) for line in output.getvalue().splitlines()]
            first_manifest = json.loads(
                (Path(payloads[0]["run_dir"]) / "manifest.json").read_text()
            )
            second_run = Path(payloads[1]["run_dir"])
            second_manifest = json.loads((second_run / "manifest.json").read_text())
            self.assertEqual(first_manifest["lookup_batch"]["batch_status"], "partial")
            self.assertEqual(first_manifest["lookup_batch"]["pending_count"], 2)
            self.assertEqual(second_manifest["lookup_batch"]["batch_status"], "completed")
            self.assertEqual(second_manifest["lookup_batch"]["completed_count"], 3)
            self.assertIn("artifact_completed_at", second_manifest)
            rows = [
                json.loads(line)
                for line in (second_run / "reference_results.jsonl").read_text().splitlines()
            ]
            self.assertTrue(
                all(row["lookup"]["match_status"] == "high-confidence" for row in rows)
            )

    def test_cli_uses_last_checkpoint_event_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._fake_pdf(root)
            checkpoint = root / "checkpoint.jsonl"

            def failed_lookup(*_args, **_kwargs):
                return [], [{"source": "pubmed", "stage": "discovery"}], [], {
                    "execution_status": "failed",
                    "search_complete": False,
                    "identity_resolved": False,
                    "pagination_states": ["missing"],
                    "planned_queries_executed": False,
                    "candidate_exhaustion_proven": False,
                }

            with (
                patch("ai_research_agent.pdf_audit.PdfReader", _FakeReader),
                patch("ai_research_agent.cli.lookup_reference", failed_lookup),
                redirect_stdout(io.StringIO()),
            ):
                first = main([
                    "audit-pdf-references", str(path), "--lookup",
                    "--lookup-references", "1", "--checkpoint", str(checkpoint),
                    "--output", str(root / "runs"), "--json",
                ])
            self.assertEqual(first, 3)
            events = load_checkpoint(checkpoint)
            prior = events[-1]
            self.assertEqual(prior["execution_status"], "failed")
            _append_event(checkpoint, {
                "schema_version": prior["schema_version"],
                "event_type": "attempt_started",
                "recorded_at": prior["recorded_at"],
                "batch_id": prior["batch_id"],
                "reference_id": prior["reference_id"],
                "reference_payload_sha256": prior["reference_payload_sha256"],
                "attempt_number": 2,
                "execution_status": "pending",
                "match_status": None,
                "transport_mode": prior["transport_mode"],
                "cache_enabled": prior["cache_enabled"],
                "semantic_support_status": "NOT_ASSESSED",
                "semantic_support_assessed": False,
            })

            output = io.StringIO()
            with (
                patch("ai_research_agent.pdf_audit.PdfReader", _FakeReader),
                patch("ai_research_agent.cli.lookup_reference", failed_lookup),
                redirect_stdout(output),
            ):
                second = main([
                    "audit-pdf-references", str(path), "--lookup",
                    "--lookup-references", "1", "--max-items", "0",
                    "--checkpoint", str(checkpoint), "--output", str(root / "runs"),
                    "--json",
                ])
            self.assertEqual(second, 3)
            run_dir = Path(json.loads(output.getvalue())["run_dir"])
            row = json.loads(
                (run_dir / "reference_results.jsonl").read_text().splitlines()[0]
            )
            self.assertEqual(row["lookup"]["execution_status"], "pending")
            self.assertIsNone(row["lookup"]["match_status"])
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["lookup_batch"]["batch_status"], "pending")
            self.assertEqual(manifest["lookup_batch"]["pending_count"], 1)


if __name__ == "__main__":
    unittest.main()
