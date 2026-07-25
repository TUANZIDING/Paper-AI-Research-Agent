from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class BenchmarkProfile:
    total_cases: int
    category_counts: dict[str, int]
    human_adjudicated_cases: int
    synthetic_cases: int
    shape_gate_passed: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ReliabilityMetrics:
    total_cases: int
    missing_cases: int
    status_mismatches: int
    citation_false_positives: int
    citation_false_negatives: int
    critical_failures: tuple[str, ...]

    @property
    def release_gate_passed(self) -> bool:
        return (
            not self.critical_failures
            and self.missing_cases == 0
            and self.status_mismatches == 0
            and self.citation_false_positives == 0
            and self.citation_false_negatives == 0
        )


def load_golden_cases(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Golden corpus must be a JSON array")
    case_ids: set[str] = set()
    cases: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
            raise ValueError("Every golden case requires a string case_id")
        if item["case_id"] in case_ids:
            raise ValueError(f"Duplicate golden case: {item['case_id']}")
        case_ids.add(item["case_id"])
        cases.append(item)
    return cases


def profile_benchmark(
    cases: Iterable[dict[str, Any]],
    *,
    minimum_cases: int = 200,
    minimum_per_category: int = 20,
) -> BenchmarkProfile:
    items = list(cases)
    counts: dict[str, int] = {}
    errors: list[str] = []
    human = 0
    synthetic = 0
    for case in items:
        category = case.get("category")
        if not isinstance(category, str) or not category:
            errors.append(f"category:{case.get('case_id', '<missing>')}")
            continue
        counts[category] = counts.get(category, 0) + 1
        if case.get("human_adjudicated") is True:
            human += 1
        elif case.get("label_source") == "synthetic_contract_v1":
            synthetic += 1
        else:
            errors.append(f"label_provenance:{case.get('case_id', '<missing>')}")
    if len(items) < minimum_cases:
        errors.append(f"minimum_cases:{len(items)}/{minimum_cases}")
    for category, count in sorted(counts.items()):
        if count < minimum_per_category:
            errors.append(f"category_minimum:{category}:{count}/{minimum_per_category}")
    if len(counts) < 6:
        errors.append(f"category_coverage:{len(counts)}/6")
    return BenchmarkProfile(
        total_cases=len(items),
        category_counts=counts,
        human_adjudicated_cases=human,
        synthetic_cases=synthetic,
        shape_gate_passed=not errors,
        errors=tuple(errors),
    )


def evaluate_golden_cases(
    cases: Iterable[dict[str, Any]],
    actual_records: Iterable[dict[str, Any]],
) -> ReliabilityMetrics:
    expected = {case["case_id"]: case for case in cases}
    actual: dict[str, dict[str, Any]] = {}
    input_failures: list[str] = []
    for index, record in enumerate(actual_records):
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            input_failures.append(f"actual_missing_case_id:{index}")
            continue
        if case_id in actual:
            input_failures.append(f"actual_duplicate:{case_id}")
            continue
        actual[case_id] = record
    for case_id in sorted(set(actual) - set(expected)):
        input_failures.append(f"actual_unexpected:{case_id}")
    missing = 0
    status_mismatches = 0
    false_positives = 0
    false_negatives = 0
    failures: list[str] = list(input_failures)
    for case_id, case in expected.items():
        record = actual.get(case_id)
        if record is None:
            missing += 1
            failures.append(f"missing:{case_id}")
            continue
        expected_status = case.get("expected_publication_status")
        if expected_status is not None and record.get("publication_status") != expected_status:
            status_mismatches += 1
            if case.get("critical", True):
                failures.append(f"status:{case_id}")
        expected_ready = bool(case.get("expected_citation_ready", False))
        actual_ready = bool(record.get("citation_ready", False))
        if actual_ready and not expected_ready:
            false_positives += 1
            failures.append(f"citation_false_positive:{case_id}")
        elif expected_ready and not actual_ready:
            false_negatives += 1
            if case.get("critical", False):
                failures.append(f"citation_false_negative:{case_id}")
    return ReliabilityMetrics(
        total_cases=len(expected),
        missing_cases=missing,
        status_mismatches=status_mismatches,
        citation_false_positives=false_positives,
        citation_false_negatives=false_negatives,
        critical_failures=tuple(sorted(set(failures))),
    )
