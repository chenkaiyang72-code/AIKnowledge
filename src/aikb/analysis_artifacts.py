from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from aikb.source_relations import (
    SourceCondition,
    SourceFacts,
    SourceRelation,
    SymbolOccurrence,
)
from aikb.structured_chunks import CodeChunk, ParseOutcome


ANALYSIS_ARTIFACT_SCHEMA_VERSION = 1


def encode_analysis_artifact(
    parse_outcome: ParseOutcome,
    source_facts: SourceFacts,
) -> bytes:
    payload = {
        "schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "parse_outcome": {
            "parse_status": parse_outcome.parse_status,
            "syntax_error_count": parse_outcome.syntax_error_count,
            "chunks": [asdict(item) for item in parse_outcome.chunks],
        },
        "source_facts": {
            "conditions": [asdict(item) for item in source_facts.conditions],
            "occurrences": [asdict(item) for item in source_facts.occurrences],
            "relations": [asdict(item) for item in source_facts.relations],
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _condition_from_dict(value: dict[str, Any] | None) -> SourceCondition | None:
    if value is None:
        return None
    return SourceCondition(**value)


def decode_analysis_artifact(payload: bytes) -> tuple[ParseOutcome, SourceFacts]:
    value = json.loads(payload.decode("utf-8"))
    if value.get("schema_version") != ANALYSIS_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported analysis artifact schema")

    parse_value = value["parse_outcome"]
    parse_outcome = ParseOutcome(
        chunks=[CodeChunk(**item) for item in parse_value["chunks"]],
        parse_status=parse_value["parse_status"],
        syntax_error_count=parse_value["syntax_error_count"],
    )
    facts_value = value["source_facts"]
    conditions = [SourceCondition(**item) for item in facts_value["conditions"]]
    occurrences: list[SymbolOccurrence] = []
    for item in facts_value["occurrences"]:
        occurrence = dict(item)
        occurrence["condition"] = _condition_from_dict(occurrence.get("condition"))
        occurrences.append(SymbolOccurrence(**occurrence))
    relations: list[SourceRelation] = []
    for item in facts_value["relations"]:
        relation = dict(item)
        relation["condition"] = _condition_from_dict(relation.get("condition"))
        relations.append(SourceRelation(**relation))
    return parse_outcome, SourceFacts(conditions, occurrences, relations)
