from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from ai_research_agent.http import JsonHttpClient, RemoteServiceError
from ai_research_agent.sources import PaginationState, PubMedSource


def _item(pmid: str) -> dict:
    return {
        "title": f"Fixed title {pmid}",
        "authors": [{"name": "A Author"}],
        "articleids": [],
        "pubtype": ["Journal Article"],
        "pubdate": "2026",
    }


class HistoryClient:
    def __init__(self, total: int, *, missing_webenv: bool = False):
        self.total = total
        self.missing_webenv = missing_webenv
        self.urls: list[str] = []

    def get_bytes(self, url: str, **kwargs) -> bytes:
        raise AssertionError("not used by history search")

    def get_json(self, url: str) -> dict:
        self.urls.append(url)
        params = parse_qs(urlsplit(url).query)
        if "esearch.fcgi" in url:
            result = {
                "count": str(self.total),
                "querykey": "7",
                "webenv": "fixed-snapshot-token",
                "idlist": [],
            }
            if self.missing_webenv:
                result.pop("webenv")
            return {"esearchresult": result}
        self.assert_history_params(params)
        start = int(params["retstart"][0])
        size = int(params["retmax"][0])
        ids = [
            str(index)
            for index in range(start + 1, min(start + size, self.total) + 1)
        ]
        return {
            "result": {
                "uids": ids,
                **{pmid: _item(pmid) for pmid in ids},
            }
        }

    def assert_history_params(self, params: dict[str, list[str]]) -> None:
        if params.get("query_key") != ["7"]:
            raise AssertionError("ESummary did not use query_key")
        if params.get("WebEnv") != ["fixed-snapshot-token"]:
            raise AssertionError("ESummary did not use WebEnv")
        if "id" in params:
            raise AssertionError("History ESummary must not send a dynamic ID list")


class XmlClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def get_bytes(self, url: str, **kwargs) -> bytes:
        self.calls.append((url, kwargs))
        return self.payload


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def getcode(self) -> int:
        return self.status


class PubMedHistoryTests(unittest.TestCase):
    def test_one_esearch_then_fixed_history_esummary_pages(self):
        client = HistoryClient(5)
        source = PubMedSource(client)
        records, total = source.search("bone", None, page_size=2)
        self.assertEqual((len(records), total), (5, 5))
        self.assertEqual(
            sum("esearch.fcgi" in url for url in client.urls), 1
        )
        self.assertEqual(
            sum("esummary.fcgi" in url for url in client.urls), 3
        )
        self.assertIs(source.last_pagination.state, PaginationState.COMPLETE)
        self.assertTrue(source.last_pagination.history_used)
        self.assertEqual(source.last_pagination.history_query_key, "7")
        self.assertIsNone(source.last_pagination.history_failure)

    def test_history_limit_is_explicitly_truncated(self):
        source = PubMedSource(HistoryClient(5))
        records, total = source.search("bone", 3, page_size=2)
        self.assertEqual((len(records), total), (3, 5))
        self.assertIs(source.last_pagination.state, PaginationState.TRUNCATED)
        self.assertTrue(source.last_pagination.history_used)

    def test_missing_webenv_fails_and_records_history_failure(self):
        source = PubMedSource(HistoryClient(2, missing_webenv=True))
        with self.assertRaisesRegex(RemoteServiceError, "WebEnv"):
            source.search("bone", None)
        self.assertIs(source.last_pagination.state, PaginationState.PARTIAL)
        self.assertFalse(source.last_pagination.history_used)
        self.assertIn("WebEnv", source.last_pagination.history_failure)


class PubMedRelationTests(unittest.TestCase):
    def test_extracts_retraction_correction_and_concern_relations(self):
        payload = b"""<?xml version='1.0' encoding='UTF-8'?>
        <PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345</PMID><CommentsCorrectionsList>
          <CommentsCorrections RefType='RetractionIn'>
            <PMID>111</PMID><RefSource>Journal. doi:10.1000/retracted</RefSource>
            <Note>Retracted by publisher</Note>
          </CommentsCorrections>
          <CommentsCorrections RefType='ErratumIn'>
            <PMID>222</PMID><ArticleIdList><ArticleId IdType='doi'>10.1000/fix</ArticleId></ArticleIdList>
          </CommentsCorrections>
          <CommentsCorrections RefType='ExpressionOfConcernIn'>
            <PMID>333</PMID><DOI>10.1000/concern</DOI><Note>Editorial notice</Note>
          </CommentsCorrections>
        </CommentsCorrectionsList></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
        client = XmlClient(payload)
        relations = PubMedSource(client).fetch_status_relations("12345")
        self.assertEqual(
            [item["ref_type"] for item in relations],
            ["RetractionIn", "ErratumIn", "ExpressionOfConcernIn"],
        )
        self.assertEqual(
            [item["doi"] for item in relations],
            ["10.1000/retracted", "10.1000/fix", "10.1000/concern"],
        )
        self.assertEqual(relations[0]["pmid"], "111")
        self.assertEqual(relations[0]["note"], "Retracted by publisher")
        self.assertEqual(client.calls[0][1]["max_bytes"], 5_000_000)

    def test_rejects_doctype_and_entity_xml(self):
        for payload in (
            b"<!DOCTYPE x [<!ENTITY boom 'x'>]><x>&boom;</x>",
            b"<!ENTITY boom SYSTEM 'file:///etc/passwd'><x/>",
        ):
            with self.subTest(payload=payload):
                source = PubMedSource(XmlClient(payload))
                with self.assertRaisesRegex(RemoteServiceError, "DOCTYPE|ENTITY"):
                    source.fetch_status_relations("123")

    def test_allows_only_canonical_nlm_pubmed_doctype(self):
        payload = b'''<?xml version="1.0"?>
        <!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">
        <PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>
        <CommentsCorrectionsList><CommentsCorrections RefType="ErratumIn">
        <PMID>42</PMID></CommentsCorrections></CommentsCorrectionsList>
        </MedlineCitation></PubmedArticle></PubmedArticleSet>'''
        relations = PubMedSource(XmlClient(payload)).fetch_status_relations("123")
        self.assertEqual(relations[0]["ref_type"], "ErratumIn")

    def test_rejects_empty_error_or_wrong_target_xml(self):
        payloads = (
            b"<PubmedArticleSet/>",
            b"<PubmedArticleSet><ERROR>Invalid id</ERROR></PubmedArticleSet>",
            b"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>999</PMID></MedlineCitation></PubmedArticle></PubmedArticleSet>",
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(RemoteServiceError):
                    PubMedSource(XmlClient(payload)).fetch_status_relations("123")


class TextTransportTests(unittest.TestCase):
    def test_get_text_uses_archive_and_preserves_xml_bytes(self):
        payload = b"<root>safe</root>"
        with tempfile.TemporaryDirectory() as directory:
            client = JsonHttpClient(
                "Agent", retries=0, min_interval=0, archive_dir=directory
            )
            with patch(
                "ai_research_agent.http.urlopen",
                return_value=FakeResponse(payload),
            ):
                text = client.get_text(
                    "https://example.test/x.xml?api_key=secret&WebEnv=session-secret",
                    accept="application/xml",
                    max_bytes=100,
                )
            self.assertEqual(text, "<root>safe</root>")
            bodies = list(Path(directory).glob("*.response"))
            metadata = list(Path(directory).glob("*.metadata.json"))
            self.assertEqual(bodies[0].read_bytes(), payload)
            self.assertNotIn(
                "secret", json.loads(metadata[0].read_text())["request"]["url"]
            )

    def test_get_text_429_is_explicit(self):
        client = JsonHttpClient("Agent", retries=0, min_interval=0)
        error = HTTPError(
            "https://example.test/x.xml", 429, "Too Many Requests", {}, None
        )
        error.read = lambda: b"limited"
        with patch("ai_research_agent.http.urlopen", side_effect=error):
            with self.assertRaisesRegex(RemoteServiceError, "HTTP 429"):
                client.get_text("https://example.test/x.xml")

    def test_get_bytes_enforces_size_limit(self):
        client = JsonHttpClient("Agent", retries=0, min_interval=0)
        with patch(
            "ai_research_agent.http.urlopen", return_value=FakeResponse(b"12345")
        ):
            with self.assertRaisesRegex(RemoteServiceError, "exceeds 4 byte"):
                client.get_bytes("https://example.test/x", max_bytes=4)


if __name__ == "__main__":
    unittest.main()
