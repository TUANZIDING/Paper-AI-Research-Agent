import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ai_research_agent.audit import verify_run


class RunAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        raw = self.root / "raw_responses"
        raw.mkdir()
        self.body = raw / "sample.response"
        self.body.write_bytes(b'{"ok": true}')
        digest = hashlib.sha256(self.body.read_bytes()).hexdigest()
        self.metadata = raw / "sample.metadata.json"
        self.metadata.write_text(
            json.dumps(
                {
                    "response": {
                        "body_file": self.body.name,
                        "byte_length": self.body.stat().st_size,
                        "sha256": digest,
                    }
                }
            ),
            encoding="utf-8",
        )
        hashes = {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.body, self.metadata)
        }
        (self.root / "manifest.json").write_text(
            json.dumps({"output_sha256": hashes}), encoding="utf-8"
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_valid_run(self):
        result = verify_run(self.root)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.checked_files, 2)

    def test_tampered_body_is_detected(self):
        self.body.write_bytes(b"tampered")
        result = verify_run(self.root)
        self.assertFalse(result.valid)
        self.assertTrue(any("hash mismatch" in error for error in result.errors))

    def test_untracked_file_is_detected(self):
        (self.root / "extra.txt").write_text("extra", encoding="utf-8")
        result = verify_run(self.root)
        self.assertFalse(result.valid)
        self.assertIn("untracked file: extra.txt", result.errors)


if __name__ == "__main__":
    unittest.main()
