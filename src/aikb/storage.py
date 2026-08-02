from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from aikb.catalog import SearchHit


LexicalChannel = Literal[
    "lexical_fts5",
    "lexical_postgres_fts",
    "lexical_zoekt",
]


@dataclass(frozen=True)
class LexicalSearchResult:
    channel: LexicalChannel
    hits: tuple[SearchHit, ...]


@dataclass(frozen=True)
class SourceLocation:
    repository: str
    snapshot_id: str
    path: str
    line: int
    rank: float


class ReadCatalog(Protocol):
    """Read-side contract shared by SQLite and PostgreSQL adapters."""

    def resolve_snapshots(
        self,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def search(
        self,
        query: str,
        top_k: int = 10,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]: ...

    def search_lexical(
        self,
        query: str,
        top_k: int = 10,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> LexicalSearchResult: ...

    def resolve_location_chunks(
        self,
        locations: list[SourceLocation],
        top_k: int = 10,
    ) -> list[SearchHit]: ...

    def search_symbol_chunks(
        self,
        names: list[str],
        top_k: int = 20,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]: ...

    def search_relation_chunks(
        self,
        names: list[str],
        top_k: int = 20,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]: ...

    def find_symbol(
        self,
        name: str,
        top_k: int = 50,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]: ...
