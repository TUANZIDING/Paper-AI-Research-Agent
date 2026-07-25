import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ai_research_agent.external_review import (
    ExternalReviewError,
    canonical_packet_bytes,
    prepare_review_packet,
    verify_external_review,
)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class ExternalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.material = self.root / "article.pdf"
        self.material.write_bytes(b"controlled material")
        self.private_key = self.root / "private.pem"
        self.public_key = self.root / "public.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.private_key)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["openssl", "pkey", "-in", str(self.private_key), "-pubout", "-out", str(self.public_key)],
            check=True, capture_output=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def packet(self):
        return prepare_review_packet(
            material_path=self.material,
            reviewer_role="anonymous_senior_domain_reviewer",
            confirmation_statement="I personally reviewed the exact material identified by this digest.",
            issued_at="2026-07-26T00:00:00Z",
        )

    def sign(self, packet):
        payload = self.root / "packet.json"
        signature = self.root / "packet.sig"
        payload.write_bytes(canonical_packet_bytes(packet))
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(self.private_key), "-rawin", "-in", str(payload), "-out", str(signature)],
            check=True, capture_output=True,
        )
        return signature

    def test_signature_proves_key_control_not_identity_or_reading(self):
        packet = self.packet()
        result = verify_external_review(
            packet=packet, signature_path=self.sign(packet),
            public_key_path=self.public_key, material_path=self.material,
        )
        self.assertTrue(result.signature_verified)
        self.assertTrue(result.material_hash_verified)
        self.assertFalse(result.identity_verified)
        self.assertFalse(result.human_read_confirmed)

    def test_tampered_packet_or_material_is_blocked(self):
        packet = self.packet()
        signature = self.sign(packet)
        packet["confirmation_statement"] = "Tampered statement that remains long enough."
        with self.assertRaises(ExternalReviewError):
            verify_external_review(
                packet=packet, signature_path=signature,
                public_key_path=self.public_key, material_path=self.material,
            )

    def test_packet_cannot_claim_verified_identity(self):
        packet = self.packet()
        packet["identity_claim"] = "identity_verified"
        with self.assertRaises(ExternalReviewError):
            verify_external_review(
                packet=packet, signature_path=self.sign(self.packet()),
                public_key_path=self.public_key, material_path=self.material,
            )

    def test_invalid_issued_at_is_rejected_before_signature_upgrade(self):
        import hashlib

        packet = self.packet()
        packet["issued_at"] = "not-a-date"
        unsigned = dict(packet)
        unsigned.pop("packet_sha256")
        packet["packet_sha256"] = hashlib.sha256(
            canonical_packet_bytes(unsigned)
        ).hexdigest()
        with self.assertRaisesRegex(ExternalReviewError, "issued_at"):
            verify_external_review(
                packet=packet, signature_path=self.sign(packet),
                public_key_path=self.public_key, material_path=self.material,
            )

    def test_rsa_key_is_rejected_when_policy_requires_ed25519(self):
        private_key = self.root / "rsa-private.pem"
        public_key = self.root / "rsa-public.pem"
        payload = self.root / "rsa-packet.json"
        signature = self.root / "rsa-packet.sig"
        packet = self.packet()
        payload.write_bytes(canonical_packet_bytes(packet))
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
             "-out", str(private_key)], check=True, capture_output=True,
        )
        subprocess.run(
            ["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(private_key), "-rawin",
             "-in", str(payload), "-out", str(signature)], check=True, capture_output=True,
        )
        with self.assertRaisesRegex(ExternalReviewError, "Ed25519"):
            verify_external_review(
                packet=packet, signature_path=signature,
                public_key_path=public_key, material_path=self.material,
            )


if __name__ == "__main__":
    unittest.main()
