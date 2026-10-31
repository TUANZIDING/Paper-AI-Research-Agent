"""Offline tests for the v0.4 exhaustive-retrieval modules."""

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from ai_research_agent.crossrefsearchv04 import CrossrefSearchSource
from ai_research_agent.europepmcgraphv04 import EuropePmcGraph
from ai_research_agent.exhaustivev04 import ExhaustiveConfig, ExhaustiveRun
from ai_research_agent.exportv04 import prisma_counts, to_asreview_csv, to_bibtex, to_ris
from ai_research_agent.http import RemoteServiceError
from ai_research_agent.models import AccessStatus, Publication
from ai_research_agent.openaccessv04 import OpenAccessResolver
from ai_research_agent.openalexv04 import OpenAlexSource, abstract_from_inverted_index, short_openalex_id
from ai_research_agent.snowballv04 import (
    SATURATION_DISCLAIMER,
    RelevanceRule,
    SnowballEngine,
    record_keys,
    saturation_svg,
)


class FakeClient:
    """Route URLs to canned JSON by substring; record every request."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.last_fetched_at = "2026-10-31T00:00:00+00:00"
        self.last_response_sha256 = "a" * 64

    def get_json(self, url):
        self.calls.append(url)
        for needle, payload in self.routes:
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload(url) if callable(payload) else payload
        raise RemoteServiceError(f"HTTP 404; no route for {url}")


class FakeGraph:
    """A tiny citation graph: PMID -> (references, citations)."""

    name = "fake_graph"

    def __init__(self, edges, titles):
        self.edges = edges
        self.titles = titles
        self.abstracts = {}
        self.calls = []

    def backward(self, pmid, limit):
        self.calls.append(("backward", pmid))
        refs = self.edges.get(pmid, ([], []))[0]
        return refs[:limit], len(refs), 0

    def forward(self, pmid, limit):
        self.calls.append(("forward", pmid))
        cites = self.edges.get(pmid, ([], []))[1]
        return cites[:limit], len(cites), 0

    def resolve_pmid(self, doi):
        return None

    def fetch_core(self, pmids, batch=100):
        return [Publication(title=self.titles[p], pmid=p) for p in pmids if p in self.titles]


RULE = RelevanceRule.parse(["radiat*|irradiat*", "muscle*|myopath*"])


class RelevanceRuleTests(unittest.TestCase):
    def test_all_groups_must_match_with_prefix_terms(self):
        self.assertTrue(RULE.matches("Irradiated skeletal muscles show fibrosis"))
        self.assertFalse(RULE.matches("Radiation dermatitis after breast radiotherapy"))
        self.assertFalse(RULE.matches("Muscle atrophy in ageing"))

    def test_whole_word_terms_do_not_match_inside_words(self):
        rule = RelevanceRule.parse(["rat"])
        self.assertFalse(rule.matches("radiation"))
        self.assertTrue(rule.matches("a rat model"))

    def test_empty_rule_is_rejected(self):
        with self.assertRaises(ValueError):
            RelevanceRule.parse(["", "  "])


class SnowballTests(unittest.TestCase):
    def build(self):
        titles = {
            "1": "Radiation injury of skeletal muscle (seed)",
            "2": "Irradiated muscle satellite cells",
            "3": "Unrelated cardiology trial",
            "4": "Radiation myopathy case series",
            "5": "Radiation therapy and muscle fibrosis in mice",
            "6": "Radiation muscle late effects",
        }
        edges = {
            "1": (["2", "3"], ["4"]),
            "2": (["1"], ["5"]),
            "4": ([], ["5", "6"]),
            "5": ([], []),
            "6": (["4"], []),
        }
        return FakeGraph(edges, titles), titles

    def test_rounds_log_new_records_and_saturate(self):
        graph, titles = self.build()
        seed = Publication(title=titles["1"], pmid="1")
        result = SnowballEngine(graph, RULE, max_rounds=5).run([seed])
        self.assertTrue(result.saturated)
        self.assertEqual([r.new_relevant for r in result.rounds], [2, 2, 0])
        self.assertEqual(result.rounds[0].new_not_relevant, 1)  # PMID 3
        self.assertEqual(result.rounds[-1].cumulative_relevant, 4)
        self.assertIn("Saturated at round 3", result.stop_reason)
        self.assertEqual(sorted(p.pmid for p in result.relevant), ["2", "4", "5", "6"])
        self.assertIn("not mean the search is complete", result.disclaimer)

    def test_already_seen_records_are_not_counted_again(self):
        graph, titles = self.build()
        seed = Publication(title=titles["1"], pmid="1")
        seen = [Publication(title=titles["2"], pmid="2")]
        result = SnowballEngine(graph, RULE, max_rounds=5).run([seed], already_seen=seen)
        self.assertNotIn("2", [p.pmid for p in result.relevant])

    def test_max_rounds_is_reported_as_not_saturated(self):
        graph, titles = self.build()
        result = SnowballEngine(graph, RULE, max_rounds=1).run(
            [Publication(title=titles["1"], pmid="1")]
        )
        self.assertFalse(result.saturated)
        self.assertIn("NOT saturated", result.stop_reason)

    def test_truncation_is_counted(self):
        graph, titles = self.build()
        result = SnowballEngine(graph, RULE, max_rounds=1, per_paper_limit=1).run(
            [Publication(title=titles["1"], pmid="1")]
        )
        self.assertEqual(result.rounds[0].papers_truncated, 1)

    def test_graph_errors_are_recorded_not_hidden(self):
        class Broken(FakeGraph):
            def forward(self, pmid, limit):
                raise RemoteServiceError("HTTP 503")

        graph = Broken({"1": (["2"], [])}, {"1": "Radiation muscle", "2": "Radiation muscle two"})
        result = SnowballEngine(graph, RULE, max_rounds=1).run([Publication(title="Radiation muscle", pmid="1")])
        self.assertTrue(any("HTTP 503" in e for e in result.rounds[0].errors))

    def test_record_keys_and_svg(self):
        keys = record_keys(Publication(title="A long enough title for keys", doi="10.1/X", pmid="9"))
        self.assertEqual(keys, {"doi:10.1/x", "pmid:9", "title:a long enough title for keys"})
        graph, titles = self.build()
        result = SnowballEngine(graph, RULE).run([Publication(title=titles["1"], pmid="1")])
        svg = saturation_svg(result.rounds)
        self.assertTrue(svg.startswith("<svg") and "R3" in svg)


class SourceTests(unittest.TestCase):
    def test_openalex_helpers(self):
        self.assertEqual(short_openalex_id("https://openalex.org/W123"), "W123")
        self.assertIsNone(short_openalex_id("10.1/x"))
        self.assertEqual(
            abstract_from_inverted_index({"muscle": [1], "Irradiated": [0], "fibrosis": [2]}),
            "Irradiated muscle fibrosis",
        )

    def test_openalex_search_maps_ids_and_marks_truncation(self):
        page = {
            "meta": {"count": 5, "next_cursor": "c2"},
            "results": [{
                "id": "https://openalex.org/W1",
                "title": "Radiation and muscle",
                "doi": "https://doi.org/10.1/ABC",
                "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/42"},
                "publication_year": 2020,
                "open_access": {"is_oa": True, "oa_url": "https://x.org/a.pdf"},
                "abstract_inverted_index": {"Hello": [0]},
                "referenced_works": ["https://openalex.org/W9"],
            }],
        }
        source = OpenAlexSource(FakeClient([("api.openalex.org/works", page)]), api_key="")
        records, total = source.search("q", 1)
        self.assertEqual(total, 5)
        self.assertEqual(records[0].doi, "10.1/abc")
        self.assertEqual(records[0].pmid, "42")
        self.assertIs(records[0].access_status, AccessStatus.OPEN)
        self.assertEqual(source.last_pagination.state.value, "truncated")
        self.assertEqual(source.references["W1"], ["W9"])

    def test_openalex_key_is_sent_but_never_hardcoded(self):
        client = FakeClient([("api.openalex.org", {"meta": {"count": 0}, "results": []})])
        OpenAlexSource(client, api_key="k").search("q", 5)
        self.assertIn("api_key=k", client.calls[0])

    def test_crossref_requires_cap_and_reports_truncation(self):
        payload = {"message": {"total-results": 1000, "items": [
            {"DOI": "10.2/x", "title": ["T"], "issued": {"date-parts": [[2019]]},
             "author": [{"family": "Li", "given": "Q"}], "abstract": "<jats:p>Muscle</jats:p>"},
        ], "next-cursor": "n"}}
        source = CrossrefSearchSource(FakeClient([("api.crossref.org", payload)]))
        with self.assertRaises(ValueError):
            source.search("q", None)
        records, total = source.search('("a"[tiab] AND b)', 1)
        self.assertEqual(records[0].authors, ["Li Q"])
        self.assertEqual(source.last_pagination.state.value, "truncated")
        self.assertEqual(source.abstracts[records[0].dedupe_key], "Muscle")
        self.assertEqual(source.translate_query('("a"[tiab] AND b*)'), "a b")

    def test_crossref_keeps_relevance_order_and_valid_select(self):
        payload = {"message": {"total-results": 0, "items": []}}
        client = FakeClient([("api.crossref.org", payload)])
        CrossrefSearchSource(client).search("q", 5)
        url = client.calls[0]
        self.assertIn("sort=relevance", url)
        self.assertIn("offset=0", url)
        self.assertNotIn("cursor=", url)
        self.assertIn("references-count", url)
        self.assertNotIn("%2Creference-count", url)
        with self.assertRaises(ValueError):
            CrossrefSearchSource(client).search("q", 10001)

    def test_europe_pmc_graph_links_and_core(self):
        refs = {"hitCount": 3, "referenceList": {"reference": [
            {"source": "MED", "id": "11"}, {"source": "AGR", "id": "x"}, {"title": "no id"}]}}
        cites = {"hitCount": 1, "citationList": {"citation": [{"source": "MED", "id": "22"}]}}
        core = {"resultList": {"result": [{
            "id": "11", "source": "MED", "pmid": "11", "title": "Radiation muscle",
            "abstractText": "Abstract <b>text</b>", "isOpenAccess": "Y", "pmcid": "PMC1",
            "license": "cc by",
        }]}}
        client = FakeClient([("/references", refs), ("/citations", cites), ("/search", core)])
        graph = EuropePmcGraph(client)
        self.assertEqual(graph.backward("5", 10), (["11"], 3, 2))
        self.assertEqual(graph.forward("5", 10), (["22"], 1, 0))
        records = graph.fetch_core(["11"])
        self.assertEqual(records[0].pmcid, "PMC1")
        self.assertIn("EXT_ID%3A11", client.calls[-1])
        self.assertEqual(graph.licenses[records[0].dedupe_key], "cc by")


class OpenAccessTests(unittest.TestCase):
    def resolver(self, location):
        payload = {"best_oa_location": location}
        return OpenAccessResolver(FakeClient([("api.unpaywall.org", payload)]), email="x@example.org")

    def test_cc_by_is_machine_readable(self):
        decision = self.resolver({"url_for_pdf": "https://journal.org/a.pdf", "license": "cc-by",
                                  "version": "publishedVersion"}).decide(
            Publication(title="T", doi="10.1/a"))
        self.assertEqual(decision.category, "oa_machine_readable")

    def test_nc_license_is_link_only(self):
        decision = self.resolver({"url_for_pdf": "https://journal.org/a.pdf", "license": "cc-by-nc",
                                  "version": "publishedVersion"}).decide(
            Publication(title="T", doi="10.1/a"))
        self.assertEqual(decision.category, "oa_link_only")

    def test_no_location_needs_institutional_access(self):
        decision = self.resolver(None).decide(Publication(title="T", doi="10.1/a"))
        self.assertEqual(decision.category, "needs_institutional_access")
        self.assertEqual(decision.landing_url, "https://doi.org/10.1/a")

    def test_unpaywall_is_skipped_without_email(self):
        client = FakeClient([])
        resolver = OpenAccessResolver(client, email=None)
        resolver.email = None
        decision = resolver.decide(Publication(title="T", doi="10.1/a"))
        self.assertEqual(client.calls, [])
        self.assertIn("Unpaywall skipped", decision.note)

    def test_europe_pmc_oa_subset_with_cc_by(self):
        publication = Publication(title="T", pmid="1", pmcid="PMC9", access_status=AccessStatus.OPEN)
        resolver = OpenAccessResolver(FakeClient([]), email=None,
                                      epmc_licenses={publication.dedupe_key: "cc by"})
        decision = resolver.decide(publication)
        self.assertEqual(decision.category, "oa_machine_readable")
        self.assertIn("PMC9/fullTextXML", decision.url)

    def test_download_only_touches_machine_readable(self):
        class Recorder:
            def __init__(self):
                self.urls = []

            def download(self, **kwargs):
                self.urls.append(kwargs["url"])
                raise __import__("ai_research_agent.fulltext", fromlist=["x"]).FulltextPolicyError("blocked")

        resolver = OpenAccessResolver(FakeClient([]), email=None)
        good = self.resolver({"url_for_pdf": "https://j.org/a.pdf", "license": "cc-by",
                              "version": "publishedVersion"}).decide(Publication(title="A", doi="10.1/a"))
        bad = self.resolver({"url_for_pdf": "https://j.org/b.pdf", "license": None,
                             "version": "publishedVersion"}).decide(Publication(title="B", doi="10.1/b"))
        recorder = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            count = resolver.download([good, bad], Path(tmp), downloader=recorder)
        self.assertEqual(count, 0)
        self.assertEqual(recorder.urls, ["https://j.org/a.pdf"])
        self.assertIn("download blocked", good.note)


class ExportTests(unittest.TestCase):
    def rows(self):
        p = Publication(title="Radiation {muscle} & 50%", authors=["Ding H", "Zou J"], year=2025,
                        journal="J Test", doi="10.1/X", pmid="7")
        return [(p, "An abstract", "database_boolean_query")]

    def test_ris(self):
        text = to_ris(self.rows())
        self.assertIn("TY  - JOUR", text)
        self.assertIn("AU  - Zou J", text)
        self.assertIn("DO  - 10.1/x", text)
        self.assertIn("AN  - PMID:7", text)
        self.assertTrue(text.strip().endswith("ER  -"))

    def test_bibtex_escapes(self):
        text = to_bibtex(self.rows())
        self.assertIn("@article{ding2025radiation,", text)
        self.assertIn("\\{muscle\\} \\& 50\\%", text)

    def test_asreview_labels_left_blank(self):
        text = to_asreview_csv([{"title": "T", "abstract": "A", "included": "1"}])
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(rows[0]["included"], "")
        self.assertEqual(rows[0]["record_id"], "1")

    def test_prisma_counts(self):
        counts = prisma_counts(database_records={"pubmed": 10, "europe_pmc": 8}, citation_records=5,
                               other_records=0, after_dedup=15, auto_excluded=2,
                               to_human_screening=13, oa_available=4, needs_access=9)
        self.assertEqual(counts["identification"]["duplicates_removed"], 8)
        self.assertFalse(counts["screening"]["human_screening_completed"])


class ExhaustiveRunTests(unittest.TestCase):
    def test_end_to_end_offline(self):
        esearch = {"esearchresult": {"count": "1", "querykey": "1", "webenv": "W", "idlist": ["1"]}}
        esummary = {"result": {"uids": ["1"], "1": {
            "title": "Radiation injury of skeletal muscle",
            "articleids": [{"idtype": "doi", "value": "10.9/seed"}], "pubdate": "2021"}}}
        epmc_search = {"hitCount": 1, "resultList": {"result": [{
            "id": "1", "source": "MED", "pmid": "1", "doi": "10.9/seed",
            "title": "Radiation injury of skeletal muscle", "isOpenAccess": "N"}]}}

        def epmc(url):
            if "resultType=core" in url:
                if "EXT_ID%3A2" in url:
                    return {"resultList": {"result": [{
                        "id": "2", "source": "MED", "pmid": "2", "title": "Irradiated muscle stem cells",
                        "abstractText": "x", "isOpenAccess": "N"}]}}
                return {"resultList": {"result": [{
                    "id": "1", "source": "MED", "pmid": "1", "doi": "10.9/seed",
                    "title": "Radiation injury of skeletal muscle", "abstractText": "Seed abstract",
                    "isOpenAccess": "N"}]}}
            return epmc_search

        routes = [
            ("esearch.fcgi", esearch),
            ("esummary.fcgi", esummary),
            ("MED/1/references", {"hitCount": 1, "referenceList": {"reference": [{"source": "MED", "id": "2"}]}}),
            ("MED/1/citations", {"hitCount": 0, "citationList": {"citation": []}}),
            ("MED/2/references", {"hitCount": 0, "referenceList": {"reference": []}}),
            ("MED/2/citations", {"hitCount": 0, "citationList": {"citation": []}}),
            ("europepmc/webservices/rest/search", epmc),
            ("api.openalex.org", RemoteServiceError("GET failed (HTTP 429; HTTPError)")),
            ("api.crossref.org", {"message": {"total-results": 0, "items": []}}),
            ("api.unpaywall.org", {"best_oa_location": None}),
        ]
        config = ExhaustiveConfig(
            query="q", relevance_groups=["radiat*|irradiat*", "muscle*"],
            sources=["pubmed", "europe_pmc", "openalex", "crossref"], max_rounds=3,
        )
        run = ExhaustiveRun(FakeClient(routes), config)
        run.openalex.api_key = None
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary = run.run(out)
            manifest = json.loads((out / "exhaustive-manifest-v0.4.json").read_text())
            asreview = (out / "asreview-v0.4.csv").read_text()
            self.assertTrue((out / "saturation-v0.4.svg").exists())
            self.assertTrue((out / "screening-v0.4.ris").exists())
        self.assertEqual(summary["merged_after_dedup"], 1)
        self.assertEqual(summary["source_contribution"]["pubmed"]["records_after_merge"], 1)
        self.assertEqual(summary["snowball"]["new_relevant_total"], 1)
        self.assertTrue(summary["snowball"]["saturated"])
        self.assertEqual(summary["screening_records"], 2)
        self.assertIn("Irradiated muscle stem cells", asreview)
        self.assertTrue(any(i["source"] == "openalex" for i in summary["issues"]))
        self.assertIn("screening-v0.4.ris", manifest["output_sha256"])
        self.assertIn(SATURATION_DISCLAIMER, manifest["scope_statement"])
        self.assertEqual(summary["oa_triage"].get("needs_institutional_access"), 2)


if __name__ == "__main__":
    unittest.main()
