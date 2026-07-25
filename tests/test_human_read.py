import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ai_research_agent.human_read import (
    RESCINDED_EVENT,
    SELF_ATTESTED_EVENT,
    HumanReadGateError,
    attest_human_read,
    rescind_human_read,
    verify_chain,
)


class HumanReadGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.material = self.root / "article.pdf"
        self.material.write_bytes(b"reviewed source material")
        self.digest = hashlib.sha256(self.material.read_bytes()).hexdigest()
        self.log = self.root / "human-read-events.jsonl"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def confirm(self):
        return attest_human_read(
            event_log=self.log,
            material_path=self.material,
            expected_sha256=self.digest,
            reviewer_role="anonymous_senior_domain_reviewer",
            confirmation_statement=(
                "I personally read the supplied material and confirm this event."
            ),
            actor_type="human",
            explicit_command=True,
        )

    def test_successful_confirmation_and_chain(self):
        event = self.confirm()
        self.assertEqual(event["event_type"], SELF_ATTESTED_EVENT)
        self.assertFalse(event["identity_verified"])
        self.assertEqual(event["attestation_method"], "unverified_cli_claim")
        self.assertEqual(event["material_sha256"], self.digest)
        result = verify_chain(self.log)
        self.assertTrue(result.valid)
        self.assertEqual(result.event_count, 1)

    def test_confirmation_is_not_created_without_explicit_command(self):
        with self.assertRaises(HumanReadGateError):
            attest_human_read(
                event_log=self.log,
                material_path=self.material,
                expected_sha256=self.digest,
                reviewer_role="anonymous_domain_reviewer",
                confirmation_statement="I personally completed the human review.",
            )
        self.assertFalse(self.log.exists())

    def test_hash_mismatch_is_blocked(self):
        with self.assertRaisesRegex(HumanReadGateError, "mismatch"):
            attest_human_read(
                event_log=self.log,
                material_path=self.material,
                expected_sha256="0" * 64,
                reviewer_role="anonymous_domain_reviewer",
                confirmation_statement="I personally completed the human review.",
                explicit_command=True,
            )

    def test_relative_material_path_is_blocked(self):
        with self.assertRaisesRegex(HumanReadGateError, "absolute"):
            attest_human_read(
                event_log=self.log,
                material_path="article.pdf",
                expected_sha256=self.digest,
                reviewer_role="anonymous_domain_reviewer",
                confirmation_statement="I personally completed the human review.",
                explicit_command=True,
            )

    def test_missing_confirmation_statement_is_blocked(self):
        with self.assertRaisesRegex(HumanReadGateError, "explicit statement"):
            attest_human_read(
                event_log=self.log,
                material_path=self.material,
                expected_sha256=self.digest,
                reviewer_role="anonymous_domain_reviewer",
                confirmation_statement="",
                explicit_command=True,
            )

    def test_machine_actor_is_blocked(self):
        with self.assertRaisesRegex(HumanReadGateError, "machine/system labels are blocked"):
            attest_human_read(
                event_log=self.log,
                material_path=self.material,
                expected_sha256=self.digest,
                reviewer_role="anonymous_domain_reviewer",
                confirmation_statement="System claims it completed the review.",
                actor_type="system",
                explicit_command=True,
            )

    def test_tampered_chain_is_detected(self):
        self.confirm()
        event = json.loads(self.log.read_text(encoding="utf-8"))
        event["confirmation_statement"] = "tampered statement"
        self.log.write_text(json.dumps(event) + "\n", encoding="utf-8")
        result = verify_chain(self.log)
        self.assertFalse(result.valid)
        self.assertIn("Event hash mismatch", result.error)

    def test_rescission_appends_event_and_preserves_confirmation(self):
        confirmation = self.confirm()
        rescission = rescind_human_read(
            event_log=self.log,
            target_event_hash=confirmation["event_hash"],
            reviewer_role="anonymous_research_integrity_reviewer",
            reason="The reviewed material version is now superseded.",
            actor_type="human",
            explicit_command=True,
        )
        self.assertEqual(rescission["event_type"], RESCINDED_EVENT)
        self.assertEqual(
            rescission["previous_event_hash"], confirmation["event_hash"]
        )
        events = [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["event_type"] for event in events],
            [SELF_ATTESTED_EVENT, RESCINDED_EVENT],
        )
        result = verify_chain(self.log)
        self.assertTrue(result.valid)
        self.assertEqual(result.event_count, 2)


if __name__ == "__main__":
    unittest.main()
