from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_QUERY_KEYS = {
    "api_key",
    "apikey",
    "email",
    "mailto",
    "token",
    "access_token",
}
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "user-agent",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_url(url: str) -> str:
    """Remove credentials and contact identifiers from an archived URL."""
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in _SENSITIVE_QUERY_KEYS
    ]
    hostname = parts.hostname or ""
    if parts.port:
        hostname = f"{hostname}:{parts.port}"
    return urlunsplit((parts.scheme, hostname, parts.path, urlencode(query), parts.fragment))


def redact_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Keep non-sensitive request headers only.

    User-Agent is intentionally omitted because this project permits a contact
    email inside it.
    """
    return {
        key: value
        for key, value in (headers or {}).items()
        if key.casefold() not in _SENSITIVE_HEADER_NAMES
        and "email" not in key.casefold()
        and "key" not in key.casefold()
        and "token" not in key.casefold()
    }


@dataclass(frozen=True)
class ArchivedResponse:
    metadata_path: Path
    body_path: Path
    sha256: str


class ResponseArchive:
    """Append-only archive for HTTP response bytes and sanitized metadata."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def store(
        self,
        *,
        url: str,
        status: int | None,
        body: bytes,
        headers: Mapping[str, str] | None = None,
        fetched_at: str | None = None,
        error: str | None = None,
    ) -> ArchivedResponse:
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(body).hexdigest()
        timestamp = fetched_at or utc_now()
        stamp = re.sub(r"[^0-9]", "", timestamp)[:20] or "unknown"
        base = f"{stamp}-{digest[:16]}"
        body_path = self._available_path(base, ".response")
        metadata_path = body_path.with_suffix(".metadata.json")
        body_path.write_bytes(body)
        metadata = {
            "schema_version": 1,
            "request": {
                "method": "GET",
                "url": redact_url(url),
                "headers": redact_headers(headers),
            },
            "fetched_at": timestamp,
            "http_status": status,
            "response": {
                "body_file": body_path.name,
                "byte_length": len(body),
                "sha256": digest,
            },
            "error": error,
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return ArchivedResponse(metadata_path, body_path, digest)

    def _available_path(self, base: str, suffix: str) -> Path:
        candidate = self.root / f"{base}{suffix}"
        counter = 1
        while candidate.exists():
            candidate = self.root / f"{base}-{counter}{suffix}"
            counter += 1
        return candidate
