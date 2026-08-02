from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aikb.catalog import Catalog, SearchHit
from aikb.retrieval import ChannelContribution, retrieve_hybrid


CONTEXT_PACK_SCHEMA = "urn:aiknowledge:schema:context-pack:v1"
CONTEXT_PACK_VERSION = "1.1"
CONTEXT_BUILDER_VERSION = "context-pack-v1.1+hybrid-rrf-v1"
CHARS_PER_ESTIMATED_TOKEN = 4
MIN_EVIDENCE_CHARS = 64


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SnapshotReference(StrictModel):
    repository: str
    snapshot_id: str
    revision: str
    source_digest: str
    manifest_digest: str
    index_profile_digest: str
    state: Literal["building", "validated", "active", "superseded"]


class ContextScope(StrictModel):
    kind: Literal["repository_set"] = "repository_set"
    requested_repository: str | None = None
    requested_snapshot_id: str | None = None
    snapshots: list[SnapshotReference]
    partial_visibility: bool = False


class RetrievalChannelContribution(StrictModel):
    channel: Literal["lexical_fts5", "symbol_exact", "relation_source"]
    rank: int
    weight: float
    reciprocal_score: float


class RetrievalScore(StrictModel):
    channel: Literal["hybrid_rrf"] = "hybrid_rrf"
    rank: float
    rrf_k: int
    contributions: list[RetrievalChannelContribution]


class CodeEvidence(StrictModel):
    id: str
    type: Literal["code"] = "code"
    chunk_id: str
    blob_id: str
    content_hash: str
    repository: str
    snapshot_id: str
    revision: str
    path: str
    lines: tuple[int, int]
    kind: str
    symbol: str | None = None
    generator: str
    citation: str
    retrieval: RetrievalScore
    content: str
    content_truncated: bool = False


class SymbolOccurrenceContext(StrictModel):
    role: Literal["definition", "declaration"]
    kind: str
    signature: str | None = None
    confidence: str
    citation: str
    source_condition: str | None = None


class RelationContext(StrictModel):
    kind: str
    source_symbol: str | None = None
    target_text: str
    target_symbol: str | None = None
    target_path: str | None = None
    confidence: str
    citation: str
    source_condition: str | None = None


class SymbolContext(StrictModel):
    name: str
    occurrences: list[SymbolOccurrenceContext]
    relations: list[RelationContext]
    truncated: bool = False


class Coverage(StrictModel):
    complete: bool = False
    evidence_status: Literal["none", "available_unassessed"]
    partial_visibility: bool = False
    reason: str


class ContextBudget(StrictModel):
    evidence_token_budget: int
    max_evidence_items: int
    max_symbols: int
    max_relations_per_symbol: int
    evidence_items_used: int
    evidence_chars_used: int
    estimated_evidence_tokens: int
    omitted_candidate_count: int
    truncated: bool


class TraceCandidate(StrictModel):
    chunk_id: str
    channel: Literal["hybrid_rrf"] = "hybrid_rrf"
    fused_score: float
    contributions: list[RetrievalChannelContribution]
    selected: bool
    disposition: Literal["selected", "item_budget", "token_budget"]


class RetrievalTrace(StrictModel):
    id: str
    builder_version: str
    normalized_query: str
    candidate_count: int
    selected_count: int
    channel_candidate_counts: dict[str, int]
    rrf_k: int
    candidates: list[TraceCandidate]


class ContextPack(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": CONTEXT_PACK_SCHEMA},
    )

    schema_uri: Literal["urn:aiknowledge:schema:context-pack:v1"]
    schema_version: Literal["1.1"]
    id: str
    query: str
    scope: ContextScope
    evidence: list[CodeEvidence]
    symbols: list[SymbolContext]
    team_knowledge: list[dict[str, Any]] = Field(default_factory=list)
    coverage: Coverage
    gaps: list[str]
    warnings: list[str]
    budget: ContextBudget
    retrieval_trace: RetrievalTrace


def _stable_id(prefix: str, payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _normalize_query(query: str) -> str:
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("query must not be empty")
    return normalized


def _identifier_candidates(query: str, hits: list[SearchHit]) -> list[str]:
    candidates = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", query)
    candidates.extend(hit.symbol for hit in hits if hit.symbol)
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def _build_symbol_contexts(
    catalog: Catalog,
    query: str,
    hits: list[SearchHit],
    repository: str | None,
    snapshot_id: str | None,
    max_symbols: int,
    max_relations_per_symbol: int,
) -> list[SymbolContext]:
    if max_symbols == 0:
        return []
    symbols: list[SymbolContext] = []
    lookup_limit = max(8, max_relations_per_symbol * 2)
    for name in _identifier_candidates(query, hits):
        report = catalog.find_symbol(
            name=name,
            top_k=min(200, lookup_limit),
            repository=repository,
            snapshot_id=snapshot_id,
        )
        if not report["occurrences"] and not report["relations"]:
            continue
        occurrence_rows = report["occurrences"][:4]
        relation_rows = report["relations"][:max_relations_per_symbol]
        symbols.append(
            SymbolContext(
                name=name,
                occurrences=[
                    SymbolOccurrenceContext(
                        role=row["role"],
                        kind=row["kind"],
                        signature=row["signature"],
                        confidence=row["confidence"],
                        citation=row["citation"],
                        source_condition=row["source_condition"],
                    )
                    for row in occurrence_rows
                ],
                relations=[
                    RelationContext(
                        kind=row["kind"],
                        source_symbol=row["source_symbol"],
                        target_text=row["target_text"],
                        target_symbol=row["target_symbol"],
                        target_path=row["target_path"],
                        confidence=row["confidence"],
                        citation=row["citation"],
                        source_condition=row["source_condition"],
                    )
                    for row in relation_rows
                ],
                truncated=(
                    len(report["occurrences"]) > len(occurrence_rows)
                    or len(report["relations"]) > len(relation_rows)
                ),
            )
        )
        if len(symbols) >= max_symbols:
            break
    return symbols


def _contribution_models(
    contributions: tuple[ChannelContribution, ...],
) -> list[RetrievalChannelContribution]:
    return [
        RetrievalChannelContribution(
            channel=item.channel,
            rank=item.rank,
            weight=item.weight,
            reciprocal_score=item.reciprocal_score,
        )
        for item in contributions
    ]


def build_context_pack(
    catalog: Catalog,
    query: str,
    repository: str | None = None,
    snapshot_id: str | None = None,
    max_evidence_items: int = 8,
    evidence_token_budget: int = 3_000,
    max_symbols: int = 5,
    max_relations_per_symbol: int = 8,
) -> ContextPack:
    normalized_query = _normalize_query(query)
    if not 1 <= max_evidence_items <= 50:
        raise ValueError("max-evidence-items must be between 1 and 50")
    if not 64 <= evidence_token_budget <= 100_000:
        raise ValueError("evidence-token-budget must be between 64 and 100000")
    if not 0 <= max_symbols <= 20:
        raise ValueError("max-symbols must be between 0 and 20")
    if not 0 <= max_relations_per_symbol <= 50:
        raise ValueError("max-relations-per-symbol must be between 0 and 50")

    snapshot_rows = catalog.resolve_snapshots(
        repository=repository,
        snapshot_id=snapshot_id,
    )
    candidate_limit = min(100, max(max_evidence_items * 3, max_evidence_items))
    retrieval_result = retrieve_hybrid(
        catalog=catalog,
        query=normalized_query,
        top_k=candidate_limit,
        repository=repository,
        snapshot_id=snapshot_id,
    )
    hybrid_hits = list(retrieval_result.hits)
    hits = [item.hit for item in hybrid_hits]
    evidence_char_budget = evidence_token_budget * CHARS_PER_ESTIMATED_TOKEN
    evidence_chars_used = 0
    evidence: list[CodeEvidence] = []
    trace_candidates: list[TraceCandidate] = []

    for hybrid_hit in hybrid_hits:
        hit = hybrid_hit.hit
        contribution_models = _contribution_models(hybrid_hit.contributions)
        if len(evidence) >= max_evidence_items:
            trace_candidates.append(
                TraceCandidate(
                    chunk_id=hit.chunk_id,
                    fused_score=hybrid_hit.fused_score,
                    contributions=contribution_models,
                    selected=False,
                    disposition="item_budget",
                )
            )
            continue
        remaining_chars = evidence_char_budget - evidence_chars_used
        if remaining_chars < MIN_EVIDENCE_CHARS:
            trace_candidates.append(
                TraceCandidate(
                    chunk_id=hit.chunk_id,
                    fused_score=hybrid_hit.fused_score,
                    contributions=contribution_models,
                    selected=False,
                    disposition="token_budget",
                )
            )
            continue
        content = hit.content
        content_truncated = hit.content_truncated
        if len(content) > remaining_chars:
            content = content[: max(1, remaining_chars - 1)].rstrip() + "…"
            content_truncated = True
        evidence_chars_used += len(content)
        evidence.append(
            CodeEvidence(
                id=_stable_id(
                    "evidence",
                    {
                        "snapshot_id": hit.snapshot_id,
                        "chunk_id": hit.chunk_id,
                        "content_hash": hit.content_hash,
                    },
                ),
                chunk_id=hit.chunk_id,
                blob_id=hit.blob_id,
                content_hash=hit.content_hash,
                repository=hit.repository,
                snapshot_id=hit.snapshot_id,
                revision=hit.revision,
                path=hit.path,
                lines=(hit.start_line, hit.end_line),
                kind=hit.kind,
                symbol=hit.symbol,
                generator=hit.generator,
                citation=(
                    f"{hit.repository}@{hit.revision}:"
                    f"{hit.path}:{hit.start_line}-{hit.end_line}"
                ),
                retrieval=RetrievalScore(
                    rank=hybrid_hit.fused_score,
                    rrf_k=retrieval_result.rrf_k,
                    contributions=contribution_models,
                ),
                content=content,
                content_truncated=content_truncated,
            )
        )
        trace_candidates.append(
            TraceCandidate(
                chunk_id=hit.chunk_id,
                fused_score=hybrid_hit.fused_score,
                contributions=contribution_models,
                selected=True,
                disposition="selected",
            )
        )

    symbols = _build_symbol_contexts(
        catalog=catalog,
        query=normalized_query,
        hits=hits,
        repository=repository,
        snapshot_id=snapshot_id,
        max_symbols=max_symbols,
        max_relations_per_symbol=max_relations_per_symbol,
    )
    omitted_candidate_count = sum(
        not candidate.selected for candidate in trace_candidates
    )
    truncated = omitted_candidate_count > 0 or any(
        item.content_truncated for item in evidence
    )
    budget = ContextBudget(
        evidence_token_budget=evidence_token_budget,
        max_evidence_items=max_evidence_items,
        max_symbols=max_symbols,
        max_relations_per_symbol=max_relations_per_symbol,
        evidence_items_used=len(evidence),
        evidence_chars_used=evidence_chars_used,
        estimated_evidence_tokens=math.ceil(
            evidence_chars_used / CHARS_PER_ESTIMATED_TOKEN
        ),
        omitted_candidate_count=omitted_candidate_count,
        truncated=truncated,
    )
    trace_payload = {
        "builder_version": CONTEXT_BUILDER_VERSION,
        "query": normalized_query,
        "repository": repository,
        "snapshot_id": snapshot_id,
        "resolved_snapshots": [row["snapshot_id"] for row in snapshot_rows],
        "candidates": [
            {
                "chunk_id": candidate.chunk_id,
                "fused_score": round(candidate.fused_score, 12),
                "contributions": [
                    contribution.model_dump(mode="json")
                    for contribution in candidate.contributions
                ],
                "selected": candidate.selected,
                "disposition": candidate.disposition,
            }
            for candidate in trace_candidates
        ],
        "symbols": [symbol.name for symbol in symbols],
        "channel_candidate_counts": retrieval_result.channel_candidate_counts,
        "rrf_k": retrieval_result.rrf_k,
        "budget": budget.model_dump(mode="json"),
    }
    trace_id = _stable_id("trace", trace_payload)
    retrieval_trace = RetrievalTrace(
        id=trace_id,
        builder_version=CONTEXT_BUILDER_VERSION,
        normalized_query=normalized_query,
        candidate_count=len(trace_candidates),
        selected_count=len(evidence),
        channel_candidate_counts=retrieval_result.channel_candidate_counts,
        rrf_k=retrieval_result.rrf_k,
        candidates=trace_candidates,
    )

    if evidence:
        coverage = Coverage(
            evidence_status="available_unassessed",
            partial_visibility=False,
            reason=(
                "indexed evidence is available, but answer completeness has not "
                "been assessed"
            ),
        )
        gaps = [
            "answer coverage has not been assessed; reason only from cited evidence"
        ]
    else:
        coverage = Coverage(
            evidence_status="none",
            partial_visibility=False,
            reason="no indexed evidence matched the query in the requested scope",
        )
        gaps = ["no indexed evidence matched the query in the requested scope"]
    if omitted_candidate_count:
        gaps.append("additional retrieval candidates were omitted by context budget")

    warnings: list[str] = []
    if any(row["state"] != "active" for row in snapshot_rows):
        warnings.append("an explicitly selected snapshot is not currently active")
    scope = ContextScope(
        requested_repository=repository,
        requested_snapshot_id=snapshot_id,
        snapshots=[SnapshotReference(**row) for row in snapshot_rows],
    )
    pack_payload = {
        "trace_id": trace_id,
        "snapshot_ids": [row["snapshot_id"] for row in snapshot_rows],
        "evidence_ids": [item.id for item in evidence],
    }
    return ContextPack(
        schema_uri=CONTEXT_PACK_SCHEMA,
        schema_version=CONTEXT_PACK_VERSION,
        id=_stable_id("context", pack_payload),
        query=normalized_query,
        scope=scope,
        evidence=evidence,
        symbols=symbols,
        coverage=coverage,
        gaps=gaps,
        warnings=warnings,
        budget=budget,
        retrieval_trace=retrieval_trace,
    )


def context_pack_json_schema() -> dict[str, Any]:
    return ContextPack.model_json_schema()
