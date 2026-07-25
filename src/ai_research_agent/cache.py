from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = 1
ATTEMPT_STATES = frozenset({"completed", "partial", "failed"})

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth_token",
        "email",
        "key",
        "token",
        "access_token",
        "refresh_token",
    }
)
_OPAQUE_IDENTITY_QUERY_KEYS = frozenset({"webenv"})


class CacheError(RuntimeError):
    """Base error for the persistent HTTP cache."""


class CacheIntegrityError(CacheError):
    """Raised when stored bytes no longer match their recorded digest."""


class CacheSchemaError(CacheError):
    """Raised when a database schema cannot safely be opened or migrated."""


@dataclass(frozen=True)
class NormalizedRequest:
    method: str
    sanitized_url: str
    accept: str
    request_key: str


@dataclass(frozen=True)
class CachedResponse:
    request: NormalizedRequest
    body: bytes
    response_sha256: str
    status: int
    fetched_at: str
    source: str
    etag: str | None = None
    last_modified: str | None = None
    response_id: int | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_request(
    method: str,
    url: str,
    accept: str = "application/json",
) -> NormalizedRequest:
    normalized_method = method.strip().upper()
    if not normalized_method or any(character.isspace() for character in normalized_method):
        raise ValueError("method must be a non-empty HTTP token")
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be an absolute HTTP(S) URL")

    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    safe_query: list[tuple[str, str]] = []
    opaque_identity: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.strip().casefold().replace("-", "_")
        if normalized_key in _OPAQUE_IDENTITY_QUERY_KEYS:
            opaque_identity.append(
                (normalized_key, hashlib.sha256(value.encode("utf-8")).hexdigest())
            )
            continue
        if _is_sensitive_query_key(normalized_key):
            continue
        safe_query.append((key, value))
    safe_query.sort(key=lambda item: (item[0].casefold(), item[0], item[1]))
    path = parsed.path or "/"
    sanitized_url = urlunsplit(
        (scheme, host, path, urlencode(safe_query, doseq=True), "")
    )
    normalized_accept = ",".join(
        part.strip().casefold() for part in accept.split(",") if part.strip()
    )
    if not normalized_accept:
        normalized_accept = "*/*"
    opaque_identity.sort()
    opaque_marker = "&".join(f"{key}={digest}" for key, digest in opaque_identity)
    identity = "\x1f".join(
        (normalized_method, sanitized_url, normalized_accept, opaque_marker)
    ).encode("utf-8")
    request_key = hashlib.sha256(identity).hexdigest()
    return NormalizedRequest(
        method=normalized_method,
        sanitized_url=sanitized_url,
        accept=normalized_accept,
        request_key=request_key,
    )


def make_request_key(
    method: str,
    url: str,
    accept: str = "application/json",
) -> str:
    return normalize_request(method, url, accept).request_key


def _is_sensitive_query_key(key: str) -> bool:
    return (
        key in _SENSITIVE_QUERY_KEYS
        or "token" in key
        or key.endswith("_api_key")
        or key.endswith("_email")
    )


class HttpCache:
    """Concurrent-safe, content-addressed SQLite cache.

    Connections are short lived so the object may be shared by threads. Writes
    use ``BEGIN IMMEDIATE`` and the database runs in WAL mode.
    """

    def __init__(self, database: str | Path, *, busy_timeout_ms: int = 5000):
        self.database = Path(database)
        self.busy_timeout_ms = busy_timeout_ms
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise CacheSchemaError(
                        f"Cache schema {version} is newer than supported {SCHEMA_VERSION}."
                    )
                if version == 0:
                    self._migrate_zero_to_one(connection)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                stored = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                if stored is None or int(stored[0]) != SCHEMA_VERSION:
                    raise CacheSchemaError("schema_meta version does not match user_version")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _migrate_zero_to_one(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')",
            """
            CREATE TABLE cache_requests (
                request_key TEXT PRIMARY KEY,
                method TEXT NOT NULL,
                sanitized_url TEXT NOT NULL,
                accept TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE response_blobs (
                sha256 TEXT PRIMARY KEY,
                body BLOB NOT NULL,
                byte_length INTEGER NOT NULL CHECK (byte_length >= 0)
            )
            """,
            """
            CREATE TABLE response_records (
                response_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_key TEXT NOT NULL REFERENCES cache_requests(request_key),
                response_sha256 TEXT NOT NULL REFERENCES response_blobs(sha256),
                status INTEGER NOT NULL CHECK (status BETWEEN 100 AND 599),
                fetched_at TEXT NOT NULL,
                source TEXT NOT NULL,
                etag TEXT,
                last_modified TEXT
            )
            """,
            """
            CREATE INDEX response_request_latest
                ON response_records(request_key, response_id DESC)
            """,
            """
            CREATE TABLE request_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_key TEXT NOT NULL REFERENCES cache_requests(request_key),
                started_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE request_checkpoints (
                checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES request_attempts(attempt_id),
                state TEXT NOT NULL CHECK (state IN ('completed', 'partial', 'failed')),
                checkpoint TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                detail TEXT
            )
            """,
            """
            CREATE TRIGGER response_records_no_update
                BEFORE UPDATE ON response_records BEGIN
                SELECT RAISE(ABORT, 'response_records are append-only'); END
            """,
            """
            CREATE TRIGGER response_records_no_delete
                BEFORE DELETE ON response_records BEGIN
                SELECT RAISE(ABORT, 'response_records are append-only'); END
            """,
            """
            CREATE TRIGGER request_attempts_no_update
                BEFORE UPDATE ON request_attempts BEGIN
                SELECT RAISE(ABORT, 'request_attempts are append-only'); END
            """,
            """
            CREATE TRIGGER request_attempts_no_delete
                BEFORE DELETE ON request_attempts BEGIN
                SELECT RAISE(ABORT, 'request_attempts are append-only'); END
            """,
            """
            CREATE TRIGGER request_checkpoints_no_update
                BEFORE UPDATE ON request_checkpoints BEGIN
                SELECT RAISE(ABORT, 'request_checkpoints are append-only'); END
            """,
            """
            CREATE TRIGGER request_checkpoints_no_delete
                BEFORE DELETE ON request_checkpoints BEGIN
                SELECT RAISE(ABORT, 'request_checkpoints are append-only'); END
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def put_response(
        self,
        *,
        method: str,
        url: str,
        body: bytes,
        status: int,
        fetched_at: str,
        source: str,
        accept: str = "application/json",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> CachedResponse:
        request = normalize_request(method, url, accept)
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        if status < 100 or status > 599:
            raise ValueError("status must be an HTTP status code")
        if not fetched_at.strip():
            raise ValueError("fetched_at is required")
        if not source.strip():
            raise ValueError("source is required")
        digest = hashlib.sha256(body).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_request(connection, request)
                connection.execute(
                    "INSERT OR IGNORE INTO response_blobs(sha256, body, byte_length) "
                    "VALUES (?, ?, ?)",
                    (digest, body, len(body)),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO response_records(
                        request_key, response_sha256, status, fetched_at,
                        source, etag, last_modified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.request_key,
                        digest,
                        status,
                        fetched_at,
                        source.strip(),
                        etag,
                        last_modified,
                    ),
                )
                response_id = int(cursor.lastrowid)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return CachedResponse(
            request=request,
            body=body,
            response_sha256=digest,
            status=status,
            fetched_at=fetched_at,
            source=source.strip(),
            etag=etag,
            last_modified=last_modified,
            response_id=response_id,
        )

    def get_response(
        self,
        *,
        method: str,
        url: str,
        accept: str = "application/json",
    ) -> CachedResponse | None:
        request = normalize_request(method, url, accept)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT r.response_id, r.response_sha256, r.status, r.fetched_at,
                       r.source, r.etag, r.last_modified, b.body, b.byte_length
                FROM response_records AS r
                JOIN response_blobs AS b ON b.sha256 = r.response_sha256
                WHERE r.request_key = ?
                ORDER BY r.response_id DESC
                LIMIT 1
                """,
                (request.request_key,),
            ).fetchone()
        if row is None:
            return None
        body = bytes(row["body"])
        actual = hashlib.sha256(body).hexdigest()
        if actual != row["response_sha256"] or len(body) != row["byte_length"]:
            raise CacheIntegrityError(
                f"Cached response integrity check failed for {request.request_key}."
            )
        return CachedResponse(
            request=request,
            body=body,
            response_sha256=row["response_sha256"],
            status=int(row["status"]),
            fetched_at=row["fetched_at"],
            source=row["source"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            response_id=int(row["response_id"]),
        )

    def begin_attempt(
        self,
        *,
        method: str,
        url: str,
        accept: str = "application/json",
        started_at: str | None = None,
    ) -> int:
        request = normalize_request(method, url, accept)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_request(connection, request)
                cursor = connection.execute(
                    "INSERT INTO request_attempts(request_key, started_at) VALUES (?, ?)",
                    (request.request_key, started_at or utc_now()),
                )
                attempt_id = int(cursor.lastrowid)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return attempt_id

    def append_checkpoint(
        self,
        attempt_id: int,
        *,
        state: str,
        checkpoint: str,
        detail: str | None = None,
        recorded_at: str | None = None,
    ) -> int:
        if state not in ATTEMPT_STATES:
            raise ValueError(f"state must be one of {sorted(ATTEMPT_STATES)}")
        if not checkpoint.strip():
            raise ValueError("checkpoint is required")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO request_checkpoints(
                        attempt_id, state, checkpoint, recorded_at, detail
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        state,
                        checkpoint.strip(),
                        recorded_at or utc_now(),
                        detail,
                    ),
                )
                checkpoint_id = int(cursor.lastrowid)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise CacheError(f"Unknown attempt_id {attempt_id}") from exc
            except Exception:
                connection.rollback()
                raise
        return checkpoint_id

    def record_attempt(
        self,
        *,
        method: str,
        url: str,
        state: str,
        checkpoint: str,
        accept: str = "application/json",
        detail: str | None = None,
        recorded_at: str | None = None,
    ) -> int:
        attempt_id = self.begin_attempt(
            method=method,
            url=url,
            accept=accept,
            started_at=recorded_at,
        )
        self.append_checkpoint(
            attempt_id,
            state=state,
            checkpoint=checkpoint,
            detail=detail,
            recorded_at=recorded_at,
        )
        return attempt_id

    def list_checkpoints(self, attempt_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT checkpoint_id, attempt_id, state, checkpoint, recorded_at, detail
                FROM request_checkpoints
                WHERE attempt_id = ?
                ORDER BY checkpoint_id
                """,
                (attempt_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _ensure_request(
        connection: sqlite3.Connection, request: NormalizedRequest
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO cache_requests(
                request_key, method, sanitized_url, accept, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                request.request_key,
                request.method,
                request.sanitized_url,
                request.accept,
                utc_now(),
            ),
        )
        existing = connection.execute(
            """
            SELECT method, sanitized_url, accept FROM cache_requests
            WHERE request_key = ?
            """,
            (request.request_key,),
        ).fetchone()
        if existing is None or (
            existing["method"], existing["sanitized_url"], existing["accept"]
        ) != (request.method, request.sanitized_url, request.accept):
            raise CacheIntegrityError("Request-key collision or request metadata mismatch.")
