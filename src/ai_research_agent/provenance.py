from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
from typing import Any, Iterable

from .models import normalize_title


@dataclass(frozen=True)
class FieldObservation:
    source: str
    evidence_id: str
    value: Any
    retrieved_at: str


@dataclass(frozen=True)
class FieldDecision:
    field_name: str
    selected_value: Any
    selected_evidence_id: str
    rule: str
    alternatives: tuple[FieldObservation, ...] = ()
    conflict: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_field(
    field_name: str,
    observations: Iterable[FieldObservation],
    *,
    source_priority: tuple[str, ...],
) -> FieldDecision:
    """Select deterministically while retaining every competing observation."""
    candidates = [item for item in observations if item.value not in (None, "", [])]
    if not candidates:
        raise ValueError(f"No usable observations for {field_name}")
    priority = {source: index for index, source in enumerate(source_priority)}
    candidates.sort(
        key=lambda item: (
            priority.get(item.source, len(priority)),
            item.source,
            item.evidence_id,
            _canonical(item.value),
        )
    )
    selected = candidates[0]
    alternatives = tuple(candidates[1:])
    normalized_values = {_normalized_for_field(field_name, item.value) for item in candidates}
    return FieldDecision(
        field_name=field_name,
        selected_value=selected.value,
        selected_evidence_id=selected.evidence_id,
        rule="source_priority_v1",
        alternatives=alternatives,
        conflict=len(normalized_values) > 1,
    )


def _normalized_for_field(field_name: str, value: Any) -> str:
    if field_name == "title" and isinstance(value, str):
        return normalize_title(value)
    return _canonical(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class VersionRelation(str, Enum):
    PREPRINT_OF = "preprint_of"
    ACCEPTED_MANUSCRIPT_OF = "accepted_manuscript_of"
    VERSION_OF_RECORD_OF = "version_of_record_of"
    CORRECTION_OF = "correction_of"
    RETRACTION_OF = "retraction_of"
    EXPRESSION_OF_CONCERN_OF = "expression_of_concern_of"
    POSSIBLE_SAME_STUDY = "possible_same_study"


@dataclass(frozen=True)
class VersionEdge:
    subject_id: str
    relation: VersionRelation
    object_id: str
    evidence_ids: tuple[str, ...]
    confirmed_by_human: bool = False


@dataclass
class VersionGraph:
    edges: list[VersionEdge] = field(default_factory=list)

    def add(self, edge: VersionEdge) -> None:
        if not edge.subject_id or not edge.object_id:
            raise ValueError("Version identifiers must be non-empty")
        if edge.subject_id == edge.object_id:
            raise ValueError("A version relation cannot point to itself")
        if not edge.evidence_ids:
            raise ValueError("A version relation requires evidence")
        if edge in self.edges:
            return
        if edge.relation is not VersionRelation.POSSIBLE_SAME_STUDY:
            if self._would_create_cycle(edge.subject_id, edge.object_id):
                raise ValueError("Version relation would create a directed cycle")
        self.edges.append(edge)
        self.edges.sort(
            key=lambda item: (item.subject_id, item.relation.value, item.object_id)
        )

    def _would_create_cycle(self, subject_id: str, object_id: str) -> bool:
        adjacency: dict[str, set[str]] = {}
        for existing in self.edges:
            if existing.relation is VersionRelation.POSSIBLE_SAME_STUDY:
                continue
            adjacency.setdefault(existing.subject_id, set()).add(existing.object_id)
        stack = [object_id]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current == subject_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency.get(current, ()))
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"edges": [asdict(edge) for edge in self.edges]}
