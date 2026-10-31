"""Iterative citation snowballing with an explicit saturation stop rule (v0.4).

Each round takes the frontier (relevant records found in the previous round),
pulls backward links (references) and forward links (citing works), removes
everything already seen, and applies a transparent keyword pre-screen. Only
records that pass the pre-screen enter the next frontier. The run stops when
a round adds no more than ``stop_threshold`` new relevant records.

Saturation here is a heuristic over the chosen citation graph and keyword
rule. It is never evidence that a systematic-review search is complete.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import html
import re
from typing import Iterable, Protocol

from .http import RemoteServiceError
from .models import Publication, normalize_doi, normalize_title

SATURATION_DISCLAIMER = (
    "Saturation means the chosen citation graph and keyword pre-screen stopped "
    "yielding new candidates. It does not mean the search is complete for a "
    "systematic review: Embase, Web of Science, Scopus, CNKI/Wanfang/VIP, grey "
    "literature and human screening are still required."
)


def record_keys(publication: Publication) -> set[str]:
    keys = set()
    doi = normalize_doi(publication.doi)
    if doi:
        keys.add(f"doi:{doi}")
    if publication.pmid:
        keys.add(f"pmid:{publication.pmid}")
    title = normalize_title(publication.title or "")
    if len(title) >= 20:
        keys.add(f"title:{title}")
    return keys


def clean_abstract(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


@dataclass(frozen=True)
class RelevanceRule:
    """All groups must match; within a group any term may match.

    A term ending in ``*`` is a word-prefix match (``irradiat*``); other terms
    are matched as whole words or phrases. Matching is case-insensitive.
    """

    groups: tuple[tuple[str, ...], ...]

    @classmethod
    def parse(cls, specs: Iterable[str]) -> "RelevanceRule":
        groups = []
        for spec in specs:
            terms = tuple(term.strip() for term in spec.split("|") if term.strip())
            if terms:
                groups.append(terms)
        if not groups:
            raise ValueError("At least one relevance group is required")
        return cls(tuple(groups))

    @staticmethod
    def _pattern(term: str) -> re.Pattern[str]:
        if term.endswith("*"):
            return re.compile(r"\b" + re.escape(term[:-1].casefold()) + r"\w*")
        return re.compile(r"\b" + re.escape(term.casefold()) + r"\b")

    def matches(self, text: str) -> bool:
        haystack = text.casefold()
        return all(
            any(self._pattern(term).search(haystack) for term in group)
            for group in self.groups
        )

    def to_list(self) -> list[str]:
        return ["|".join(group) for group in self.groups]


class CitationGraph(Protocol):
    name: str
    abstracts: dict[str, str]

    def backward(self, pmid: str, limit: int) -> tuple[list[str], int, int]: ...
    def forward(self, pmid: str, limit: int) -> tuple[list[str], int, int]: ...
    def resolve_pmid(self, doi: str) -> str | None: ...
    def fetch_core(self, pmids: list[str], batch: int = 100) -> list[Publication]: ...


@dataclass
class RoundLog:
    round: int
    frontier_size: int
    frontier_without_pmid: int = 0
    backward_links: int = 0
    forward_links: int = 0
    links_without_pmid: int = 0
    papers_truncated: int = 0
    new_unique_candidates: int = 0
    new_relevant: int = 0
    new_not_relevant: int = 0
    unresolved_candidates: int = 0
    cumulative_relevant: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SnowballResult:
    graph: str
    rule: list[str]
    rounds: list[RoundLog]
    relevant: list[Publication]
    not_relevant: list[Publication]
    stop_reason: str
    saturated: bool
    settings: dict[str, object]
    disclaimer: str = SATURATION_DISCLAIMER

    def curve(self) -> list[dict[str, object]]:
        return [asdict(item) for item in self.rounds]


class SnowballEngine:
    def __init__(
        self,
        graph: CitationGraph,
        rule: RelevanceRule,
        *,
        max_rounds: int = 5,
        per_paper_limit: int = 500,
        stop_threshold: int = 0,
        max_candidates_per_round: int = 5000,
        directions: tuple[str, ...] = ("backward", "forward"),
    ):
        if max_rounds < 1 or per_paper_limit < 1 or stop_threshold < 0:
            raise ValueError("Invalid snowball limits")
        if not set(directions) <= {"backward", "forward"} or not directions:
            raise ValueError("directions must be backward and/or forward")
        self.graph = graph
        self.rule = rule
        self.max_rounds = max_rounds
        self.per_paper_limit = per_paper_limit
        self.stop_threshold = stop_threshold
        self.max_candidates_per_round = max_candidates_per_round
        self.directions = directions

    def text_for(self, publication: Publication, extra_abstracts: dict[str, str] | None = None) -> str:
        abstract = self.graph.abstracts.get(publication.dedupe_key) or (extra_abstracts or {}).get(
            publication.dedupe_key
        )
        return f"{publication.title} {clean_abstract(abstract)}"

    def run(
        self,
        seeds: list[Publication],
        already_seen: Iterable[Publication] = (),
    ) -> SnowballResult:
        seen: set[str] = set()
        for item in list(already_seen) + list(seeds):
            seen |= record_keys(item)
        relevant: list[Publication] = []
        not_relevant: list[Publication] = []
        rounds: list[RoundLog] = []
        frontier = list(seeds)
        stop_reason = ""
        saturated = False
        for number in range(1, self.max_rounds + 1):
            log = RoundLog(round=number, frontier_size=len(frontier))
            if not frontier:
                stop_reason = f"Round {number}: frontier is empty; no new relevant records to expand."
                saturated = True
                break
            linked: list[str] = []
            for paper in frontier:
                pmid = paper.pmid
                if not pmid and paper.doi:
                    try:
                        pmid = self.graph.resolve_pmid(paper.doi)
                    except RemoteServiceError as exc:
                        log.errors.append(f"resolve {paper.doi}: {exc}")
                    if pmid:
                        paper.pmid = pmid
                if not pmid:
                    log.frontier_without_pmid += 1
                    continue
                for direction in self.directions:
                    try:
                        found, total, without = getattr(self.graph, direction)(
                            pmid, self.per_paper_limit
                        )
                    except RemoteServiceError as exc:
                        log.errors.append(f"{direction} PMID {pmid}: {exc}")
                        continue
                    if total > self.per_paper_limit:
                        log.papers_truncated += 1
                    if direction == "backward":
                        log.backward_links += len(found)
                    else:
                        log.forward_links += len(found)
                    log.links_without_pmid += without
                    linked.extend(found)
            new_pmids = [value for value in dict.fromkeys(linked) if f"pmid:{value}" not in seen]
            if len(new_pmids) > self.max_candidates_per_round:
                log.errors.append(
                    f"Round candidate budget exceeded: kept {self.max_candidates_per_round} "
                    f"of {len(new_pmids)} new PMIDs"
                )
                new_pmids = new_pmids[: self.max_candidates_per_round]
            for value in new_pmids:
                seen.add(f"pmid:{value}")
            try:
                resolved = self.graph.fetch_core(new_pmids)
            except RemoteServiceError as exc:
                resolved = []
                log.errors.append(f"metadata fetch: {exc}")
            resolved_pmids = {item.pmid for item in resolved if item.pmid}
            log.unresolved_candidates = len(set(new_pmids) - resolved_pmids)
            next_frontier: list[Publication] = []
            for publication in resolved:
                keys = record_keys(publication) - {f"pmid:{publication.pmid}"}
                if keys & seen:
                    continue  # same work already seen under a DOI or title
                seen |= record_keys(publication)
                log.new_unique_candidates += 1
                if self.rule.matches(self.text_for(publication)):
                    relevant.append(publication)
                    next_frontier.append(publication)
                    log.new_relevant += 1
                else:
                    not_relevant.append(publication)
                    log.new_not_relevant += 1
            log.cumulative_relevant = len(relevant)
            rounds.append(log)
            if log.new_relevant <= self.stop_threshold:
                stop_reason = (
                    f"Saturated at round {number}: {log.new_relevant} new relevant record(s), "
                    f"at or below the stop threshold of {self.stop_threshold}."
                )
                saturated = True
                break
            frontier = next_frontier
        else:
            stop_reason = (
                f"Stopped at the maximum of {self.max_rounds} rounds while round "
                f"{self.max_rounds} still added {rounds[-1].new_relevant} relevant record(s): "
                "NOT saturated."
            )
        return SnowballResult(
            graph=self.graph.name,
            rule=self.rule.to_list(),
            rounds=rounds,
            relevant=relevant,
            not_relevant=not_relevant,
            stop_reason=stop_reason,
            saturated=saturated,
            settings={
                "max_rounds": self.max_rounds,
                "per_paper_limit": self.per_paper_limit,
                "stop_threshold": self.stop_threshold,
                "max_candidates_per_round": self.max_candidates_per_round,
                "directions": list(self.directions),
                "seeds": len(seeds),
            },
        )


def saturation_svg(rounds: list[RoundLog], title: str = "Snowball saturation") -> str:
    """A dependency-free bar (new relevant) + line (cumulative) chart."""

    width, height, pad = 640, 360, 56
    if not rounds:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
            f'<text x="20" y="40">{html.escape(title)}: no rounds</text></svg>'
        )
    peak = max(max(item.cumulative_relevant for item in rounds), 1)
    plot_w, plot_h = width - 2 * pad, height - 2 * pad
    step = plot_w / len(rounds)
    bar_w = step * 0.5
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text x="{pad}" y="28" font-size="15" font-weight="bold">{html.escape(title)}</text>',
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="#333"/>',
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height - pad}" stroke="#333"/>',
        f'<text x="{pad - 8}" y="{pad + 4}" text-anchor="end">{peak}</text>',
        f'<text x="{pad - 8}" y="{height - pad + 4}" text-anchor="end">0</text>',
    ]
    points = []
    for index, item in enumerate(rounds):
        x = pad + step * index + (step - bar_w) / 2
        bar_h = plot_h * item.new_relevant / peak
        parts.append(
            f'<rect x="{x:.1f}" y="{height - pad - bar_h:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" fill="#0F766E"/>'
        )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{height - pad - bar_h - 6:.1f}" '
            f'text-anchor="middle">{item.new_relevant}</text>'
        )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{height - pad + 18:.1f}" '
            f'text-anchor="middle">R{item.round}</text>'
        )
        cy = height - pad - plot_h * item.cumulative_relevant / peak
        points.append(f"{x + bar_w / 2:.1f},{cy:.1f}")
    parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="#D97706" stroke-width="2"/>')
    parts.append(
        f'<text x="{width - pad}" y="{height - 14}" text-anchor="end">'
        'bars: new relevant per round · line: cumulative</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)
