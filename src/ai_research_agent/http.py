from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .archive import ResponseArchive, utc_now
from .cache import HttpCache
from .replay import ReplayError, ReplayProvider


class RemoteServiceError(RuntimeError):
    pass


def _archive_url(url: str) -> str:
    """Drop opaque server-session handles before archive metadata is written."""

    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() != "webenv"
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


class JsonHttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        retries: int = 2,
        min_interval: float = 0.35,
        archive_dir: str | Path | None = None,
        archive: ResponseArchive | None = None,
        cache: HttpCache | None = None,
        offline: bool = False,
    ):
        if archive_dir is not None and archive is not None:
            raise ValueError("Pass archive_dir or archive, not both")
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.archive = archive or (
            ResponseArchive(archive_dir) if archive_dir is not None else None
        )
        if offline and cache is None:
            raise ValueError("offline replay requires a cache")
        self.cache = cache
        self.offline = offline
        self.replay = ReplayProvider(cache) if offline and cache is not None else None
        self.last_fetched_at: str | None = None
        self.last_response_sha256: str | None = None
        self._last_request_at = 0.0

    def _get_bytes(
        self,
        url: str,
        *,
        accept: str,
        max_bytes: int | None = None,
    ) -> bytes:
        if self.replay is not None:
            try:
                cached = self.replay.get_response(url, accept=accept)
            except ReplayError as exc:
                raise RemoteServiceError(str(exc)) from exc
            if max_bytes is not None and len(cached.body) > max_bytes:
                raise RemoteServiceError(
                    f"Cached response exceeds {max_bytes} byte limit"
                )
            self.last_fetched_at = cached.fetched_at
            self.last_response_sha256 = cached.response_sha256
            if self.archive is not None:
                self.archive.store(
                    url=_archive_url(url),
                    status=cached.status,
                    body=cached.body,
                    headers={"Accept": accept, "User-Agent": self.user_agent},
                    fetched_at=cached.fetched_at,
                )
            return cached.body
        cache_attempt_id = (
            self.cache.begin_attempt(method="GET", url=url, accept=accept)
            if self.cache is not None else None
        )
        last_error: Exception | None = None
        last_status: int | None = None
        for attempt in range(self.retries + 1):
            delay = self.min_interval - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            request = Request(
                url,
                headers={
                    "Accept": accept,
                    "User-Agent": self.user_agent,
                },
            )
            try:
                self._last_request_at = time.monotonic()
                with urlopen(request, timeout=self.timeout) as response:
                    payload = (
                        response.read(max_bytes + 1)
                        if max_bytes is not None
                        else response.read()
                    )
                    status = getattr(response, "status", None) or response.getcode()
                last_status = status
                fetched_at = utc_now()
                self.last_fetched_at = fetched_at
                self.last_response_sha256 = hashlib.sha256(payload).hexdigest()
                if self.archive:
                    self.archive.store(
                        url=_archive_url(url),
                        status=status,
                        body=payload,
                        headers=dict(request.header_items()),
                        fetched_at=fetched_at,
                    )
                if max_bytes is not None and len(payload) > max_bytes:
                    raise RemoteServiceError(
                        f"Response exceeds {max_bytes} byte limit (HTTP {status})"
                    )
                if self.cache is not None:
                    cached = self.cache.put_response(
                        method="GET",
                        url=url,
                        body=payload,
                        status=status,
                        fetched_at=fetched_at,
                        source=urlsplit(url).hostname or "unknown",
                        accept=accept,
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                    )
                    self.last_response_sha256 = cached.response_sha256
                    self.cache.append_checkpoint(
                        cache_attempt_id,
                        state="completed",
                        checkpoint="http_response_cached",
                        detail=f"HTTP {status}; sha256={cached.response_sha256}",
                    )
                return payload
            except HTTPError as exc:
                last_status = exc.code
                body = exc.read()
                if self.archive:
                    self.archive.store(
                        url=_archive_url(url),
                        status=exc.code,
                        body=body,
                        headers=dict(request.header_items()),
                        fetched_at=utc_now(),
                        error=f"HTTP {exc.code}",
                    )
                last_error = exc
                retryable = exc.code in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if attempt >= self.retries or not retryable:
                    if self.cache is not None:
                        self.cache.append_checkpoint(
                            cache_attempt_id,
                            state="failed",
                            checkpoint="http_request_failed",
                            detail=f"HTTP {exc.code}",
                        )
                    break
                if self.cache is not None:
                    self.cache.append_checkpoint(
                        cache_attempt_id,
                        state="partial",
                        checkpoint="http_retry_scheduled",
                        detail=f"HTTP {exc.code}; retry={attempt + 1}",
                    )
                time.sleep(0.5 * (2**attempt))
            except (URLError, TimeoutError, RemoteServiceError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    if self.cache is not None:
                        self.cache.append_checkpoint(
                            cache_attempt_id,
                            state="failed",
                            checkpoint="http_request_failed",
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    break
                if self.cache is not None:
                    self.cache.append_checkpoint(
                        cache_attempt_id,
                        state="partial",
                        checkpoint="http_retry_scheduled",
                        detail=f"{type(exc).__name__}; retry={attempt + 1}",
                    )
                time.sleep(0.5 * (2**attempt))
        status_text = f"HTTP {last_status}; " if last_status is not None else ""
        raise RemoteServiceError(
            f"GET failed ({status_text}{type(last_error).__name__}: {last_error})"
        ) from last_error

    def get_bytes(
        self,
        url: str,
        *,
        accept: str = "application/octet-stream",
        max_bytes: int | None = None,
    ) -> bytes:
        """Fetch and optionally size-limit bytes using the shared audit path."""

        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive or None")
        return self._get_bytes(url, accept=accept, max_bytes=max_bytes)

    def get_text(
        self,
        url: str,
        *,
        encoding: str = "utf-8",
        accept: str = "text/plain",
        max_bytes: int | None = None,
    ) -> str:
        """Fetch archived bytes and decode them without executing content."""

        payload = self.get_bytes(url, accept=accept, max_bytes=max_bytes)
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError as exc:
            raise RemoteServiceError(
                f"Invalid {encoding} text response for request"
            ) from exc

    def get_json(self, url: str) -> dict[str, Any]:
        payload = self.get_bytes(url, accept="application/json")
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RemoteServiceError("Invalid JSON response for request") from exc
        if not isinstance(decoded, dict):
            raise RemoteServiceError("Expected a JSON object")
        return decoded
