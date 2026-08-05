from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from aikb.catalog import SearchHit
from aikb.storage import LexicalChannel, LexicalSearchResult, ReadCatalog


RetrievalChannel = LexicalChannel | Literal["symbol_exact", "relation_source"]
RRF_K = 60
MAX_IDENTIFIER_TERMS = 16
CHANNEL_WEIGHTS: dict[RetrievalChannel, float] = {
    "lexical_fts5": 1.0,
    "lexical_postgres_fts": 1.0,
    "lexical_zoekt": 1.0,
    "symbol_exact": 2.0,
    "relation_source": 0.75,
}


@dataclass(frozen=True)
class ChannelContribution:
    channel: RetrievalChannel
    rank: int
    weight: float
    reciprocal_score: float

    def as_dict(self) -> dict[str, str | int | float]:
        return {
            "channel": self.channel,
            "rank": self.rank,
            "weight": self.weight,
            "reciprocal_score": self.reciprocal_score,
        }


@dataclass(frozen=True)
class HybridHit:
    hit: SearchHit
    fused_score: float
    contributions: tuple[ChannelContribution, ...]


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    identifier_terms: tuple[str, ...]
    hits: tuple[HybridHit, ...]
    channel_candidate_counts: dict[RetrievalChannel, int]
    rrf_k: int


def extract_identifier_terms(query: str) -> list[str]:
    terms = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", query)
    return list(dict.fromkeys(terms))[:MAX_IDENTIFIER_TERMS]


def retrieve_hybrid(
    catalog: ReadCatalog,
    query: str,
    top_k: int = 20,
    repository: str | None = None,
    snapshot_id: str | None = None,
    precomputed_lexical: LexicalSearchResult | None = None,
) -> RetrievalResult:
    normalized_query = " ".join(query.split())
    if not normalized_query:
        raise ValueError("query must not be empty")
    if top_k < 1 or top_k > 100:
        raise ValueError("top-k must be between 1 and 100")

    identifiers = extract_identifier_terms(normalized_query)
    lexical_result = precomputed_lexical
    if lexical_result is None:
        lexical_result = catalog.search_lexical(
            normalized_query,
            top_k=top_k,
            repository=repository,
            snapshot_id=snapshot_id,
        )
    channel_hits: dict[RetrievalChannel, list[SearchHit]] = {
        lexical_result.channel: list(lexical_result.hits),
        "symbol_exact": catalog.search_symbol_chunks(
            identifiers,
            top_k=top_k,
            repository=repository,
            snapshot_id=snapshot_id,
        ),
        "relation_source": catalog.search_relation_chunks(
            identifiers,
            top_k=top_k,
            repository=repository,
            snapshot_id=snapshot_id,
        ),
    }
    hit_by_key: dict[tuple[str, str], SearchHit] = {}
    contributions_by_key: dict[
        tuple[str, str], list[ChannelContribution]
    ] = {}
    scores_by_key: dict[tuple[str, str], float] = {}
    channel_sequence: tuple[RetrievalChannel, ...] = (
        lexical_result.channel,
        "symbol_exact",
        "relation_source",
    )
    for channel in channel_sequence:
        weight = CHANNEL_WEIGHTS[channel]
        for rank, hit in enumerate(channel_hits[channel], start=1):
            key = (hit.snapshot_id, hit.chunk_id)
            contribution_score = weight / (RRF_K + rank)
            hit_by_key.setdefault(key, hit)
            scores_by_key[key] = scores_by_key.get(key, 0.0) + contribution_score
            contributions_by_key.setdefault(key, []).append(
                ChannelContribution(
                    channel=channel,
                    rank=rank,
                    weight=weight,
                    reciprocal_score=contribution_score,
                )
            )

    channel_order = {
        lexical_result.channel: 0,
        "symbol_exact": 1,
        "relation_source": 2,
    }
    fused = [
        HybridHit(
            hit=hit_by_key[key],
            fused_score=scores_by_key[key],
            contributions=tuple(
                sorted(
                    contributions_by_key[key],
                    key=lambda item: channel_order[item.channel],
                )
            ),
        )
        for key in hit_by_key
    ]
    fused.sort(
        key=lambda item: (
            -item.fused_score,
            min(contribution.rank for contribution in item.contributions),
            item.hit.repository,
            item.hit.path,
            item.hit.start_line,
            item.hit.chunk_id,
        )
    )
    return RetrievalResult(
        query=normalized_query,
        identifier_terms=tuple(identifiers),
        hits=tuple(fused[:top_k]),
        channel_candidate_counts={
            channel: len(hits) for channel, hits in channel_hits.items()
        },
        rrf_k=RRF_K,
    )
