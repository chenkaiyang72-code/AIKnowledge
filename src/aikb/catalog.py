from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aikb.storage import LexicalSearchResult, SourceLocation


SCHEMA_VERSION = 6


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

CREATE TABLE IF NOT EXISTS solution (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solution_snapshot (
    id TEXT PRIMARY KEY,
    solution_id TEXT NOT NULL REFERENCES solution(id),
    revision TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('building', 'validated', 'active', 'superseded')),
    member_count INTEGER NOT NULL DEFAULT 0 CHECK (member_count >= 0),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (solution_id, revision, manifest_digest)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_solution_snapshot_per_solution
ON solution_snapshot(solution_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS solution_snapshot_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    solution_snapshot_id TEXT NOT NULL REFERENCES solution_snapshot(id),
    state TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solution_snapshot_member (
    solution_snapshot_id TEXT NOT NULL REFERENCES solution_snapshot(id),
    repository_id TEXT NOT NULL REFERENCES repository(id),
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id),
    role TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
    PRIMARY KEY (solution_snapshot_id, repository_id),
    UNIQUE (solution_snapshot_id, snapshot_id),
    UNIQUE (solution_snapshot_id, role),
    UNIQUE (solution_snapshot_id, ordinal)
);

CREATE INDEX IF NOT EXISTS solution_snapshot_member_snapshot
ON solution_snapshot_member(snapshot_id);

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

CREATE INDEX IF NOT EXISTS chunk_file_range
ON chunk(file_id, start_line, end_line);

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

CREATE INDEX IF NOT EXISTS relation_source_symbol
ON relation(snapshot_id, source_symbol_id, kind);

CREATE INDEX IF NOT EXISTS relation_target_text
ON relation(snapshot_id, target_text, kind);

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

    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            if not path.is_file():
                raise FileNotFoundError(f"catalog does not exist: {path}")
            self.connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if read_only:
            self.connection.execute("PRAGMA query_only = ON")
        else:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = NORMAL")
            # Full-repository scans stage unresolved relations in a temporary
            # table. Keep it disk-backed across SQLite build configurations.
            self.connection.execute("PRAGMA temp_store = FILE")

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        if self.read_only:
            raise RuntimeError("cannot initialize or migrate a read-only catalog")
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
            if current_version == 5:
                self._migrate_v5_to_v6()
                current_version = 6
            if current_version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported catalog schema {current_version}; expected {SCHEMA_VERSION}"
                )

    def validate_schema(self) -> None:
        """Validate a pre-existing catalog without creating or migrating it."""

        try:
            row = self.connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise RuntimeError("catalog schema is not initialized") from error
        if row is None:
            raise RuntimeError("catalog schema version is missing")
        current_version = int(row["value"])
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
            ("5",),
        )
        self.connection.commit()

    def _migrate_v5_to_v6(self) -> None:
        # initialize() executes the idempotent schema DDL first, so migration
        # only advances the catalog contract after the solution tables exist.
        required_tables = {
            "solution",
            "solution_snapshot",
            "solution_snapshot_event",
            "solution_snapshot_member",
        }
        existing_tables = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = required_tables - existing_tables
        if missing:
            raise RuntimeError(
                "solution schema migration did not create: "
                + ", ".join(sorted(missing))
            )
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
        solutions = self.connection.execute(
            "SELECT COUNT(*) AS count FROM solution"
        ).fetchone()["count"]
        solution_snapshots = self.connection.execute(
            "SELECT COUNT(*) AS count FROM solution_snapshot"
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
        active_solution_rows = self.connection.execute(
            """
            SELECT ss.id AS solution_snapshot_id, sol.name AS solution,
                   ss.revision, ss.manifest_digest, ss.state,
                   ss.member_count, ss.created_at, ss.activated_at
            FROM solution_snapshot AS ss
            JOIN solution AS sol ON sol.id = ss.solution_id
            WHERE ss.state = 'active'
            ORDER BY sol.name
            """
        ).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "database": str(self.path.resolve()),
            "repository_count": repositories,
            "snapshot_count": snapshots,
            "active_snapshots": [dict(row) for row in active_rows],
            "solution_count": solutions,
            "solution_snapshot_count": solution_snapshots,
            "active_solution_snapshots": [
                dict(row) for row in active_solution_rows
            ],
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

    @staticmethod
    def _search_hit_from_row(
        row: sqlite3.Row,
        rank: float,
    ) -> SearchHit:
        if "content" in row.keys():
            content = row["content"]
            content_hash = row["content_hash"]
        else:
            source_text = zlib.decompress(row["compressed_content"]).decode(
                "utf-8", errors="replace"
            )
            source_lines = source_text.splitlines(keepends=True)
            content = "".join(
                source_lines[row["start_line"] - 1 : row["end_line"]]
            )
            # Tree-sitter chunks can begin or end inside a source line. The
            # fast path restores complete cited lines from the source blob
            # rather than scanning the multi-million-row FTS table for the
            # exact AST text, so hash the evidence body actually returned.
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        content_truncated = False
        if len(content) > 1_600:
            content = content[:1_600].rstrip() + "\n…"
            content_truncated = True
        return SearchHit(
            chunk_id=row["chunk_id"],
            blob_id=row["blob_id"],
            content_hash=content_hash,
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
            content_truncated=content_truncated,
        )

    def search_symbol_chunks(
        self,
        names: list[str],
        top_k: int = 20,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]:
        ordered_names = list(dict.fromkeys(name for name in names if name))
        if not ordered_names:
            return []
        if top_k < 1 or top_k > 100:
            raise ValueError("top-k must be between 1 and 100")
        scope_predicates: list[str] = []
        scope_parameters: list[Any] = []
        if snapshot_id:
            scope_predicates.append("s.id = ?")
            scope_parameters.append(snapshot_id)
        else:
            scope_predicates.append("s.state = 'active'")
        if repository:
            scope_predicates.append("r.name = ?")
            scope_parameters.append(repository)
        scope_rows = self.connection.execute(
            f"""
            SELECT s.id AS snapshot_id, s.repository_id
            FROM snapshot AS s
            JOIN repository AS r ON r.id = s.repository_id
            WHERE {' AND '.join(scope_predicates)}
            """,
            scope_parameters,
        ).fetchall()
        if not scope_rows:
            return []
        snapshot_ids = [row["snapshot_id"] for row in scope_rows]
        repository_ids = list(
            dict.fromkeys(row["repository_id"] for row in scope_rows)
        )
        snapshot_placeholders = ",".join("?" for _ in snapshot_ids)
        repository_placeholders = ",".join("?" for _ in repository_ids)
        per_name_limit = min(top_k * 3, 100)
        rows: list[sqlite3.Row] = []
        for name in ordered_names:
            symbol_rows = self.connection.execute(
                f"""
                SELECT id
                FROM logical_symbol INDEXED BY logical_symbol_repository_name
                WHERE repository_id IN ({repository_placeholders})
                  AND name = ?
                """,
                [*repository_ids, name],
            ).fetchall()
            if not symbol_rows:
                continue
            symbol_ids = [row["id"] for row in symbol_rows]
            symbol_placeholders = ",".join("?" for _ in symbol_ids)
            rows.extend(
                self.connection.execute(
                    f"""
                    SELECT c.id AS chunk_id, f.blob_id, c.content_hash,
                           r.name AS repository, s.id AS snapshot_id, s.revision,
                           f.path, c.start_line, c.end_line, c.kind, c.symbol,
                           c.generator, b.compressed_content,
                           ls.name AS matched_name, so.role,
                           so.start_line AS occurrence_line
                    FROM symbol_occurrence AS so
                        INDEXED BY symbol_occurrence_snapshot_symbol
                    JOIN logical_symbol AS ls ON ls.id = so.logical_symbol_id
                    JOIN source_file AS f ON f.id = so.file_id
                    JOIN chunk AS c ON c.file_id = f.id
                        AND c.start_line <= so.end_line
                        AND c.end_line >= so.start_line
                    JOIN blob AS b ON b.id = f.blob_id
                    JOIN snapshot AS s ON s.id = so.snapshot_id
                    JOIN repository AS r ON r.id = s.repository_id
                    WHERE so.snapshot_id IN ({snapshot_placeholders})
                      AND so.logical_symbol_id IN ({symbol_placeholders})
                    ORDER BY
                        CASE so.role
                            WHEN 'definition' THEN 0
                            WHEN 'declaration' THEN 1
                            ELSE 2
                        END,
                        f.path,
                        so.start_line,
                        c.id
                    LIMIT ?
                    """,
                    [*snapshot_ids, *symbol_ids, per_name_limit],
                ).fetchall()
            )
        name_order = {name: index for index, name in enumerate(ordered_names)}
        sorted_rows = sorted(
            rows,
            key=lambda row: (
                name_order[row["matched_name"]],
                0 if row["role"] == "definition" else 1,
                row["path"],
                row["occurrence_line"],
                row["chunk_id"],
            ),
        )
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for row in sorted_rows:
            if row["chunk_id"] in seen:
                continue
            seen.add(row["chunk_id"])
            hits.append(self._search_hit_from_row(row, float(len(hits) + 1)))
            if len(hits) >= top_k:
                break
        return hits

    def search_relation_chunks(
        self,
        names: list[str],
        top_k: int = 20,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]:
        ordered_names = list(dict.fromkeys(name for name in names if name))
        if not ordered_names:
            return []
        if top_k < 1 or top_k > 100:
            raise ValueError("top-k must be between 1 and 100")
        placeholders = ",".join("?" for _ in ordered_names)
        snapshot_predicates: list[str] = []
        snapshot_parameters: list[Any] = []
        if snapshot_id:
            snapshot_predicates.append("s.id = ?")
            snapshot_parameters.append(snapshot_id)
        else:
            snapshot_predicates.append("s.state = 'active'")
        if repository:
            snapshot_predicates.append("r.name = ?")
            snapshot_parameters.append(repository)
        branch_limit = top_k * 10
        parameters: list[Any] = [
            *snapshot_parameters,
            *ordered_names,
            branch_limit,
            branch_limit,
            *ordered_names,
            branch_limit,
            top_k * 30,
        ]
        rows = self.connection.execute(
            f"""
            WITH candidate_snapshot(id, repository_id) AS (
                SELECT s.id, s.repository_id
                FROM snapshot AS s
                JOIN repository AS r ON r.id = s.repository_id
                WHERE {' AND '.join(snapshot_predicates)}
            ),
            matched_symbol(id) AS (
                SELECT ls.id
                FROM logical_symbol AS ls
                JOIN candidate_snapshot AS scope
                    ON scope.repository_id = ls.repository_id
                WHERE ls.name IN ({placeholders})
            ),
            candidate_relation(id) AS (
                SELECT id FROM (
                    SELECT rel.id
                    FROM relation AS rel
                    WHERE rel.snapshot_id IN (SELECT id FROM candidate_snapshot)
                      AND rel.source_symbol_id IN (SELECT id FROM matched_symbol)
                    LIMIT ?
                )
                UNION
                SELECT id FROM (
                    SELECT rel.id
                    FROM relation AS rel
                    WHERE rel.snapshot_id IN (SELECT id FROM candidate_snapshot)
                      AND rel.target_symbol_id IN (SELECT id FROM matched_symbol)
                    LIMIT ?
                )
                UNION
                SELECT id FROM (
                    SELECT rel.id
                    FROM relation AS rel
                    WHERE rel.snapshot_id IN (SELECT id FROM candidate_snapshot)
                      AND rel.target_text IN ({placeholders})
                    LIMIT ?
                )
            )
            SELECT c.id AS chunk_id, f.blob_id, c.content_hash,
                   r.name AS repository, s.id AS snapshot_id, s.revision,
                   f.path, c.start_line, c.end_line, c.kind, c.symbol,
                   c.generator, b.compressed_content,
                   source_symbol.name AS source_name,
                   target_symbol.name AS target_name,
                   rel.target_text, rel.start_line AS relation_line,
                   rel.confidence
            FROM candidate_relation AS candidate
            JOIN relation AS rel ON rel.id = candidate.id
            JOIN source_file AS f ON f.id = rel.source_file_id
            JOIN chunk AS c ON c.file_id = f.id
                AND c.start_line <= rel.end_line
                AND c.end_line >= rel.start_line
            JOIN blob AS b ON b.id = f.blob_id
            JOIN snapshot AS s ON s.id = rel.snapshot_id
            JOIN repository AS r ON r.id = s.repository_id
            LEFT JOIN logical_symbol AS source_symbol
                ON source_symbol.id = rel.source_symbol_id
            LEFT JOIN logical_symbol AS target_symbol
                ON target_symbol.id = rel.target_symbol_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
        name_order = {name: index for index, name in enumerate(ordered_names)}

        def matched_order(row: sqlite3.Row) -> int:
            matches = [
                name_order[value]
                for value in (
                    row["source_name"],
                    row["target_name"],
                    row["target_text"],
                )
                if value in name_order
            ]
            return min(matches) if matches else len(name_order)

        sorted_rows = sorted(
            rows,
            key=lambda row: (
                matched_order(row),
                0 if row["confidence"] == "source_exact" else 1,
                row["path"],
                row["relation_line"],
                row["chunk_id"],
            ),
        )
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for row in sorted_rows:
            if row["chunk_id"] in seen:
                continue
            seen.add(row["chunk_id"])
            hits.append(self._search_hit_from_row(row, float(len(hits) + 1)))
            if len(hits) >= top_k:
                break
        return hits

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
            hits.append(self._search_hit_from_row(row, row["fts_rank"]))
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
        from aikb.storage import LexicalSearchResult

        return LexicalSearchResult(
            channel="lexical_fts5",
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
        hits: list[SearchHit] = []
        seen: set[tuple[str, str]] = set()
        for location in locations:
            if location.line < 1:
                continue
            row = self.connection.execute(
                """
                SELECT c.id AS chunk_id, f.blob_id, c.content_hash,
                       r.name AS repository, s.id AS snapshot_id, s.revision,
                       f.path, c.start_line, c.end_line, c.kind, c.symbol,
                       c.generator, b.compressed_content
                FROM chunk AS c
                JOIN source_file AS f ON f.id = c.file_id
                JOIN blob AS b ON b.id = f.blob_id
                JOIN snapshot AS s ON s.id = c.snapshot_id
                JOIN repository AS r ON r.id = s.repository_id
                WHERE r.name = ? AND s.id = ? AND f.path = ?
                  AND c.start_line <= ? AND c.end_line >= ?
                ORDER BY (c.end_line - c.start_line), c.ordinal
                LIMIT 1
                """,
                (
                    location.repository,
                    location.snapshot_id,
                    location.path,
                    location.line,
                    location.line,
                ),
            ).fetchone()
            if row is None:
                continue
            key = (row["snapshot_id"], row["chunk_id"])
            if key in seen:
                continue
            seen.add(key)
            hits.append(self._search_hit_from_row(row, location.rank))
            if len(hits) >= top_k:
                break
        return hits

    def dump_summary_json(self) -> str:
        return json.dumps(self.summary(), ensure_ascii=False, indent=2)
