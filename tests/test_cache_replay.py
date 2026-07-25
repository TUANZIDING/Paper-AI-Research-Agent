import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest

from ai_research_agent.cache import (
    CacheIntegrityError,
    CacheSchemaError,
    HttpCache,
    SCHEMA_VERSION,
    make_request_key,
    normalize_request,
)
from ai_research_agent.replay import ReplayCacheMiss, ReplayProvider
from ai_research_agent.http import JsonHttpClient


class CacheReplayTests(unittest.TestCase):
    def test_webenv_is_not_persisted_but_distinguishes_request_keys(self):
        from ai_research_agent.cache import normalize_request

        first = normalize_request(
            "GET", "https://eutils.ncbi.nlm.nih.gov/x?query_key=1&WebEnv=secret-a"
        )
        second = normalize_request(
            "GET", "https://eutils.ncbi.nlm.nih.gov/x?query_key=1&WebEnv=secret-b"
        )
        self.assertNotIn("secret-a", first.sanitized_url)
        self.assertNotIn("WebEnv", first.sanitized_url)
        self.assertNotEqual(first.request_key, second.request_key)

    def test_offline_replay_rejects_cached_non_success_status(self):
        from ai_research_agent.replay import ReplayError, ReplayProvider

        with tempfile.TemporaryDirectory() as directory:
            cache = HttpCache(Path(directory) / "cache.sqlite")
            cache.put_response(
                method="GET",
                url="https://example.test/error",
                body=b'{"error":"unavailable"}',
                status=503,
                fetched_at="2026-07-26T00:00:00Z",
                source="example",
            )
            with self.assertRaises(ReplayError):
                ReplayProvider(cache).get_json("https://example.test/error")

    def test_offline_http_client_archives_replayed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = HttpCache(root / "cache.sqlite")
            body = b'{"ok":true}'
            cache.put_response(
                method="GET", url="https://example.test/data?WebEnv=opaque",
                body=body, status=200, fetched_at="2026-07-26T00:00:00Z",
                source="example",
            )
            archive = root / "archive"
            client = JsonHttpClient(
                "Agent", cache=cache, offline=True, archive_dir=archive
            )
            self.assertEqual(
                client.get_json("https://example.test/data?WebEnv=opaque"),
                {"ok": True},
            )
            self.assertEqual(len(list(archive.glob("*.response"))), 1)
            metadata = json.loads(next(archive.glob("*.metadata.json")).read_text())
            self.assertNotIn("WebEnv", metadata["request"]["url"])
            self.assertEqual(metadata["response"]["sha256"], hashlib.sha256(body).hexdigest())

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "http-cache.sqlite3"
        self.cache = HttpCache(self.database)
        self.url = (
            "HTTPS://Example.ORG:443/api?b=2&email=person%40example.org"
            "&api_key=secret&a=1&token=also-secret"
        )
        self.body = json.dumps({"records": [1, 2]}, sort_keys=True).encode()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def store(self, body=None):
        return self.cache.put_response(
            method="get",
            url=self.url,
            accept="Application/JSON",
            body=self.body if body is None else body,
            status=200,
            fetched_at="2026-07-26T00:00:00Z",
            source="example",
            etag='"v1"',
            last_modified="Sat, 26 Jul 2026 00:00:00 GMT",
        )

    def test_request_key_is_stable_and_excludes_credentials(self):
        normalized = normalize_request("get", self.url, "Application/JSON")
        clean = normalize_request(
            "GET", "https://example.org/api?a=1&b=2", "application/json"
        )
        self.assertEqual(normalized.request_key, clean.request_key)
        self.assertEqual(normalized.sanitized_url, "https://example.org/api?a=1&b=2")
        combined = normalized.request_key + normalized.sanitized_url
        self.assertNotIn("person@example.org", combined)
        self.assertNotIn("secret", combined)
        self.assertNotIn("api_key", combined)
        self.assertNotIn("token", combined)
        self.assertNotIn("email", combined)
        self.assertEqual(
            normalized.request_key,
            make_request_key("GET", self.url, "application/json"),
        )

    def test_response_metadata_and_offline_replay_are_deterministic(self):
        stored = self.store()
        provider = ReplayProvider(self.cache)
        first = provider.get_bytes(self.url)
        second = provider.get_bytes(self.url)
        self.assertEqual(first, self.body)
        self.assertEqual(first, second)
        self.assertEqual(provider.get_json(self.url), {"records": [1, 2]})
        replayed = provider.get_response(self.url)
        self.assertEqual(replayed.response_sha256, hashlib.sha256(self.body).hexdigest())
        self.assertEqual(replayed.status, 200)
        self.assertEqual(replayed.fetched_at, "2026-07-26T00:00:00Z")
        self.assertEqual(replayed.source, "example")
        self.assertEqual(replayed.etag, '"v1"')
        self.assertEqual(replayed.last_modified, "Sat, 26 Jul 2026 00:00:00 GMT")
        self.assertEqual(replayed.response_id, stored.response_id)

    def test_cache_miss_fails_without_a_network_fallback(self):
        provider = ReplayProvider(self.cache)
        with self.assertRaises(ReplayCacheMiss):
            provider.get_bytes("https://example.org/not-cached")

    def test_tampered_blob_is_detected(self):
        stored = self.store()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE response_blobs SET body = ? WHERE sha256 = ?",
                (b"tampered", stored.response_sha256),
            )
        with self.assertRaises(CacheIntegrityError):
            ReplayProvider(self.cache).get_bytes(self.url)

    def test_attempts_and_checkpoints_are_append_only(self):
        attempt_id = self.cache.begin_attempt(method="GET", url=self.url)
        self.cache.append_checkpoint(
            attempt_id, state="partial", checkpoint="page-1"
        )
        self.cache.append_checkpoint(
            attempt_id, state="completed", checkpoint="all-pages"
        )
        checkpoints = self.cache.list_checkpoints(attempt_id)
        self.assertEqual([item["state"] for item in checkpoints], ["partial", "completed"])
        with sqlite3.connect(self.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE request_checkpoints SET state = 'failed' WHERE attempt_id = ?",
                    (attempt_id,),
                )

    def test_completed_partial_and_failed_attempts_are_supported(self):
        ids = [
            self.cache.record_attempt(
                method="GET",
                url=f"https://example.org/{state}",
                state=state,
                checkpoint="terminal",
            )
            for state in ("completed", "partial", "failed")
        ]
        self.assertEqual(
            [self.cache.list_checkpoints(value)[0]["state"] for value in ids],
            ["completed", "partial", "failed"],
        )

    def test_concurrent_writes_use_wal_and_transactions(self):
        def write(index):
            return self.cache.put_response(
                method="GET",
                url=f"https://example.org/page?index={index}",
                body=str(index).encode(),
                status=200,
                fetched_at="2026-07-26T00:00:00Z",
                source="concurrency-test",
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(write, range(12)))
        self.assertEqual(len({item.response_id for item in responses}), 12)
        with sqlite3.connect(self.database) as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            count = connection.execute("SELECT count(*) FROM response_records").fetchone()[0]
        self.assertEqual(mode.casefold(), "wal")
        self.assertEqual(count, 12)

    def test_schema_version_is_recorded_and_newer_schema_is_rejected(self):
        self.assertEqual(self.cache.schema_version, SCHEMA_VERSION)
        with sqlite3.connect(self.database) as connection:
            stored = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(stored), SCHEMA_VERSION)
        with sqlite3.connect(self.database) as connection:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with self.assertRaises(CacheSchemaError):
            HttpCache(self.database)


if __name__ == "__main__":
    unittest.main()
