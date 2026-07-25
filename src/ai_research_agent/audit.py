from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunVerification:
    valid: bool
    checked_files: int
    errors: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_run(run_dir: str | Path) -> RunVerification:
    """Verify manifest hashes and archived-response body hashes.

    This detects later file changes. It does not prove that an upstream API
    supplied scientifically correct metadata or that a paper's claims are true.
    """
    root = Path(run_dir).resolve()
    errors: list[str] = []
    manifest_path = root / "manifest.json"
    try:
        manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return RunVerification(False, 0, (f"manifest unreadable: {exc}",))
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("output_sha256"), dict
    ):
        return RunVerification(False, 0, ("manifest output_sha256 is missing",))

    expected_files = set(manifest["output_sha256"])
    actual_files = {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    for unexpected in sorted(actual_files - expected_files):
        errors.append(f"untracked file: {unexpected}")
    for missing in sorted(expected_files - actual_files):
        errors.append(f"missing file: {missing}")

    checked = 0
    for relative, expected in sorted(manifest["output_sha256"].items()):
        if not isinstance(relative, str) or not isinstance(expected, str):
            errors.append("invalid output_sha256 entry")
            continue
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file():
            if relative not in (actual_files - expected_files):
                errors.append(f"unsafe or missing path: {relative}")
            continue
        checked += 1
        actual = _sha256(candidate)
        if actual != expected:
            errors.append(f"manifest hash mismatch: {relative}")

    for metadata_path in sorted(root.glob("raw_responses/*.metadata.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            response = metadata["response"]
            body_name = response["body_file"]
            expected_hash = response["sha256"]
            expected_length = response["byte_length"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            errors.append(f"invalid archive metadata: {metadata_path.name}: {exc}")
            continue
        if not isinstance(body_name, str) or Path(body_name).name != body_name:
            errors.append(f"unsafe archive body path: {metadata_path.name}")
            continue
        body_path = metadata_path.parent / body_name
        if not body_path.is_file():
            errors.append(f"archive body missing: {body_name}")
            continue
        if _sha256(body_path) != expected_hash:
            errors.append(f"archive body hash mismatch: {body_name}")
        if body_path.stat().st_size != expected_length:
            errors.append(f"archive body length mismatch: {body_name}")

    return RunVerification(not errors, checked, tuple(errors))
