from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Engine, bindparam, create_engine, text

from aikb.catalog import SearchHit
from aikb.storage import LexicalSearchResult, SourceLocation


class PostgresCatalog:
    """PostgreSQL read adapter implementing the ReadCatalog protocol."""

    def __init__(self, url: str, engine: Engine | None = None):
        self.engine = engine or create_engine(url)
        self._owns_engine = engine is None

    def __enter__(self) -> PostgresCatalog:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    @staticmethod
    def _hit(row: Any, rank: float) -> SearchHit:
        content = row["content"]
        truncated = False
        if len(content) > 1_600:
            content = content[:1_600].rstrip() + "\n…"
            truncated = True
        return SearchHit(
            chunk_id=row["chunk_id"],
            blob_id=row["blob_id"],
            content_hash=row["content_hash"],
            repository=row["repository"],
            snapshot_id=row["snapshot_id"],
            revision=row["revision"],
            path=row["path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            kind=row["kind"],
            symbol=row["symbol"],
            generator=row["generator"],
            rank=rank,
            content=content,
            content_truncated=truncated,
        )

    @staticmethod
    def _scope_sql(
        repository: str | None,
        snapshot_id: str | None,
    ) -> tuple[str, dict[str, Any]]:
        clauses: list[str] = []
        parameters: dict[str, Any] = {}
        if snapshot_id:
            clauses.append("s.id = :snapshot_id")
            parameters["snapshot_id"] = snapshot_id
        else:
            clauses.append("s.state = 'active'")
        if repository:
            clauses.append("r.name = :repository")
            parameters["repository"] = repository
        return " AND ".join(clauses), parameters

    def resolve_snapshots(
        self,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scope_sql, parameters = self._scope_sql(repository, snapshot_id)
        statement = text(
            "SELECT r.name AS repository, s.id AS snapshot_id, s.revision, "
            "s.source_digest, s.manifest_digest, s.index_profile_digest, s.state "
            "FROM snapshot s JOIN repository r ON r.id = s.repository_id "
            f"WHERE {scope_sql} ORDER BY r.name, s.id"
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement, parameters).mappings().all()
        if snapshot_id and not rows:
            raise ValueError(f"snapshot not found in requested scope: {snapshot_id}")
        if repository and not rows:
            raise ValueError(f"repository has no snapshot in requested scope: {repository}")
        if not rows:
            raise ValueError("catalog has no active snapshots")
        return [dict(row) for row in rows]

    def search(
        self,
        query: str,
        top_k: int = 10,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1 or top_k > 100:
            raise ValueError("top-k must be between 1 and 100")
        terms = list(dict.fromkeys(re.findall(r"[\w]+", query)))[:32]
        if not terms:
            raise ValueError("query must contain at least one searchable term")
        scope_sql, parameters = self._scope_sql(repository, snapshot_id)
        parameters.update(
            {f"term_{index}": term for index, term in enumerate(terms)}
        )
        parameters["limit"] = top_k * 5
        tsquery_sql = " || ".join(
            f"plainto_tsquery('simple', :term_{index})"
            for index in range(len(terms))
        )
        statement = text(
            "SELECT c.id AS chunk_id, f.blob_id, c.content_hash, "
            "r.name AS repository, s.id AS snapshot_id, s.revision, f.path, "
            "c.start_line, c.end_line, c.kind, c.symbol, c.generator, c.content, "
            "ts_rank_cd(to_tsvector('simple', c.content), "
            f"({tsquery_sql})) AS lexical_rank "
            "FROM chunk c JOIN source_file f ON f.id = c.file_id "
            "JOIN snapshot s ON s.id = c.snapshot_id "
            "JOIN repository r ON r.id = s.repository_id "
            "WHERE to_tsvector('simple', c.content) @@ "
            f"({tsquery_sql}) "
            f"AND {scope_sql} "
            "ORDER BY lexical_rank DESC, f.path, c.start_line LIMIT :limit"
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement, parameters).mappings().all()
        hits: list[SearchHit] = []
        per_file: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (row["snapshot_id"], row["path"])
            if per_file.get(key, 0) >= 2:
                continue
            per_file[key] = per_file.get(key, 0) + 1
            hits.append(self._hit(row, -float(row["lexical_rank"])))
            if len(hits) >= top_k:
                break
        return hits

    def search_lexical(
        self,
        query: str,
        top_k: int = 10,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> LexicalSearchResult:
        return LexicalSearchResult(
            channel="lexical_postgres_fts",
            hits=tuple(
                self.search(
                    query=query,
                    top_k=top_k,
                    repository=repository,
                    snapshot_id=snapshot_id,
                )
            ),
        )

    def resolve_location_chunks(
        self,
        locations: list[SourceLocation],
        top_k: int = 10,
    ) -> list[SearchHit]:
        if top_k < 1 or top_k > 100:
            raise ValueError("top-k must be between 1 and 100")
        statement = text(
            "SELECT c.id AS chunk_id, f.blob_id, c.content_hash, "
            "r.name AS repository, s.id AS snapshot_id, s.revision, f.path, "
            "c.start_line, c.end_line, c.kind, c.symbol, c.generator, c.content "
            "FROM chunk c JOIN source_file f ON f.id=c.file_id "
            "JOIN snapshot s ON s.id=c.snapshot_id "
            "JOIN repository r ON r.id=s.repository_id "
            "WHERE r.name=:repository AND s.id=:snapshot_id AND f.path=:path "
            "AND c.start_line<=:line AND c.end_line>=:line "
            "ORDER BY (c.end_line-c.start_line),c.ordinal LIMIT 1"
        )
        hits: list[SearchHit] = []
        seen: set[tuple[str, str]] = set()
        with self.engine.connect() as connection:
            for location in locations:
                if location.line < 1:
                    continue
                row = connection.execute(
                    statement,
                    {
                        "repository": location.repository,
                        "snapshot_id": location.snapshot_id,
                        "path": location.path,
                        "line": location.line,
                    },
                ).mappings().first()
                if row is None:
                    continue
                key = (row["snapshot_id"], row["chunk_id"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append(self._hit(row, location.rank))
                if len(hits) >= top_k:
                    break
        return hits

    def _named_chunk_search(
        self,
        query: str,
        parameters: dict[str, Any],
        names: list[str],
        top_k: int,
    ) -> list[SearchHit]:
        statement = text(query).bindparams(bindparam("names", expanding=True))
        parameters.update({"names": names, "limit": top_k * 10})
        with self.engine.connect() as connection:
            rows = connection.execute(statement, parameters).mappings().all()
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for row in rows:
            if row["chunk_id"] in seen:
                continue
            seen.add(row["chunk_id"])
            hits.append(self._hit(row, float(len(hits) + 1)))
            if len(hits) >= top_k:
                break
        return hits

    def search_symbol_chunks(
        self,
        names: list[str],
        top_k: int = 20,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]:
        names = list(dict.fromkeys(name for name in names if name))
        if not names:
            return []
        if top_k < 1 or top_k > 100:
            raise ValueError("top-k must be between 1 and 100")
        scope_sql, parameters = self._scope_sql(repository, snapshot_id)
        query = (
            "SELECT c.id AS chunk_id, f.blob_id, c.content_hash, "
            "r.name AS repository, s.id AS snapshot_id, s.revision, f.path, "
            "c.start_line, c.end_line, c.kind, c.symbol, c.generator, c.content "
            "FROM symbol_occurrence so "
            "JOIN logical_symbol ls ON ls.id = so.logical_symbol_id "
            "JOIN source_file f ON f.id = so.file_id "
            "JOIN chunk c ON c.file_id = f.id AND c.start_line <= so.end_line "
            "AND c.end_line >= so.start_line "
            "JOIN snapshot s ON s.id = so.snapshot_id "
            "JOIN repository r ON r.id = s.repository_id "
            "WHERE ls.name IN :names AND " + scope_sql + " "
            "ORDER BY CASE so.role WHEN 'definition' THEN 0 ELSE 1 END, "
            "f.path, so.start_line LIMIT :limit"
        )
        return self._named_chunk_search(query, parameters, names, top_k)

    def search_relation_chunks(
        self,
        names: list[str],
        top_k: int = 20,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]:
        names = list(dict.fromkeys(name for name in names if name))
        if not names:
            return []
        if top_k < 1 or top_k > 100:
            raise ValueError("top-k must be between 1 and 100")
        scope_sql, parameters = self._scope_sql(repository, snapshot_id)
        query = (
            "SELECT c.id AS chunk_id, f.blob_id, c.content_hash, "
            "r.name AS repository, s.id AS snapshot_id, s.revision, f.path, "
            "c.start_line, c.end_line, c.kind, c.symbol, c.generator, c.content "
            "FROM relation rel JOIN source_file f ON f.id = rel.source_file_id "
            "JOIN chunk c ON c.file_id = f.id AND c.start_line <= rel.end_line "
            "AND c.end_line >= rel.start_line "
            "JOIN snapshot s ON s.id = rel.snapshot_id "
            "JOIN repository r ON r.id = s.repository_id "
            "LEFT JOIN logical_symbol ss ON ss.id = rel.source_symbol_id "
            "LEFT JOIN logical_symbol ts ON ts.id = rel.target_symbol_id "
            "WHERE (ss.name IN :names OR ts.name IN :names "
            "OR rel.target_text IN :names) AND " + scope_sql + " "
            "ORDER BY CASE rel.confidence WHEN 'source_exact' THEN 0 ELSE 1 END, "
            "f.path, rel.start_line LIMIT :limit"
        )
        return self._named_chunk_search(query, parameters, names, top_k)

    def find_symbol(
        self,
        name: str,
        top_k: int = 50,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("symbol name must not be empty")
        scope_sql, parameters = self._scope_sql(repository, snapshot_id)
        parameters.update({"name": name, "limit": top_k})
        occurrence_sql = text(
            "SELECT ls.id AS logical_symbol_id, ls.name, ls.kind, ls.namespace, "
            "ls.signature, so.role, so.confidence, so.generator, "
            "s.id AS snapshot_id, s.revision, r.name AS repository, f.path, "
            "so.start_line, so.end_line, sc.expression AS source_condition "
            "FROM symbol_occurrence so JOIN logical_symbol ls ON ls.id=so.logical_symbol_id "
            "JOIN source_file f ON f.id=so.file_id JOIN snapshot s ON s.id=so.snapshot_id "
            "JOIN repository r ON r.id=s.repository_id "
            "LEFT JOIN source_condition sc ON sc.id=so.condition_id "
            "WHERE ls.name=:name AND " + scope_sql + " "
            "ORDER BY r.name,f.path,so.start_line LIMIT :limit"
        )
        relation_sql = text(
            "SELECT rel.id,rel.kind,rel.target_text,rel.confidence,rel.generator, "
            "rel.start_line,rel.end_line,s.id AS snapshot_id,s.revision, "
            "r.name AS repository,sf.path AS source_path,ss.name AS source_symbol, "
            "tf.path AS target_path,ts.name AS target_symbol, "
            "sc.expression AS source_condition FROM relation rel "
            "JOIN source_file sf ON sf.id=rel.source_file_id "
            "JOIN snapshot s ON s.id=rel.snapshot_id JOIN repository r ON r.id=s.repository_id "
            "LEFT JOIN logical_symbol ss ON ss.id=rel.source_symbol_id "
            "LEFT JOIN logical_symbol ts ON ts.id=rel.target_symbol_id "
            "LEFT JOIN source_file tf ON tf.id=rel.target_file_id "
            "LEFT JOIN source_condition sc ON sc.id=rel.condition_id "
            "WHERE (ss.name=:name OR ts.name=:name OR rel.target_text=:name) AND "
            + scope_sql
            + " ORDER BY r.name,sf.path,rel.start_line,rel.kind LIMIT :limit"
        )
        with self.engine.connect() as connection:
            occurrence_rows = connection.execute(occurrence_sql, parameters).mappings().all()
            relation_rows = connection.execute(relation_sql, parameters).mappings().all()
        occurrences = [self._with_citation(row, "path") for row in occurrence_rows]
        relations = [self._with_citation(row, "source_path") for row in relation_rows]
        return {
            "name": name,
            "occurrence_count": len(occurrences),
            "relation_count": len(relations),
            "occurrences": occurrences,
            "relations": relations,
        }

    @staticmethod
    def _with_citation(row: Any, path_key: str) -> dict[str, Any]:
        item = dict(row)
        item["citation"] = (
            f"{row['repository']}@{row['revision']}:"
            f"{row[path_key]}:{row['start_line']}-{row['end_line']}"
        )
        return item
