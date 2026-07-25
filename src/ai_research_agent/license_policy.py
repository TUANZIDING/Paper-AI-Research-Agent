"""Fail-closed policy for automated handling of openly located full text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import ipaddress
import re
from typing import Any, Mapping
from urllib.parse import urlparse


class LicenseDecision(str, Enum):
    ALLOWED_METADATA_ONLY = "allowed_metadata_only"
    ALLOWED_LINK_ONLY = "allowed_link_only"
    ALLOWED_MACHINE_READ = "allowed_machine_read"
    UNKNOWN = "unknown"


class ManuscriptVersion(str, Enum):
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PUBLISHED = "published"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LicenseAssessment:
    decision: LicenseDecision
    version: ManuscriptVersion
    normalized_license: str | None
    reason: str

    @property
    def may_fetch_full_text(self) -> bool:
        """Always false until a separate DNS/IP/redirect fetch gate passes."""
        return False

    @property
    def license_allows_machine_read(self) -> bool:
        return self.decision is LicenseDecision.ALLOWED_MACHINE_READ


_VERSION_ALIASES = {
    "submitted": ManuscriptVersion.SUBMITTED,
    "submittedversion": ManuscriptVersion.SUBMITTED,
    "preprint": ManuscriptVersion.SUBMITTED,
    "accepted": ManuscriptVersion.ACCEPTED,
    "acceptedversion": ManuscriptVersion.ACCEPTED,
    "accepted manuscript": ManuscriptVersion.ACCEPTED,
    "published": ManuscriptVersion.PUBLISHED,
    "publishedversion": ManuscriptVersion.PUBLISHED,
    "version of record": ManuscriptVersion.PUBLISHED,
}

# Deliberately narrow. Licenses with NC or ND restrictions require a separate
# use-specific legal/policy review and therefore do not enter this allowlist.
_MACHINE_READABLE_LICENSES = {
    "cc by",
    "cc by 1.0",
    "cc by 2.0",
    "cc by 2.5",
    "cc by 3.0",
    "cc by 4.0",
    "cc by sa",
    "cc by sa 1.0",
    "cc by sa 2.0",
    "cc by sa 2.5",
    "cc by sa 3.0",
    "cc by sa 4.0",
    "cc0",
    "cc0 1.0",
    "public domain",
}


def _normalize_version(value: Any) -> ManuscriptVersion:
    if not isinstance(value, str):
        return ManuscriptVersion.UNKNOWN
    normalized = re.sub(r"[_\-\s]+", " ", value.strip().casefold())
    compact = normalized.replace(" ", "")
    return _VERSION_ALIASES.get(
        normalized, _VERSION_ALIASES.get(compact, ManuscriptVersion.UNKNOWN)
    )


def _safe_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
    ):
        return None
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return None
    return value


def _normalize_license(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().casefold()
    if not value:
        return None
    if value in {"implied", "unknown", "none", "null", "unspecified"}:
        return value

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc.casefold() in {
        "creativecommons.org",
        "www.creativecommons.org",
    }:
        parts = [part for part in parsed.path.casefold().split("/") if part]
        if len(parts) >= 2 and parts[0] == "publicdomain" and parts[1] == "zero":
            version = parts[2] if len(parts) >= 3 else "1.0"
            return f"cc0 {version}"
        if len(parts) >= 3 and parts[0] == "licenses":
            kind = parts[1].replace("-", " ")
            return f"cc {kind} {parts[2]}"

    normalized = re.sub(r"[_\-/\s]+", " ", value)
    normalized = normalized.replace("creative commons", "cc")
    return " ".join(normalized.split())


def assess_fulltext_license(
    *,
    location: Any = None,
    url: Any = None,
    license: Any = None,
    version: Any = None,
) -> LicenseAssessment:
    """Return the maximum permitted automation level for one OA location.

    ``location`` may be a mapping with ``url``/``url_for_pdf``, ``license``,
    and ``version`` keys. Explicit keyword values take precedence. All values
    are treated as untrusted data and only parsed; none are executed.
    """

    if location is not None and not isinstance(location, Mapping):
        return LicenseAssessment(
            LicenseDecision.UNKNOWN,
            ManuscriptVersion.UNKNOWN,
            None,
            "invalid_location_type",
        )
    if isinstance(location, Mapping):
        if url is None:
            url = location.get("url_for_pdf") or location.get("url")
        if license is None:
            license = location.get("license")
        if version is None:
            version = location.get("version")

    normalized_version = _normalize_version(version)
    normalized_license = _normalize_license(license)
    safe_url = _safe_http_url(url)

    if url is not None and safe_url is None:
        return LicenseAssessment(
            LicenseDecision.UNKNOWN,
            normalized_version,
            normalized_license,
            "invalid_or_unsafe_url",
        )
    if safe_url is None:
        if normalized_license in _MACHINE_READABLE_LICENSES:
            return LicenseAssessment(
                LicenseDecision.UNKNOWN,
                normalized_version,
                normalized_license,
                "explicit_license_but_no_fulltext_location",
            )
        return LicenseAssessment(
            LicenseDecision.ALLOWED_METADATA_ONLY,
            normalized_version,
            normalized_license,
            "no_fulltext_location",
        )
    if normalized_license in _MACHINE_READABLE_LICENSES:
        return LicenseAssessment(
            LicenseDecision.ALLOWED_MACHINE_READ,
            normalized_version,
            normalized_license,
            "explicit_allowlisted_license",
        )
    return LicenseAssessment(
        LicenseDecision.ALLOWED_LINK_ONLY,
        normalized_version,
        normalized_license,
        "missing_implied_unknown_or_restricted_license",
    )
