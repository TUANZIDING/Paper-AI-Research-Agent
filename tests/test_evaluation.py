from pathlib import Path
import unittest

from ai_research_agent.evaluation import (
    evaluate_golden_cases,
    load_golden_cases,
    profile_benchmark,
)
from scripts.generate_reliability_benchmark import build_cases


FIXTURE = Path(__file__).parent / "fixtures/golden/reliability_cases.json"
BENCHMARK = Path(__file__).parent / "fixtures/golden/reliability_cases_240.json"


class ReliabilityEvaluationTests(unittest.TestCase):
    def test_seed_corpus_is_unique_and_loadable(self):
        cases = load_golden_cases(FIXTURE)
        self.assertEqual(len(cases), 6)

    def test_false_positive_is_release_blocking(self):
        cases = load_golden_cases(FIXTURE)
        actual = [
            {
                "case_id": case["case_id"],
                "publication_status": case["expected_publication_status"],
                "citation_ready": case["expected_citation_ready"],
            }
            for case in cases
        ]
        actual[1]["citation_ready"] = True
        metrics = evaluate_golden_cases(cases, actual)
        self.assertEqual(metrics.citation_false_positives, 1)
        self.assertFalse(metrics.release_gate_passed)

    def test_exact_expected_results_pass_seed_gate(self):
        cases = load_golden_cases(FIXTURE)
        actual = [
            {
                "case_id": case["case_id"],
                "publication_status": case["expected_publication_status"],
                "citation_ready": case["expected_citation_ready"],
            }
            for case in cases
        ]
        metrics = evaluate_golden_cases(cases, actual)
        self.assertTrue(metrics.release_gate_passed, metrics.critical_failures)

    def test_240_case_synthetic_benchmark_is_explicitly_not_human_gold(self):
        cases = load_golden_cases(BENCHMARK)
        profile = profile_benchmark(cases)
        self.assertEqual(profile.total_cases, 240)
        self.assertEqual(profile.synthetic_cases, 240)
        self.assertEqual(profile.human_adjudicated_cases, 0)
        self.assertTrue(profile.shape_gate_passed, profile.errors)

    def test_benchmark_generation_is_deterministic(self):
        self.assertEqual(load_golden_cases(BENCHMARK), build_cases(30))

    def test_rejecting_every_normal_case_fails_release_gate(self):
        cases = load_golden_cases(BENCHMARK)
        actual = [{
            "case_id": case["case_id"],
            "publication_status": case["expected_publication_status"],
            "citation_ready": False,
        } for case in cases]
        metrics = evaluate_golden_cases(cases, actual)
        self.assertEqual(metrics.citation_false_negatives, 30)
        self.assertFalse(metrics.release_gate_passed)

    def test_duplicate_and_unexpected_actual_cases_fail_closed(self):
        cases = load_golden_cases(FIXTURE)
        actual = [{
            "case_id": case["case_id"],
            "publication_status": case["expected_publication_status"],
            "citation_ready": case["expected_citation_ready"],
        } for case in cases]
        actual.extend([dict(actual[0]), {
            "case_id": "unexpected", "publication_status": "unknown",
            "citation_ready": False,
        }])
        metrics = evaluate_golden_cases(cases, actual)
        self.assertFalse(metrics.release_gate_passed)
        self.assertIn(f"actual_duplicate:{actual[0]['case_id']}", metrics.critical_failures)
        self.assertIn("actual_unexpected:unexpected", metrics.critical_failures)


if __name__ == "__main__":
    unittest.main()
