"""Conservative Crossmark/Crossref status adapter.

Crossmark metadata is publicly exposed through Crossref's REST metadata, but
Crossref does not document a separate structured Crossmark status API for this
use case.  This module therefore distinguishes a configurable normalized
provider from the public Crossref REST fallback and never labels either route
as an independently verified live Crossmark dialog check.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Protocol, Sequence
from urllib.parse import quote, urlencode

from .archive import utc_now
from .http import JsonHttpClient, RemoteServiceError
from .models import normalize_doi


class CrossmarkStatus(str, Enum):
    RETRACTED = "retracted"
    CORRECTION = "correction"
    EXPRESSION_OF_CONCERN = "expression_of_concern"
    UNKNOWN = "unknown"


class ResponseKind(str, Enum):
    NORMALIZED_PROVIDER = "normalized_provider"
    CROSSREF_WORK = "crossref_work"
    CROSSREF_UPDATES = "crossref_updates"


class CoverageRoute(str, Enum):
    CONFIGURED_PROVIDER = "configured_provider"
    CROSSREF_PUBLIC_REST_FALLBACK = "crossref_public_rest_fallback"
    NONE = "none"


@dataclass(frozen=True)
class ProviderResponse:
    provider: str
    kind: ResponseKind
    url: str
    status_code: int
    body: bytes
    retrieved_at: str


@dataclass(frozen=True)
class CrossmarkEvidence:
    source: str
    target_doi: str
    url: str
    retrieved_at: str
    response_sha256: str
    evidence_id: str


@dataclass(frozen=True)
class CrossmarkAssessment:
    target_doi: str | None
    status: CrossmarkStatus
    complete: bool
    route: CoverageRoute
    provider_name: str | None
    fallback_used: bool
    crossmark_realtime_verified: bool
    crossmark_participation_declared: bool | None
    signals: tuple[str, ...]
    evidence: tuple[CrossmarkEvidence, ...]
    errors: tuple[str, ...]


class StatusProvider(Protocol):
    name: str

    def fetch(self, doi: str) -> Sequence[ProviderResponse]: ...


class CrossmarkAdapterError(RemoteServiceError):
    pass


class CrossrefPublicProvider:
    """Fetch public Crossref work and reverse-update JSON without scraping."""

    name = "crossref_public_rest"
    endpoint = "https://api.crossref.org/v1/works"

    def __init__(self, client: JsonHttpClient, mailto: str | None = None):
        self.client = client
        self.mailto = mailto

    def fetch(self, doi: str) -> Sequence[ProviderResponse]:
        target = normalize_doi(doi)
        if not target:
            raise ValueError("A valid DOI is required")
        common = {"mailto": self.mailto} if self.mailto else {}
        suffix = f"?{urlencode(common)}" if common else ""
        work_url = f"{self.endpoint}/{quote(target, safe='')}{suffix}"
        update_params = {"filter": f"updates:{target}", "rows": "100"}
        if self.mailto:
            update_params["mailto"] = self.mailto
        updates_url = f"{self.endpoint}?{urlencode(update_params)}"
        responses: list[ProviderResponse] = []
        for kind, url in (
            (ResponseKind.CROSSREF_WORK, work_url),
            (ResponseKind.CROSSREF_UPDATES, updates_url),
        ):
            body = self.client.get_bytes(
                url, accept="application/json", max_bytes=5_000_000
            )
            responses.append(
                ProviderResponse(
                    provider=self.name,
                    kind=kind,
                    url=url,
                    status_code=200,
                    body=body,
                    retrieved_at=(
                        getattr(self.client, "last_fetched_at", None) or utc_now()
                    ),
                )
            )
        return tuple(responses)


def assess_crossmark(
    doi: str,
    *,
    provider: StatusProvider | None = None,
    crossref_fallback: StatusProvider | None = None,
) -> CrossmarkAssessment:
    """Assess controlled post-publication signals with fail-closed routing."""

    target = normalize_doi(doi)
    if not target:
        return _failed(None, ["invalid DOI"])
    errors: list[str] = []
    if provider is not None:
        try:
            return _assess_responses(
                target,
                provider.fetch(target),
                route=CoverageRoute.CONFIGURED_PROVIDER,
                provider_name=provider.name,
                fallback_used=False,
                prior_errors=(),
            )
        except (CrossmarkAdapterError, RemoteServiceError, ValueError, TypeError, OSError, TimeoutError) as exc:
            errors.append(f"configured provider {provider.name}: {exc}")
    if crossref_fallback is not None:
        try:
            return _assess_responses(
                target,
                crossref_fallback.fetch(target),
                route=CoverageRoute.CROSSREF_PUBLIC_REST_FALLBACK,
                provider_name=crossref_fallback.name,
                fallback_used=provider is not None,
                prior_errors=tuple(errors),
            )
        except (CrossmarkAdapterError, RemoteServiceError, ValueError, TypeError, OSError, TimeoutError) as exc:
            errors.append(f"Crossref fallback {crossref_fallback.name}: {exc}")
    if not errors:
        errors.append("no configured provider or Crossref fallback")
    return _failed(target, errors)


def _assess_responses(
    target: str,
    responses: Sequence[ProviderResponse],
    *,
    route: CoverageRoute,
    provider_name: str,
    fallback_used: bool,
    prior_errors: tuple[str, ...],
) -> CrossmarkAssessment:
    if not isinstance(responses, (list, tuple)) or not responses:
        raise CrossmarkAdapterError("provider returned no responses")
    signals: list[tuple[CrossmarkStatus, str]] = []
    evidence: list[CrossmarkEvidence] = []
    participation: bool | None = None
    kinds: set[ResponseKind] = set()
    for response in responses:
        if not isinstance(response, ProviderResponse):
            raise CrossmarkAdapterError("provider returned an invalid response object")
        if response.provider != provider_name:
            raise CrossmarkAdapterError("response provider identity mismatch")
        if response.status_code < 200 or response.status_code >= 300:
            raise CrossmarkAdapterError(
                f"non-success HTTP status {response.status_code}"
            )
        if len(response.body) > 5_000_000:
            raise CrossmarkAdapterError("response exceeds 5000000 byte limit")
        evidence.append(_evidence(target, response))
        kinds.add(response.kind)
        payload = _json_object(response.body)
        if response.kind is ResponseKind.NORMALIZED_PROVIDER:
            signals.extend(_parse_normalized(payload, target))
        elif response.kind is ResponseKind.CROSSREF_WORK:
            parsed_signals, declared = _parse_crossref_work(payload, target)
            signals.extend(parsed_signals)
            participation = declared
        elif response.kind is ResponseKind.CROSSREF_UPDATES:
            signals.extend(_parse_crossref_updates(payload, target))
        else:  # pragma: no cover - Enum exhaustiveness guard
            raise CrossmarkAdapterError("unsupported response kind")
    if route is CoverageRoute.CROSSREF_PUBLIC_REST_FALLBACK and kinds != {
        ResponseKind.CROSSREF_WORK,
        ResponseKind.CROSSREF_UPDATES,
    }:
        raise CrossmarkAdapterError(
            "Crossref fallback requires both work and reverse-update responses"
        )
    if route is CoverageRoute.CONFIGURED_PROVIDER and kinds != {
        ResponseKind.NORMALIZED_PROVIDER
    }:
        raise CrossmarkAdapterError(
            "configured provider must use the normalized provider contract"
        )
    status = _highest_status(status for status, _ in signals)
    selected_signals = tuple(
        dict.fromkeys(signal for signal_status, signal in signals if signal_status is status)
    ) if status is not CrossmarkStatus.UNKNOWN else ()
    return CrossmarkAssessment(
        target_doi=target,
        status=status,
        complete=True,
        route=route,
        provider_name=provider_name,
        fallback_used=fallback_used,
        # No documented independent structured Crossmark API was called.
        crossmark_realtime_verified=False,
        crossmark_participation_declared=participation,
        signals=selected_signals,
        evidence=tuple(evidence),
        errors=prior_errors,
    )


def _parse_normalized(
    payload: dict[str, Any], target: str
) -> list[tuple[CrossmarkStatus, str]]:
    declared_target = normalize_doi(payload.get("target_doi"))
    if declared_target != target:
        raise CrossmarkAdapterError("normalized provider target DOI mismatch")
    updates = payload.get("updates")
    if not isinstance(updates, list):
        raise CrossmarkAdapterError("normalized provider lacks updates list")
    signals: list[tuple[CrossmarkStatus, str]] = []
    for update in updates:
        if not isinstance(update, dict):
            raise CrossmarkAdapterError("normalized provider update is not an object")
        if normalize_doi(update.get("target_doi")) != target:
            raise CrossmarkAdapterError("normalized provider update target mismatch")
        status = _controlled_status(update.get("type"))
        if status:
            signals.append((status, f"configured_provider:update:{status.value}"))
    return signals


def _parse_crossref_work(
    payload: dict[str, Any], target: str
) -> tuple[list[tuple[CrossmarkStatus, str]], bool]:
    message = payload.get("message")
    if not isinstance(message, dict):
        raise CrossmarkAdapterError("Crossref work response lacks message")
    if normalize_doi(message.get("DOI")) != target:
        raise CrossmarkAdapterError("Crossref work target DOI mismatch")
    signals: list[tuple[CrossmarkStatus, str]] = []
    relation = message.get("relation")
    if isinstance(relation, dict):
        relation_map = {
            "is-retracted-by": CrossmarkStatus.RETRACTED,
            "is-corrected-by": CrossmarkStatus.CORRECTION,
            "is-expression-of-concern-by": CrossmarkStatus.EXPRESSION_OF_CONCERN,
        }
        for key, relation_value in relation.items():
            normalized = str(key).strip().casefold().replace("_", "-")
            status = relation_map.get(normalized)
            if status and isinstance(relation_value, list) and relation_value:
                signals.append((status, f"crossref:relation:{status.value}"))
    declared = bool(message.get("update-policy"))
    return signals, declared


def _parse_crossref_updates(
    payload: dict[str, Any], target: str
) -> list[tuple[CrossmarkStatus, str]]:
    message = payload.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("items"), list):
        raise CrossmarkAdapterError("Crossref updates response lacks message.items")
    items = message["items"]
    try:
        total = int(message.get("total-results", len(items)))
    except (TypeError, ValueError) as exc:
        raise CrossmarkAdapterError("Crossref updates has invalid total-results") from exc
    if total != len(items):
        raise CrossmarkAdapterError(
            f"Crossref updates incomplete: returned {len(items)} of {total}"
        )
    signals: list[tuple[CrossmarkStatus, str]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("update-to"), list):
            raise CrossmarkAdapterError("Crossref update item lacks update-to")
        matched = [
            update for update in item["update-to"]
            if isinstance(update, dict)
            and normalize_doi(update.get("DOI")) == target
        ]
        if not matched:
            raise CrossmarkAdapterError("Crossref update item target DOI mismatch")
        for update in matched:
            status = _controlled_status(update.get("type"))
            if status:
                signals.append((status, f"crossref:update-to:{status.value}"))
    return signals


def _controlled_status(value: Any) -> CrossmarkStatus | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[-_\s]+", "_", value.strip().casefold())
    if normalized in {"retraction", "retracted", "partial_retraction", "withdrawal", "removal"}:
        return CrossmarkStatus.RETRACTED
    if normalized in {"correction", "corrigendum", "erratum"}:
        return CrossmarkStatus.CORRECTION
    if normalized == "expression_of_concern":
        return CrossmarkStatus.EXPRESSION_OF_CONCERN
    return None


def _highest_status(values: Any) -> CrossmarkStatus:
    statuses = set(values)
    for status in (
        CrossmarkStatus.RETRACTED,
        CrossmarkStatus.EXPRESSION_OF_CONCERN,
        CrossmarkStatus.CORRECTION,
    ):
        if status in statuses:
            return status
    return CrossmarkStatus.UNKNOWN


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CrossmarkAdapterError("invalid JSON response") from exc
    if not isinstance(value, dict):
        raise CrossmarkAdapterError("JSON response is not an object")
    return value


def _evidence(target: str, response: ProviderResponse) -> CrossmarkEvidence:
    digest = hashlib.sha256(response.body).hexdigest()
    identity = "\x1f".join(
        (response.provider, response.kind.value, target, response.url, digest)
    )
    return CrossmarkEvidence(
        source=response.provider,
        target_doi=target,
        url=response.url,
        retrieved_at=response.retrieved_at,
        response_sha256=digest,
        evidence_id="ev-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
    )


def _failed(target: str | None, errors: list[str]) -> CrossmarkAssessment:
    return CrossmarkAssessment(
        target_doi=target,
        status=CrossmarkStatus.UNKNOWN,
        complete=False,
        route=CoverageRoute.NONE,
        provider_name=None,
        fallback_used=False,
        crossmark_realtime_verified=False,
        crossmark_participation_declared=None,
        signals=(),
        evidence=(),
        errors=tuple(errors),
    )
