import unittest

from ai_research_agent.models import (
    Publication,
    RetractionStatus,
    VerificationStatus,
    normalize_doi,
    normalize_title,
)


class ModelTests(unittest.TestCase):
    def test_normalize_doi(self):
        self.assertEqual(
            normalize_doi("https://doi.org/10.1016/J.PHYMED.2025.157429."),
            "10.1016/j.phymed.2025.157429",
        )
        self.assertIsNone(normalize_doi("not-a-doi"))

    def test_citation_ready_requires_verification_and_no_retraction(self):
        record = Publication(
            title="Example",
            verification_status=VerificationStatus.CROSS_SOURCE_VERIFIED,
            retraction_status=RetractionStatus.NOT_FLAGGED,
            publication_status_check_complete=True,
        )
        self.assertTrue(record.citation_ready)
        record.conflicts.append("mismatch")
        self.assertFalse(record.citation_ready)

    def test_title_normalization(self):
        self.assertEqual(
            normalize_title("PINK1-dependent mitophagy."),
            "pink1 dependent mitophagy",
        )

    def test_evidence_id_is_stable(self):
        from ai_research_agent.models import SourceEvidence

        left = SourceEvidence("pubmed", "123", "https://example/123", "time-a")
        right = SourceEvidence("pubmed", "123", "https://example/123", "time-b")
        self.assertEqual(left.evidence_id, right.evidence_id)


if __name__ == "__main__":
    unittest.main()
