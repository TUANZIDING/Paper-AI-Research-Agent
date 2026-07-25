from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_research_agent.claim_evidence import (
    AnchorKind,
    ClaimEvidenceError,
    HumanReviewStatus,
    LocatorStatus,
    SupportStatus,
    bind_claim_evidence,
    locate_anchor,
    sha256_bytes,
)
from ai_research_agent.human_read import attest_human_read, rescind_human_read


EVIDENCE_ID = "ev-" + "a" * 20


class ClaimEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.text = "First paragraph.\n\nThe intervention reduced mortality.\n\nLimitations."
        self.material = self.text.encode("utf-8")
        self.material_hash = sha256_bytes(self.material)

    def bind(self, **overrides):
        values = {
            "claim_id": "claim-1",
            "claim_text": "The intervention reduced mortality.",
            "material": self.material,
            "expected_material_sha256": self.material_hash,
            "material_content_type": "text/plain",
            "evidence_id": EVIDENCE_ID,
            "anchor_kind": AnchorKind.QUOTE,
            "anchor_value": "The intervention reduced mortality.",
            "text": self.text,
            "support_status": SupportStatus.SUPPORTED,
        }
        values.update(overrides)
        return bind_claim_evidence(**values)

    def test_exact_quote_binds_hashes_but_not_human_read(self):
        binding = self.bind()
        self.assertEqual(binding.locator_status, "located")
        self.assertEqual(binding.requested_support_status, "SUPPORTED")
        self.assertEqual(binding.support_status, "AMBIGUOUS")
        self.assertEqual(binding.human_review_status, "not_attested")
        self.assertFalse(binding.identity_verified)
        self.assertEqual(len(binding.excerpt_sha256), 64)

    def test_wrong_material_hash_blocks(self):
        with self.assertRaisesRegex(ClaimEvidenceError, "Material SHA-256 mismatch"):
            self.bind(expected_material_sha256="0" * 64)

    def test_unbound_parallel_text_is_blocked(self):
        with self.assertRaisesRegex(ClaimEvidenceError, "deterministic extraction"):
            self.bind(text="The intervention reduced mortality.")

    def test_quote_not_found_and_repeated_quote_do_not_bind(self):
        missing = locate_anchor(
            anchor_kind="quote", anchor_value="absent", text=self.text
        )
        self.assertIs(missing.status, LocatorStatus.NOT_FOUND)
        with self.assertRaisesRegex(ClaimEvidenceError, "not_found"):
            self.bind(anchor_value="absent")
        repeated = locate_anchor(
            anchor_kind="quote", anchor_value="same", text="same and same"
        )
        self.assertIs(repeated.status, LocatorStatus.AMBIGUOUS)

    def test_page_and_paragraph_ranges_are_strict(self):
        out = locate_anchor(
            anchor_kind="page", anchor_value="2-4", text="", pages=["p1", "p2"]
        )
        self.assertIs(out.status, LocatorStatus.OUT_OF_BOUNDS)
        backwards = locate_anchor(
            anchor_kind="paragraph", anchor_value="3-2", text=self.text
        )
        self.assertIs(backwards.status, LocatorStatus.INVALID_ANCHOR)
        with self.assertRaisesRegex(ClaimEvidenceError, "controlled extraction"):
            self.bind(anchor_kind="page", anchor_value="3", pages=["only"])

    def test_excerpt_hash_mismatch_blocks(self):
        with self.assertRaisesRegex(ClaimEvidenceError, "Excerpt SHA-256 mismatch"):
            self.bind(expected_excerpt_sha256="0" * 64)

    def test_partially_supported_compound_claim_cannot_upgrade(self):
        binding = self.bind(
            component_support_statuses=[
                SupportStatus.SUPPORTED,
                SupportStatus.UNSUPPORTED,
            ]
        )
        self.assertEqual(binding.requested_support_status, "SUPPORTED")
        self.assertEqual(binding.support_status, "AMBIGUOUS")

    def test_supported_input_without_component_proof_stays_ambiguous(self):
        binding = self.bind(component_support_statuses=[])
        self.assertEqual(binding.requested_support_status, "SUPPORTED")
        self.assertEqual(binding.support_status, "AMBIGUOUS")

    def test_controlled_attestation_remains_identity_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material_path = root / "article.pdf"
            material_path.write_bytes(self.material)
            log = root / "events.jsonl"
            event = attest_human_read(
                event_log=log,
                material_path=material_path.resolve(),
                expected_sha256=self.material_hash,
                reviewer_role="anonymous_senior_domain_reviewer",
                confirmation_statement="I personally read the exact hashed material.",
                actor_type="human",
                explicit_command=True,
            )
            binding = self.bind(
                attestation_event_log=log,
                attestation_event_hash=event["event_hash"],
            )
            self.assertEqual(
                binding.human_review_status, HumanReviewStatus.SELF_ATTESTED.value
            )
            self.assertFalse(binding.identity_verified)
            self.assertEqual(binding.attestation_event_hash, event["event_hash"])

    def test_missing_or_wrong_attestation_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "events.jsonl"
            with self.assertRaises(ClaimEvidenceError):
                self.bind(attestation_event_log=log, attestation_event_hash="b" * 64)

    def test_rescinded_attestation_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material_path = root / "article.pdf"
            material_path.write_bytes(self.material)
            log = root / "events.jsonl"
            event = attest_human_read(
                event_log=log,
                material_path=material_path.resolve(),
                expected_sha256=self.material_hash,
                reviewer_role="anonymous_senior_domain_reviewer",
                confirmation_statement="I personally read the exact hashed material.",
                actor_type="human",
                explicit_command=True,
            )
            rescind_human_read(
                event_log=log,
                target_event_hash=event["event_hash"],
                reviewer_role="anonymous_senior_domain_reviewer",
                reason="The earlier attestation was entered in error.",
                actor_type="human",
                explicit_command=True,
            )
            with self.assertRaisesRegex(ClaimEvidenceError, "rescinded"):
                self.bind(
                    attestation_event_log=log,
                    attestation_event_hash=event["event_hash"],
                )

    def test_schema_contract_lists_safety_fields(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas/claim-evidence.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        required = set(schema["required"])
        self.assertTrue(
            {
                "claim_text_hash", "material_sha256", "excerpt_sha256",
                "locator_status", "human_review_status", "identity_verified",
            }.issubset(required)
        )


if __name__ == "__main__":
    unittest.main()
