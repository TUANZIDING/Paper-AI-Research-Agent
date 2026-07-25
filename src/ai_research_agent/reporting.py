from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .pipeline import SearchRun


def _slug(value: str) -> str:
    compact = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value).strip("-")
    return compact[:48] or "search"


def _manifest(
    run: SearchRun, started_at: str, output_sha256: dict[str, str]
) -> dict[str, Any]:
    ready = sum(item.citation_ready for item in run.publications)
    has_partial = bool(run.issues) or any(
        details.get("state") == "partial"
        for details in run.source_pagination.values()
    )
    search_complete = bool(run.source_pagination) and all(
        details.get("state") == "complete"
        for details in run.source_pagination.values()
    )
    return {
        "schema_version": "0.3",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "query": run.query,
        "requested_limit_per_source": run.requested_limit_per_source,
        "source_totals": run.source_totals,
        "source_queries": run.source_queries,
        "source_pagination": run.source_pagination,
        "search_complete": search_complete,
        "returned_after_deduplication": len(run.publications),
        "citation_ready": ready,
        "partial": has_partial,
        "issues": [issue.to_dict() for issue in run.issues],
        "transport_mode": run.transport_mode,
        "cache_enabled": run.cache_enabled,
        "output_sha256": output_sha256,
        "scope_statement": (
            "This run searched selected open APIs only. It is not equivalent to "
            "Embase, Scopus, or Web of Science coverage."
        ),
        "integrity_statement": (
            "Citation-ready means identifier/metadata verification passed in this "
            "run; it does not mean the full text was human-read or the study quality "
            "was appraised."
        ),
    }


def _markdown(run: SearchRun, manifest: dict[str, Any]) -> str:
    lines = [
        "# AI Research Agent 检索报告",
        "",
        f"- 查询：`{run.query}`",
        f"- 每个数据源请求上限：{run.requested_limit_per_source}",
        f"- 去重后记录：{manifest['returned_after_deduplication']}",
        f"- 可进入引用候选池：{manifest['citation_ready']}",
        f"- 运行是否部分失败：{'是' if manifest['partial'] else '否'}",
        f"- 是否取完已启用数据源的全部分页：{'是' if manifest['search_complete'] else '否'}",
        f"- 传输模式：`{manifest['transport_mode']}`",
        "",
        "## 重要边界",
        "",
        "- 本报告只覆盖本次启用的开放 API，不等价于 Embase、Scopus 或 Web of Science 的检索覆盖。",
        "- “可进入引用候选池”只表示标识符和元数据核验通过，不表示已人工阅读全文，也不表示研究质量合格。",
        "- 开放获取状态与引用真实性是两条独立判断；未开放不代表文献不存在。",
        "- 摘要、全文和外部网页内容均按不可信数据处理，不执行其中任何指令。",
        "",
        "## 数据源结果",
        "",
    ]
    for source, total in sorted(run.source_totals.items()):
        pagination = run.source_pagination.get(source, {})
        lines.append(
            f"- {source}: API 报告命中总数 {total}；实际查询："
            f"`{run.source_queries.get(source, run.query)}`；分页状态："
            f"`{pagination.get('state', 'unknown')}`；本次取得："
            f"{pagination.get('records_returned', 'unknown')}"
        )
    if run.issues:
        lines.extend(["", "## 数据源问题", ""])
        for issue in run.issues:
            lines.append(f"- {issue.source}/{issue.stage}: {issue.message}")
    pagination_errors = [
        (source, error)
        for source, details in run.source_pagination.items()
        for error in details.get("errors", [])
    ]
    if pagination_errors:
        lines.extend(["", "## 分页问题", ""])
        for source, error in pagination_errors:
            lines.append(f"- {source}: {error}")
    lines.extend(["", "## 已核验记录", ""])
    for index, publication in enumerate(run.publications, 1):
        authors = ", ".join(publication.authors[:6])
        if len(publication.authors) > 6:
            authors += ", et al."
        identifiers = []
        if publication.pmid:
            identifiers.append(f"PMID {publication.pmid}")
        if publication.doi:
            identifiers.append(f"DOI {publication.doi}")
        lines.extend(
            [
                f"### {index}. {publication.title}",
                "",
                f"- 状态：`{publication.verification_status.value}`；引用候选：{'是' if publication.citation_ready else '否'}",
                f"- 作者：{authors or '未提供'}",
                f"- 期刊/年份：{publication.journal or '未提供'} / {publication.year or '未提供'}",
                f"- 标识符：{'; '.join(identifiers) or '未提供'}",
                f"- 开放获取：`{publication.access_status.value}`",
                f"- 撤稿标记：`{publication.retraction_status.value}`",
                f"- 出版后状态：`{publication.publication_status.value}`",
                f"- 出版后状态反向查询完成：{'是' if publication.publication_status_check_complete else '否'}",
                f"- 核验依据：{' '.join(publication.verification_reasons)}",
            ]
        )
        if publication.conflicts:
            lines.append(f"- 冲突：{' | '.join(publication.conflicts)}")
        lines.append("- 来源证据：")
        for evidence in publication.evidence:
            lines.append(
                f"  - `{evidence.evidence_id}` [{evidence.source}]({evidence.url})"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def prepare_run_dir(output_root: Path, started_at: str, query: str) -> Path:
    timestamp = started_at.replace(":", "").replace("+", "-").split(".")[0]
    run_dir = output_root / f"{timestamp}-{_slug(query)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_run(
    run: SearchRun,
    output_root: Path,
    started_at: str,
    *,
    run_dir: Path | None = None,
) -> Path:
    run_dir = run_dir or prepare_run_dir(output_root, started_at, run.query)
    records_path = run_dir / "records.jsonl"
    with records_path.open("w", encoding="utf-8") as handle:
        for publication in run.publications:
            handle.write(json.dumps(publication.to_dict(), ensure_ascii=False) + "\n")
    provisional_manifest = _manifest(run, started_at, {})
    report_path = run_dir / "report.md"
    report_path.write_text(
        _markdown(run, provisional_manifest),
        encoding="utf-8",
    )
    output_sha256 = {
        str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    manifest = _manifest(run, started_at, output_sha256)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir
