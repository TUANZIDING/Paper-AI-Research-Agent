from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.verify_release import (
    _check_archive_redaction,
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
