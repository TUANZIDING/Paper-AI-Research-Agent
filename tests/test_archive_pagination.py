from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from ai_research_agent.archive import ResponseArchive, redact_url
from ai_research_agent.http import JsonHttpClient, RemoteServiceError
from ai_research_agent.sources import (
    EuropePmcSource,
    PaginationState,
    PubMedSource,
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status


class ArchiveAndHttpTests(unittest.TestCase):
    def test_archive_preserves_bytes_hash_and_redacts_contact_fields(self):
        body = b'{"ok":true}\n'
        with tempfile.TemporaryDirectory() as directory:
            archive = ResponseArchive(directory)
            result = archive.store(
                url=(
                    "https://example.test/search?q=bone&api_key=secret"
                    "&email=person%40example.test&mailto=person%40example.test"
                ),
                status=200,
                body=body,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Agent (mailto:person@example.test)",
                    "Authorization": "Bearer secret",
                },
                fetched_at="2026-07-25T01:02:03+00:00",
            )
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(result.body_path.read_bytes(), body)
            self.assertEqual(metadata["response"]["sha256"], hashlib.sha256(body).hexdigest())
            serialized = json.dumps(metadata)
            self.assertNotIn("secret", serialized)
            self.assertNotIn("person", serialized)
            self.assertEqual(metadata["request"]["headers"], {"Accept": "application/json"})
            self.assertEqual(metadata["http_status"], 200)

    def test_http_client_archives_original_bytes(self):
        body = b'{ "value": 1 }\n'
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(
                "Agent mailto:private@example.test",
                retries=0,
                min_interval=0,
                archive_dir=directory,
            )
            with patch(
                "ai_research_agent.http.urlopen",
                return_value=FakeResponse(body),
            ):
                self.assertEqual(
                    client.get_json(
                        "https://example.test/?q=x&api_key=hidden&email=x%40y.test"
                    ),
                    {"value": 1},
                )
            bodies = list(Path(directory).glob("*.response"))
            metadata = list(Path(directory).glob("*.metadata.json"))
            self.assertEqual(len(bodies), 1)
            self.assertEqual(bodies[0].read_bytes(), body)
            self.assertNotIn("hidden", metadata[0].read_text(encoding="utf-8"))
            self.assertNotIn("private", metadata[0].read_text(encoding="utf-8"))

    def test_invalid_json_is_an_explicit_error(self):
        client = JsonHttpClient("Agent", retries=0, min_interval=0)
        with patch(
            "ai_research_agent.http.urlopen",
            return_value=FakeResponse(b"not-json"),
        ):
            with self.assertRaisesRegex(RemoteServiceError, "Invalid JSON"):
                client.get_json("https://example.test/")

    def test_429_is_an_explicit_error_with_status(self):
        client = JsonHttpClient("Agent", retries=0, min_interval=0)
        error = HTTPError(
            "https://example.test/",
            429,
            "Too Many Requests",
            {},
            None,
        )
        error.read = lambda: b'{"error":"limited"}'
        with patch("ai_research_agent.http.urlopen", side_effect=error):
            with self.assertRaisesRegex(RemoteServiceError, "HTTP 429"):
                client.get_json("https://example.test/")

    def test_5xx_is_an_explicit_error_after_retries(self):
        client = JsonHttpClient("Agent", retries=1, min_interval=0)

        def unavailable():
            error = HTTPError(
                "https://example.test/",
                503,
                "Service Unavailable",
                {},
                None,
            )
            error.read = lambda: b'{"error":"unavailable"}'
            return error

        with patch(
            "ai_research_agent.http.urlopen",
            side_effect=[unavailable(), unavailable()],
        ), patch("ai_research_agent.http.time.sleep"):
            with self.assertRaisesRegex(RemoteServiceError, "HTTP 503"):
                client.get_json("https://example.test/")

    def test_redact_url_drops_userinfo_and_sensitive_query(self):
        value = redact_url(
            "https://user:pass@example.test/x?q=1&token=x&email=a%40b.test"
        )
        self.assertEqual(value, "https://example.test/x?q=1")


def pubmed_item(pmid: str) -> dict:
    return {
        "title": f"Title {pmid}",
        "authors": [{"name": "A Author"}],
        "articleids": [],
        "pubtype": ["Journal Article"],
        "pubdate": "2025",
    }


class FakePubMedClient:
    def __init__(self, total: int):
        self.total = total

    def get_json(self, url: str) -> dict:
        params = parse_qs(urlsplit(url).query)
        if "esearch.fcgi" in url:
            start = int(params["retstart"][0])
            size = int(params["retmax"][0])
            ids = [str(i) for i in range(start + 1, min(start + size, self.total) + 1)]
            return {"esearchresult": {"count": str(self.total), "idlist": ids}}
        ids = params["id"][0].split(",")
        return {"result": {pmid: pubmed_item(pmid) for pmid in ids}}


class FakeEuropePmcClient:
    def __init__(self, total: int, stuck_cursor: bool = False):
        self.total = total
        self.stuck_cursor = stuck_cursor

    def get_json(self, url: str) -> dict:
        params = parse_qs(urlsplit(url).query)
        cursor = params["cursorMark"][0]
        start = 0 if cursor == "*" else int(cursor)
        size = int(params["pageSize"][0])
        items = [
            {
                "id": str(index + 1),
                "pmid": str(index + 1),
                "title": f"Europe title {index + 1}",
                "pubYear": "2025",
            }
            for index in range(start, min(start + size, self.total))
        ]
        next_cursor = cursor if self.stuck_cursor else str(start + len(items))
        return {
            "hitCount": self.total,
            "nextCursorMark": next_cursor,
            "resultList": {"result": items},
        }


class PaginationTests(unittest.TestCase):
    def test_pubmed_complete_pagination(self):
        source = PubMedSource(FakePubMedClient(5))
        records, total = source.search("bone", None, page_size=2)
        self.assertEqual((len(records), total), (5, 5))
        self.assertEqual(source.last_pagination.state, PaginationState.COMPLETE)
        self.assertEqual(source.last_pagination.pages_fetched, 3)

    def test_pubmed_explicit_cap_is_truncated(self):
        source = PubMedSource(FakePubMedClient(5))
        records, total = source.search("bone", 3, page_size=2)
        self.assertEqual((len(records), total), (3, 5))
        self.assertEqual(source.last_pagination.state, PaginationState.TRUNCATED)
        self.assertEqual(source.last_pagination.max_records, 3)

    def test_europe_pmc_complete_cursor_pagination(self):
        source = EuropePmcSource(FakeEuropePmcClient(5))
        records, total = source.search("bone", None, page_size=2)
        self.assertEqual((len(records), total), (5, 5))
        self.assertEqual(source.last_pagination.state, PaginationState.COMPLETE)
        self.assertEqual(source.last_pagination.pages_fetched, 3)

    def test_europe_pmc_explicit_cap_is_truncated(self):
        source = EuropePmcSource(FakeEuropePmcClient(5))
        records, total = source.search("bone", 3, page_size=2)
        self.assertEqual((len(records), total), (3, 5))
        self.assertEqual(source.last_pagination.state, PaginationState.TRUNCATED)

    def test_europe_pmc_stuck_cursor_is_partial(self):
        source = EuropePmcSource(FakeEuropePmcClient(5, stuck_cursor=True))
        records, total = source.search("bone", None, page_size=2)
        self.assertEqual((len(records), total), (2, 5))
        self.assertEqual(source.last_pagination.state, PaginationState.PARTIAL)
        self.assertTrue(source.last_pagination.errors)

    def test_malformed_source_payload_is_explicit_and_partial(self):
        class MalformedClient:
            def get_json(self, url: str) -> dict:
                return {"unexpected": True}

        source = PubMedSource(MalformedClient())
        with self.assertRaisesRegex(RemoteServiceError, "lacks esearchresult"):
            source.search("bone", 2)
        self.assertEqual(source.last_pagination.state, PaginationState.PARTIAL)
        self.assertTrue(source.last_pagination.errors)


if __name__ == "__main__":
    unittest.main()
