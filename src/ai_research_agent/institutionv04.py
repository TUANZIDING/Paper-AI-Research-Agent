"""Institutional-access profiles (v0.4).

A profile describes how *one user* reaches content that their own institution
subscribes to: the federated-login (CARSI/Shibboleth/OpenAthens) entry page
for each publisher platform, a WebVPN portal, and interlibrary-loan/document
delivery. It turns the "needs institutional access" list into a manual
checklist of links the user opens in their own browser (or with the Zotero
Connector).

Boundaries, enforced by design:

* The profile holds **no credentials**. Validation rejects password-, token-,
  cookie- or secret-like keys and URLs with embedded user info.
* The tool never logs in, never stores sessions, and never downloads through
  institutional access. Library licences forbid software-driven bulk
  downloading; each article is opened by the person entitled to it.
* Profiles live outside the repository (``--institution-profile PATH`` or
  ``RESEARCH_AGENT_INSTITUTION_PROFILE``). Anyone else using this project
  supplies a profile for the institution that licenses *them*.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

PROFILE_ENV = "RESEARCH_AGENT_INSTITUTION_PROFILE"
FORBIDDEN_KEY_PARTS = ("password", "passwd", "secret", "token", "cookie", "session", "apikey", "api_key", "username", "student_id")
USAGE_NOTICE = (
    "Open each link yourself and sign in with your own institutional account. "
    "Do not use scripts or download tools for bulk retrieval; library licences "
    "forbid it. Items your institution does not license: use interlibrary loan / "
    "document delivery or contact the authors."
)


class ProfileError(ValueError):
    pass


@dataclass
class PlatformEntry:
    name: str
    entry_url: str
    doi_prefixes: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)


@dataclass
class InstitutionProfile:
    institution: str
    platforms: list[PlatformEntry]
    federation_entity_id: str | None = None
    webvpn_portal: str | None = None
    document_delivery: str | None = None
    federation_discovery: str | None = None

    def match(self, doi: str | None, landing_url: str | None = None) -> PlatformEntry | None:
        doi = (doi or "").lower()
        host = urlparse(landing_url or "").hostname or ""
        for platform in self.platforms:
            if doi and any(doi.startswith(prefix.rstrip("/").lower() + "/") for prefix in platform.doi_prefixes):
                return platform
            if host and any(host == h or host.endswith("." + h) for h in platform.hosts):
                return platform
        return None


def _check_url(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        raise ProfileError(f"{where}: expected an http(s) URL")
    parsed = urlparse(value)
    if parsed.username or parsed.password:
        raise ProfileError(f"{where}: URLs must not embed credentials")
    return value


def _check_keys(data: Any, path: str = "profile") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            lowered = str(key).lower().replace("-", "_")
            if any(part in lowered for part in FORBIDDEN_KEY_PARTS):
                raise ProfileError(f"{path}.{key}: profiles must not contain credentials or account identifiers")
            _check_keys(value, f"{path}.{key}")
    elif isinstance(data, list):
        for index, value in enumerate(data):
            _check_keys(value, f"{path}[{index}]")


def parse_profile(data: dict[str, Any]) -> InstitutionProfile:
    if not isinstance(data, dict):
        raise ProfileError("profile must be a JSON object")
    _check_keys(data)
    institution = data.get("institution")
    if not isinstance(institution, str) or not institution.strip():
        raise ProfileError("profile.institution is required")
    platforms = []
    for index, raw in enumerate(data.get("platforms") or []):
        where = f"profile.platforms[{index}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ProfileError(f"{where}: name is required")
        platforms.append(PlatformEntry(
            name=raw["name"],
            entry_url=_check_url(raw.get("entry_url"), f"{where}.entry_url"),
            doi_prefixes=[str(p) for p in raw.get("doi_prefixes") or []],
            hosts=[str(h).lower() for h in raw.get("hosts") or []],
        ))
    optional = {}
    for key in ("webvpn_portal", "document_delivery", "federation_discovery"):
        if data.get(key) is not None:
            optional[key] = _check_url(data[key], f"profile.{key}")
    entity = data.get("federation_entity_id")
    if entity is not None and not isinstance(entity, str):
        raise ProfileError("profile.federation_entity_id must be a string")
    return InstitutionProfile(institution=institution.strip(), platforms=platforms,
                              federation_entity_id=entity, **optional)


def load_profile(path: str | Path | None = None) -> InstitutionProfile | None:
    """Load from an explicit path or ``RESEARCH_AGENT_INSTITUTION_PROFILE``; None if unset."""

    location = path or os.getenv(PROFILE_ENV)
    if not location:
        return None
    file = Path(location).expanduser()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read institution profile {file}: {exc}") from exc
    return parse_profile(data)


@dataclass
class AccessRow:
    title: str
    doi: str
    pmid: str
    landing_url: str
    platform: str
    route: str
    entry_url: str


def plan_access(rows: Iterable[dict[str, str]], profile: InstitutionProfile | None) -> list[AccessRow]:
    planned = []
    for row in rows:
        doi = (row.get("doi") or "").strip()
        landing = (row.get("landing_url") or "").strip() or (f"https://doi.org/{quote(doi, safe='/')}" if doi else "")
        if profile is None:
            platform, route, entry = "", "no_profile", ""
        else:
            match = profile.match(doi, landing)
            if match is not None:
                platform, route, entry = match.name, "federated_login", match.entry_url
            elif profile.webvpn_portal:
                platform, route, entry = "", "webvpn_then_doi", profile.webvpn_portal
            elif profile.document_delivery:
                platform, route, entry = "", "document_delivery", profile.document_delivery
            else:
                platform, route, entry = "", "doi_only", ""
        planned.append(AccessRow(row.get("title", ""), doi, row.get("pmid", ""), landing, platform, route, entry))
    return planned


def to_tsv(planned: list[AccessRow], profile: InstitutionProfile | None) -> str:
    def clean(value: str) -> str:
        return " ".join(str(value).split())

    header = "title\tdoi\tpmid\tlanding_url\tinstitution\tplatform\troute\tentry_url"
    lines = [header]
    for row in planned:
        lines.append("\t".join(clean(v) for v in (
            row.title, row.doi, row.pmid, row.landing_url,
            profile.institution if profile else "", row.platform, row.route, row.entry_url)))
    return "\n".join(lines) + "\n"


def to_html(planned: list[AccessRow], profile: InstitutionProfile | None) -> str:
    esc = html.escape
    title = f"Institutional access checklist — {profile.institution}" if profile else "Institutional access checklist"
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{esc(title)}</title>",
        "<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:2em auto;padding:0 1em}"
        "li{margin:.6em 0}small{color:#555}</style></head><body>",
        f"<h1>{esc(title)}</h1>",
        f"<p><strong>{esc(USAGE_NOTICE)}</strong></p>",
    ]
    if profile is None:
        parts.append("<p>No institution profile configured: links go to the DOI landing page only.</p>")
    else:
        extras = []
        if profile.webvpn_portal:
            extras.append(f"<a href='{esc(profile.webvpn_portal)}'>WebVPN portal</a>")
        if profile.document_delivery:
            extras.append(f"<a href='{esc(profile.document_delivery)}'>Interlibrary loan / document delivery</a>")
        if extras:
            parts.append("<p>" + " · ".join(extras) + "</p>")
    parts.append("<ol>")
    for row in planned:
        links = []
        if row.entry_url and row.route == "federated_login":
            links.append(f"<a href='{esc(row.entry_url)}'>1. Sign in via {esc(row.platform)}</a>")
        if row.landing_url:
            links.append(f"<a href='{esc(row.landing_url)}'>{'2. ' if links else ''}Open article</a>")
        meta = " · ".join(v for v in (f"DOI {row.doi}" if row.doi else "", f"PMID {row.pmid}" if row.pmid else "", row.route) if v)
        parts.append(f"<li>{esc(row.title)}<br><small>{esc(meta)}</small><br>{' &nbsp; '.join(links)}</li>")
    parts.append("</ol></body></html>")
    return "\n".join(parts) + "\n"


def read_needs_tsv(path: str | Path) -> list[dict[str, str]]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:] if line.strip()]


def write_access_outputs(out_dir: Path, rows: list[dict[str, str]], profile: InstitutionProfile | None) -> dict[str, int]:
    planned = plan_access(rows, profile)
    (out_dir / "needs-institutional-access-v0.4.tsv").write_text(to_tsv(planned, profile), encoding="utf-8")
    (out_dir / "institutional-access-v0.4.html").write_text(to_html(planned, profile), encoding="utf-8")
    counts: dict[str, int] = {}
    for row in planned:
        counts[row.route] = counts.get(row.route, 0) + 1
    return counts


MAX_INTERACTIVE_ARTICLES = 20


def login_pages(rows: list[dict[str, str]], profile: InstitutionProfile) -> list[tuple[str, str]]:
    """Distinct sign-in pages needed for these rows, in first-seen order."""

    pages: dict[str, str] = {}
    for row in plan_access(rows, profile):
        if row.route == "federated_login" and row.entry_url not in pages:
            pages[row.entry_url] = row.platform
        elif row.route == "webvpn_then_doi" and row.entry_url not in pages:
            pages[row.entry_url] = "WebVPN"
    return [(name, url) for url, name in pages.items()]


def open_in_browser(
    rows: list[dict[str, str]],
    profile: InstitutionProfile,
    *,
    open_login: bool = True,
    open_articles: int = 0,
    opener=None,
    ask=input,
    interactive: bool | None = None,
) -> dict[str, int]:
    """Pop up sign-in pages (and optionally articles) in the user's own browser.

    The user types their credentials into their institution's page; this
    process never sees, stores or replays them. Articles open one at a time and
    only after the user presses Enter, capped at MAX_INTERACTIVE_ARTICLES, so
    retrieval stays human-paced as library licences require.
    """

    import sys
    import webbrowser

    opener = opener or webbrowser.open_new_tab
    if interactive is None:
        interactive = sys.stdin.isatty()
    opened = {"login_pages": 0, "articles": 0}
    if open_login:
        for name, url in login_pages(rows, profile):
            print(f"Opening sign-in page for {name}: {url}")
            opener(url)
            opened["login_pages"] += 1
        if opened["login_pages"]:
            print("Sign in with your own account in the browser windows that opened.")
    if open_articles:
        if not interactive:
            raise ProfileError("--open-articles needs an interactive terminal (human-paced opening)")
        limit = min(open_articles, MAX_INTERACTIVE_ARTICLES)
        candidates = [row for row in plan_access(rows, profile) if row.landing_url][:limit]
        for index, row in enumerate(candidates, 1):
            reply = ask(f"[{index}/{len(candidates)}] Enter to open, q to stop: {row.title[:80]} ")
            if reply.strip().lower() == "q":
                break
            opener(row.landing_url)
            opened["articles"] += 1
    return opened
