from __future__ import annotations

import json
from typing import Any

from .cache import CachedResponse, HttpCache


class ReplayError(RuntimeError):
    """Base class for strictly offline replay failures."""


class ReplayCacheMiss(ReplayError):
    """Raised when a response is unavailable; replay never falls back to network."""


class ReplayDecodeError(ReplayError):
    """Raised when cached bytes are not valid JSON for a JSON replay."""


class ReplayProvider:
    """Offline-only provider backed exclusively by an ``HttpCache``.

    This class deliberately has no network client and no cache-miss callback.
    """

    def __init__(self, cache: HttpCache):
        self.cache = cache

    def get_response(
        self,
        url: str,
        *,
        method: str = "GET",
        accept: str = "application/json",
    ) -> CachedResponse:
        response = self.cache.get_response(method=method, url=url, accept=accept)
        if response is None:
            raise ReplayCacheMiss(
                f"Offline replay cache miss for {method.strip().upper()} request."
            )
        if response.status < 200 or response.status >= 300:
            raise ReplayError(
                f"Offline replay rejected cached HTTP {response.status} response."
            )
        return response

    def get_bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        accept: str = "application/json",
    ) -> bytes:
        return self.get_response(url, method=method, accept=accept).body

    def get_json(
        self,
        url: str,
        *,
        method: str = "GET",
        accept: str = "application/json",
    ) -> Any:
        body = self.get_bytes(url, method=method, accept=accept)
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayDecodeError("Cached response is not valid UTF-8 JSON.") from exc
