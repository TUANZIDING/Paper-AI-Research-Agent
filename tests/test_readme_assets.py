from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReadmeAssetTests(unittest.TestCase):
    def test_bilingual_readmes_link_to_each_other(self):
        chinese = (ROOT / "README.md").read_text(encoding="utf-8")
        english = (ROOT / "README.en.md").read_text(encoding="utf-8")
        self.assertIn('href="README.en.md"', chinese)
        self.assertIn('href="README.md"', english)
        self.assertIn("docs/assets/ai-research-agent-hero.webp", chinese)
        self.assertIn("docs/assets/ai-research-agent-hero.webp", english)

    def test_hero_is_a_small_repository_local_webp(self):
        hero = ROOT / "docs/assets/ai-research-agent-hero.webp"
        body = hero.read_bytes()
        self.assertLess(len(body), 500_000)
        self.assertEqual(body[:4], b"RIFF")
        self.assertEqual(body[8:12], b"WEBP")

    def test_landing_page_document_links_exist(self):
        required = (
            "SOURCE_POLICY.md", "ACCEPTANCE_GATES.md", "ROADMAP.md",
            "CLAIM_EVIDENCE.md", "CROSSMARK_POLICY.md",
            "HUMAN_AND_LEGAL_GATES.md", "ASSET_PROVENANCE.md",
        )
        for name in required:
            with self.subTest(name=name):
                self.assertTrue((ROOT / "docs" / name).is_file())


if __name__ == "__main__":
    unittest.main()
