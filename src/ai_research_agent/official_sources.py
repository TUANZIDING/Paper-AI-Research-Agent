from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


class OfficialSourcePolicyError(ValueError):
    """An official-source candidate failed a conservative policy check."""


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{1,79}$")
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "key",
        "password",
        "signature",
        "token",
    }
)
_MAX_REGISTRATION_EVIDENCE_BYTES = 1_000_000


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_host(hostname: str) -> str:
    try:
        host = hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise OfficialSourcePolicyError("hostname is not valid IDNA") from exc
    if not host or len(host) > 253:
        raise OfficialSourcePolicyError("hostname is empty or too long")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise OfficialSourcePolicyError("local hostnames are forbidden")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise OfficialSourcePolicyError("IP-literal URLs are forbidden")
    labels = host.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or re.fullmatch(r"[a-z0-9-]+", label) is None
        for label in labels
    ):
        raise OfficialSourcePolicyError("hostname is not a registrable DNS name")
    return host


def _normalized_path(raw_path: str) -> str:
    path = unquote(raw_path or "/")
    if "\x00" in path or "\\" in path:
        raise OfficialSourcePolicyError("ambiguous URL path is forbidden")
    if any(part in {".", ".."} for part in path.split("/")):
        raise OfficialSourcePolicyError("dot segments are forbidden in source URLs")
    # Reject nested escaping of separators/dot segments. This keeps path-prefix
    # matching conservative instead of trying to emulate every origin server.
    lowered = path.casefold()
    if any(value in lowered for value in ("%2f", "%5c", "%2e")):
        raise OfficialSourcePolicyError("double-encoded path components are forbidden")
    if not path.startswith("/"):
        raise OfficialSourcePolicyError("URL path must be absolute")
    return path or "/"


def _normalize_https_url(url: str) -> tuple[str, str, str]:
    if not isinstance(url, str) or not url.strip() or len(url) > 8_192:
        raise OfficialSourcePolicyError("URL is empty or too long")
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise OfficialSourcePolicyError("URL is malformed") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise OfficialSourcePolicyError("only absolute HTTPS URLs are accepted")
    if parsed.username is not None or parsed.password is not None:
        raise OfficialSourcePolicyError("URL userinfo is forbidden")
    if port not in (None, 443):
        raise OfficialSourcePolicyError("only the default HTTPS port is accepted")
    host = _normalized_host(parsed.hostname)
    path = _normalized_path(parsed.path)
    query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    if any(key.casefold() in _SENSITIVE_QUERY_KEYS for key, _ in query):
        raise OfficialSourcePolicyError("credential-like query parameters are forbidden")
    normalized = urlunsplit(("https", host, path, urlencode(query), ""))
    return normalized, host, path


def _validate_observed_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise OfficialSourcePolicyError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OfficialSourcePolicyError("observed_at must include a timezone")
    return value


def _normalize_prefix(prefix: str) -> str:
    normalized = _normalized_path(prefix)
    return normalized if normalized == "/" else normalized.rstrip("/")


@dataclass(frozen=True)
class OfficialDomainRule:
    """A manually governed host/path rule; never learned from page content."""

    hostname: str
    path_prefixes: tuple[str, ...] = ("/",)
    include_subdomains: bool = False

    def normalized(self) -> OfficialDomainRule:
        host = _normalized_host(self.hostname)
        if not self.path_prefixes:
            raise OfficialSourcePolicyError("at least one path prefix is required")
        prefixes = tuple(sorted({_normalize_prefix(value) for value in self.path_prefixes}))
        return OfficialDomainRule(host, prefixes, bool(self.include_subdomains))

    def matches(self, *, hostname: str, path: str) -> bool:
        rule = self.normalized()
        host_matches = hostname == rule.hostname or (
            rule.include_subdomains and hostname.endswith("." + rule.hostname)
        )
        if not host_matches:
            return False
        return any(
            prefix == "/" or path == prefix or path.startswith(prefix + "/")
            for prefix in rule.path_prefixes
        )


@dataclass(frozen=True)
class OfficialOrganizationRegistration:
    organization_id: str
    organization_name: str
    rules: tuple[OfficialDomainRule, ...]
    registrar_role: str
    registration_evidence_source_url: str
    registration_evidence_sha256: str
    registration_evidence_byte_length: int
    evidence_observed_at: str
    registration_status: str = "manual_allowlist_candidate"
    registrar_identity_verified: bool = False
    final_official_status_verified: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def sha256(self) -> str:
        return _sha256(_canonical_bytes(self.to_dict()))


def create_manual_registration(
    *,
    organization_id: str,
    organization_name: str,
    rules: Iterable[OfficialDomainRule],
    registrar_role: str,
    evidence_source_url: str,
    evidence_bytes: bytes,
    evidence_observed_at: str,
) -> OfficialOrganizationRegistration:
    """Create an auditable allowlist candidate from separately supplied evidence.

    This records an operator decision; it does not authenticate that operator or
    independently prove that an organization controls a domain.
    """

    normalized_id = organization_id.strip().casefold()
    if _IDENTIFIER.fullmatch(normalized_id) is None:
        raise OfficialSourcePolicyError("organization_id has an invalid format")
    name = " ".join(organization_name.split())
    role = " ".join(registrar_role.split())
    if not name or len(name) > 300 or not role or len(role) > 160:
        raise OfficialSourcePolicyError("organization name and registrar role are required")
    if not isinstance(evidence_bytes, bytes) or not (
        1 <= len(evidence_bytes) <= _MAX_REGISTRATION_EVIDENCE_BYTES
    ):
        raise OfficialSourcePolicyError("registration evidence bytes are missing or too large")
    source_url, _, _ = _normalize_https_url(evidence_source_url)
    normalized_rules = tuple(
        sorted(
            {rule.normalized() for rule in rules},
            key=lambda item: (item.hostname, item.path_prefixes, item.include_subdomains),
        )
    )
    if not normalized_rules:
        raise OfficialSourcePolicyError("at least one domain rule is required")
    return OfficialOrganizationRegistration(
        organization_id=normalized_id,
        organization_name=name,
        rules=normalized_rules,
        registrar_role=role,
        registration_evidence_source_url=source_url,
        registration_evidence_sha256=_sha256(evidence_bytes),
        registration_evidence_byte_length=len(evidence_bytes),
        evidence_observed_at=_validate_observed_at(evidence_observed_at),
    )


@dataclass(frozen=True)
class OfficialSourceCandidate:
    schema_version: int
    organization_id: str
    organization_name: str
    title: str
    normalized_url: str
    url_sha256: str
    matched_hostname: str
    matched_path_prefix: str
    registration_sha256: str
    registration_evidence_source_url: str
    registration_evidence_sha256: str
    evidence_observed_at: str
    candidate_status: str
    network_fetch_permitted: bool
    official_identity_verified: bool
    human_read_confirmed: bool
    final_legal_judgment: bool
    limitations: tuple[str, ...]
    evidence_id: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def expected_evidence_id(self) -> str:
        payload = self.to_dict()
        payload.pop("evidence_id", None)
        return "official-source:" + _sha256(_canonical_bytes(payload))


class OfficialSourceRegistry:
    """Explicit trust-root registry for official-source URL candidates.

    The registry never accepts organization claims extracted from a target page.
    It also performs no network request, so candidate creation cannot be an SSRF
    sink. A later fetcher must independently resolve, classify, and pin DNS.
    """

    def __init__(self, registrations: Iterable[OfficialOrganizationRegistration]):
        entries: dict[str, OfficialOrganizationRegistration] = {}
        for registration in registrations:
            if registration.organization_id in entries:
                raise OfficialSourcePolicyError("duplicate organization registration")
            entries[registration.organization_id] = registration
        self._entries = entries

    @property
    def snapshot_sha256(self) -> str:
        payload = {
            key: self._entries[key].to_dict() for key in sorted(self._entries)
        }
        return _sha256(_canonical_bytes(payload))

    def propose(
        self, *, organization_id: str, url: str, title: str
    ) -> OfficialSourceCandidate:
        key = organization_id.strip().casefold()
        registration = self._entries.get(key)
        if registration is None:
            raise OfficialSourcePolicyError("organization is not in the explicit registry")
        normalized_url, host, path = _normalize_https_url(url)
        matched_rule: OfficialDomainRule | None = None
        matched_prefix: str | None = None
        for rule in registration.rules:
            if not rule.matches(hostname=host, path=path):
                continue
            matched_rule = rule.normalized()
            matched_prefix = next(
                prefix
                for prefix in matched_rule.path_prefixes
                if prefix == "/" or path == prefix or path.startswith(prefix + "/")
            )
            break
        if matched_rule is None or matched_prefix is None:
            raise OfficialSourcePolicyError(
                "URL is outside the organization's registered host/path allowlist"
            )
        clean_title = " ".join(title.split())
        if not clean_title or len(clean_title) > 500:
            raise OfficialSourcePolicyError("candidate title is missing or too long")
        base: dict[str, object] = {
            "schema_version": 1,
            "organization_id": registration.organization_id,
            "organization_name": registration.organization_name,
            "title": clean_title,
            "normalized_url": normalized_url,
            "url_sha256": _sha256(normalized_url.encode("utf-8")),
            "matched_hostname": host,
            "matched_path_prefix": matched_prefix,
            "registration_sha256": registration.sha256,
            "registration_evidence_source_url": registration.registration_evidence_source_url,
            "registration_evidence_sha256": registration.registration_evidence_sha256,
            "evidence_observed_at": registration.evidence_observed_at,
            "candidate_status": "registered_official_source_candidate",
            "network_fetch_permitted": False,
            "official_identity_verified": False,
            "human_read_confirmed": False,
            "final_legal_judgment": False,
            "limitations": (
                "Manual registration is a trust-root candidate, not independent domain-control proof.",
                "No network request or DNS validation was performed.",
                "Candidate status does not establish legal permission, currentness, or human reading.",
            ),
        }
        evidence_id = "official-source:" + _sha256(_canonical_bytes(base))
        return OfficialSourceCandidate(**base, evidence_id=evidence_id)


@dataclass(frozen=True)
class SavedOfficialSourceEvidence:
    path: Path
    sha256: str


def _atomic_create(path: Path, payload: bytes, *, prefix: str) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=prefix, delete=False
        ) as stream:
            temporary_name = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary = Path(temporary_name)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise OfficialSourcePolicyError("evidence artifact already exists") from exc
        temporary.unlink()
        temporary_name = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def save_registration_evidence_bundle(
    registration: OfficialOrganizationRegistration,
    evidence_bytes: bytes,
    directory: str | Path,
) -> SavedOfficialSourceEvidence:
    """Freeze the manual registry record and the exact evidence bytes it hashes."""

    if (
        not isinstance(evidence_bytes, bytes)
        or len(evidence_bytes) != registration.registration_evidence_byte_length
        or _sha256(evidence_bytes) != registration.registration_evidence_sha256
    ):
        raise OfficialSourcePolicyError("registration evidence bytes do not match the record")
    root = Path(directory)
    if not root.is_absolute():
        raise OfficialSourcePolicyError("evidence directory must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    envelope: dict[str, object] = {
        "schema_version": 1,
        "artifact_type": "manual_official_source_registration_evidence",
        "registration": registration.to_dict(),
        "registration_sha256": registration.sha256,
        "evidence_encoding": "base64",
        "evidence_bytes": base64.b64encode(evidence_bytes).decode("ascii"),
    }
    payload = _canonical_bytes(envelope) + b"\n"
    digest = _sha256(payload)
    destination = root / f"official-registration-{registration.sha256}.json"
    _atomic_create(destination, payload, prefix=".official-registration.")
    return SavedOfficialSourceEvidence(destination, digest)


def load_registration_evidence_bundle(
    path: str | Path,
) -> OfficialOrganizationRegistration:
    """Load and verify a frozen manual registration bundle.

    Loading proves only that the embedded bytes and registration are internally
    consistent. It does not verify the registrar's identity or domain control.
    """

    source = Path(path)
    try:
        envelope = json.loads(source.read_text(encoding="utf-8"))
        if (
            envelope.get("schema_version") != 1
            or envelope.get("artifact_type")
            != "manual_official_source_registration_evidence"
            or envelope.get("evidence_encoding") != "base64"
        ):
            raise OfficialSourcePolicyError("registration bundle has an unsupported contract")
        registration_data = envelope["registration"]
        if not isinstance(registration_data, dict):
            raise OfficialSourcePolicyError("registration bundle record is malformed")
        rules_data = registration_data["rules"]
        if not isinstance(rules_data, list):
            raise OfficialSourcePolicyError("registration bundle rules are malformed")
        rules = tuple(
            OfficialDomainRule(
                hostname=str(rule["hostname"]),
                path_prefixes=tuple(str(value) for value in rule["path_prefixes"]),
                include_subdomains=bool(rule["include_subdomains"]),
            )
            for rule in rules_data
        )
        registration = OfficialOrganizationRegistration(
            organization_id=str(registration_data["organization_id"]),
            organization_name=str(registration_data["organization_name"]),
            rules=rules,
            registrar_role=str(registration_data["registrar_role"]),
            registration_evidence_source_url=str(
                registration_data["registration_evidence_source_url"]
            ),
            registration_evidence_sha256=str(
                registration_data["registration_evidence_sha256"]
            ),
            registration_evidence_byte_length=int(
                registration_data["registration_evidence_byte_length"]
            ),
            evidence_observed_at=str(registration_data["evidence_observed_at"]),
            registration_status=str(registration_data["registration_status"]),
            registrar_identity_verified=bool(
                registration_data["registrar_identity_verified"]
            ),
            final_official_status_verified=bool(
                registration_data["final_official_status_verified"]
            ),
        )
        evidence_bytes = base64.b64decode(
            str(envelope["evidence_bytes"]), validate=True
        )
    except OfficialSourcePolicyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise OfficialSourcePolicyError("registration bundle is unreadable or malformed") from exc
    if _canonical_bytes(registration.to_dict()) != _canonical_bytes(registration_data):
        raise OfficialSourcePolicyError("registration bundle is not canonical")
    if envelope.get("registration_sha256") != registration.sha256:
        raise OfficialSourcePolicyError("registration bundle hash is inconsistent")
    if (
        len(evidence_bytes) != registration.registration_evidence_byte_length
        or _sha256(evidence_bytes) != registration.registration_evidence_sha256
    ):
        raise OfficialSourcePolicyError("registration evidence bytes do not match the record")
    return registration


def save_candidate_evidence(
    candidate: OfficialSourceCandidate, directory: str | Path
) -> SavedOfficialSourceEvidence:
    """Persist canonical candidate evidence without overwriting an existing file."""

    if (
        re.fullmatch(r"official-source:[0-9a-f]{64}", candidate.evidence_id) is None
        or candidate.evidence_id != candidate.expected_evidence_id()
    ):
        raise OfficialSourcePolicyError("candidate evidence_id is invalid or inconsistent")
    root = Path(directory)
    if not root.is_absolute():
        raise OfficialSourcePolicyError("evidence directory must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(candidate.to_dict()) + b"\n"
    digest = _sha256(payload)
    destination = root / f"{candidate.evidence_id.replace(':', '-')}.json"
    try:
        _atomic_create(destination, payload, prefix=".official-source.")
    except OfficialSourcePolicyError as exc:
        raise OfficialSourcePolicyError("candidate evidence already exists") from exc
    return SavedOfficialSourceEvidence(destination, digest)


def verify_saved_candidate_evidence(
    path: str | Path, *, expected_sha256: str
) -> bool:
    candidate_path = Path(path)
    if not candidate_path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return False
    return _sha256(candidate_path.read_bytes()) == expected_sha256
