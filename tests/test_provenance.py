import unittest

from ai_research_agent.provenance import (
    FieldObservation,
    VersionEdge,
    VersionGraph,
    VersionRelation,
    decide_field,
)


class ProvenanceTests(unittest.TestCase):
    def test_priority_selection_retains_alternative(self):
        decision = decide_field(
            "title",
            [
                FieldObservation("crossref", "ev-2", "A title", "later"),
                FieldObservation("pubmed", "ev-1", "A title.", "earlier"),
            ],
            source_priority=("pubmed", "crossref"),
        )
        self.assertEqual(decision.selected_evidence_id, "ev-1")
        self.assertEqual(len(decision.alternatives), 1)
        self.assertFalse(decision.conflict)

    def test_materially_different_title_is_conflict(self):
        decision = decide_field(
            "title",
            [
                FieldObservation("pubmed", "ev-1", "Study A", "now"),
                FieldObservation("crossref", "ev-2", "Different study", "now"),
            ],
            source_priority=("pubmed", "crossref"),
        )
        self.assertTrue(decision.conflict)

    def test_version_graph_requires_evidence_and_blocks_cycle(self):
        graph = VersionGraph()
        graph.add(
            VersionEdge(
                "preprint:1",
                VersionRelation.PREPRINT_OF,
                "doi:10.1/final",
                ("ev-1",),
            )
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            graph.add(
                VersionEdge(
                    "doi:10.1/final",
                    VersionRelation.VERSION_OF_RECORD_OF,
                    "preprint:1",
                    ("ev-2",),
                )
            )

    def test_possible_same_study_never_implies_human_confirmation(self):
        graph = VersionGraph()
        graph.add(
            VersionEdge(
                "title:a",
                VersionRelation.POSSIBLE_SAME_STUDY,
                "title:b",
                ("ev-1",),
            )
        )
        self.assertFalse(graph.edges[0].confirmed_by_human)


if __name__ == "__main__":
    unittest.main()
