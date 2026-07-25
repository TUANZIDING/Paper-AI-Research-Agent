#!/usr/bin/env python3
"""Generate the deterministic synthetic reliability benchmark.

This corpus exercises software contracts. It is deliberately labelled as
synthetic and must never be reported as a human-adjudicated gold standard.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


CATEGORIES = (
    ("normal", "unknown", True, False),
    ("retraction", "retracted", False, True),
    ("correction", "correction", False, True),
    ("expression_of_concern", "expression_of_concern", False, True),
    ("identifier_mismatch", "unknown", False, True),
    ("partial_api", "unknown", False, True),
    ("wrong_target", "unknown", False, True),
    ("missing_status_hash", "unknown", False, True),
)


def build_cases(per_category: int = 30) -> list[dict[str, object]]:
    if per_category < 25:
        raise ValueError("per_category must be at least 25 (200 total cases)")
    cases: list[dict[str, object]] = []
    for category, status, ready, critical in CATEGORIES:
        for index in range(1, per_category + 1):
            cases.append(
                {
                    "case_id": f"synthetic-{category}-{index:03d}",
                    "category": category,
                    "scenario_variant": index,
                    "expected_publication_status": status,
                    "expected_citation_ready": ready,
                    "critical": critical,
                    "label_source": "synthetic_contract_v1",
                    "human_adjudicated": False,
                }
            )
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/golden/reliability_cases_240.json"),
    )
    parser.add_argument("--per-category", type=int, default=30)
    args = parser.parse_args()
    cases = build_cases(args.per_category)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(cases)} synthetic cases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
