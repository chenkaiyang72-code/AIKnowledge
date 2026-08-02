from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 5


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repository (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repository(id),
    revision TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    index_profile_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('building', 'validated', 'active', 'superseded')),
    file_count INTEGER NOT NULL DEFAULT 0 CHECK (file_count >= 0),
    blob_count INTEGER NOT NULL DEFAULT 0 CHECK (blob_count >= 0),
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    structured_chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (structured_chunk_count >= 0),
    fallback_chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (fallback_chunk_count >= 0),
    parse_error_count INTEGER NOT NULL DEFAULT 0 CHECK (parse_error_count >= 0),
    symbol_occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK (symbol_occurrence_count >= 0),
    relation_count INTEGER NOT NULL DEFAULT 0 CHECK (relation_count >= 0),
    condition_count INTEGER NOT NULL DEFAULT 0 CHECK (condition_count >= 0),
    analysis_cache_hit_count INTEGER NOT NULL DEFAULT 0 CHECK (analysis_cache_hit_count >= 0),
    analysis_cache_miss_count INTEGER NOT NULL DEFAULT 0 CHECK (analysis_cache_miss_count >= 0),
    seed_file_count INTEGER NOT NULL DEFAULT 0 CHECK (seed_file_count >= 0),
    dependency_file_count INTEGER NOT NULL DEFAULT 0 CHECK (dependency_file_count >= 0),
    dependency_unresolved_count INTEGER NOT NULL DEFAULT 0 CHECK (dependency_unresolved_count >= 0),
    dependency_ambiguous_count INTEGER NOT NULL DEFAULT 0 CHECK (dependency_ambiguous_count >= 0),
    dependency_expansion_truncated INTEGER NOT NULL DEFAULT 0
        CHECK (dependency_expansion_truncated IN (0, 1)),
    byte_count INTEGER NOT NULL DEFAULT 0 CHECK (byte_count >= 0),
    skipped_file_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_file_count >= 0),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (repository_id, revision, manifest_digest, index_profile_digest)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_snapshot_per_repository
ON snapshot(repository_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS snapshot_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
    state TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blob (
    id TEXT PRIMARY KEY,
    algorithm TEXT NOT NULL CHECK (algorithm = 'sha256'),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    compression TEXT NOT NULL CHECK (compression = 'zlib'),
    compressed_content BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_artifact (
    id TEXT PRIMARY KEY,
    blob_id TEXT NOT NULL REFERENCES blob(id),
    language TEXT NOT NULL,
    analysis_profile_digest TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    compression TEXT NOT NULL CHECK (compression = 'zlib'),
    compressed_payload BLOB NOT NULL,
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    symbol_occurrence_count INTEGER NOT NULL CHECK (symbol_occurrence_count >= 0),
    relation_count INTEGER NOT NULL CHECK (relation_count >= 0),
    condition_count INTEGER NOT NULL CHECK (condition_count >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (blob_id, language, analysis_profile_digest)
);

CREATE INDEX IF NOT EXISTS analysis_artifact_blob_profile
ON analysis_artifact(blob_id, analysis_profile_digest);

CREATE TABLE IF NOT EXISTS source_file (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
    path TEXT NOT NULL,
    blob_id TEXT NOT NULL REFERENCES blob(id),
    language TEXT NOT NULL,
    line_count INTEGER NOT NULL CHECK (line_count >= 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    decode_status TEXT NOT NULL CHECK (decode_status IN ('utf8', 'replacement')),
    parse_status TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (parse_status IN ('structured', 'fallback', 'not_applicable')),
    syntax_error_count INTEGER NOT NULL DEFAULT 0 CHECK (syntax_error_count >= 0),
    UNIQUE (snapshot_id, path)
);

CREATE INDEX IF NOT EXISTS source_file_snapshot_path
ON source_file(snapshot_id, path);

CREATE TABLE IF NOT EXISTS chunk (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
    file_id TEXT NOT NULL REFERENCES source_file(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL CHECK (start_line >= 1),
    end_line INTEGER NOT NULL CHECK (end_line >= start_line),
    symbol TEXT,
    content_hash TEXT NOT NULL,
    generator TEXT NOT NULL,
    UNIQUE (file_id, ordinal)
);

CREATE INDEX IF NOT EXISTS chunk_snapshot_file
ON chunk(snapshot_id, file_id, ordinal);

CREATE TABLE IF NOT EXISTS logical_symbol (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repository(id),
    language TEXT NOT NULL,
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    signature TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (repository_id, language, kind, namespace, name)
);

CREATE INDEX IF NOT EXISTS logical_symbol_repository_name
ON logical_symbol(repository_id, name, kind);

CREATE TABLE IF NOT EXISTS source_condition (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
    file_id TEXT NOT NULL REFERENCES source_file(id),
    expression TEXT NOT NULL,
    start_line INTEGER NOT NULL CHECK (start_line >= 1),
    end_line INTEGER NOT NULL CHECK (end_line >= start_line),
    depth INTEGER NOT NULL CHECK (depth >= 1),
    generator TEXT NOT NULL,
    UNIQUE (file_id, expression, start_line, end_line, depth)
);

CREATE INDEX IF NOT EXISTS source_condition_snapshot_file
ON source_condition(snapshot_id, file_id, start_line);

CREATE TABLE IF NOT EXISTS symbol_occurrence (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
    file_id TEXT NOT NULL REFERENCES source_file(id),
    logical_symbol_id TEXT NOT NULL REFERENCES logical_symbol(id),
    condition_id TEXT REFERENCES source_condition(id),
    role TEXT NOT NULL CHECK (role IN ('definition', 'declaration')),
    start_line INTEGER NOT NULL CHECK (start_line >= 1),
    end_line INTEGER NOT NULL CHECK (end_line >= start_line),
    confidence TEXT NOT NULL CHECK (
        confidence IN ('source_exact', 'source_inferred', 'ambiguous_candidate', 'human_verified')
    ),
    generator TEXT NOT NULL,
    UNIQUE (file_id, logical_symbol_id, role, start_line, end_line)
);

CREATE INDEX IF NOT EXISTS symbol_occurrence_snapshot_symbol
ON symbol_occurrence(snapshot_id, logical_symbol_id, role);

CREATE TABLE IF NOT EXISTS relation (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
    source_file_id TEXT NOT NULL REFERENCES source_file(id),
    source_symbol_id TEXT REFERENCES logical_symbol(id),
    target_file_id TEXT REFERENCES source_file(id),
    target_symbol_id TEXT REFERENCES logical_symbol(id),
    condition_id TEXT REFERENCES source_condition(id),
    kind TEXT NOT NULL,
    target_text TEXT NOT NULL,
    start_line INTEGER NOT NULL CHECK (start_line >= 1),
    end_line INTEGER NOT NULL CHECK (end_line >= start_line),
    confidence TEXT NOT NULL CHECK (
        confidence IN ('source_exact', 'source_inferred', 'ambiguous_candidate', 'human_verified')
    ),
    generator TEXT NOT NULL,
    CHECK (target_file_id IS NOT NULL OR target_symbol_id IS NOT NULL OR length(target_text) > 0),
    UNIQUE (
        source_file_id, kind, target_text, start_line, end_line,
        source_symbol_id, target_file_id, target_symbol_id
    )
);

CREATE INDEX IF NOT EXISTS relation_snapshot_kind
ON relation(snapshot_id, kind, source_file_id);

CREATE INDEX IF NOT EXISTS relation_target_symbol
ON relation(snapshot_id, target_symbol_id, kind);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    chunk_id UNINDEXED,
    content,
    tokenize = "unicode61 tokenchars '_'")
;
"""


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    blob_id: str
    content_hash: str
    repository: str
    snapshot_id: str
    revision: str
    path: str
    start_line: int
    end_line: int
    kind: str
    symbol: str | None
    generator: str
    rank: float
    content: str
    content_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "blob_id": self.blob_id,
            "content_hash": self.content_hash,
            "repository": self.repository,
            "snapshot_id": self.snapshot_id,
            "revision": self.revision,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "kind": self.kind,
            "symbol": self.symbol,
            "generator": self.generator,
            "citation": (
                f"{self.repository}@{self.revision}:"
                f"{self.path}:{self.start_line}-{self.end_line}"
            ),
            "rank": self.rank,
            "content": self.content,
            "content_truncated": self.content_truncated,
        }


class Catalog:
    """SQLite bootstrap catalog for local Phase 0B experiments.

    The domain model intentionally mirrors the planned PostgreSQL store, while
    keeping the first ingest experiment runnable without external services.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(SCHEMA_SQL)
        row = self.connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.connection.commit()
        else:
            current_version = int(row["value"])
            if current_version == 1:
                self._migrate_v1_to_v2()
                current_version = 2
            if current_version == 2:
                self._migrate_v2_to_v3()
                current_version = 3
            if current_version == 3:
                self._migrate_v3_to_v4()
                current_version = 4
            if current_version == 4:
                self._migrate_v4_to_v5()
                current_version = 5
            if current_version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported catalog schema {current_version}; expected {SCHEMA_VERSION}"
                )

    def _migrate_v1_to_v2(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(snapshot)").fetchall()
        }
        if "structured_chunk_count" not in columns:
            self.connection.execute(
                "ALTER TABLE snapshot ADD COLUMN structured_chunk_count INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.execute(
                "ALTER TABLE snapshot ADD COLUMN fallback_chunk_count INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.execute(
                "ALTER TABLE snapshot ADD COLUMN parse_error_count INTEGER NOT NULL DEFAULT 0"
            )
        file_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(source_file)").fetchall()
        }
        if "parse_status" not in file_columns:
            self.connection.execute(
                "ALTER TABLE source_file ADD COLUMN parse_status TEXT NOT NULL DEFAULT 'not_applicable'"
            )
            self.connection.execute(
                "ALTER TABLE source_file ADD COLUMN syntax_error_count INTEGER NOT NULL DEFAULT 0"
            )
        chunk_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(chunk)").fetchall()
        }
        if "symbol" not in chunk_columns:
            self.connection.execute("ALTER TABLE chunk ADD COLUMN symbol TEXT")
        self.connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            ("2",),
        )
        self.connection.commit()

    def _migrate_v2_to_v3(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(snapshot)").fetchall()
        }
        additions = {
            "symbol_occurrence_count": (
                "ALTER TABLE snapshot ADD COLUMN symbol_occurrence_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (symbol_occurrence_count >= 0)"
            ),
            "relation_count": (
                "ALTER TABLE snapshot ADD COLUMN relation_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (relation_count >= 0)"
            ),
            "condition_count": (
                "ALTER TABLE snapshot ADD COLUMN condition_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (condition_count >= 0)"
            ),
        }
        for name, statement in additions.items():
            if name not in columns:
                self.connection.execute(statement)
        self.connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            ("3",),
        )
        self.connection.commit()

    def _migrate_v3_to_v4(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(snapshot)").fetchall()
        }
        additions = {
            "analysis_cache_hit_count": (
                "ALTER TABLE snapshot ADD COLUMN analysis_cache_hit_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (analysis_cache_hit_count >= 0)"
            ),
            "analysis_cache_miss_count": (
                "ALTER TABLE snapshot ADD COLUMN analysis_cache_miss_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (analysis_cache_miss_count >= 0)"
            ),
        }
        for name, statement in additions.items():
            if name not in columns:
                self.connection.execute(statement)
        self.connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            ("4",),
        )
        self.connection.commit()

    def _migrate_v4_to_v5(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(snapshot)").fetchall()
        }
        additions = {
            "seed_file_count": (
                "ALTER TABLE snapshot ADD COLUMN seed_file_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (seed_file_count >= 0)"
            ),
            "dependency_file_count": (
                "ALTER TABLE snapshot ADD COLUMN dependency_file_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (dependency_file_count >= 0)"
            ),
            "dependency_unresolved_count": (
                "ALTER TABLE snapshot ADD COLUMN dependency_unresolved_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (dependency_unresolved_count >= 0)"
            ),
            "dependency_ambiguous_count": (
                "ALTER TABLE snapshot ADD COLUMN dependency_ambiguous_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (dependency_ambiguous_count >= 0)"
            ),
            "dependency_expansion_truncated": (
                "ALTER TABLE snapshot ADD COLUMN dependency_expansion_truncated "
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK (dependency_expansion_truncated IN (0, 1))"
            ),
        }
        for name, statement in additions.items():
            if name not in columns:
                self.connection.execute(statement)
        self.connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def summary(self) -> dict[str, Any]:
        repositories = self.connection.execute(
            "SELECT COUNT(*) AS count FROM repository"
        ).fetchone()["count"]
        snapshots = self.connection.execute(
            "SELECT COUNT(*) AS count FROM snapshot"
        ).fetchone()["count"]
        active_rows = self.connection.execute(
            """
            SELECT s.id, r.name AS repository, s.revision, s.source_digest,
                   s.manifest_digest, s.state, s.file_count, s.blob_count,
                   s.chunk_count, s.structured_chunk_count,
                   s.fallback_chunk_count, s.parse_error_count,
                   s.symbol_occurrence_count, s.relation_count, s.condition_count,
                   s.analysis_cache_hit_count, s.analysis_cache_miss_count,
                   s.seed_file_count, s.dependency_file_count,
                   s.dependency_unresolved_count, s.dependency_ambiguous_count,
                   s.dependency_expansion_truncated,
                   s.byte_count, s.skipped_file_count,
                   s.created_at, s.activated_at
            FROM snapshot AS s
            JOIN repository AS r ON r.id = s.repository_id
            WHERE s.state = 'active'
            ORDER BY r.name
            """
        ).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "database": str(self.path.resolve()),
            "repository_count": repositories,
            "snapshot_count": snapshots,
            "active_snapshots": [dict(row) for row in active_rows],
        }

    def find_symbol(
        self,
        name: str,
        top_k: int = 50,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("symbol name must not be empty")
        if top_k < 1 or top_k > 200:
            raise ValueError("top-k must be between 1 and 200")

        snapshot_predicates: list[str] = []
        snapshot_parameters: list[Any] = []
        if snapshot_id:
            snapshot_predicates.append("s.id = ?")
            snapshot_parameters.append(snapshot_id)
        else:
            snapshot_predicates.append("s.state = 'active'")
        if repository:
            snapshot_predicates.append("repo.name = ?")
            snapshot_parameters.append(repository)
        snapshot_filter = " AND ".join(snapshot_predicates)

        occurrence_rows = self.connection.execute(
            f"""
            SELECT ls.id AS logical_symbol_id, ls.name, ls.kind, ls.namespace,
                   ls.signature, so.role, so.confidence, so.generator,
                   s.id AS snapshot_id, s.revision, repo.name AS repository,
                   f.path, so.start_line, so.end_line,
                   condition.expression AS source_condition
            FROM symbol_occurrence AS so
            JOIN logical_symbol AS ls ON ls.id = so.logical_symbol_id
            JOIN source_file AS f ON f.id = so.file_id
            JOIN snapshot AS s ON s.id = so.snapshot_id
            JOIN repository AS repo ON repo.id = s.repository_id
            LEFT JOIN source_condition AS condition ON condition.id = so.condition_id
            WHERE ls.name = ? AND {snapshot_filter}
            ORDER BY repo.name, f.path, so.start_line
            LIMIT ?
            """,
            [name, *snapshot_parameters, top_k],
        ).fetchall()

        relation_rows = self.connection.execute(
            f"""
            SELECT rel.id, rel.kind, rel.target_text, rel.confidence,
                   rel.generator, rel.start_line, rel.end_line,
                   s.id AS snapshot_id, s.revision, repo.name AS repository,
                   source_file.path AS source_path,
                   source_symbol.name AS source_symbol,
                   target_file.path AS target_path,
                   target_symbol.name AS target_symbol,
                   condition.expression AS source_condition
            FROM relation AS rel
            JOIN source_file AS source_file ON source_file.id = rel.source_file_id
            JOIN snapshot AS s ON s.id = rel.snapshot_id
            JOIN repository AS repo ON repo.id = s.repository_id
            LEFT JOIN logical_symbol AS source_symbol ON source_symbol.id = rel.source_symbol_id
            LEFT JOIN logical_symbol AS target_symbol ON target_symbol.id = rel.target_symbol_id
            LEFT JOIN source_file AS target_file ON target_file.id = rel.target_file_id
            LEFT JOIN source_condition AS condition ON condition.id = rel.condition_id
            WHERE (source_symbol.name = ? OR target_symbol.name = ? OR rel.target_text = ?)
              AND {snapshot_filter}
            ORDER BY repo.name, source_file.path, rel.start_line, rel.kind
            LIMIT ?
            """,
            [name, name, name, *snapshot_parameters, top_k],
        ).fetchall()

        occurrences = []
        for row in occurrence_rows:
            item = dict(row)
            item["citation"] = (
                f"{row['repository']}@{row['revision']}:"
                f"{row['path']}:{row['start_line']}-{row['end_line']}"
            )
            occurrences.append(item)
        relations = []
        for row in relation_rows:
            item = dict(row)
            item["citation"] = (
                f"{row['repository']}@{row['revision']}:"
                f"{row['source_path']}:{row['start_line']}-{row['end_line']}"
            )
            relations.append(item)
        return {
            "name": name,
            "occurrence_count": len(occurrences),
            "relation_count": len(relations),
            "occurrences": occurrences,
            "relations": relations,
        }

    def resolve_snapshots(
        self,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        predicates: list[str] = []
        parameters: list[Any] = []
        if snapshot_id:
            predicates.append("s.id = ?")
            parameters.append(snapshot_id)
        else:
            predicates.append("s.state = 'active'")
        if repository:
            predicates.append("r.name = ?")
            parameters.append(repository)
        rows = self.connection.execute(
            f"""
            SELECT r.name AS repository, s.id AS snapshot_id, s.revision,
                   s.source_digest, s.manifest_digest, s.index_profile_digest,
                   s.state
            FROM snapshot AS s
            JOIN repository AS r ON r.id = s.repository_id
            WHERE {' AND '.join(predicates)}
            ORDER BY r.name, s.id
            """,
            parameters,
        ).fetchall()
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
        terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError("query must contain at least one searchable term")
        fts_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

        predicates = ["chunk_fts MATCH ?"]
        parameters: list[Any] = [fts_query]
        if snapshot_id:
            predicates.append("s.id = ?")
            parameters.append(snapshot_id)
        else:
            predicates.append("s.state = 'active'")
        if repository:
            predicates.append("r.name = ?")
            parameters.append(repository)
        parameters.append(top_k * 5)

        rows = self.connection.execute(
            f"""
            SELECT c.id AS chunk_id, f.blob_id, c.content_hash,
                   r.name AS repository, s.id AS snapshot_id,
                   s.revision, f.path, c.start_line, c.end_line,
                   c.kind, c.symbol, c.generator,
                   bm25(chunk_fts) AS fts_rank, chunk_fts.content AS content
            FROM chunk_fts
            JOIN chunk AS c ON c.id = chunk_fts.chunk_id
            JOIN source_file AS f ON f.id = c.file_id
            JOIN snapshot AS s ON s.id = c.snapshot_id
            JOIN repository AS r ON r.id = s.repository_id
            WHERE {' AND '.join(predicates)}
            ORDER BY fts_rank, f.path, c.start_line
            LIMIT ?
            """,
            parameters,
        ).fetchall()

        hits: list[SearchHit] = []
        per_file: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (row["snapshot_id"], row["path"])
            if per_file.get(key, 0) >= 2:
                continue
            per_file[key] = per_file.get(key, 0) + 1
            content = row["content"]
            content_truncated = False
            if len(content) > 1_600:
                content = content[:1_600].rstrip() + "\n…"
                content_truncated = True
            hits.append(
                SearchHit(
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
                    rank=row["fts_rank"],
                    content=content,
                    content_truncated=content_truncated,
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def dump_summary_json(self) -> str:
        return json.dumps(self.summary(), ensure_ascii=False, indent=2)
