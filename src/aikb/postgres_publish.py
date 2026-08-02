from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, text

from aikb.catalog import Catalog
from aikb.structured_chunks import FALLBACK_GENERATOR, TREE_SITTER_GENERATOR


@dataclass(frozen=True)
class PublishResult:
    snapshot_id: str
    state: str
    idempotent: bool
    reactivated: bool
    superseded_snapshot_ids: tuple[str, ...]
    file_count: int
    chunk_count: int
    symbol_occurrence_count: int
    relation_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "state": self.state,
            "idempotent": self.idempotent,
            "reactivated": self.reactivated,
            "superseded_snapshot_ids": list(self.superseded_snapshot_ids),
            "file_count": self.file_count,
            "chunk_count": self.chunk_count,
            "symbol_occurrence_count": self.symbol_occurrence_count,
            "relation_count": self.relation_count,
        }


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class PostgresSnapshotPublisher:
    """Copies a validated SQLite snapshot into PostgreSQL atomically."""

    def __init__(self, engine: Engine, batch_size: int = 1_000):
        if batch_size < 1:
            raise ValueError("batch-size must be at least 1")
        self.engine = engine
        self.batch_size = batch_size

    def publish(
        self,
        source: Catalog,
        snapshot_id: str | None = None,
    ) -> PublishResult:
        snapshot = self._load_snapshot(source, snapshot_id)
        repository = self._load_repository(source, snapshot["repository_id"])
        with self.engine.begin() as target:
            self._upsert_repository(target, repository)
            target.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"),
                {"id": repository["id"]},
            )
            existing = target.execute(
                text("SELECT state FROM snapshot WHERE id=:id FOR UPDATE"),
                {"id": snapshot["id"]},
            ).mappings().first()
            if existing:
                return self._handle_existing(target, snapshot, existing["state"])

            self._copy_blobs_and_artifacts(source, target, snapshot["id"])
            self._insert_snapshot(target, snapshot)
            target.execute(
                text(
                    "INSERT INTO snapshot_event(snapshot_id,state) "
                    "VALUES (:id,'building')"
                ),
                {"id": snapshot["id"]},
            )
            self._copy_files_and_chunks(source, target, snapshot["id"])
            self._copy_symbols_conditions_relations(source, target, snapshot["id"])
            self._validate_counts(target, snapshot)
            target.execute(
                text("UPDATE snapshot SET state='validated' WHERE id=:id"),
                {"id": snapshot["id"]},
            )
            target.execute(
                text(
                    "INSERT INTO snapshot_event(snapshot_id,state) "
                    "VALUES (:id,'validated')"
                ),
                {"id": snapshot["id"]},
            )
            superseded = self._activate(target, snapshot)
            return self._result(snapshot, False, False, superseded)

    @staticmethod
    def _load_snapshot(source: Catalog, snapshot_id: str | None) -> dict[str, Any]:
        if snapshot_id:
            row = source.connection.execute(
                "SELECT * FROM snapshot WHERE id=?", (snapshot_id,)
            ).fetchone()
        else:
            rows = source.connection.execute(
                "SELECT * FROM snapshot WHERE state='active' ORDER BY id"
            ).fetchall()
            if len(rows) != 1:
                raise ValueError(
                    "snapshot-id is required unless the SQLite catalog has exactly one active snapshot"
                )
            row = rows[0]
        if row is None:
            raise ValueError(f"SQLite snapshot not found: {snapshot_id}")
        if row["state"] not in {"active", "superseded"}:
            raise ValueError(
                f"SQLite snapshot must be validated/published, got state={row['state']}"
            )
        return dict(row)

    @staticmethod
    def _load_repository(source: Catalog, repository_id: str) -> dict[str, Any]:
        row = source.connection.execute(
            "SELECT * FROM repository WHERE id=?", (repository_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"SQLite repository not found: {repository_id}")
        return dict(row)

    @staticmethod
    def _upsert_repository(target: Connection, repository: dict[str, Any]) -> None:
        target.execute(
            text(
                "INSERT INTO repository(id,name,source_kind,source_uri,created_at) "
                "VALUES (:id,:name,:source_kind,:source_uri,:created_at) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {**repository, "created_at": _timestamp(repository["created_at"])},
        )
        stored = target.execute(
            text("SELECT name,source_kind,source_uri FROM repository WHERE id=:id"),
            {"id": repository["id"]},
        ).mappings().one()
        expected = {key: repository[key] for key in ("name", "source_kind", "source_uri")}
        if dict(stored) != expected:
            raise RuntimeError("repository identity conflicts with existing PostgreSQL row")

    def _handle_existing(
        self,
        target: Connection,
        snapshot: dict[str, Any],
        state: str,
    ) -> PublishResult:
        if state == "active":
            self._validate_counts(target, snapshot)
            return self._result(snapshot, True, False, ())
        if state != "superseded":
            raise RuntimeError(
                f"PostgreSQL snapshot exists in incomplete state {state}; manual repair required"
            )
        self._validate_counts(target, snapshot)
        superseded = self._activate(target, snapshot)
        return self._result(snapshot, True, True, superseded)

    def _iter_rows(
        self,
        source: Catalog,
        query: str,
        parameters: Sequence[Any],
    ) -> Iterator[list[dict[str, Any]]]:
        cursor = source.connection.execute(query, parameters)
        while rows := cursor.fetchmany(self.batch_size):
            yield [dict(row) for row in rows]

    def _copy_blobs_and_artifacts(
        self,
        source: Catalog,
        target: Connection,
        snapshot_id: str,
    ) -> None:
        for blobs in self._iter_rows(
            source,
            """
            SELECT DISTINCT b.* FROM blob b
            JOIN source_file f ON f.blob_id=b.id
            WHERE f.snapshot_id=? ORDER BY b.id
            """,
            (snapshot_id,),
        ):
            for row in blobs:
                row["created_at"] = _timestamp(row["created_at"])
            target.execute(
                text(
                    "INSERT INTO blob(id,algorithm,size_bytes,compression,compressed_content,created_at) "
                    "VALUES (:id,:algorithm,:size_bytes,:compression,:compressed_content,:created_at) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                blobs,
            )
        for artifacts in self._iter_rows(
            source,
            """
            SELECT DISTINCT a.* FROM analysis_artifact a
            JOIN source_file f ON f.blob_id=a.blob_id
            WHERE f.snapshot_id=? ORDER BY a.id
            """,
            (snapshot_id,),
        ):
            for row in artifacts:
                row["created_at"] = _timestamp(row["created_at"])
            target.execute(
                text(
                    "INSERT INTO analysis_artifact("
                    "id,blob_id,language,analysis_profile_digest,schema_version,compression,"
                    "compressed_payload,chunk_count,symbol_occurrence_count,relation_count,"
                    "condition_count,created_at) VALUES ("
                    ":id,:blob_id,:language,:analysis_profile_digest,:schema_version,:compression,"
                    ":compressed_payload,:chunk_count,:symbol_occurrence_count,:relation_count,"
                    ":condition_count,:created_at) ON CONFLICT (id) DO NOTHING"
                ),
                artifacts,
            )

    @staticmethod
    def _insert_snapshot(target: Connection, snapshot: dict[str, Any]) -> None:
        values = dict(snapshot)
        values["state"] = "building"
        values["created_at"] = _timestamp(snapshot["created_at"])
        values["activated_at"] = None
        values["dependency_expansion_truncated"] = bool(
            snapshot["dependency_expansion_truncated"]
        )
        columns = [
            "id", "repository_id", "revision", "source_digest", "manifest_digest",
            "index_profile_digest", "state", "file_count", "blob_count", "chunk_count",
            "structured_chunk_count", "fallback_chunk_count", "parse_error_count",
            "symbol_occurrence_count", "relation_count", "condition_count",
            "analysis_cache_hit_count", "analysis_cache_miss_count", "seed_file_count",
            "dependency_file_count", "dependency_unresolved_count",
            "dependency_ambiguous_count", "dependency_expansion_truncated", "byte_count",
            "skipped_file_count", "created_at", "activated_at",
        ]
        target.execute(
            text(
                f"INSERT INTO snapshot({','.join(columns)}) VALUES "
                f"({','.join(':' + column for column in columns)})"
            ),
            {column: values[column] for column in columns},
        )

    def _copy_files_and_chunks(
        self,
        source: Catalog,
        target: Connection,
        snapshot_id: str,
    ) -> None:
        for files in self._iter_rows(
            source,
            "SELECT * FROM source_file WHERE snapshot_id=? ORDER BY path",
            (snapshot_id,),
        ):
            target.execute(
                text(
                    "INSERT INTO source_file(id,snapshot_id,path,blob_id,language,line_count,"
                    "size_bytes,decode_status,parse_status,syntax_error_count) VALUES ("
                    ":id,:snapshot_id,:path,:blob_id,:language,:line_count,:size_bytes,"
                    ":decode_status,:parse_status,:syntax_error_count)"
                ),
                files,
            )
        for chunks in self._iter_rows(
            source,
            """
            SELECT c.*, chunk_fts.content AS content
            FROM chunk c JOIN chunk_fts ON chunk_fts.chunk_id=c.id
            WHERE c.snapshot_id=? ORDER BY c.file_id,c.ordinal
            """,
            (snapshot_id,),
        ):
            target.execute(
                text(
                    "INSERT INTO chunk(id,snapshot_id,file_id,ordinal,kind,start_line,end_line,"
                    "symbol,content_hash,generator,content) VALUES ("
                    ":id,:snapshot_id,:file_id,:ordinal,:kind,:start_line,:end_line,:symbol,"
                    ":content_hash,:generator,:content)"
                ),
                chunks,
            )

    def _copy_symbols_conditions_relations(
        self,
        source: Catalog,
        target: Connection,
        snapshot_id: str,
    ) -> None:
        for symbols in self._iter_rows(
            source,
            """
            SELECT ls.* FROM logical_symbol ls
            WHERE ls.id IN (
                SELECT logical_symbol_id FROM symbol_occurrence WHERE snapshot_id=?
                UNION
                SELECT source_symbol_id FROM relation
                WHERE snapshot_id=? AND source_symbol_id IS NOT NULL
                UNION
                SELECT target_symbol_id FROM relation
                WHERE snapshot_id=? AND target_symbol_id IS NOT NULL
            )
            ORDER BY ls.id
            """,
            (snapshot_id, snapshot_id, snapshot_id),
        ):
            for row in symbols:
                row["created_at"] = _timestamp(row["created_at"])
            target.execute(
                text(
                    "INSERT INTO logical_symbol(id,repository_id,language,kind,namespace,name,"
                    "signature,created_at) VALUES (:id,:repository_id,:language,:kind,:namespace,"
                    ":name,:signature,:created_at) ON CONFLICT (id) DO NOTHING"
                ),
                symbols,
            )
        for conditions in self._iter_rows(
            source,
            "SELECT * FROM source_condition WHERE snapshot_id=? ORDER BY id",
            (snapshot_id,),
        ):
            target.execute(
                text(
                    "INSERT INTO source_condition(id,snapshot_id,file_id,expression,start_line,"
                    "end_line,depth,generator) VALUES (:id,:snapshot_id,:file_id,:expression,"
                    ":start_line,:end_line,:depth,:generator)"
                ),
                conditions,
            )
        for occurrences in self._iter_rows(
            source,
            "SELECT * FROM symbol_occurrence WHERE snapshot_id=? ORDER BY id",
            (snapshot_id,),
        ):
            target.execute(
                text(
                    "INSERT INTO symbol_occurrence(id,snapshot_id,file_id,logical_symbol_id,"
                    "condition_id,role,start_line,end_line,confidence,generator) VALUES ("
                    ":id,:snapshot_id,:file_id,:logical_symbol_id,:condition_id,:role,"
                    ":start_line,:end_line,:confidence,:generator)"
                ),
                occurrences,
            )
        for relations in self._iter_rows(
            source,
            "SELECT * FROM relation WHERE snapshot_id=? ORDER BY id",
            (snapshot_id,),
        ):
            target.execute(
                text(
                    "INSERT INTO relation(id,snapshot_id,source_file_id,source_symbol_id,"
                    "target_file_id,target_symbol_id,condition_id,kind,target_text,start_line,"
                    "end_line,confidence,generator) VALUES (:id,:snapshot_id,:source_file_id,"
                    ":source_symbol_id,:target_file_id,:target_symbol_id,:condition_id,:kind,"
                    ":target_text,:start_line,:end_line,:confidence,:generator)"
                ),
                relations,
            )

    def _validate_counts(self, target: Connection, snapshot: dict[str, Any]) -> None:
        queries = {
            "file_count": "SELECT count(*) FROM source_file WHERE snapshot_id=:id",
            "blob_count": "SELECT count(DISTINCT blob_id) FROM source_file WHERE snapshot_id=:id",
            "chunk_count": "SELECT count(*) FROM chunk WHERE snapshot_id=:id",
            "structured_chunk_count": (
                "SELECT count(*) FROM chunk WHERE snapshot_id=:id AND generator=:structured"
            ),
            "fallback_chunk_count": (
                "SELECT count(*) FROM chunk WHERE snapshot_id=:id AND generator=:fallback"
            ),
            "parse_error_count": (
                "SELECT COALESCE(sum(syntax_error_count),0) FROM source_file "
                "WHERE snapshot_id=:id"
            ),
            "symbol_occurrence_count": "SELECT count(*) FROM symbol_occurrence WHERE snapshot_id=:id",
            "relation_count": "SELECT count(*) FROM relation WHERE snapshot_id=:id",
            "condition_count": "SELECT count(*) FROM source_condition WHERE snapshot_id=:id",
            "byte_count": (
                "SELECT COALESCE(sum(size_bytes),0) FROM source_file WHERE snapshot_id=:id"
            ),
        }
        mismatches: list[str] = []
        parameters = {
            "id": snapshot["id"],
            "structured": TREE_SITTER_GENERATOR,
            "fallback": FALLBACK_GENERATOR,
        }
        for field, query in queries.items():
            actual = target.execute(text(query), parameters).scalar_one()
            if actual != snapshot[field]:
                mismatches.append(f"{field}: expected {snapshot[field]}, got {actual}")
        if mismatches:
            raise RuntimeError("snapshot validation failed: " + "; ".join(mismatches))

    @staticmethod
    def _activate(target: Connection, snapshot: dict[str, Any]) -> tuple[str, ...]:
        rows = target.execute(
            text(
                "UPDATE snapshot SET state='superseded' "
                "WHERE repository_id=:repository_id AND state='active' AND id<>:id "
                "RETURNING id"
            ),
            {"repository_id": snapshot["repository_id"], "id": snapshot["id"]},
        ).scalars().all()
        for old_id in rows:
            target.execute(
                text(
                    "INSERT INTO snapshot_event(snapshot_id,state) "
                    "VALUES (:id,'superseded')"
                ),
                {"id": old_id},
            )
        target.execute(
            text("UPDATE snapshot SET state='active',activated_at=now() WHERE id=:id"),
            {"id": snapshot["id"]},
        )
        target.execute(
            text("INSERT INTO snapshot_event(snapshot_id,state) VALUES (:id,'active')"),
            {"id": snapshot["id"]},
        )
        return tuple(rows)

    @staticmethod
    def _result(
        snapshot: dict[str, Any],
        idempotent: bool,
        reactivated: bool,
        superseded: tuple[str, ...],
    ) -> PublishResult:
        return PublishResult(
            snapshot_id=snapshot["id"],
            state="active",
            idempotent=idempotent,
            reactivated=reactivated,
            superseded_snapshot_ids=superseded,
            file_count=snapshot["file_count"],
            chunk_count=snapshot["chunk_count"],
            symbol_occurrence_count=snapshot["symbol_occurrence_count"],
            relation_count=snapshot["relation_count"],
        )
