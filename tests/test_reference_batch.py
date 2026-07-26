from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_research_agent.reference_batch import (
    BatchExecutionContext,
    LookupOutcome,
    ReferenceBatchError,
    ReferenceBatchIntegrityError,
    ReferenceItem,
    load_checkpoint,
    result_to_dict,
    run_reference_batch,
)


class ReferenceBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.checkpoint = self.root / "batch" / "checkpoint.jsonl"
        self.references = [
            ReferenceItem(str(number), {"number": number, "title": f"Paper {number}"})
            for number in range(1, 76)
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def test_selected_references_only_and_75_item_completion(self):
        called = []

        def lookup(item, context):
            called.append(item.reference_id)
            self.assertEqual(context, BatchExecutionContext("network", True))
            return LookupOutcome("completed", "not-matched")

        selected = [str(number) for number in range(1, 76, 2)]
        result = run_reference_batch(
            self.references, selected, self.checkpoint, lookup, cache_enabled=True
        )
        self.assertEqual(called, selected)
        self.assertEqual(result.selected_count, len(selected))
        self.assertEqual(result.completed_count, len(selected))
        self.assertEqual(result.batch_status, "completed")
        self.assertFalse(result.semantic_support_assessed)

    def test_failure_is_recorded_and_later_run_resumes_only_unfinished(self):
        calls = {"1": 0, "2": 0, "3": 0}

        def flaky(item, _context):
            calls[item.reference_id] += 1
            if item.reference_id == "2":
                raise TimeoutError("temporary API failure")
            return LookupOutcome("completed", "high-confidence")

        first = run_reference_batch(
            self.references, ["1", "2", "3"], self.checkpoint, flaky
        )
        self.assertEqual(first.completed_count, 2)
        self.assertEqual(first.failed_count, 1)
        self.assertEqual(first.batch_status, "partial")

        def recovered(item, _context):
            calls[item.reference_id] += 1
            return LookupOutcome("completed", "review")

        second = run_reference_batch(
            self.references, ["1", "2", "3"], self.checkpoint, recovered
        )
        self.assertEqual(calls, {"1": 1, "2": 2, "3": 1})
        self.assertEqual(second.completed_count, 3)
        state = next(item for item in second.states if item.reference_id == "2")
        self.assertEqual(state.attempt_count, 2)
        self.assertEqual(state.match_status, "review")
        events = load_checkpoint(self.checkpoint)
        failed_events = [
            event for event in events
            if event.get("reference_id") == "2"
            and event.get("execution_status") == "failed"
        ]
        self.assertEqual(len(failed_events), 1)

    def test_repeated_run_does_not_convert_failure_to_success_without_executor(self):
        def failed(_item, _context):
            raise RuntimeError("still unavailable")

        first = run_reference_batch(
            self.references, ["1"], self.checkpoint, failed
        )
        self.assertEqual(first.batch_status, "failed")
        second = run_reference_batch(
            self.references, ["1"], self.checkpoint, failed, max_items=0
        )
        self.assertEqual(second.attempted_this_run, 0)
        self.assertEqual(second.failed_count, 1)
        self.assertEqual(second.states[0].match_status, None)

    def test_max_items_leaves_remainder_pending_then_resumes(self):
        def lookup(_item, _context):
            return LookupOutcome("completed", "not-matched")

        first = run_reference_batch(
            self.references, ["1", "2", "3"], self.checkpoint, lookup, max_items=1
        )
        self.assertEqual(first.completed_count, 1)
        self.assertEqual(first.pending_count, 2)
        self.assertEqual(first.batch_status, "partial")
        second = run_reference_batch(
            self.references, ["1", "2", "3"], self.checkpoint, lookup
        )
        self.assertEqual(second.attempted_this_run, 2)
        self.assertEqual(second.completed_count, 3)

    def test_crash_after_attempt_start_is_pending_and_recoverable(self):
        run_reference_batch(
            self.references,
            ["1"],
            self.checkpoint,
            lambda *_: LookupOutcome("completed", "not-matched"),
        )
        lines = self.checkpoint.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-2])["event_type"], "attempt_started")
        self.checkpoint.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

        seen = []
        result = run_reference_batch(
            self.references,
            ["1"],
            self.checkpoint,
            lambda item, _context: (
                seen.append(item.reference_id)
                or LookupOutcome("completed", "high-confidence")
            ),
        )
        self.assertEqual(seen, ["1"])
        self.assertEqual(result.completed_count, 1)
        self.assertEqual(result.states[0].attempt_count, 2)

    def test_partial_execution_and_match_confidence_are_separate_axes(self):
        result = run_reference_batch(
            self.references,
            ["1", "2", "3"],
            self.checkpoint,
            lambda item, _: LookupOutcome(
                "partial",
                {"1": "high-confidence", "2": "review", "3": "not-matched"}[
                    item.reference_id
                ],
            ),
        )
        self.assertEqual(result.partial_count, 3)
        self.assertEqual(
            {state.match_status for state in result.states},
            {"high-confidence", "review", "not-matched"},
        )
        self.assertTrue(
            all(state.semantic_support_status == "NOT_ASSESSED" for state in result.states)
        )

    def test_completed_query_never_upgrades_semantic_support(self):
        result = run_reference_batch(
            self.references,
            ["1"],
            self.checkpoint,
            lambda *_: {
                "execution_status": "completed",
                "match_status": "high-confidence",
                "evidence": {"doi": "10.1000/example"},
                "semantic_support_status": "SUPPORTED",
            },
        )
        payload = result_to_dict(result)
        self.assertFalse(payload["semantic_support_assessed"])
        self.assertEqual(payload["states"][0]["semantic_support_status"], "NOT_ASSESSED")
        final = load_checkpoint(self.checkpoint)[-1]
        self.assertFalse(final["semantic_support_assessed"])
        self.assertEqual(final["semantic_support_status"], "NOT_ASSESSED")

    def test_offline_replay_requires_cache_and_is_forwarded(self):
        with self.assertRaisesRegex(ReferenceBatchError, "requires cache"):
            run_reference_batch(
                self.references,
                ["1"],
                self.checkpoint,
                lambda *_: LookupOutcome("completed", "review"),
                transport_mode="offline_replay",
                cache_enabled=False,
            )

        seen = []
        result = run_reference_batch(
            self.references,
            ["1"],
            self.checkpoint,
            lambda _item, context: (
                seen.append(context) or LookupOutcome("completed", "review")
            ),
            transport_mode="offline_replay",
            cache_enabled=True,
        )
        self.assertEqual(seen, [BatchExecutionContext("offline_replay", True)])
        self.assertEqual(result.states[0].transport_mode, "offline_replay")

    def test_changed_selection_or_payload_cannot_resume_same_checkpoint(self):
        run_reference_batch(
            self.references,
            ["1", "2"],
            self.checkpoint,
            lambda *_: LookupOutcome("completed", "not-matched"),
        )
        with self.assertRaises(ReferenceBatchIntegrityError):
            run_reference_batch(
                self.references,
                ["1"],
                self.checkpoint,
                lambda *_: LookupOutcome("completed", "not-matched"),
            )
        changed = list(self.references)
        changed[0] = ReferenceItem("1", {"number": 1, "title": "Changed"})
        with self.assertRaises(ReferenceBatchIntegrityError):
            run_reference_batch(
                changed,
                ["1", "2"],
                self.checkpoint,
                lambda *_: LookupOutcome("completed", "not-matched"),
            )

    def test_changed_batch_context_cannot_resume_same_checkpoint(self):
        run_reference_batch(
            self.references,
            ["1"],
            self.checkpoint,
            lambda *_: LookupOutcome("completed", "not-matched"),
            batch_context={"source_pdf_sha256": "a" * 64},
        )
        with self.assertRaisesRegex(
            ReferenceBatchIntegrityError, "different selected references or inputs"
        ):
            run_reference_batch(
                self.references,
                ["1"],
                self.checkpoint,
                lambda *_: LookupOutcome("completed", "not-matched"),
                batch_context={"source_pdf_sha256": "b" * 64},
            )

    def test_tampered_or_truncated_checkpoint_is_rejected(self):
        run_reference_batch(
            self.references,
            ["1"],
            self.checkpoint,
            lambda *_: LookupOutcome("completed", "not-matched"),
        )
        lines = self.checkpoint.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[-1])
        event["match_status"] = "high-confidence"
        lines[-1] = json.dumps(event)
        self.checkpoint.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(ReferenceBatchIntegrityError):
            load_checkpoint(self.checkpoint)

        self.checkpoint.write_text("{", encoding="utf-8")
        with self.assertRaises(ReferenceBatchIntegrityError):
            load_checkpoint(self.checkpoint)

    def test_invalid_match_status_fails_closed_as_failed_attempt(self):
        result = run_reference_batch(
            self.references,
            ["1"],
            self.checkpoint,
            lambda *_: LookupOutcome("completed", "semantic-support"),
        )
        self.assertEqual(result.failed_count, 1)
        self.assertIsNone(result.states[0].match_status)
        self.assertIn("ReferenceBatchError", result.states[0].error)


if __name__ == "__main__":
    unittest.main()
