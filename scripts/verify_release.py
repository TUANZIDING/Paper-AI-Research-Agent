#!/usr/bin/env python3
"""Fail-closed release checks using only the Python standard library.

This implements only the JSON Schema keywords used by this repository.  It is
not a general replacement for a standards-complete JSON Schema implementation.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import parse_qsl, urlsplit


class SchemaValidationError(ValueError):
    pass


def _resolve_pointer(root: dict[str, Any], pointer: str) -> Any:
    if not pointer.startswith("#/"):
        raise SchemaValidationError(f"unsupported external $ref: {pointer}")
    current: Any = root
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise SchemaValidationError(f"unresolved $ref: {pointer}")
        current = current[part]
    return current


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(expected, False)


def _check_datetime(value: str, path: str) -> None:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaValidationError(f"{path}: invalid date-time") from exc


def validate_json_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any] | None = None,
    path: str = "$",
) -> None:
    """Validate the repository's deliberately small JSON Schema subset."""
    root = root_schema or schema
    if "$ref" in schema:
        target = _resolve_pointer(root, schema["$ref"])
        validate_json_schema(value, target, root_schema=root, path=path)
        return
    for index, subschema in enumerate(schema.get("allOf", [])):
        if not isinstance(subschema, dict):
            raise SchemaValidationError(f"{path}: invalid allOf schema at index {index}")
        validate_json_schema(
            value, subschema, root_schema=root, path=path
        )
    conditional = schema.get("if")
    if conditional is not None:
        if not isinstance(conditional, dict):
            raise SchemaValidationError(f"{path}: invalid if schema")
        try:
            validate_json_schema(value, conditional, root_schema=root, path=path)
            branch = schema.get("then")
        except SchemaValidationError:
            branch = schema.get("else")
        if branch is not None:
            if not isinstance(branch, dict):
                raise SchemaValidationError(f"{path}: invalid conditional branch")
            validate_json_schema(value, branch, root_schema=root, path=path)
    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: value is not in enum")

    declared = schema.get("type")
    if declared is not None:
        choices = [declared] if isinstance(declared, str) else declared
        if not isinstance(choices, list) or not all(isinstance(item, str) for item in choices):
            raise SchemaValidationError(f"{path}: invalid schema type declaration")
        if not any(_matches_type(value, item) for item in choices):
            raise SchemaValidationError(f"{path}: expected type {choices!r}")

    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            raise SchemaValidationError(f"{path}: object has too few properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise SchemaValidationError(f"{path}: object has too many properties")
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise SchemaValidationError(f"{path}: missing required keys {missing!r}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                validate_json_schema(
                    item, properties[key], root_schema=root, path=f"{path}.{key}"
                )
            elif additional is False:
                raise SchemaValidationError(f"{path}: unexpected key {key!r}")
            elif isinstance(additional, dict):
                validate_json_schema(
                    item, additional, root_schema=root, path=f"{path}.{key}"
                )
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise SchemaValidationError(f"{path}: array has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaValidationError(f"{path}: array has too many items")
        if schema.get("uniqueItems"):
            canonical_items = [
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in value
            ]
            if len(canonical_items) != len(set(canonical_items)):
                raise SchemaValidationError(f"{path}: array items are not unique")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                validate_json_schema(
                    item, schema["items"], root_schema=root, path=f"{path}[{index}]"
                )
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise SchemaValidationError(f"{path}: string is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaValidationError(f"{path}: string is too long")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise SchemaValidationError(f"{path}: string does not match pattern")
        if schema.get("format") == "date-time":
            _check_datetime(value, path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(f"{path}: value is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError(f"{path}: value is above maximum")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{path}: expected a JSON object")
    return value


def _declared_version(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise SchemaValidationError(f"version declaration missing in {path}")
    return match.group(1)


def _check_versions(root: Path, schemas: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    try:
        project_version = _declared_version(
            root / "pyproject.toml", r'^version\s*=\s*"([^"]+)"'
        )
        package_version = _declared_version(
            root / "src/ai_research_agent/__init__.py",
            r'^__version__\s*=\s*"([^"]+)"',
        )
        emitter_version = _declared_version(
            root / "src/ai_research_agent/reporting.py",
            r'"schema_version"\s*:\s*"([0-9]+\.[0-9]+)"',
        )
        if project_version != package_version:
            errors.append(
                f"version mismatch: pyproject={project_version}, package={package_version}"
            )
        package_minor = ".".join(package_version.split(".")[:2])
        if package_minor != emitter_version:
            errors.append(
                f"version/schema mismatch: package={package_version}, manifest={emitter_version}"
            )
        schema_name = f"manifest-{emitter_version}.schema.json"
        if schema_name not in schemas:
            errors.append(f"active manifest schema is missing: {schema_name}")
        elif schemas[schema_name].get("properties", {}).get("schema_version", {}).get("const") != emitter_version:
            errors.append(f"active manifest schema const mismatch: {schema_name}")
    except (OSError, SchemaValidationError) as exc:
        errors.append(str(exc))
    return errors


_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "email", "mailto",
    "password", "proxy-authorization", "secret", "token", "access_token",
    "webenv",
}


def _sensitive_key_paths(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key.casefold() in _SENSITIVE_KEYS:
                found.append(child)
            found.extend(_sensitive_key_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_sensitive_key_paths(item, f"{path}[{index}]"))
    return found


def _check_archive_redaction(metadata_path: Path) -> list[str]:
    errors: list[str] = []
    try:
        metadata = load_json(metadata_path)
        for sensitive_path in _sensitive_key_paths(metadata):
            errors.append(f"{metadata_path}: sensitive field {sensitive_path}")
        request = metadata.get("request", {})
        headers = request.get("headers", {}) if isinstance(request, dict) else {}
        if not isinstance(headers, dict):
            return [f"{metadata_path}: request.headers is not an object"]
        for key in headers:
            if key.casefold() in _SENSITIVE_KEYS or any(
                marker in key.casefold() for marker in ("email", "key", "token")
            ):
                errors.append(f"{metadata_path}: sensitive header field {key!r}")
        url = request.get("url") if isinstance(request, dict) else None
        if not isinstance(url, str):
            errors.append(f"{metadata_path}: sanitized request URL is missing")
        else:
            for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True):
                if key.casefold() in _SENSITIVE_KEYS:
                    errors.append(f"{metadata_path}: sensitive query field {key!r}")
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaValidationError) as exc:
        errors.append(f"{metadata_path}: {exc}")
    return errors


def _check_synthetic_benchmark(root: Path) -> list[str]:
    """Require a large reproducible contract set without calling it human gold."""
    path = root / "tests/fixtures/golden/reliability_cases_240.json"
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"{path}: {exc}"]
    if not isinstance(cases, list) or len(cases) < 200:
        return [f"{path}: synthetic benchmark must contain at least 200 cases"]
    errors: list[str] = []
    identifiers: set[str] = set()
    categories: set[str] = set()
    for number, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            errors.append(f"{path}:{number}: case is not an object")
            continue
        identifier = case.get("case_id")
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"{path}:{number}: case ID is missing")
        elif identifier in identifiers:
            errors.append(f"{path}:{number}: duplicate case ID {identifier!r}")
        else:
            identifiers.add(identifier)
        category = case.get("category")
        if isinstance(category, str):
            categories.add(category)
        if case.get("label_source") != "synthetic_contract_v1":
            errors.append(f"{path}:{number}: label_source is not synthetic_contract_v1")
        if case.get("human_adjudicated") is not False:
            errors.append(f"{path}:{number}: synthetic case cannot claim human adjudication")
    if len(categories) < 8:
        errors.append(f"{path}: expected at least 8 adversarial categories")
    return errors


def _normalized_doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I)
    return value.rstrip(" .;,)").casefold() or None


def _check_record_semantics(
    record: dict[str, Any],
    path: str,
    *,
    archived_response_hashes: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    evidence = record.get("evidence")
    status_ids = record.get("status_evidence_ids")
    if not isinstance(evidence, list) or not isinstance(status_ids, list):
        return [f"{path}: v0.3 evidence collections are missing"]
    by_id = {
        item.get("evidence_id"): item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }
    if len(by_id) != len(evidence):
        errors.append(f"{path}: evidence IDs must be present and unique")
    expected_sources: dict[str, tuple[str, Any]] = {}
    if record.get("doi"):
        expected_sources["crossref_status"] = ("doi", _normalized_doi(record.get("doi")))
    if record.get("pmid"):
        expected_sources["pubmed_status"] = ("pmid", str(record.get("pmid")))
    resolved_status_items: list[dict[str, Any]] = []
    for evidence_id in status_ids:
        item = by_id.get(evidence_id)
        if item is None:
            errors.append(f"{path}: status evidence ID {evidence_id!r} is unresolved")
            continue
        resolved_status_items.append(item)
        if item.get("source") not in {"crossref_status", "pubmed_status"}:
            errors.append(f"{path}: status evidence ID {evidence_id!r} has wrong source")
        if re.fullmatch(r"[0-9a-f]{64}", str(item.get("response_sha256") or "")) is None:
            errors.append(f"{path}: status evidence ID {evidence_id!r} lacks SHA-256")
        elif (
            archived_response_hashes is not None
            and item["response_sha256"] not in archived_response_hashes
        ):
            errors.append(f"{path}: status evidence ID {evidence_id!r} hash is not archived")
    if record.get("publication_status_check_complete"):
        if len(status_ids) != len(expected_sources):
            errors.append(
                f"{path}: complete status gate requires {len(expected_sources)} bound status evidence IDs"
            )
        for source, (identifier_field, expected_value) in expected_sources.items():
            matching = [item for item in resolved_status_items if item.get("source") == source]
            if len(matching) != 1:
                errors.append(f"{path}: complete status gate requires exactly one {source} evidence")
                continue
            actual = matching[0].get(identifier_field)
            actual = _normalized_doi(actual) if identifier_field == "doi" else str(actual)
            if actual != expected_value or str(matching[0].get("record_id")) != str(record.get(identifier_field)):
                errors.append(f"{path}: {source} evidence target does not match record")
    if record.get("citation_ready") and not record.get("publication_status_check_complete"):
        errors.append(f"{path}: citation_ready requires a complete status gate")
    return errors


def _check_pdf_audit_manifest_semantics(
    manifest: dict[str, Any], path: str | Path
) -> list[str]:
    """Check cross-field rules that the repository's JSON Schema cannot express."""

    label = str(path)
    errors: list[str] = []
    source = manifest.get("source_pdf")
    derivation = manifest.get("derivation")
    lookup = manifest.get("lookup_policy")
    lookup_batch = manifest.get("lookup_batch")
    summary = manifest.get("summary")
    outputs = manifest.get("output_sha256")
    if not all(
        isinstance(value, dict)
        for value in (source, derivation, lookup, summary, outputs)
    ):
        return [f"{label}: PDF audit manifest sections are not objects"]

    source_name = source.get("name")
    if (
        not isinstance(source_name, str)
        or Path(source_name).name != source_name
        or "/" in source_name
        or "\\" in source_name
    ):
        errors.append(f"{label}: source_pdf.name must not contain a path")
    page_count = source.get("page_count")
    page_hashes = derivation.get("page_text_sha256")
    if (
        isinstance(page_count, int)
        and isinstance(page_hashes, list)
        and len(page_hashes) != page_count
    ):
        errors.append(
            f"{label}: derivation.page_text_sha256 count does not match source page_count"
        )

    required_outputs = {
        "reference_results.jsonl",
        "citation_map.json",
        "report.md",
    }
    missing_outputs = required_outputs - set(outputs)
    if missing_outputs:
        errors.append(
            f"{label}: required PDF audit outputs missing: {sorted(missing_outputs)!r}"
        )

    lookup_enabled = lookup.get("lookup_enabled")
    transport_mode = lookup.get("transport_mode")
    sources = lookup.get("sources")
    selected = lookup.get("selected_reference_numbers")
    checkpoint_sha256 = lookup.get("checkpoint_sha256")
    if lookup_enabled is True:
        if not isinstance(lookup_batch, dict):
            errors.append(f"{label}: enabled lookup requires lookup_batch metadata")
        if not isinstance(sources, list) or not sources:
            errors.append(f"{label}: enabled lookup requires at least one source")
        if not isinstance(selected, list) or not selected:
            errors.append(f"{label}: enabled lookup requires selected references")
        if transport_mode not in {"network", "offline_replay"}:
            errors.append(f"{label}: enabled lookup has invalid transport mode")
        if lookup.get("cache_enabled") is not True and transport_mode == "offline_replay":
            errors.append(f"{label}: offline replay requires cache_enabled=true")
        archived_checkpoint = outputs.get("batch_checkpoint.jsonl")
        if (
            not isinstance(checkpoint_sha256, str)
            or archived_checkpoint != checkpoint_sha256
        ):
            errors.append(
                f"{label}: checkpoint_sha256 must match archived batch_checkpoint.jsonl"
            )
    elif lookup_enabled is False:
        if lookup_batch is not None:
            errors.append(f"{label}: disabled lookup requires lookup_batch=null")
        if transport_mode != "none" or sources != [] or selected != []:
            errors.append(
                f"{label}: disabled lookup must use transport none and empty source/selection lists"
            )
        if checkpoint_sha256 is not None or "batch_checkpoint.jsonl" in outputs:
            errors.append(f"{label}: disabled lookup cannot contain a batch checkpoint")

    count_names = (
        "reference_count",
        "cited_reference_count",
        "citation_occurrence_count",
        "lookup_attempted_count",
        "high_confidence_match_count",
        "lookup_completed_count",
        "lookup_partial_count",
        "lookup_failed_count",
    )
    if all(
        isinstance(summary.get(name), int) and not isinstance(summary.get(name), bool)
        for name in count_names
    ):
        reference_count = summary["reference_count"]
        attempted = summary["lookup_attempted_count"]
        terminal = (
            summary["lookup_completed_count"]
            + summary["lookup_partial_count"]
            + summary["lookup_failed_count"]
        )
        if summary["cited_reference_count"] > reference_count:
            errors.append(f"{label}: cited reference count exceeds reference count")
        if attempted != terminal:
            errors.append(
                f"{label}: lookup attempted count does not equal terminal batch counts"
            )
        if summary["high_confidence_match_count"] > attempted:
            errors.append(
                f"{label}: high-confidence match count exceeds attempted lookups"
            )
        if isinstance(selected, list) and attempted > len(selected):
            errors.append(f"{label}: attempted lookup count exceeds selected references")
        if lookup_enabled is False and attempted != 0:
            errors.append(f"{label}: disabled lookup cannot report attempted lookups")

    if isinstance(lookup_batch, dict):
        batch_counts = {
            name: lookup_batch.get(name)
            for name in (
                "selected_count",
                "attempted_this_run",
                "completed_count",
                "partial_count",
                "failed_count",
                "pending_count",
            )
        }
        if all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in batch_counts.values()
        ):
            selected_count = batch_counts["selected_count"]
            classified_count = sum(
                batch_counts[name]
                for name in (
                    "completed_count",
                    "partial_count",
                    "failed_count",
                    "pending_count",
                )
            )
            if isinstance(selected, list) and selected_count != len(selected):
                errors.append(
                    f"{label}: lookup_batch selected_count does not match selected references"
                )
            if classified_count != selected_count:
                errors.append(
                    f"{label}: lookup_batch outcome counts do not equal selected_count"
                )
            if batch_counts["attempted_this_run"] > selected_count:
                errors.append(
                    f"{label}: lookup_batch attempted_this_run exceeds selected_count"
                )
            for summary_name, batch_name in (
                ("lookup_completed_count", "completed_count"),
                ("lookup_partial_count", "partial_count"),
                ("lookup_failed_count", "failed_count"),
            ):
                if summary.get(summary_name) != batch_counts[batch_name]:
                    errors.append(
                        f"{label}: summary {summary_name} does not match lookup_batch {batch_name}"
                    )
            expected_status = "partial"
            if batch_counts["completed_count"] == selected_count:
                expected_status = "completed"
            elif batch_counts["pending_count"] == selected_count:
                expected_status = "pending"
            elif batch_counts["failed_count"] == selected_count:
                expected_status = "failed"
            if lookup_batch.get("batch_status") != expected_status:
                errors.append(
                    f"{label}: lookup_batch batch_status is inconsistent with outcome counts"
                )
    return errors


def verify_release(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    schema_dir = root / "schemas"
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(schema_dir.glob("*.schema.json")):
        try:
            schema = load_json(path)
            if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
                errors.append(f"{path}: unexpected JSON Schema dialect")
            if not isinstance(schema.get("$id"), str):
                errors.append(f"{path}: $id is missing")
            schemas[path.name] = schema
        except (OSError, UnicodeError, json.JSONDecodeError, SchemaValidationError) as exc:
            errors.append(f"{path}: {exc}")
    required_schemas = {
        "manifest-0.2.schema.json", "manifest-0.3.schema.json",
        "record.schema.json", "record-0.3.schema.json",
        "human-read-self-attestation-event.schema.json",
        "claim-evidence.schema.json", "external-review-packet.schema.json",
        "pdf-reference-audit-0.1.schema.json",
    }
    missing = required_schemas - set(schemas)
    if missing:
        errors.append(f"required schemas missing: {sorted(missing)!r}")
    errors.extend(_check_versions(root, schemas))
    errors.extend(_check_synthetic_benchmark(root))

    sys.path.insert(0, str(root / "src"))
    try:
        from ai_research_agent.audit import verify_run
    except ImportError as exc:
        errors.append(f"cannot import release package: {exc}")
        verify_run = None

    for manifest_path in sorted((root / "evidence").rglob("manifest.json")):
        run_dir = manifest_path.parent
        if verify_run is not None:
            result = verify_run(run_dir)
            if not result.valid:
                errors.append(f"{run_dir}: verify-run failed: {list(result.errors)!r}")
        version = None
        try:
            manifest = load_json(manifest_path)
            version = manifest.get("schema_version")
            schema_name = (
                "pdf-reference-audit-0.1.schema.json"
                if version == "pdf-reference-audit-0.1"
                else f"manifest-{version}.schema.json"
            )
            schema = schemas.get(schema_name)
            if schema is not None:
                validate_json_schema(manifest, schema)
                if version == "pdf-reference-audit-0.1":
                    errors.extend(
                        _check_pdf_audit_manifest_semantics(manifest, manifest_path)
                    )
            elif version != "0.1":
                errors.append(f"{manifest_path}: unsupported manifest schema {version!r}")
        except (OSError, UnicodeError, json.JSONDecodeError, SchemaValidationError) as exc:
            errors.append(f"{manifest_path}: {exc}")
        record_schema = (
            None
            if version == "pdf-reference-audit-0.1"
            else schemas.get(
                "record-0.3.schema.json" if version == "0.3" else "record.schema.json"
            )
        )
        archived_response_hashes: set[str] = set()
        if version == "0.3":
            for metadata_path in sorted(run_dir.glob("raw_responses/*.metadata.json")):
                try:
                    response = load_json(metadata_path).get("response")
                    digest = response.get("sha256") if isinstance(response, dict) else None
                    if isinstance(digest, str):
                        archived_response_hashes.add(digest)
                except (OSError, UnicodeError, json.JSONDecodeError, SchemaValidationError):
                    pass
        if record_schema is not None:
            records_path = run_dir / "records.jsonl"
            try:
                for number, line in enumerate(
                    records_path.read_text(encoding="utf-8").splitlines(), 1
                ):
                    record = json.loads(line)
                    validate_json_schema(
                        record, record_schema, path=f"{records_path}:{number}"
                    )
                    if version == "0.3":
                        errors.extend(
                            _check_record_semantics(
                                record,
                                f"{records_path}:{number}",
                                archived_response_hashes=archived_response_hashes,
                            )
                        )
            except (OSError, UnicodeError, json.JSONDecodeError, SchemaValidationError) as exc:
                errors.append(str(exc))
        for metadata in sorted(run_dir.glob("raw_responses/*.metadata.json")):
            errors.extend(_check_archive_redaction(metadata))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify release consistency and evidence")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors = verify_release(args.root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print("Release verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
