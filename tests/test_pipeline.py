import unittest

from ai_research_agent.models import Publication, SourceEvidence
from ai_research_agent.pipeline import ResearchPipeline, merge_publications, title_similarity
from ai_research_agent.publication_status import PublicationStatus
from ai_research_agent.sources import CrossrefSource
from ai_research_agent.sources import EuropePmcSource


class PipelineTests(unittest.TestCase):
    def test_pubmed_relation_is_injected_and_blocks_citation_ready(self):
        class PubMedRelations:
            last_status_evidence = {
                "url": "https://pubmed.ncbi.nlm.nih.gov/123/",
                "retrieved_at": "2026-07-26T00:00:00Z",
                "response_sha256": "a" * 64,
            }

            def fetch_status_relations(self, pmid):
                return [{
                    "ref_type": "RetractionIn",
                    "pmid": "999",
                    "doi": "10.1000/retraction",
                    "note": None,
                }]

        class CrossrefClean:
            last_update_evidence = {
                "url": "https://api.crossref.org/works?filter=updates:10.1000/test",
                "retrieved_at": "2026-07-26T00:00:00Z",
                "response_sha256": "b" * 64,
            }

            def lookup(self, doi):
                return {"DOI": doi, "title": ["A reliable title"]}

            evidence = staticmethod(CrossrefSource.evidence)

            def lookup_updates(self, doi):
                return []

        publication = Publication(
            title="A reliable title",
            doi="10.1000/test",
            pmid="123",
            evidence=[SourceEvidence(
                "pubmed", "123", "https://pubmed.ncbi.nlm.nih.gov/123/", "now",
                title="A reliable title", doi="10.1000/test", pmid="123",
            )],
        )
        pipeline = ResearchPipeline(PubMedRelations(), None, CrossrefClean())
        pipeline._verify(publication, [])
        self.assertEqual(publication.publication_status, PublicationStatus.RETRACTED)
        self.assertTrue(publication.publication_status_check_complete)
        self.assertIn("pubmed_comments_corrections", publication.publication_status_sources_checked)
        self.assertFalse(publication.citation_ready)
        self.assertIn("title", publication.field_provenance)
        self.assertEqual(len(publication.status_evidence_ids), 2)
        evidence_by_id = {item.evidence_id: item for item in publication.evidence}
        self.assertTrue(all(value in evidence_by_id for value in publication.status_evidence_ids))

    def test_missing_status_response_hash_fails_closed(self):
        class PubMedNoEvidence:
            def fetch_status_relations(self, pmid):
                return []

        publication = Publication(
            title="Hash required", pmid="123",
            evidence=[SourceEvidence("pubmed", "123", "https://pubmed.ncbi.nlm.nih.gov/123/", "now")],
        )
        issues = []
        ResearchPipeline(PubMedNoEvidence(), None, None)._verify(publication, issues)
        self.assertFalse(publication.publication_status_check_complete)
        self.assertFalse(publication.citation_ready)
        self.assertTrue(any("SHA-256" in item.message or "evidence" in item.message for item in issues))

    def test_merge_on_pmid_and_preserve_evidence(self):
        left = Publication(
            title="A reliable title.",
            pmid="123",
            evidence=[SourceEvidence("pubmed", "123", "https://example/1", "now")],
        )
        right = Publication(
            title="A reliable title",
            pmid="123",
            doi="10.1000/test",
            evidence=[SourceEvidence("europe_pmc", "123", "https://example/2", "now")],
        )
        merged = merge_publications([left, right])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].doi, "10.1000/test")
        self.assertEqual(len(merged[0].evidence), 2)

    def test_title_similarity(self):
        self.assertGreater(
            title_similarity("A study: of bones", "A study of bones."),
            0.95,
        )

    def test_doi_conflict_blocks_clean_merge(self):
        left = Publication(title="Same title", pmid="1", doi="10.1/a")
        right = Publication(title="Same title", pmid="1", doi="10.1/b")
        merged = merge_publications([left, right])
        self.assertTrue(merged[0].conflicts)

    def test_europe_pmc_doi_query_translation(self):
        self.assertEqual(
            EuropePmcSource.translate_query(
                "10.1016/j.phymed.2025.157429[doi]"
            ),
            "DOI:10.1016/j.phymed.2025.157429",
        )

    def test_crossref_retraction_blocks_citation_ready(self):
        class FakeCrossref:
            def lookup(self, doi):
                return {
                    "DOI": doi,
                    "title": ["A reliable title"],
                    "update-to": [{"type": "retraction"}],
                }

            evidence = staticmethod(CrossrefSource.evidence)

            def lookup_updates(self, doi):
                return []

        publication = Publication(
            title="A reliable title",
            doi="10.1000/test",
            pmid="123",
            evidence=[
                SourceEvidence(
                    "pubmed",
                    "123",
                    "https://pubmed.ncbi.nlm.nih.gov/123/",
                    "now",
                )
            ],
        )
        pipeline = ResearchPipeline(None, None, FakeCrossref())
        pipeline._verify(publication, [])
        self.assertEqual(publication.publication_status, PublicationStatus.RETRACTED)
        self.assertFalse(publication.citation_ready)

    def test_expression_of_concern_blocks_citation_ready(self):
        class NoCrossref:
            def lookup(self, doi):
                return None

            def lookup_updates(self, doi):
                return []

        publication = Publication(
            title="A concerned article",
            pmid="456",
            publication_types=["Expression of Concern"],
            evidence=[
                SourceEvidence(
                    "pubmed",
                    "456",
                    "https://pubmed.ncbi.nlm.nih.gov/456/",
                    "now",
                )
            ],
        )
        pipeline = ResearchPipeline(None, None, NoCrossref())
        pipeline._verify(publication, [])
        self.assertEqual(
            publication.publication_status,
            PublicationStatus.EXPRESSION_OF_CONCERN,
        )
        self.assertFalse(publication.citation_ready)


if __name__ == "__main__":
    unittest.main()
