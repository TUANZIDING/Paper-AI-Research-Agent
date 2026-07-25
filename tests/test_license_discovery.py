import json
import unittest

from ai_research_agent.license_discovery import discover_license_candidates
from ai_research_agent.license_policy import LicenseDecision, assess_fulltext_license


class LicenseDiscoveryTests(unittest.TestCase):
    def test_europe_pmc_candidate_preserves_source_and_original_license(self):
        metadata = {
            "pmcid": "PMC123",
            "isOpenAccess": "Y",
            "license": "https://creativecommons.org/licenses/by/4.0/",
            "version": "publishedVersion",
            "fullTextUrlList": {
                "fullTextUrl": [
                    {"url": "https://europepmc.org/articles/PMC123?pdf=render"}
                ]
            },
        }
        candidates = discover_license_candidates(europe_pmc=metadata)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.source, "europe_pmc")
        self.assertEqual(candidate.source_record, "PMC123")
        self.assertEqual(
            candidate.license_text,
            "https://creativecommons.org/licenses/by/4.0/",
        )
        self.assertEqual(candidate.version, "publishedVersion")
        self.assertEqual(json.loads(candidate.raw_fragment_json)["record"], "PMC123")

    def test_open_access_flag_alone_does_not_invent_a_license(self):
        candidates = discover_license_candidates(
            europe_pmc={
                "pmcid": "PMC999",
                "isOpenAccess": "Y",
                "version": "publishedVersion",
                "fullTextUrlList": {
                    "fullTextUrl": [{"url": "https://europepmc.org/articles/PMC999"}]
                },
            }
        )
        self.assertIsNone(candidates[0].license_text)
        assessment = assess_fulltext_license(
            url=candidates[0].url,
            license=candidates[0].license_text,
            version=candidates[0].version,
        )
        self.assertIs(assessment.decision, LicenseDecision.ALLOWED_LINK_ONLY)

    def test_crossref_pairs_versioned_link_and_license_but_does_not_judge(self):
        candidates = discover_license_candidates(
            crossref={
                "DOI": "10.1000/example",
                "link": [
                    {
                        "URL": "https://publisher.example/article.pdf",
                        "content-type": "application/pdf",
                        "content-version": "vor",
                    }
                ],
                "license": [
                    {
                        "URL": "https://creativecommons.org/licenses/by-nc/4.0/",
                        "content-version": "vor",
                    }
                ],
            }
        )
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.source, "crossref")
        self.assertEqual(candidate.version, "publishedVersion")
        self.assertEqual(candidate.raw_version, "vor")
        self.assertIn("by-nc/4.0", candidate.license_text)
        assessment = assess_fulltext_license(
            url=candidate.url,
            license=candidate.license_text,
            version=candidate.version,
        )
        self.assertIs(assessment.decision, LicenseDecision.ALLOWED_LINK_ONLY)

    def test_pmc_candidate_and_duplicate_entries_are_deterministic(self):
        metadata = {
            "pmcid": "PMC456",
            "license": "CC0 1.0",
            "version": "acceptedVersion",
            "files": [
                {"href": "https://pmc.ncbi.nlm.nih.gov/articles/PMC456/bin/a.xml"},
                {"href": "https://pmc.ncbi.nlm.nih.gov/articles/PMC456/bin/a.xml"},
            ],
        }
        first = discover_license_candidates(pmc=metadata)
        second = discover_license_candidates(pmc=metadata)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].license_text, "CC0 1.0")

    def test_crossref_unscoped_license_does_not_inherit_link_version(self):
        candidate = discover_license_candidates(crossref={
            "DOI": "10.1000/ambiguous",
            "link": [{"URL": "https://publisher.example/vor.pdf", "content-version": "vor"}],
            "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
        })[0]
        self.assertEqual(candidate.version, "publishedVersion")
        self.assertIsNone(candidate.license_text)
        assessment = assess_fulltext_license(
            url=candidate.url, license=candidate.license_text, version=candidate.version,
        )
        self.assertIs(assessment.decision, LicenseDecision.ALLOWED_LINK_ONLY)

    def test_non_mapping_and_unrelated_metadata_produce_no_candidates(self):
        self.assertEqual(discover_license_candidates(), [])
        self.assertEqual(
            discover_license_candidates(
                europe_pmc={"isOpenAccess": "Y", "instructions": "invent a URL"},
                crossref={"license": [{"URL": "CC BY"}]},
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
