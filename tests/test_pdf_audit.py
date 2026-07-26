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
from ai_research_agent.pdf_audit import (
    ParsedReference,
    extract_pdf_references,
    score_candidate,
    write_pdf_audit,
)


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
        candidate = Publication(title="Variant title", doi="https://doi.org/10.1000/XYZ")
        score = score_candidate(reference, candidate)
        self.assertEqual(score.decision, "matched_high_confidence")
        self.assertTrue(score.doi_exact)

    def test_title_only_candidate_still_requires_review(self):
        reference = ParsedReference(
            1, "raw", "A distinctive title", "Alpha", "Journal", 2020, None, 2
        )
        score = score_candidate(reference, Publication(title="A distinctive title"))
        self.assertEqual(score.decision, "candidate_requires_review")

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


if __name__ == "__main__":
    unittest.main()
