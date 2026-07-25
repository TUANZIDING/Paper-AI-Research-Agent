from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from ai_research_agent.cli import main


class ReliabilityFeatureCliTests(unittest.TestCase):
    def test_discover_license_labels_candidates_as_non_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "crossref.json"
            metadata.write_text(json.dumps({
                "DOI": "10.1000/test",
                "link": [{"URL": "https://example.org/a.pdf", "content-version": "vor"}],
                "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/",
                             "content-version": "vor"}],
            }), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["discover-licenses", "--crossref", str(metadata)])
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["candidate_count"], 1)
            self.assertFalse(payload["final_legal_judgment"])
            self.assertFalse(payload["download_authorized"])

    def test_bind_claim_records_location_without_identity_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "article.xml"
            text_path = root / "article.txt"
            quote = "A unique exact evidence sentence."
            text_value = f"First paragraph.\n\n{quote}"
            body = text_value.encode("utf-8")
            material.write_bytes(body)
            text_path.write_text(text_value, encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = main([
                    "bind-claim", "--claim-id", "claim-1",
                    "--claim", "A test claim", "--material", str(material),
                    "--material-sha256", hashlib.sha256(body).hexdigest(),
                    "--material-content-type", "text/plain",
                    "--text", str(text_path), "--evidence-id", "ev-" + "a" * 20,
                    "--anchor-kind", "quote", "--anchor", quote,
                    "--support-status", "AMBIGUOUS",
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["locator_status"], "located")
            self.assertEqual(payload["human_review_status"], "not_attested")
            self.assertFalse(payload["identity_verified"])

    def test_prepare_review_packet_never_claims_reading_or_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "article.pdf"
            packet = root / "packet.json"
            material.write_bytes(b"controlled material")
            output = io.StringIO()
            with redirect_stdout(output):
                result = main([
                    "prepare-review-packet", "--material", str(material),
                    "--role", "anonymous_senior_reviewer",
                    "--statement", "I will personally review the exact hashed material.",
                    "--output", str(packet),
                ])
            payload = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertTrue(packet.is_file())
            self.assertFalse(payload["identity_verified"])
            self.assertFalse(payload["human_read_confirmed"])


if __name__ == "__main__":
    unittest.main()
