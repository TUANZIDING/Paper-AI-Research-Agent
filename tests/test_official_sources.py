from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from ai_research_agent.official_sources import (
    OfficialDomainRule,
    OfficialSourcePolicyError,
    OfficialSourceRegistry,
    create_manual_registration,
    load_registration_evidence_bundle,
    save_candidate_evidence,
    save_registration_evidence_bundle,
    verify_saved_candidate_evidence,
)


class OfficialSourcesTests(unittest.TestCase):
    def registration(self, *, include_subdomains: bool = False):
        return create_manual_registration(
            organization_id="example-guideline-body",
            organization_name="Example Guideline Body",
            rules=(
                OfficialDomainRule(
                    "guidance.example.org",
                    ("/standards", "/boast"),
                    include_subdomains=include_subdomains,
                ),
            ),
            registrar_role="anonymous information governance reviewer",
            evidence_source_url="https://registry.example.net/organizations/example-guideline-body",
            evidence_bytes=b"controlled registry export: example-guideline-body",
            evidence_observed_at="2026-07-27T08:30:00+08:00",
        )

    def test_registered_host_and_path_produce_candidate_with_fail_closed_flags(self):
        registry = OfficialSourceRegistry((self.registration(),))
        result = registry.propose(
            organization_id="example-guideline-body",
            url="https://guidance.example.org/boast/open-fractures?edition=2024#summary",
            title="Example trauma guideline",
        )
        self.assertEqual(
            result.normalized_url,
            "https://guidance.example.org/boast/open-fractures?edition=2024",
        )
        self.assertEqual(result.candidate_status, "registered_official_source_candidate")
        self.assertFalse(result.network_fetch_permitted)
        self.assertFalse(result.official_identity_verified)
        self.assertFalse(result.human_read_confirmed)
        self.assertFalse(result.final_legal_judgment)
        self.assertRegex(result.registration_evidence_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(result.evidence_id, r"^official-source:[0-9a-f]{64}$")

    def test_unknown_organization_and_domain_suffix_attack_are_blocked(self):
        registry = OfficialSourceRegistry((self.registration(),))
        with self.assertRaisesRegex(OfficialSourcePolicyError, "not in the explicit registry"):
            registry.propose(
                organization_id="page-claimed-official",
                url="https://guidance.example.org/boast/item",
                title="Self-claimed official page",
            )
        with self.assertRaisesRegex(OfficialSourcePolicyError, "outside"):
            registry.propose(
                organization_id="example-guideline-body",
                url="https://guidance.example.org.attacker.test/boast/item",
                title="Suffix attack",
            )

    def test_subdomains_require_explicit_rule(self):
        base_url = "https://archive.guidance.example.org/standards/item"
        with self.assertRaises(OfficialSourcePolicyError):
            OfficialSourceRegistry((self.registration(),)).propose(
                organization_id="example-guideline-body", url=base_url, title="Item"
            )
        candidate = OfficialSourceRegistry(
            (self.registration(include_subdomains=True),)
        ).propose(
            organization_id="example-guideline-body", url=base_url, title="Item"
        )
        self.assertEqual(candidate.matched_hostname, "archive.guidance.example.org")

    def test_path_prefix_uses_segment_boundary(self):
        registry = OfficialSourceRegistry((self.registration(),))
        with self.assertRaisesRegex(OfficialSourcePolicyError, "outside"):
            registry.propose(
                organization_id="example-guideline-body",
                url="https://guidance.example.org/boast-impersonation/item",
                title="Wrong path",
            )

    def test_ssrf_shaped_urls_and_credentials_are_rejected_without_network(self):
        registry = OfficialSourceRegistry((self.registration(),))
        blocked = (
            "http://guidance.example.org/boast/item",
            "https://user:secret@guidance.example.org/boast/item",
            "https://guidance.example.org:8443/boast/item",
            "https://127.0.0.1/boast/item",
            "https://localhost/boast/item",
            "https://guidance.example.org/boast/../admin",
            "https://guidance.example.org/boast/item?token=secret",
        )
        for url in blocked:
            with self.subTest(url=url), self.assertRaises(OfficialSourcePolicyError):
                registry.propose(
                    organization_id="example-guideline-body", url=url, title="Item"
                )

    def test_registration_is_deterministic_and_evidence_bound(self):
        first = self.registration()
        second = self.registration()
        self.assertEqual(first.sha256, second.sha256)
        changed = create_manual_registration(
            organization_id="example-guideline-body",
            organization_name="Example Guideline Body",
            rules=(OfficialDomainRule("guidance.example.org", ("/boast",)),),
            registrar_role="anonymous information governance reviewer",
            evidence_source_url="https://registry.example.net/organizations/example-guideline-body",
            evidence_bytes=b"different controlled evidence",
            evidence_observed_at="2026-07-27T08:30:00+08:00",
        )
        self.assertNotEqual(first.sha256, changed.sha256)
        self.assertNotEqual(
            first.registration_evidence_sha256, changed.registration_evidence_sha256
        )

    def test_registration_bundle_freezes_exact_evidence_bytes(self):
        registration = self.registration()
        evidence = b"controlled registry export: example-guideline-body"
        with tempfile.TemporaryDirectory() as temporary:
            saved = save_registration_evidence_bundle(
                registration, evidence, Path(temporary).resolve()
            )
            self.assertTrue(
                verify_saved_candidate_evidence(saved.path, expected_sha256=saved.sha256)
            )
            payload = json.loads(saved.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["registration_sha256"], registration.sha256)
            self.assertEqual(payload["evidence_encoding"], "base64")
            self.assertEqual(load_registration_evidence_bundle(saved.path), registration)
            with self.assertRaisesRegex(OfficialSourcePolicyError, "do not match"):
                save_registration_evidence_bundle(
                    registration, b"tampered", Path(temporary).resolve()
                )

    def test_tampered_registration_bundle_is_rejected(self):
        registration = self.registration()
        evidence = b"controlled registry export: example-guideline-body"
        with tempfile.TemporaryDirectory() as temporary:
            saved = save_registration_evidence_bundle(
                registration, evidence, Path(temporary).resolve()
            )
            payload = json.loads(saved.path.read_text(encoding="utf-8"))
            payload["registration"]["organization_name"] = "Forged Body"
            saved.path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(OfficialSourcePolicyError, "hash"):
                load_registration_evidence_bundle(saved.path)

    def test_candidate_is_saved_content_addressably_and_tamper_is_detected(self):
        candidate = OfficialSourceRegistry((self.registration(),)).propose(
            organization_id="example-guideline-body",
            url="https://guidance.example.org/standards/hip-fracture",
            title="Hip fracture standard",
        )
        with tempfile.TemporaryDirectory() as temporary:
            saved = save_candidate_evidence(candidate, Path(temporary).resolve())
            self.assertTrue(
                verify_saved_candidate_evidence(saved.path, expected_sha256=saved.sha256)
            )
            payload = json.loads(saved.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["registration_sha256"], candidate.registration_sha256)
            saved.path.write_text("{}\n", encoding="utf-8")
            self.assertFalse(
                verify_saved_candidate_evidence(saved.path, expected_sha256=saved.sha256)
            )

    def test_relative_evidence_directory_and_duplicate_write_are_blocked(self):
        candidate = OfficialSourceRegistry((self.registration(),)).propose(
            organization_id="example-guideline-body",
            url="https://guidance.example.org/standards/item",
            title="Item",
        )
        with self.assertRaisesRegex(OfficialSourcePolicyError, "absolute"):
            save_candidate_evidence(candidate, "relative/evidence")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            save_candidate_evidence(candidate, directory)
            with self.assertRaisesRegex(OfficialSourcePolicyError, "already exists"):
                save_candidate_evidence(candidate, directory)

    def test_forged_candidate_id_cannot_escape_or_be_archived(self):
        candidate = OfficialSourceRegistry((self.registration(),)).propose(
            organization_id="example-guideline-body",
            url="https://guidance.example.org/standards/item",
            title="Item",
        )
        forged = replace(candidate, evidence_id="../../outside")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(OfficialSourcePolicyError, "inconsistent"):
                save_candidate_evidence(forged, Path(temporary).resolve())

    def test_registration_rejects_empty_evidence_and_naive_time(self):
        base = dict(
            organization_id="example-guideline-body",
            organization_name="Example Guideline Body",
            rules=(OfficialDomainRule("guidance.example.org"),),
            registrar_role="reviewer",
            evidence_source_url="https://registry.example.net/entry",
            evidence_observed_at="2026-07-27T08:30:00+08:00",
        )
        with self.assertRaises(OfficialSourcePolicyError):
            create_manual_registration(**base, evidence_bytes=b"")
        with self.assertRaisesRegex(OfficialSourcePolicyError, "timezone"):
            create_manual_registration(
                **{**base, "evidence_observed_at": "2026-07-27T08:30:00"},
                evidence_bytes=b"evidence",
            )


if __name__ == "__main__":
    unittest.main()
