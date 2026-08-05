from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from aikb.catalog import SearchHit


DEFAULT_QUERY_INSTRUCTION = (
    "Given a Linux kernel engineering question, retrieve source code or "
    "authoritative in-tree documentation that provides evidence to answer it"
)
SEMANTIC_DOCUMENT_TEMPLATE_VERSION = "code-evidence-v1"
SEMANTIC_RRF_K = 60
SEMANTIC_RRF_WEIGHT = 0.5


@dataclass(frozen=True)
class EmbeddingModelSpec:
    provider: str
    model_name: str
    model_revision: str
    dimension: int
    max_sequence_length: int = 2_048
    weights_sha256: str | None = None
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    document_template_version: str = SEMANTIC_DOCUMENT_TEMPLATE_VERSION

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("embedding provider must not be empty")
        if not self.model_name.strip():
            raise ValueError("embedding model name must not be empty")
        if not self.model_revision.strip():
            raise ValueError("embedding model revision must not be empty")
        if self.dimension < 1:
            raise ValueError("embedding dimension must be positive")
        if self.max_sequence_length < 32:
            raise ValueError("embedding max sequence length must be at least 32")
        if self.weights_sha256 is not None and (
            len(self.weights_sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in self.weights_sha256)
        ):
            raise ValueError("embedding weights SHA-256 must be 64 hexadecimal characters")
        if not self.query_instruction.strip():
            raise ValueError("query instruction must not be empty")

    @property
    def fingerprint(self) -> str:
        value = {
            "provider": self.provider,
            "model_name": self.model_name,
            "model_revision": self.model_revision,
            "dimension": self.dimension,
            "max_sequence_length": self.max_sequence_length,
            "weights_sha256": self.weights_sha256,
            "query_instruction": self.query_instruction,
            "document_template_version": self.document_template_version,
        }
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "model_revision": self.model_revision,
            "dimension": self.dimension,
            "max_sequence_length": self.max_sequence_length,
            "weights_sha256": self.weights_sha256,
            "query_instruction": self.query_instruction,
            "document_template_version": self.document_template_version,
            "fingerprint": self.fingerprint,
        }


class EmbeddingProvider(Protocol):
    @property
    def spec(self) -> EmbeddingModelSpec: ...

    def embed_queries(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...

    def embed_documents(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...


class SentenceTransformerEmbeddingProvider:
    """Optional local provider loaded only by semantic evaluation commands."""

    def __init__(
        self,
        model_name: str,
        model_revision: str,
        dimension: int,
        *,
        model_path: Path | None = None,
        device: str | None = None,
        batch_size: int = 16,
        max_seq_length: int = 2_048,
        weights_sha256: str | None = None,
        query_instruction: str = DEFAULT_QUERY_INSTRUCTION,
    ) -> None:
        if batch_size < 1:
            raise ValueError("embedding batch size must be positive")
        if max_seq_length < 32:
            raise ValueError("embedding max sequence length must be at least 32")
        self._spec = EmbeddingModelSpec(
            provider="sentence_transformers",
            model_name=model_name,
            model_revision=model_revision,
            dimension=dimension,
            max_sequence_length=max_seq_length,
            weights_sha256=weights_sha256,
            query_instruction=query_instruction,
        )
        self.model_path = model_path
        self.device = device
        self.batch_size = batch_size
        self.max_seq_length = max_seq_length
        self._model: object | None = None

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    def _load_model(self) -> object:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                "semantic evaluation needs the optional 'semantic' dependencies"
            ) from error
        kwargs: dict[str, object] = {
            "revision": self.spec.model_revision,
            "truncate_dim": self.spec.dimension,
            "trust_remote_code": False,
        }
        if self.device:
            kwargs["device"] = self.device
        model_source = str(self.model_path) if self.model_path else self.spec.model_name
        if self.model_path:
            # A downloaded immutable directory is already pinned by the model
            # spec; passing revision to a local path produces a noisy warning.
            kwargs.pop("revision")
        model = SentenceTransformer(model_source, **kwargs)
        model.max_seq_length = self.max_seq_length
        self._model = model
        return model

    def _encode(
        self,
        texts: Sequence[str],
        *,
        prompt: str | None = None,
    ) -> list[tuple[float, ...]]:
        if not texts:
            return []
        model = self._load_model()
        kwargs: dict[str, object] = {
            "batch_size": self.batch_size,
            "show_progress_bar": False,
            "convert_to_numpy": True,
            "normalize_embeddings": True,
            "truncate_dim": self.spec.dimension,
        }
        if prompt is not None:
            kwargs["prompt"] = prompt
        encoded = model.encode(list(texts), **kwargs)
        return [tuple(float(value) for value in row) for row in encoded]

    def embed_queries(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        prompt = f"Instruct: {self.spec.query_instruction}\nQuery:"
        return self._encode(texts, prompt=prompt)

    def embed_documents(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return self._encode(texts)


class EmbeddingCache:
    """Content-addressed local cache separated by immutable model fingerprint."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                model_fingerprint TEXT NOT NULL,
                input_kind TEXT NOT NULL CHECK (input_kind IN ('query', 'document')),
                input_hash TEXT NOT NULL,
                dimension INTEGER NOT NULL CHECK (dimension > 0),
                vector BLOB NOT NULL,
                PRIMARY KEY (model_fingerprint, input_kind, input_hash)
            )
            """
        )

    def __enter__(self) -> EmbeddingCache:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _input_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _pack(vector: Sequence[float]) -> bytes:
        return struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def _unpack(value: bytes, dimension: int) -> tuple[float, ...]:
        expected_size = dimension * 4
        if len(value) != expected_size:
            raise ValueError("cached embedding byte length does not match dimension")
        return tuple(struct.unpack(f"<{dimension}f", value))

    def get_many(
        self,
        spec: EmbeddingModelSpec,
        input_kind: str,
        texts: Sequence[str],
    ) -> tuple[list[tuple[float, ...] | None], int]:
        if input_kind not in {"query", "document"}:
            raise ValueError("embedding cache input kind is invalid")
        values: list[tuple[float, ...] | None] = []
        hits = 0
        for text in texts:
            row = self.connection.execute(
                """
                SELECT dimension, vector
                FROM embedding_cache
                WHERE model_fingerprint = ?
                  AND input_kind = ?
                  AND input_hash = ?
                """,
                (spec.fingerprint, input_kind, self._input_hash(text)),
            ).fetchone()
            if row is None:
                values.append(None)
                continue
            if row[0] != spec.dimension:
                raise ValueError("cached embedding dimension does not match model")
            values.append(self._unpack(row[1], row[0]))
            hits += 1
        return values, hits

    def put_many(
        self,
        spec: EmbeddingModelSpec,
        input_kind: str,
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        if len(texts) != len(vectors):
            raise ValueError("embedding cache input/vector count mismatch")
        if input_kind not in {"query", "document"}:
            raise ValueError("embedding cache input kind is invalid")
        rows = []
        for text, vector in zip(texts, vectors, strict=True):
            _validate_vector(vector, spec.dimension)
            rows.append(
                (
                    spec.fingerprint,
                    input_kind,
                    self._input_hash(text),
                    spec.dimension,
                    self._pack(vector),
                )
            )
        self.connection.executemany(
            """
            INSERT INTO embedding_cache(
                model_fingerprint, input_kind, input_hash, dimension, vector
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(model_fingerprint, input_kind, input_hash) DO NOTHING
            """,
            rows,
        )
        self.connection.commit()


@dataclass(frozen=True)
class SemanticRerankHit:
    hit: SearchHit
    original_rank: int
    semantic_rank: int
    semantic_score: float
    fused_score: float


@dataclass(frozen=True)
class SemanticRerankResult:
    semantic_hits: tuple[SemanticRerankHit, ...]
    fused_hits: tuple[SemanticRerankHit, ...]
    query_cache_hits: int
    query_cache_misses: int
    document_cache_hits: int
    document_cache_misses: int


def render_semantic_document(hit: SearchHit) -> str:
    symbol = hit.symbol or "(none)"
    return (
        f"File: {hit.path}\n"
        f"Lines: {hit.start_line}-{hit.end_line}\n"
        f"Kind: {hit.kind}\n"
        f"Symbol: {symbol}\n"
        "Content:\n"
        f"{hit.content}"
    )


def _validate_vector(vector: Sequence[float], dimension: int) -> None:
    if len(vector) != dimension:
        raise ValueError(
            f"embedding provider returned dimension {len(vector)}; expected {dimension}"
        )
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("embedding provider returned a non-finite value")


def _cached_embeddings(
    provider: EmbeddingProvider,
    cache: EmbeddingCache,
    input_kind: str,
    texts: Sequence[str],
) -> tuple[list[tuple[float, ...]], int, int]:
    cached, hits = cache.get_many(provider.spec, input_kind, texts)
    missing_indexes = [index for index, value in enumerate(cached) if value is None]
    if missing_indexes:
        missing_texts = [texts[index] for index in missing_indexes]
        if input_kind == "query":
            generated = provider.embed_queries(missing_texts)
        else:
            generated = provider.embed_documents(missing_texts)
        if len(generated) != len(missing_texts):
            raise ValueError("embedding provider returned the wrong vector count")
        for vector in generated:
            _validate_vector(vector, provider.spec.dimension)
        cache.put_many(provider.spec, input_kind, missing_texts, generated)
        for index, vector in zip(missing_indexes, generated, strict=True):
            cached[index] = vector
    if any(value is None for value in cached):
        raise RuntimeError("embedding cache failed to materialize every input")
    return [value for value in cached if value is not None], hits, len(missing_indexes)


def rerank_candidates(
    query: str,
    candidates: Sequence[SearchHit],
    provider: EmbeddingProvider,
    cache: EmbeddingCache,
) -> SemanticRerankResult:
    normalized_query = " ".join(query.split())
    if not normalized_query:
        raise ValueError("semantic query must not be empty")
    if not candidates:
        return SemanticRerankResult((), (), 0, 0, 0, 0)
    documents = [render_semantic_document(hit) for hit in candidates]
    queries, query_hits, query_misses = _cached_embeddings(
        provider, cache, "query", [normalized_query]
    )
    document_vectors, document_hits, document_misses = _cached_embeddings(
        provider, cache, "document", documents
    )
    query_vector = queries[0]
    scores = [
        sum(left * right for left, right in zip(query_vector, vector, strict=True))
        for vector in document_vectors
    ]
    semantic_order = sorted(
        range(len(candidates)),
        key=lambda index: (
            -scores[index],
            candidates[index].repository,
            candidates[index].path,
            candidates[index].start_line,
            candidates[index].chunk_id,
        ),
    )
    semantic_rank = {
        candidate_index: rank
        for rank, candidate_index in enumerate(semantic_order, start=1)
    }
    values = [
        SemanticRerankHit(
            hit=hit,
            original_rank=index + 1,
            semantic_rank=semantic_rank[index],
            semantic_score=scores[index],
            fused_score=(
                1.0 / (SEMANTIC_RRF_K + index + 1)
                + SEMANTIC_RRF_WEIGHT
                / (SEMANTIC_RRF_K + semantic_rank[index])
            ),
        )
        for index, hit in enumerate(candidates)
    ]
    semantic_hits = tuple(
        sorted(
            values,
            key=lambda item: (
                item.semantic_rank,
                item.original_rank,
            ),
        )
    )
    fused_hits = tuple(
        sorted(
            values,
            key=lambda item: (
                -item.fused_score,
                item.original_rank,
                item.semantic_rank,
                item.hit.repository,
                item.hit.path,
                item.hit.start_line,
                item.hit.chunk_id,
            ),
        )
    )
    return SemanticRerankResult(
        semantic_hits=semantic_hits,
        fused_hits=fused_hits,
        query_cache_hits=query_hits,
        query_cache_misses=query_misses,
        document_cache_hits=document_hits,
        document_cache_misses=document_misses,
    )
