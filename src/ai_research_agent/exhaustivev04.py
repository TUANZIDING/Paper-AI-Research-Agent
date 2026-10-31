"""Exhaustive-retrieval workflow (v0.4): multi-source search, citation
snowballing, saturation stop, lawful OA triage and exports in one run.

Routing of records into human screening:

* Boolean database queries (PubMed, Europe PMC) are the human-designed filter,
  so every hit goes to human title/abstract screening.
* Relevance-ranked free-text sources (OpenAlex search, Crossref) and citation
  snowball candidates pass a transparent keyword pre-screen first; exclusions
  are counted and exported, never hidden.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .crossrefsearchv04 import CrossrefSearchSource
from .europepmcgraphv04 import EuropePmcGraph
from .exportv04 import prisma_counts, to_asreview_csv, to_bibtex, to_ris
from .http import JsonHttpClient, RemoteServiceError
from .models import AccessStatus, Publication, normalize_doi
from .openaccessv04 import OpenAccessResolver
from .openalexv04 import OpenAlexGraph, OpenAlexSource
from .pipeline import merge_publications
from .snowballv04 import (
    SATURATION_DISCLAIMER,
    RelevanceRule,
    SnowballEngine,
    SnowballResult,
    clean_abstract,
    record_keys,
    saturation_svg,
)
from .institutionv04 import USAGE_NOTICE as INSTITUTION_NOTICE, load_profile, write_access_outputs
from .sources import EuropePmcSource, PubMedSource

BOOLEAN_SOURCES = ("pubmed", "europe_pmc")
RANKED_SOURCES = ("openalex", "crossref")
SCOPE_STATEMENT = (
    "This run used open APIs only (PubMed, Europe PMC, OpenAlex, Crossref, "
    "Unpaywall). It is not equivalent to Embase, Web of Science, Scopus or "
    "Chinese databases, and it does not replace human screening. "
    + SATURATION_DISCLAIMER
)


@dataclass
class ExhaustiveConfig:
    query: str
    relevance_groups: list[str]
    sources: list[str] = field(default_factory=lambda: ["pubmed", "europe_pmc", "openalex", "crossref"])
    source_queries: dict[str, str] = field(default_factory=dict)
    limit_per_source: int | None = 2000
    ranked_limit: int = 200
    seeds: list[str] = field(default_factory=list)
    seed_from_search: bool = True
    max_seeds: int = 300
    graph: str = "europepmc"
    max_rounds: int = 4
    per_paper_limit: int = 500
    stop_threshold: int = 0
    oa_check: bool = True
    oa_limit: int | None = None
    download_oa: bool = False
    download_max: int = 20
    institution_profile: str | None = None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sources_of(publication: Publication) -> set[str]:
    return {item.source for item in publication.evidence}


class ExhaustiveRun:
    def __init__(self, client: JsonHttpClient, config: ExhaustiveConfig):
        self.client = client
        self.config = config
        self.rule = RelevanceRule.parse(config.relevance_groups)
        self.graph_meta = EuropePmcGraph(client)
        self.openalex = OpenAlexSource(client)
        self.crossref = CrossrefSearchSource(client)
        self.issues: list[dict[str, str]] = []
        self.abstracts: dict[str, str] = {}

    # -- discovery -----------------------------------------------------
    def discover(self) -> tuple[list[Publication], dict[str, Any]]:
        sources = {
            "pubmed": PubMedSource(self.client),
            "europe_pmc": EuropePmcSource(self.client),
            "openalex": self.openalex,
            "crossref": self.crossref,
        }
        found: list[Publication] = []
        report: dict[str, Any] = {}
        for name in self.config.sources:
            source = sources[name]
            query = self.config.source_queries.get(name, self.config.query)
            limit = self.config.ranked_limit if name in RANKED_SOURCES else self.config.limit_per_source
            entry: dict[str, Any] = {"query": query, "limit": limit, "kind": "boolean" if name in BOOLEAN_SOURCES else "ranked"}
            try:
                records, total = source.search(query, limit, page_size=100 if name != "crossref" else 200)
                found.extend(records)
                entry.update(total_reported=total, returned=len(records))
            except RemoteServiceError as exc:
                entry.update(total_reported=None, returned=0, error=str(exc))
                self.issues.append({"source": name, "stage": "discovery", "message": str(exc)})
            pagination = getattr(source, "last_pagination", None)
            if pagination is not None:
                entry["pagination_state"] = pagination.state.value
            report[name] = entry
        return found, report

    def enrich(self, publications: list[Publication]) -> None:
        pmids = [item.pmid for item in publications if item.pmid]
        try:
            cores = self.graph_meta.fetch_core(pmids)
        except RemoteServiceError as exc:
            self.issues.append({"source": "europe_pmc", "stage": "enrich", "message": str(exc)})
            cores = []
        by_pmid = {item.pmid: item for item in cores if item.pmid}
        for publication in publications:
            core = by_pmid.get(publication.pmid or "")
            if core is None:
                continue
            publication.pmcid = publication.pmcid or core.pmcid
            publication.doi = publication.doi or core.doi
            if core.access_status is AccessStatus.OPEN:
                publication.access_status = AccessStatus.OPEN
            abstract = self.graph_meta.abstracts.get(core.dedupe_key)
            if abstract:
                self.abstracts[publication.dedupe_key] = abstract
            license_value = self.graph_meta.licenses.get(core.dedupe_key)
            if license_value:
                self.graph_meta.licenses[publication.dedupe_key] = license_value
        for store in (self.openalex.abstracts, self.crossref.abstracts):
            for key, value in store.items():
                self.abstracts.setdefault(key, value)

    def abstract_of(self, publication: Publication) -> str:
        return clean_abstract(
            self.abstracts.get(publication.dedupe_key)
            or self.graph_meta.abstracts.get(publication.dedupe_key)
        )

    def resolve_seeds(self) -> list[Publication]:
        pmids: list[str] = []
        for raw in self.config.seeds:
            value = raw.strip()
            if re.fullmatch(r"(?:pmid:)?\d+", value, re.I):
                pmids.append(value.split(":")[-1])
            elif normalize_doi(value):
                try:
                    pmid = self.graph_meta.resolve_pmid(normalize_doi(value))
                except RemoteServiceError as exc:
                    pmid = None
                    self.issues.append({"source": "europe_pmc", "stage": "seed", "message": str(exc)})
                if pmid:
                    pmids.append(pmid)
                else:
                    self.issues.append({"source": "seed", "stage": "seed", "message": f"No PMID for seed {value}"})
        return self.graph_meta.fetch_core(pmids) if pmids else []

    # -- full run ------------------------------------------------------
    def run(self, out_dir: Path) -> dict[str, Any]:
        started = datetime.now(timezone.utc).isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)
        discovered, source_report = self.discover()
        per_source_raw = {name: entry.get("returned", 0) for name, entry in source_report.items()}
        merged = merge_publications(discovered)
        self.enrich(merged)

        # contribution: records found by each source, and only by that source
        contribution = {}
        for name in self.config.sources:
            label = name
            hits = [item for item in merged if label in _sources_of(item)]
            only = [item for item in hits if _sources_of(item) == {label}]
            contribution[name] = {"records_after_merge": len(hits), "unique_to_source": len(only)}

        screening: list[tuple[Publication, str]] = []
        auto_excluded: list[tuple[Publication, str]] = []
        for item in merged:
            origins = _sources_of(item)
            text = f"{item.title} {self.abstract_of(item)}"
            if origins & set(BOOLEAN_SOURCES):
                screening.append((item, "database_boolean_query"))
            elif self.rule.matches(text):
                screening.append((item, "ranked_source_passed_prescreen"))
            else:
                auto_excluded.append((item, "ranked_source_failed_prescreen"))

        seeds = self.resolve_seeds()
        if self.config.seed_from_search:
            pool = [
                item for item, _ in screening
                if self.rule.matches(f"{item.title} {self.abstract_of(item)}")
            ]
            seeds.extend(pool[: self.config.max_seeds])
        graph = self.graph_meta if self.config.graph == "europepmc" else OpenAlexGraph(self.openalex, self.graph_meta)
        engine = SnowballEngine(
            graph,
            self.rule,
            max_rounds=self.config.max_rounds,
            per_paper_limit=self.config.per_paper_limit,
            stop_threshold=self.config.stop_threshold,
        )
        snowball: SnowballResult = engine.run(seeds, already_seen=merged)
        for item in snowball.relevant:
            screening.append((item, "citation_snowball_passed_prescreen"))
        for item in snowball.not_relevant:
            auto_excluded.append((item, "citation_snowball_failed_prescreen"))

        # lawful OA triage for records going to human screening
        decisions = []
        resolver = OpenAccessResolver(self.client, epmc_licenses=self.graph_meta.licenses)
        if self.config.oa_check:
            subset = screening if self.config.oa_limit is None else screening[: self.config.oa_limit]
            decisions = [resolver.decide(item) for item, _ in subset]
            if self.config.download_oa:
                resolver.download(decisions, out_dir / "fulltext-oa", max_files=self.config.download_max)
            for issue in resolver.issues:
                self.issues.append({"source": "unpaywall", "stage": "oa", "message": issue})
        if self.config.oa_check and not resolver.email:
            self.issues.append({
                "source": "unpaywall", "stage": "oa",
                "message": "Unpaywall skipped: set UNPAYWALL_EMAIL or RESEARCH_AGENT_EMAIL",
            })
        oa_counts: dict[str, int] = {}
        for item in decisions:
            oa_counts[item.category] = oa_counts.get(item.category, 0) + 1

        # exports
        rows = [(item, self.abstract_of(item), note) for item, note in screening]
        (out_dir / "screening-v0.4.ris").write_text(to_ris(rows), encoding="utf-8")
        (out_dir / "screening-v0.4.bib").write_text(to_bibtex(rows), encoding="utf-8")
        asreview_rows = [
            {
                "title": item.title,
                "abstract": self.abstract_of(item),
                "authors": "; ".join(item.authors),
                "year": item.year or "",
                "journal": item.journal or "",
                "doi": normalize_doi(item.doi) or "",
                "pmid": item.pmid or "",
                "url": f"https://doi.org/{normalize_doi(item.doi)}" if normalize_doi(item.doi) else (
                    f"https://pubmed.ncbi.nlm.nih.gov/{item.pmid}/" if item.pmid else ""
                ),
                "keywords": "",
                "origin": note,
                "auto_prescreen": "passed" if note != "database_boolean_query" else "not_applied",
            }
            for item, note in screening
        ]
        (out_dir / "asreview-v0.4.csv").write_text(to_asreview_csv(asreview_rows), encoding="utf-8")
        excluded_rows = [
            {
                "title": item.title, "abstract": self.abstract_of(item),
                "authors": "; ".join(item.authors), "year": item.year or "",
                "journal": item.journal or "", "doi": normalize_doi(item.doi) or "",
                "pmid": item.pmid or "", "origin": note, "auto_prescreen": "failed",
            }
            for item, note in auto_excluded
        ]
        (out_dir / "auto-excluded-v0.4.csv").write_text(to_asreview_csv(excluded_rows), encoding="utf-8")
        with (out_dir / "oa-triage-v0.4.jsonl").open("w", encoding="utf-8") as handle:
            for item in decisions:
                handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        needs = [item for item in decisions if item.category == "needs_institutional_access"]
        profile = load_profile(self.config.institution_profile)
        access_routes = write_access_outputs(out_dir, [
            {"title": item.title, "doi": item.doi or "", "pmid": item.pmid or "", "landing_url": item.landing_url or ""}
            for item in needs
        ], profile)
        curve_lines = ["round,frontier,backward_links,forward_links,new_unique,new_relevant,cumulative_relevant,papers_truncated"]
        for log in snowball.rounds:
            curve_lines.append(
                f"{log.round},{log.frontier_size},{log.backward_links},{log.forward_links},"
                f"{log.new_unique_candidates},{log.new_relevant},{log.cumulative_relevant},{log.papers_truncated}"
            )
        (out_dir / "saturation-v0.4.csv").write_text("\n".join(curve_lines) + "\n", encoding="utf-8")
        (out_dir / "saturation-v0.4.svg").write_text(saturation_svg(snowball.rounds), encoding="utf-8")

        database_records = {name: per_source_raw.get(name, 0) for name in self.config.sources}
        prisma = prisma_counts(
            database_records=database_records,
            citation_records=len(snowball.relevant) + len(snowball.not_relevant),
            other_records=0,
            after_dedup=len(merged) + len(snowball.relevant) + len(snowball.not_relevant),
            auto_excluded=len(auto_excluded),
            to_human_screening=len(screening),
            oa_available=oa_counts.get("oa_machine_readable", 0) + oa_counts.get("oa_link_only", 0),
            needs_access=oa_counts.get("needs_institutional_access", 0),
        )
        (out_dir / "prisma-v0.4.json").write_text(json.dumps(prisma, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        summary = {
            "schema_version": "exhaustive-0.4",
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "config": {**asdict(self.config), "seeds": list(self.config.seeds),
                       "institution_profile": bool(self.config.institution_profile)},
            "relevance_rule": self.rule.to_list(),
            "sources": source_report,
            "source_contribution": contribution,
            "merged_after_dedup": len(merged),
            "snowball": {
                "graph": snowball.graph,
                "seeds": len(seeds),
                "settings": snowball.settings,
                "rounds": snowball.curve(),
                "saturated": snowball.saturated,
                "stop_reason": snowball.stop_reason,
                "new_relevant_total": len(snowball.relevant),
                "new_not_relevant_total": len(snowball.not_relevant),
            },
            "screening_records": len(screening),
            "auto_excluded_records": len(auto_excluded),
            "oa_triage": oa_counts,
            "institutional_access": {
                "institution": profile.institution if profile else None,
                "routes": access_routes,
                "notice": INSTITUTION_NOTICE,
            },
            "prisma": prisma,
            "issues": self.issues,
            "scope_statement": SCOPE_STATEMENT,
        }
        (out_dir / "exhaustive-report-v0.4.md").write_text(render_report(summary), encoding="utf-8")
        outputs = sorted(path for path in out_dir.glob("*-v0.4.*") if path.is_file())
        summary["output_sha256"] = {path.name: _sha(path) for path in outputs}
        (out_dir / "exhaustive-manifest-v0.4.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return summary


def render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Exhaustive retrieval report (v0.4)",
        "",
        f"- Query: `{summary['config']['query']}`",
        f"- Keyword pre-screen (all groups must match): `{' AND '.join('(' + g + ')' for g in summary['relevance_rule'])}`",
        "",
        "## Sources",
        "",
        "| Source | Kind | Reported hits | Returned | Pagination | After merge | Unique to source |",
        "|---|---|---:|---:|---|---:|---:|",
    ]
    for name, entry in summary["sources"].items():
        contribution = summary["source_contribution"].get(name, {})
        lines.append(
            f"| {name} | {entry.get('kind')} | {entry.get('total_reported')} | {entry.get('returned')} | "
            f"{entry.get('pagination_state', entry.get('error', ''))} | "
            f"{contribution.get('records_after_merge', 0)} | {contribution.get('unique_to_source', 0)} |"
        )
    snow = summary["snowball"]
    lines += [
        "",
        f"Merged after de-duplication: **{summary['merged_after_dedup']}**",
        "",
        f"## Citation snowballing ({snow['graph']}, {snow['seeds']} seeds)",
        "",
        "| Round | Frontier | Backward links | Forward links | New unique | New relevant | Cumulative relevant | Papers truncated |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for log in snow["rounds"]:
        lines.append(
            f"| {log['round']} | {log['frontier_size']} | {log['backward_links']} | {log['forward_links']} | "
            f"{log['new_unique_candidates']} | {log['new_relevant']} | {log['cumulative_relevant']} | {log['papers_truncated']} |"
        )
    lines += [
        "",
        f"Stop reason: {snow['stop_reason']}",
        "",
        "## Screening and access",
        "",
        f"- To human title/abstract screening: {summary['screening_records']}",
        f"- Excluded by automated keyword pre-screen: {summary['auto_excluded_records']}",
        f"- Open-access triage: {json.dumps(summary['oa_triage'], ensure_ascii=False)}",
        f"- Institutional access ({summary.get('institutional_access', {}).get('institution') or 'no profile'}): "
        f"{json.dumps(summary.get('institutional_access', {}).get('routes', {}), ensure_ascii=False)}; "
        "checklist in `institutional-access-v0.4.html` (manual, one article at a time)",
        "",
        "## Issues",
        "",
    ]
    lines += [f"- {item['source']} / {item['stage']}: {item['message']}" for item in summary["issues"]] or ["- none"]
    lines += ["", "## Scope", "", summary["scope_statement"], ""]
    return "\n".join(lines)
