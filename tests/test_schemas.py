from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.verify_release import (
    _check_archive_redaction,
    _check_pdf_audit_manifest_semantics,
    _check_record_semantics,
    SchemaValidationError,
    load_json,
    validate_json_schema,
    verify_release,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"


class SchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest_02 = load_json(SCHEMAS / "manifest-0.2.schema.json")
        cls.manifest_03 = load_json(SCHEMAS / "manifest-0.3.schema.json")
        cls.record_schema = load_json(SCHEMAS / "record.schema.json")
        cls.record_03_schema = load_json(SCHEMAS / "record-0.3.schema.json")
        cls.pdf_audit_schema = load_json(
            SCHEMAS / "pdf-reference-audit-0.1.schema.json"
        )
        cls.event_schema = load_json(
            SCHEMAS / "human-read-self-attestation-event.schema.json"
        )
        manifests = sorted((ROOT / "evidence").glob("*/manifest.json"))
        cls.latest_manifest_path = manifests[-1]
        cls.latest_run = cls.latest_manifest_path.parent

    def test_schema_documents_declare_draft_and_unique_ids(self):
        ids = set()
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            schema = load_json(path)
            self.assertEqual(
                schema["$schema"],
                "https://json-schema.org/draft/2020-12/schema",
            )
            self.assertNotIn(schema["$id"], ids)
            ids.add(schema["$id"])

    def test_latest_evidence_manifest_and_records_validate(self):
        manifest = load_json(self.latest_manifest_path)
        schema = {
            "0.2": self.manifest_02,
            "0.3": self.manifest_03,
        }[manifest["schema_version"]]
        record_schema = (
            self.record_03_schema
            if manifest["schema_version"] == "0.3"
            else self.record_schema
        )
        validate_json_schema(manifest, schema)
        records = (self.latest_run / "records.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertTrue(records)
        for line in records:
            validate_json_schema(json.loads(line), record_schema)

    def test_synthetic_manifest_03_validates_and_wrong_version_fails(self):
        fixture = load_json(self.latest_manifest_path)
        fixture["schema_version"] = "0.3"
        fixture["transport_mode"] = "network"
        fixture["cache_enabled"] = False
        validate_json_schema(fixture, self.manifest_03)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(fixture, self.manifest_02)

    def test_synthetic_self_attestation_validates(self):
        fixture = {
            "schema_version": 1,
            "event_type": "HUMAN_READ_SELF_ATTESTED",
            "recorded_at": "2026-07-26T01:02:03Z",
            "actor_type": "claimed_human",
            "attestation_method": "unverified_cli_claim",
            "identity_verified": False,
            "reviewer_role": "anonymous_senior_domain_reviewer",
            "material_path": "/controlled/material.pdf",
            "material_sha256": "a" * 64,
            "confirmation_statement": "I personally read the exact hashed material.",
            "previous_event_hash": "0" * 64,
            "event_hash": "b" * 64,
        }
        validate_json_schema(fixture, self.event_schema)
        machine_claim = deepcopy(fixture)
        machine_claim["actor_type"] = "ai"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(machine_claim, self.event_schema)

    def _pdf_manifest(self, *, lookup_enabled: bool = False):
        outputs = {
            "reference_results.jsonl": "1" * 64,
            "citation_map.json": "2" * 64,
            "report.md": "3" * 64,
        }
        lookup_policy = {
            "lookup_enabled": False,
            "transport_mode": "none",
            "sources": [],
            "selected_reference_numbers": [],
        }
        summary = {
            "reference_count": 2,
            "cited_reference_count": 1,
            "citation_occurrence_count": 1,
            "dangling_citation_numbers": [],
            "uncited_reference_numbers": [2],
            "lookup_attempted_count": 0,
            "high_confidence_match_count": 0,
            "lookup_completed_count": 0,
            "lookup_partial_count": 0,
            "lookup_failed_count": 0,
        }
        if lookup_enabled:
            outputs["batch_checkpoint.jsonl"] = "4" * 64
            lookup_policy = {
                "lookup_enabled": True,
                "transport_mode": "network",
                "sources": ["pubmed"],
                "selected_reference_numbers": [1],
                "limit_per_reference": 5,
                "cache_enabled": False,
                "checkpoint_sha256": "4" * 64,
            }
            summary.update(
                lookup_attempted_count=1,
                high_confidence_match_count=1,
                lookup_completed_count=1,
            )
            lookup_batch = {
                "batch_id": "d" * 64,
                "batch_status": "completed",
                "selected_count": 1,
                "attempted_this_run": 1,
                "completed_count": 1,
                "partial_count": 0,
                "failed_count": 0,
                "pending_count": 0,
                "semantic_support_assessed": False,
            }
        else:
            lookup_batch = None
        return {
            "schema_version": "pdf-reference-audit-0.1",
            "run_type": "pdf_reference_audit",
            "started_at": "2026-07-27T01:02:03Z",
            "artifact_completed_at": "2026-07-27T01:03:03Z",
            "source_pdf": {
                "name": "article.pdf",
                "sha256": "a" * 64,
                "byte_length": 1000,
                "page_count": 2,
                "source_material_archived": False,
            },
            "derivation": {
                "extractor": "pypdf",
                "extractor_version": "6.12.2",
                "policy_version": "pdf-reference-audit-0.2",
                "page_text_sha256": ["b" * 64, "c" * 64],
            },
            "lookup_policy": lookup_policy,
            "lookup_batch": lookup_batch,
            "summary": summary,
            "output_sha256": outputs,
            "human_adjudicated": False,
            "semantic_support_assessed": False,
            "coverage_equivalent_to_subscription_indexes": False,
        }

    def test_pdf_audit_manifest_schema_accepts_disabled_and_batch_lookup(self):
        for fixture in (self._pdf_manifest(), self._pdf_manifest(lookup_enabled=True)):
            with self.subTest(lookup=fixture["lookup_policy"]["lookup_enabled"]):
                validate_json_schema(fixture, self.pdf_audit_schema)
                self.assertEqual(
                    _check_pdf_audit_manifest_semantics(fixture, "manifest"), []
                )

    def test_pdf_audit_schema_rejects_incomplete_summary_and_lookup_contracts(self):
        cases = []
        fixture = self._pdf_manifest()
        del fixture["summary"]["lookup_failed_count"]
        cases.append(fixture)
        fixture = self._pdf_manifest()
        fixture["derivation"]["page_text_sha256"] = []
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["lookup_policy"]["sources"] = ["pubmed", "pubmed"]
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        del fixture["lookup_policy"]["checkpoint_sha256"]
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["lookup_policy"]["transport_mode"] = "offline_replay"
        fixture["lookup_policy"]["cache_enabled"] = False
        cases.append(fixture)
        fixture = self._pdf_manifest()
        fixture["lookup_policy"]["sources"] = ["pubmed"]
        cases.append(fixture)
        fixture = self._pdf_manifest()
        fixture["lookup_batch"] = {
            "batch_id": "d" * 64,
            "batch_status": "pending",
            "selected_count": 1,
            "attempted_this_run": 0,
            "completed_count": 0,
            "partial_count": 0,
            "failed_count": 0,
            "pending_count": 1,
            "semantic_support_assessed": False,
        }
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["lookup_batch"] = None
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        del fixture["lookup_batch"]["pending_count"]
        cases.append(fixture)
        for number, invalid in enumerate(cases):
            with self.subTest(case=number), self.assertRaises(SchemaValidationError):
                validate_json_schema(invalid, self.pdf_audit_schema)

    def test_pdf_audit_semantics_reject_cross_field_and_batch_hash_conflicts(self):
        cases = []
        fixture = self._pdf_manifest()
        fixture["source_pdf"]["name"] = "/private/article.pdf"
        cases.append(fixture)
        fixture = self._pdf_manifest()
        fixture["source_pdf"]["page_count"] = 3
        cases.append(fixture)
        fixture = self._pdf_manifest()
        fixture["summary"]["lookup_attempted_count"] = 1
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["lookup_policy"]["checkpoint_sha256"] = "f" * 64
        cases.append(fixture)
        fixture = self._pdf_manifest()
        del fixture["output_sha256"]["report.md"]
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["lookup_batch"]["selected_count"] = 2
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["lookup_batch"]["pending_count"] = 1
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["lookup_batch"]["attempted_this_run"] = 2
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["lookup_batch"]["batch_status"] = "partial"
        cases.append(fixture)
        fixture = self._pdf_manifest(lookup_enabled=True)
        fixture["summary"]["lookup_completed_count"] = 0
        fixture["summary"]["lookup_partial_count"] = 1
        cases.append(fixture)
        for number, invalid in enumerate(cases):
            with self.subTest(case=number):
                self.assertTrue(
                    _check_pdf_audit_manifest_semantics(invalid, "manifest")
                )

    def test_release_verifier_dispatches_nested_pdf_audit_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
            shutil.copytree(ROOT / "schemas", root / "schemas")
            package = root / "src/ai_research_agent"
            package.mkdir(parents=True)
            for name in ("__init__.py", "reporting.py", "audit.py"):
                shutil.copy2(ROOT / f"src/ai_research_agent/{name}", package / name)
            benchmark = root / "tests/fixtures/golden"
            benchmark.mkdir(parents=True)
            shutil.copy2(
                ROOT / "tests/fixtures/golden/reliability_cases_240.json",
                benchmark / "reliability_cases_240.json",
            )
            run = root / "evidence/nested/pdf-audit"
            run.mkdir(parents=True)
            output_hashes = {}
            for name, body in (
                ("reference_results.jsonl", b"{}\n"),
                ("citation_map.json", b"{}\n"),
                ("report.md", b"# report\n"),
            ):
                (run / name).write_bytes(body)
                output_hashes[name] = hashlib.sha256(body).hexdigest()
            manifest = self._pdf_manifest()
            manifest["output_sha256"] = output_hashes
            (run / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            self.assertEqual(verify_release(root), [])
            manifest["summary"]["reference_count"] = 0
            (run / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            errors = verify_release(root)
            self.assertTrue(
                any("value is below minimum" in error for error in errors),
                errors,
            )

    def test_record_rejects_sensitive_or_unknown_fields(self):
        line = (self.latest_run / "records.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()[0]
        fixture = json.loads(line)
        fixture["api_key"] = "must-not-appear"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(fixture, self.record_schema)

    def test_record_03_requires_status_gate_fields(self):
        fixture = {
            "title": "Test", "authors": [], "journal": None, "year": 2026,
            "doi": "10.1000/test", "pmid": "1", "pmcid": None,
            "publication_types": [], "access_status": "unknown", "access_url": None,
            "retraction_status": "unknown", "publication_status": "unknown",
            "status_signals": [], "status_relations": [],
            "publication_status_sources_checked": [], "status_evidence_ids": [],
            "publication_status_check_complete": False,
            "verification_status": "unverified", "verification_reasons": [],
            "conflicts": [], "evidence": [], "field_provenance": {},
            "citation_ready": False,
        }
        validate_json_schema(fixture, self.record_03_schema)
        del fixture["publication_status_check_complete"]
        fixture["citation_ready"] = True
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(fixture, self.record_03_schema)

    def test_record_03_status_evidence_cross_reference_is_fail_closed(self):
        fixture = {
            "doi": "10.1000/test", "pmid": None, "evidence": [],
            "status_evidence_ids": ["ev-" + "a" * 20],
            "publication_status_check_complete": True, "citation_ready": True,
        }
        self.assertTrue(_check_record_semantics(fixture, "record"))

    def test_record_03_requires_one_status_source_per_identifier(self):
        crossref = {
            "evidence_id": "ev-" + "a" * 20, "source": "crossref_status",
            "record_id": "10.1000/test", "doi": "10.1000/test", "pmid": None,
            "response_sha256": "a" * 64,
        }
        duplicate = dict(crossref, evidence_id="ev-" + "b" * 20)
        record = {
            "doi": "10.1000/test", "pmid": "123",
            "evidence": [crossref, duplicate],
            "status_evidence_ids": [crossref["evidence_id"], duplicate["evidence_id"]],
            "publication_status_check_complete": True, "citation_ready": True,
        }
        errors = _check_record_semantics(
            record, "record", archived_response_hashes={"a" * 64}
        )
        self.assertTrue(any("pubmed_status" in error for error in errors))

    def test_archive_redaction_check_rejects_nested_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.metadata.json"
            path.write_text(
                json.dumps(
                    {
                        "request": {
                            "url": "https://example.test/search?q=bone",
                            "headers": {"Accept": "application/json"},
                        },
                        "debug": {"api_key": "must-not-appear"},
                    }
                ),
                encoding="utf-8",
            )
            errors = _check_archive_redaction(path)
            self.assertTrue(any("$.debug.api_key" in error for error in errors))

    def test_archive_redaction_rejects_webenv_in_request_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.metadata.json"
            path.write_text(
                json.dumps({"request": {"url": "https://example.test/?WebEnv=secret", "headers": {}}}),
                encoding="utf-8",
            )
            self.assertTrue(_check_archive_redaction(path))

    def test_release_verifier_accepts_repository(self):
        self.assertEqual(verify_release(ROOT), [])


if __name__ == "__main__":
    unittest.main()
