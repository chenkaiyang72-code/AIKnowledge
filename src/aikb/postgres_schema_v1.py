from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


POSTGRES_SCHEMA_VERSION = 1

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


schema_metadata = Table(
    "schema_metadata",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text, nullable=False),
)

repository = Table(
    "repository",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(512), nullable=False, unique=True),
    Column("source_kind", String(64), nullable=False),
    Column("source_uri", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

snapshot = Table(
    "snapshot",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "repository_id",
        String(64),
        ForeignKey("repository.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("revision", Text, nullable=False),
    Column("source_digest", String(64), nullable=False),
    Column("manifest_digest", String(64), nullable=False),
    Column("index_profile_digest", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("file_count", Integer, nullable=False, server_default="0"),
    Column("blob_count", Integer, nullable=False, server_default="0"),
    Column("chunk_count", Integer, nullable=False, server_default="0"),
    Column("structured_chunk_count", Integer, nullable=False, server_default="0"),
    Column("fallback_chunk_count", Integer, nullable=False, server_default="0"),
    Column("parse_error_count", Integer, nullable=False, server_default="0"),
    Column("symbol_occurrence_count", Integer, nullable=False, server_default="0"),
    Column("relation_count", Integer, nullable=False, server_default="0"),
    Column("condition_count", Integer, nullable=False, server_default="0"),
    Column("analysis_cache_hit_count", Integer, nullable=False, server_default="0"),
    Column("analysis_cache_miss_count", Integer, nullable=False, server_default="0"),
    Column("seed_file_count", Integer, nullable=False, server_default="0"),
    Column("dependency_file_count", Integer, nullable=False, server_default="0"),
    Column("dependency_unresolved_count", Integer, nullable=False, server_default="0"),
    Column("dependency_ambiguous_count", Integer, nullable=False, server_default="0"),
    Column(
        "dependency_expansion_truncated",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    Column("byte_count", BigInteger, nullable=False, server_default="0"),
    Column("skipped_file_count", Integer, nullable=False, server_default="0"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("activated_at", DateTime(timezone=True)),
    CheckConstraint(
        "state IN ('building', 'validated', 'active', 'superseded')",
        name="state",
    ),
    CheckConstraint(
        "file_count >= 0 AND blob_count >= 0 AND chunk_count >= 0 "
        "AND structured_chunk_count >= 0 AND fallback_chunk_count >= 0 "
        "AND parse_error_count >= 0 AND symbol_occurrence_count >= 0 "
        "AND relation_count >= 0 AND condition_count >= 0 "
        "AND analysis_cache_hit_count >= 0 AND analysis_cache_miss_count >= 0 "
        "AND seed_file_count >= 0 AND dependency_file_count >= 0 "
        "AND dependency_unresolved_count >= 0 AND dependency_ambiguous_count >= 0 "
        "AND byte_count >= 0 AND skipped_file_count >= 0",
        name="nonnegative_counts",
    ),
    UniqueConstraint(
        "repository_id",
        "revision",
        "manifest_digest",
        "index_profile_digest",
        name="uq_snapshot_identity",
    ),
)
Index(
    "uq_snapshot_one_active_per_repository",
    snapshot.c.repository_id,
    unique=True,
    postgresql_where=snapshot.c.state == "active",
)

snapshot_event = Table(
    "snapshot_event",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "snapshot_id",
        String(64),
        ForeignKey("snapshot.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("state", String(16), nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

blob = Table(
    "blob",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("algorithm", String(16), nullable=False, server_default="sha256"),
    Column("size_bytes", BigInteger, nullable=False),
    Column("compression", String(16), nullable=False, server_default="zlib"),
    Column("compressed_content", LargeBinary, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint("algorithm = 'sha256'", name="algorithm"),
    CheckConstraint("compression = 'zlib'", name="compression"),
    CheckConstraint("size_bytes >= 0", name="nonnegative_size"),
)

analysis_artifact = Table(
    "analysis_artifact",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "blob_id",
        String(64),
        ForeignKey("blob.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("language", String(64), nullable=False),
    Column("analysis_profile_digest", String(64), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("compression", String(16), nullable=False, server_default="zlib"),
    Column("compressed_payload", LargeBinary, nullable=False),
    Column("chunk_count", Integer, nullable=False),
    Column("symbol_occurrence_count", Integer, nullable=False),
    Column("relation_count", Integer, nullable=False),
    Column("condition_count", Integer, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint("schema_version >= 1", name="positive_schema_version"),
    CheckConstraint("compression = 'zlib'", name="compression"),
    CheckConstraint(
        "chunk_count >= 0 AND symbol_occurrence_count >= 0 "
        "AND relation_count >= 0 AND condition_count >= 0",
        name="nonnegative_counts",
    ),
    UniqueConstraint(
        "blob_id",
        "language",
        "analysis_profile_digest",
        name="uq_analysis_artifact_blob_language_profile",
    ),
)
Index(
    "ix_analysis_artifact_blob_profile",
    analysis_artifact.c.blob_id,
    analysis_artifact.c.analysis_profile_digest,
)

source_file = Table(
    "source_file",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "snapshot_id",
        String(64),
        ForeignKey("snapshot.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "blob_id",
        String(64),
        ForeignKey("blob.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("path", Text, nullable=False),
    Column("language", String(64), nullable=False),
    Column("line_count", Integer, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("decode_status", String(16), nullable=False),
    Column("parse_status", String(32), nullable=False),
    Column("syntax_error_count", Integer, nullable=False, server_default="0"),
    CheckConstraint("line_count >= 0 AND size_bytes >= 0", name="nonnegative_size"),
    CheckConstraint("syntax_error_count >= 0", name="nonnegative_errors"),
    CheckConstraint("decode_status IN ('utf8', 'replacement')", name="decode_status"),
    CheckConstraint(
        "parse_status IN ('structured', 'fallback', 'not_applicable')",
        name="parse_status",
    ),
    UniqueConstraint("snapshot_id", "path", name="uq_source_file_snapshot_path"),
)
Index("ix_source_file_snapshot_path", source_file.c.snapshot_id, source_file.c.path)

chunk = Table(
    "chunk",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "snapshot_id",
        String(64),
        ForeignKey("snapshot.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "file_id",
        String(64),
        ForeignKey("source_file.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("kind", String(64), nullable=False),
    Column("start_line", Integer, nullable=False),
    Column("end_line", Integer, nullable=False),
    Column("symbol", Text),
    Column("content_hash", String(64), nullable=False),
    Column("generator", String(128), nullable=False),
    CheckConstraint("ordinal >= 0", name="nonnegative_ordinal"),
    CheckConstraint("start_line >= 1 AND end_line >= start_line", name="valid_lines"),
    UniqueConstraint("file_id", "ordinal", name="uq_chunk_file_ordinal"),
)
Index("ix_chunk_snapshot_file", chunk.c.snapshot_id, chunk.c.file_id, chunk.c.ordinal)
Index("ix_chunk_file_range", chunk.c.file_id, chunk.c.start_line, chunk.c.end_line)

logical_symbol = Table(
    "logical_symbol",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "repository_id",
        String(64),
        ForeignKey("repository.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("language", String(64), nullable=False),
    Column("kind", String(64), nullable=False),
    Column("namespace", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("signature", Text),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "repository_id",
        "language",
        "kind",
        "namespace",
        "name",
        name="uq_logical_symbol_identity",
    ),
)
Index(
    "ix_logical_symbol_repository_name_kind",
    logical_symbol.c.repository_id,
    logical_symbol.c.name,
    logical_symbol.c.kind,
)

source_condition = Table(
    "source_condition",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "snapshot_id",
        String(64),
        ForeignKey("snapshot.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "file_id",
        String(64),
        ForeignKey("source_file.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("expression", Text, nullable=False),
    Column("start_line", Integer, nullable=False),
    Column("end_line", Integer, nullable=False),
    Column("depth", Integer, nullable=False),
    Column("generator", String(128), nullable=False),
    CheckConstraint("start_line >= 1 AND end_line >= start_line", name="valid_lines"),
    CheckConstraint("depth >= 1", name="positive_depth"),
    UniqueConstraint(
        "file_id",
        "expression",
        "start_line",
        "end_line",
        "depth",
        name="uq_source_condition_identity",
    ),
)
Index(
    "ix_source_condition_snapshot_file_line",
    source_condition.c.snapshot_id,
    source_condition.c.file_id,
    source_condition.c.start_line,
)

symbol_occurrence = Table(
    "symbol_occurrence",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "snapshot_id",
        String(64),
        ForeignKey("snapshot.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "file_id",
        String(64),
        ForeignKey("source_file.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "logical_symbol_id",
        String(64),
        ForeignKey("logical_symbol.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("condition_id", String(64), ForeignKey("source_condition.id", ondelete="SET NULL")),
    Column("role", String(16), nullable=False),
    Column("start_line", Integer, nullable=False),
    Column("end_line", Integer, nullable=False),
    Column("confidence", String(32), nullable=False),
    Column("generator", String(128), nullable=False),
    CheckConstraint("role IN ('definition', 'declaration')", name="role"),
    CheckConstraint("start_line >= 1 AND end_line >= start_line", name="valid_lines"),
    CheckConstraint(
        "confidence IN ('source_exact', 'source_inferred', "
        "'ambiguous_candidate', 'human_verified')",
        name="confidence",
    ),
    UniqueConstraint(
        "file_id",
        "logical_symbol_id",
        "role",
        "start_line",
        "end_line",
        name="uq_symbol_occurrence_identity",
    ),
)
Index(
    "ix_symbol_occurrence_snapshot_symbol_role",
    symbol_occurrence.c.snapshot_id,
    symbol_occurrence.c.logical_symbol_id,
    symbol_occurrence.c.role,
)

relation = Table(
    "relation",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "snapshot_id",
        String(64),
        ForeignKey("snapshot.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "source_file_id",
        String(64),
        ForeignKey("source_file.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_symbol_id", String(64), ForeignKey("logical_symbol.id", ondelete="SET NULL")),
    Column("target_file_id", String(64), ForeignKey("source_file.id", ondelete="SET NULL")),
    Column("target_symbol_id", String(64), ForeignKey("logical_symbol.id", ondelete="SET NULL")),
    Column("condition_id", String(64), ForeignKey("source_condition.id", ondelete="SET NULL")),
    Column("kind", String(64), nullable=False),
    Column("target_text", Text, nullable=False),
    Column("start_line", Integer, nullable=False),
    Column("end_line", Integer, nullable=False),
    Column("confidence", String(32), nullable=False),
    Column("generator", String(128), nullable=False),
    CheckConstraint("start_line >= 1 AND end_line >= start_line", name="valid_lines"),
    CheckConstraint(
        "confidence IN ('source_exact', 'source_inferred', "
        "'ambiguous_candidate', 'human_verified')",
        name="confidence",
    ),
    CheckConstraint(
        "target_file_id IS NOT NULL OR target_symbol_id IS NOT NULL "
        "OR length(target_text) > 0",
        name="target_present",
    ),
)
Index("ix_relation_snapshot_kind_file", relation.c.snapshot_id, relation.c.kind, relation.c.source_file_id)
Index("ix_relation_target_symbol", relation.c.snapshot_id, relation.c.target_symbol_id, relation.c.kind)
Index("ix_relation_source_symbol", relation.c.snapshot_id, relation.c.source_symbol_id, relation.c.kind)
Index("ix_relation_target_text", relation.c.snapshot_id, relation.c.target_text, relation.c.kind)

embedding_model = Table(
    "embedding_model",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("provider", String(128), nullable=False),
    Column("model_name", String(256), nullable=False),
    Column("model_version", String(128), nullable=False),
    Column("dimension", Integer, nullable=False),
    Column("distance", String(16), nullable=False, server_default="cosine"),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint("dimension > 0", name="positive_dimension"),
    CheckConstraint("distance IN ('cosine', 'l2', 'inner_product')", name="distance"),
    UniqueConstraint(
        "provider",
        "model_name",
        "model_version",
        name="uq_embedding_model_identity",
    ),
)

chunk_embedding = Table(
    "chunk_embedding",
    metadata,
    Column(
        "chunk_id",
        String(64),
        ForeignKey("chunk.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "model_id",
        String(128),
        ForeignKey("embedding_model.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("embedding", Vector(), nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    PrimaryKeyConstraint("chunk_id", "model_id"),
)
Index("ix_chunk_embedding_model", chunk_embedding.c.model_id, chunk_embedding.c.chunk_id)

retrieval_trace = Table(
    "retrieval_trace",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("principal_id", String(256), nullable=False),
    Column("security_domain_id", String(256), nullable=False),
    Column("query_hash", String(64), nullable=False),
    Column("scope", JSONB, nullable=False),
    Column("retriever_versions", JSONB, nullable=False),
    Column("budget", JSONB, nullable=False),
    Column("result_summary", JSONB, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
Index(
    "ix_retrieval_trace_domain_created",
    retrieval_trace.c.security_domain_id,
    retrieval_trace.c.created_at,
)
