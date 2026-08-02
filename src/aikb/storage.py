from __future__ import annotations

from typing import Any, Protocol

from aikb.catalog import SearchHit


class ReadCatalog(Protocol):
    """Read-side contract shared by SQLite and future PostgreSQL adapters."""

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
