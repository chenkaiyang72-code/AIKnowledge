from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from aikb.analysis_artifacts import (
    ANALYSIS_ARTIFACT_SCHEMA_VERSION,
    decode_analysis_artifact,
    encode_analysis_artifact,
)
from aikb.catalog import Catalog
from aikb.evaluation import inspect_source
from aikb.source_relations import (
    AMBIGUOUS_CANDIDATE,
    SOURCE_INFERRED,
    extract_dependency_references,
    extract_source_facts,
    include_candidates,
)
from aikb.structured_chunks import (
    FALLBACK_GENERATOR,
    TREE_SITTER_GENERATOR,
    build_chunks,
    tree_sitter_versions,
)


INGEST_GENERATOR = "tree-sitter-c-v3+source-relations-v2+line-window-v1"
DEFAULT_CHUNK_LINES = 120
DEFAULT_CHUNK_OVERLAP = 20
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_DEPENDENCY_MAX_FILES = 500
DEFAULT_DEPENDENCY_MAX_CANDIDATES = 8


LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".h": "c-header",
    ".s": "assembly",
    ".rs": "rust",
    ".py": "python",
    ".md": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


@dataclass(frozen=True)
class FileCandidate:
    absolute_path: Path
    relative_path: str
    blob_id: str
    language: str
    line_count: int
    size_bytes: int
    decode_status: str


@dataclass(frozen=True)
class ScanResult:
    files: list[FileCandidate]
    manifest_digest: str
    skipped_file_count: int
    byte_count: int


@dataclass(frozen=True)
class DependencyExpansionResult:
    scan: ScanResult
    seed_file_count: int
    dependency_file_count: int
    unresolved_reference_count: int
    ambiguous_reference_count: int
    truncated: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def load_scope(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        scope = json.load(stream)
    if not isinstance(scope, dict):
        raise ValueError("scope must contain a JSON object")
    return scope


def detect_language(path: Path) -> str | None:
    name = path.name
    lowered = name.lower()
    if name in {"Makefile", "Kbuild", "Kconfig"} or lowered.startswith(
        ("makefile.", "kbuild.", "kconfig.")
    ):
        return "kbuild" if "kbuild" in lowered or "makefile" in lowered else "kconfig"
    if path.suffix == ".S":
        return "assembly-preprocessed"
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def is_excluded(relative_path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in patterns)


def iter_source_files(
    source: Path,
    include_roots: Iterable[str],
    exclude_globs: Iterable[str],
) -> Iterator[tuple[Path, str]]:
    source = source.resolve()
    seen: set[str] = set()
    for root_text in sorted(set(include_roots)):
        root = (source / root_text).resolve()
        try:
            root.relative_to(source)
        except ValueError as error:
            raise ValueError(f"include root escapes source tree: {root_text}") from error
        if not root.exists():
            continue
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            for directory, directories, filenames in os.walk(root, followlinks=False):
                directory_path = Path(directory)
                directories[:] = sorted(
                    item
                    for item in directories
                    if not (directory_path / item).is_symlink()
                    and not is_excluded(
                        (directory_path / item).relative_to(source).as_posix() + "/",
                        exclude_globs,
                    )
                )
                candidates.extend(directory_path / item for item in sorted(filenames))
        for path in candidates:
            if path.is_symlink() or not path.is_file():
                continue
            relative_path = path.relative_to(source).as_posix()
            if relative_path in seen or is_excluded(relative_path, exclude_globs):
                continue
            seen.add(relative_path)
            yield path, relative_path


def decode_source(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8"), "utf8"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "replacement"


def _candidate_from_path(
    source: Path,
    absolute_path: Path,
    relative_path: str,
    max_file_bytes: int,
) -> FileCandidate | None:
    language = detect_language(absolute_path)
    if language is None:
        return None
    size_bytes = absolute_path.stat().st_size
    if size_bytes > max_file_bytes:
        return None
    data = absolute_path.read_bytes()
    if b"\x00" in data[:8192]:
        return None
    text, decode_status = decode_source(data)
    return FileCandidate(
        absolute_path=absolute_path,
        relative_path=relative_path,
        blob_id=hashlib.sha256(data).hexdigest(),
        language=language,
        line_count=len(text.splitlines()),
        size_bytes=len(data),
        decode_status=decode_status,
    )


def _scan_result_from_files(
    files: Iterable[FileCandidate], skipped_file_count: int
) -> ScanResult:
    ordered = sorted(files, key=lambda item: item.relative_path)
    if not ordered:
        raise ValueError("source scan found no supported text files")
    manifest = hashlib.sha256()
    byte_count = 0
    for candidate in ordered:
        byte_count += candidate.size_bytes
        manifest.update(candidate.relative_path.encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(candidate.blob_id.encode("ascii"))
        manifest.update(b"\0")
        manifest.update(str(candidate.size_bytes).encode("ascii"))
        manifest.update(b"\n")
    return ScanResult(
        ordered,
        manifest.hexdigest(),
        skipped_file_count,
        byte_count,
    )


def scan_source(
    source: Path,
    include_roots: Iterable[str],
    exclude_globs: Iterable[str],
    max_file_bytes: int,
    max_files: int | None = None,
) -> ScanResult:
    if max_file_bytes < 1:
        raise ValueError("max-file-bytes must be positive")
    if max_files is not None and max_files < 1:
        raise ValueError("max-files must be positive")

    files: list[FileCandidate] = []
    skipped = 0
    for absolute_path, relative_path in iter_source_files(
        source, include_roots, exclude_globs
    ):
        candidate = _candidate_from_path(
            source, absolute_path, relative_path, max_file_bytes
        )
        if candidate is None:
            skipped += 1
            continue
        files.append(candidate)
        if max_files is not None and len(files) >= max_files:
            break
    return _scan_result_from_files(files, skipped)


def _discover_dependency_paths(
    source: Path,
    exclude_globs: Iterable[str],
    max_file_bytes: int,
) -> tuple[set[str], dict[str, list[str]]]:
    available: set[str] = set()
    by_basename: dict[str, list[str]] = {}
    for absolute_path, relative_path in iter_source_files(
        source, ["."], exclude_globs
    ):
        if detect_language(absolute_path) is None:
            continue
        if absolute_path.stat().st_size > max_file_bytes:
            continue
        available.add(relative_path)
        by_basename.setdefault(posixpath.basename(relative_path), []).append(
            relative_path
        )
    for paths in by_basename.values():
        paths.sort()
    return available, by_basename


def _dependency_variants(kind: str, target: str) -> list[str]:
    target = target.strip().replace("\\", "/")
    if not target or "$" in target or "%" in target:
        return []
    if kind != "kbuild_target":
        return [target]
    if target.endswith(".o"):
        stem = target[:-2]
        return [f"{stem}.c", f"{stem}.S", f"{stem}.s", f"{stem}.rs"]
    if target.endswith("/"):
        return [
            f"{target}Makefile",
            f"{target}Kbuild",
            f"{target}Kconfig",
        ]
    return [target]


def _resolve_dependency_paths(
    source_path: str,
    kind: str,
    target: str,
    available: set[str],
    by_basename: dict[str, list[str]],
) -> list[str]:
    variants = _dependency_variants(kind, target)
    if not variants:
        return []
    source_directory = posixpath.dirname(source_path)
    direct: set[str] = set()
    for variant in variants:
        for candidate in (
            posixpath.normpath(posixpath.join(source_directory, variant)),
            posixpath.normpath(variant),
            posixpath.normpath(posixpath.join("include", variant)),
        ):
            if candidate in available:
                direct.add(candidate)
    if direct:
        return sorted(direct)

    suffix_matches: set[str] = set()
    for variant in variants:
        normalized = posixpath.normpath(variant)
        basename = posixpath.basename(normalized)
        suffix = f"/{normalized}"
        for candidate in by_basename.get(basename, []):
            if candidate == normalized or candidate.endswith(suffix):
                suffix_matches.add(candidate)
    return sorted(suffix_matches)


def expand_scan_dependencies(
    source: Path,
    seed_scan: ScanResult,
    exclude_globs: Iterable[str],
    max_file_bytes: int,
    depth: int,
    max_dependency_files: int,
    max_candidates_per_reference: int = DEFAULT_DEPENDENCY_MAX_CANDIDATES,
) -> DependencyExpansionResult:
    if depth < 0:
        raise ValueError("dependency-depth must not be negative")
    if max_dependency_files < 1:
        raise ValueError("dependency-max-files must be positive")
    if max_candidates_per_reference < 1:
        raise ValueError("dependency-max-candidates must be positive")

    selected = {item.relative_path: item for item in seed_scan.files}
    seed_file_count = len(selected)
    if depth == 0:
        return DependencyExpansionResult(
            scan=seed_scan,
            seed_file_count=seed_file_count,
            dependency_file_count=0,
            unresolved_reference_count=0,
            ambiguous_reference_count=0,
            truncated=False,
        )

    available, by_basename = _discover_dependency_paths(
        source, exclude_globs, max_file_bytes
    )
    frontier = list(seed_scan.files)
    added_count = 0
    skipped_count = seed_scan.skipped_file_count
    unresolved_count = 0
    ambiguous_count = 0
    truncated = False

    for _level in range(depth):
        next_frontier: list[FileCandidate] = []
        stop = False
        for source_candidate in sorted(
            frontier, key=lambda item: item.relative_path
        ):
            data = source_candidate.absolute_path.read_bytes()
            references = extract_dependency_references(
                data, source_candidate.language
            )
            for reference in references:
                candidates = _resolve_dependency_paths(
                    source_candidate.relative_path,
                    reference.kind,
                    reference.target,
                    available,
                    by_basename,
                )
                if not candidates:
                    unresolved_count += 1
                    continue
                if len(candidates) > 1:
                    ambiguous_count += 1
                if len(candidates) > max_candidates_per_reference:
                    unresolved_count += 1
                    truncated = True
                    continue
                for relative_path in candidates:
                    if relative_path in selected:
                        continue
                    if added_count >= max_dependency_files:
                        truncated = True
                        stop = True
                        break
                    absolute_path = source / Path(relative_path)
                    candidate = _candidate_from_path(
                        source,
                        absolute_path,
                        relative_path,
                        max_file_bytes,
                    )
                    if candidate is None:
                        skipped_count += 1
                        unresolved_count += 1
                        continue
                    selected[relative_path] = candidate
                    next_frontier.append(candidate)
                    added_count += 1
                if stop:
                    break
            if stop:
                break
        if stop or not next_frontier:
            break
        frontier = next_frontier

    expanded_scan = _scan_result_from_files(selected.values(), skipped_count)
    return DependencyExpansionResult(
        scan=expanded_scan,
        seed_file_count=seed_file_count,
        dependency_file_count=len(selected) - seed_file_count,
        unresolved_reference_count=unresolved_count,
        ambiguous_reference_count=ambiguous_count,
        truncated=truncated,
    )


def ingest_source(
    catalog: Catalog,
    scope: dict[str, Any],
    source: Path,
    archive: Path | None = None,
    include_roots: Iterable[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    dependency_depth: int | None = None,
    dependency_max_files: int | None = None,
    dependency_max_candidates: int | None = None,
) -> dict[str, Any]:
    source = source.resolve()
    inspect_source(scope, source, archive)
    source_definition = scope["source"]
    roots = list(include_roots or scope.get("include_roots", []))
    if not roots:
        roots = ["."]
    excludes = list(scope.get("exclude_globs", []))
    seed_scan = scan_source(source, roots, excludes, max_file_bytes, max_files)
    dependency_config = scope.get("dependency_expansion", {})
    resolved_dependency_depth = (
        dependency_depth
        if dependency_depth is not None
        else int(dependency_config.get("depth", 0))
    )
    resolved_dependency_max_files = (
        dependency_max_files
        if dependency_max_files is not None
        else int(
            dependency_config.get(
                "max_files", DEFAULT_DEPENDENCY_MAX_FILES
            )
        )
    )
    resolved_dependency_max_candidates = (
        dependency_max_candidates
        if dependency_max_candidates is not None
        else int(
            dependency_config.get(
                "max_candidates_per_reference",
                DEFAULT_DEPENDENCY_MAX_CANDIDATES,
            )
        )
    )
    dependency_expansion = expand_scan_dependencies(
        source=source,
        seed_scan=seed_scan,
        exclude_globs=excludes,
        max_file_bytes=max_file_bytes,
        depth=resolved_dependency_depth,
        max_dependency_files=resolved_dependency_max_files,
        max_candidates_per_reference=resolved_dependency_max_candidates,
    )
    scan = dependency_expansion.scan
    project = source_definition["project"]
    source_kind = source_definition["kind"]
    source_uri = source_definition.get("archive_name") or str(source)
    revision = source_definition.get("git_commit") or f"release:{source_definition['version']}"
    source_digest = source_definition.get("archive_sha256") or scan.manifest_digest
    repository_id = stable_id("repo", project)
    parser_versions = tree_sitter_versions()
    analysis_profile = {
        "generator": INGEST_GENERATOR,
        "artifact_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "parser_versions": parser_versions,
        "chunk_lines": chunk_lines,
        "chunk_overlap": chunk_overlap,
    }
    analysis_profile_json = json.dumps(
        analysis_profile, ensure_ascii=False, sort_keys=True
    )
    analysis_profile_digest = hashlib.sha256(
        analysis_profile_json.encode("utf-8")
    ).hexdigest()
    index_profile = {
        "generator": INGEST_GENERATOR,
        "analysis_profile_digest": analysis_profile_digest,
        "index_policy": {
            "mode": "source_only",
            "execute_build": False,
            "requires_build_artifacts": False,
        },
        "parser_versions": parser_versions,
        "include_roots": roots,
        "exclude_globs": excludes,
        "max_files": max_files,
        "max_file_bytes": max_file_bytes,
        "chunk_lines": chunk_lines,
        "chunk_overlap": chunk_overlap,
        "dependency_depth": resolved_dependency_depth,
        "dependency_max_files": resolved_dependency_max_files,
        "dependency_max_candidates": resolved_dependency_max_candidates,
    }
    profile_json = json.dumps(index_profile, ensure_ascii=False, sort_keys=True)
    profile_digest = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
    snapshot_id = stable_id(
        "snap",
        repository_id,
        revision,
        source_digest,
        scan.manifest_digest,
        profile_digest,
    )
    file_ids_by_path = {
        candidate.relative_path: stable_id(
            "file", snapshot_id, candidate.relative_path
        )
        for candidate in scan.files
    }
    available_paths = set(file_ids_by_path)

    existing = catalog.connection.execute(
        "SELECT state FROM snapshot WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if existing is not None:
        if existing["state"] not in {"active", "superseded"}:
            raise RuntimeError(
                f"snapshot {snapshot_id} exists in unexpected state {existing['state']}"
            )
        reactivated = False
        if existing["state"] == "superseded":
            reactivated_at = utc_now()
            connection = catalog.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                active_rows = connection.execute(
                    "SELECT id FROM snapshot WHERE repository_id = ? AND state = 'active'",
                    (repository_id,),
                ).fetchall()
                for active in active_rows:
                    connection.execute(
                        "UPDATE snapshot SET state = 'superseded' WHERE id = ?",
                        (active["id"],),
                    )
                    connection.execute(
                        """
                        INSERT INTO snapshot_event(snapshot_id, state, recorded_at)
                        VALUES (?, 'superseded', ?)
                        """,
                        (active["id"], reactivated_at),
                    )
                connection.execute(
                    """
                    UPDATE snapshot
                    SET state = 'active', activated_at = ?
                    WHERE id = ? AND state = 'superseded'
                    """,
                    (reactivated_at, snapshot_id),
                )
                connection.execute(
                    """
                    INSERT INTO snapshot_event(snapshot_id, state, recorded_at)
                    VALUES (?, 'active', ?)
                    """,
                    (snapshot_id, reactivated_at),
                )
                connection.commit()
                reactivated = True
            except Exception:
                connection.rollback()
                raise
        row = catalog.connection.execute(
            "SELECT * FROM snapshot WHERE id = ?", (snapshot_id,)
        ).fetchone()
        result = dict(row)
        result.update(
            {
                "repository": project,
                "idempotent": True,
                "reactivated": reactivated,
            }
        )
        return result

    now = utc_now()
    connection = catalog.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO repository(id, name, source_kind, source_uri, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                source_kind = excluded.source_kind,
                source_uri = excluded.source_uri
            """,
            (repository_id, project, source_kind, source_uri, now),
        )
        connection.execute(
            """
            INSERT INTO snapshot(
                id, repository_id, revision, source_digest, manifest_digest,
                index_profile_digest, state, skipped_file_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'building', ?, ?)
            """,
            (
                snapshot_id,
                repository_id,
                revision,
                source_digest,
                scan.manifest_digest,
                profile_digest,
                scan.skipped_file_count,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO snapshot_event(snapshot_id, state, recorded_at) VALUES (?, 'building', ?)",
            (snapshot_id, now),
        )

        chunk_count = 0
        structured_chunk_count = 0
        fallback_chunk_count = 0
        parse_error_count = 0
        symbol_occurrence_count = 0
        source_condition_count = 0
        relation_count = 0
        analysis_cache_hit_count = 0
        analysis_cache_miss_count = 0
        unique_blobs: set[str] = set()
        symbol_ids_by_file_name_kind: dict[tuple[str, str, str], str] = {}
        symbol_ids_by_name_kind: dict[tuple[str, str], set[str]] = {}
        # A full Linux tree can produce millions of relations.  Stage them in
        # SQLite instead of retaining Python objects until every symbol has
        # been discovered.  ``sequence`` preserves deterministic source order.
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS pending_relation_stage(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                condition_id TEXT,
                kind TEXT NOT NULL,
                target_text TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                source_name TEXT,
                source_kind TEXT,
                target_kind TEXT,
                target_path TEXT,
                confidence TEXT NOT NULL,
                generator TEXT NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM pending_relation_stage")
        for candidate in scan.files:
            data = candidate.absolute_path.read_bytes()
            actual_blob_id = hashlib.sha256(data).hexdigest()
            if actual_blob_id != candidate.blob_id:
                raise RuntimeError(
                    f"source changed during ingest: {candidate.relative_path}"
                )
            text, decode_status = decode_source(data)
            if decode_status != candidate.decode_status:
                raise RuntimeError(
                    f"source decoding changed during ingest: {candidate.relative_path}"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO blob(
                    id, algorithm, size_bytes, compression, compressed_content, created_at)
                VALUES (?, 'sha256', ?, 'zlib', ?, ?)
                """,
                (candidate.blob_id, len(data), zlib.compress(data), now),
            )
            unique_blobs.add(candidate.blob_id)
            file_id = file_ids_by_path[candidate.relative_path]
            analysis_artifact_id = stable_id(
                "analysis",
                candidate.blob_id,
                candidate.language,
                analysis_profile_digest,
            )
            cached_analysis = connection.execute(
                """
                SELECT compressed_payload
                FROM analysis_artifact
                WHERE id = ?
                """,
                (analysis_artifact_id,),
            ).fetchone()
            if cached_analysis is None:
                try:
                    parse_outcome = build_chunks(
                        data=data,
                        language=candidate.language,
                        chunk_lines=chunk_lines,
                        overlap=chunk_overlap,
                    )
                    source_facts = extract_source_facts(data, candidate.language)
                except Exception as error:
                    raise RuntimeError(
                        "source analysis failed for "
                        f"{candidate.relative_path}: {error}"
                    ) from error
                artifact_conditions = set(source_facts.conditions)
                artifact_conditions.update(
                    item.condition
                    for item in source_facts.occurrences
                    if item.condition is not None
                )
                artifact_conditions.update(
                    item.condition
                    for item in source_facts.relations
                    if item.condition is not None
                )
                payload = encode_analysis_artifact(parse_outcome, source_facts)
                connection.execute(
                    """
                    INSERT INTO analysis_artifact(
                        id, blob_id, language, analysis_profile_digest,
                        schema_version, compression, compressed_payload,
                        chunk_count, symbol_occurrence_count, relation_count,
                        condition_count, created_at)
                    VALUES (?, ?, ?, ?, ?, 'zlib', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        analysis_artifact_id,
                        candidate.blob_id,
                        candidate.language,
                        analysis_profile_digest,
                        ANALYSIS_ARTIFACT_SCHEMA_VERSION,
                        zlib.compress(payload),
                        len(parse_outcome.chunks),
                        len(source_facts.occurrences),
                        len(source_facts.relations),
                        len(artifact_conditions),
                        now,
                    ),
                )
                analysis_cache_miss_count += 1
            else:
                try:
                    parse_outcome, source_facts = decode_analysis_artifact(
                        zlib.decompress(cached_analysis["compressed_payload"])
                    )
                except Exception as error:
                    raise RuntimeError(
                        "cached source analysis failed for "
                        f"{candidate.relative_path}: {error}"
                    ) from error
                analysis_cache_hit_count += 1
            parse_error_count += parse_outcome.syntax_error_count
            connection.execute(
                """
                INSERT INTO source_file(
                    id, snapshot_id, path, blob_id, language, line_count,
                    size_bytes, decode_status, parse_status, syntax_error_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    snapshot_id,
                    candidate.relative_path,
                    candidate.blob_id,
                    candidate.language,
                    candidate.line_count,
                    candidate.size_bytes,
                    candidate.decode_status,
                    parse_outcome.parse_status,
                    parse_outcome.syntax_error_count,
                ),
            )
            fact_conditions = set(source_facts.conditions)
            fact_conditions.update(
                item.condition
                for item in source_facts.occurrences
                if item.condition is not None
            )
            fact_conditions.update(
                item.condition
                for item in source_facts.relations
                if item.condition is not None
            )
            condition_ids: dict[object, str] = {}
            for condition in sorted(
                fact_conditions,
                key=lambda item: (
                    item.start_line,
                    item.end_line,
                    item.depth,
                    item.expression,
                ),
            ):
                condition_id = stable_id(
                    "condition",
                    file_id,
                    condition.expression,
                    str(condition.start_line),
                    str(condition.end_line),
                    str(condition.depth),
                )
                connection.execute(
                    """
                    INSERT INTO source_condition(
                        id, snapshot_id, file_id, expression, start_line,
                        end_line, depth, generator)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        condition_id,
                        snapshot_id,
                        file_id,
                        condition.expression,
                        condition.start_line,
                        condition.end_line,
                        condition.depth,
                        condition.generator,
                    ),
                )
                condition_ids[condition] = condition_id
                source_condition_count += 1

            for occurrence in source_facts.occurrences:
                if occurrence.namespace_scope == "repository":
                    namespace = "repository"
                elif occurrence.namespace_scope == "file":
                    namespace = f"file:{candidate.relative_path}"
                else:
                    namespace = (
                        f"file:{candidate.relative_path}:"
                        f"{occurrence.namespace_scope}"
                    )
                symbol_id = stable_id(
                    "symbol",
                    repository_id,
                    candidate.language,
                    occurrence.kind,
                    namespace,
                    occurrence.name,
                )
                connection.execute(
                    """
                    INSERT INTO logical_symbol(
                        id, repository_id, language, kind, namespace,
                        name, signature, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repository_id, language, kind, namespace, name)
                    DO UPDATE SET signature = COALESCE(
                        logical_symbol.signature, excluded.signature
                    )
                    """,
                    (
                        symbol_id,
                        repository_id,
                        candidate.language,
                        occurrence.kind,
                        namespace,
                        occurrence.name,
                        occurrence.signature,
                        now,
                    ),
                )
                occurrence_id = stable_id(
                    "occurrence",
                    file_id,
                    symbol_id,
                    occurrence.role,
                    str(occurrence.start_line),
                    str(occurrence.end_line),
                )
                occurrence_cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO symbol_occurrence(
                        id, snapshot_id, file_id, logical_symbol_id,
                        condition_id, role, start_line, end_line,
                        confidence, generator)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        occurrence_id,
                        snapshot_id,
                        file_id,
                        symbol_id,
                        condition_ids.get(occurrence.condition),
                        occurrence.role,
                        occurrence.start_line,
                        occurrence.end_line,
                        occurrence.confidence,
                        occurrence.generator,
                    ),
                )
                symbol_ids_by_file_name_kind[
                    (candidate.relative_path, occurrence.name, occurrence.kind)
                ] = symbol_id
                symbol_ids_by_name_kind.setdefault(
                    (occurrence.name, occurrence.kind), set()
                ).add(symbol_id)
                symbol_occurrence_count += occurrence_cursor.rowcount

            connection.executemany(
                """
                INSERT INTO pending_relation_stage(
                    file_id, relative_path, condition_id, kind, target_text,
                    start_line, end_line, source_name, source_kind,
                    target_kind, target_path, confidence, generator)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        file_id,
                        candidate.relative_path,
                        condition_ids.get(relation.condition),
                        relation.kind,
                        relation.target_text,
                        relation.start_line,
                        relation.end_line,
                        relation.source_name,
                        relation.source_kind,
                        relation.target_kind,
                        relation.target_path,
                        relation.confidence,
                        relation.generator,
                    )
                    for relation in source_facts.relations
                )
            )
            for ordinal, code_chunk in enumerate(parse_outcome.chunks):
                content = code_chunk.content
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                chunk_id = stable_id(
                    "chunk",
                    file_id,
                    str(ordinal),
                    str(code_chunk.start_line),
                    str(code_chunk.end_line),
                    content_hash,
                )
                connection.execute(
                    """
                    INSERT INTO chunk(
                        id, snapshot_id, file_id, ordinal, kind, start_line,
                        end_line, symbol, content_hash, generator)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        snapshot_id,
                        file_id,
                        ordinal,
                        code_chunk.kind,
                        code_chunk.start_line,
                        code_chunk.end_line,
                        code_chunk.symbol,
                        content_hash,
                        code_chunk.generator,
                    ),
                )
                connection.execute(
                    "INSERT INTO chunk_fts(chunk_id, content) VALUES (?, ?)",
                    (chunk_id, content),
                )
                chunk_count += 1
                if code_chunk.generator == TREE_SITTER_GENERATOR:
                    structured_chunk_count += 1
                elif code_chunk.generator == FALLBACK_GENERATOR:
                    fallback_chunk_count += 1

        staged_relations = connection.execute(
            "SELECT * FROM pending_relation_stage ORDER BY sequence"
        )
        for pending in staged_relations:
            source_symbol_id: str | None = None
            if pending["source_name"] and pending["source_kind"]:
                source_symbol_id = symbol_ids_by_file_name_kind.get(
                    (
                        pending["relative_path"],
                        pending["source_name"],
                        pending["source_kind"],
                    )
                )
                if source_symbol_id is None:
                    source_candidates = symbol_ids_by_name_kind.get(
                        (pending["source_name"], pending["source_kind"]), set()
                    )
                    if len(source_candidates) == 1:
                        source_symbol_id = next(iter(source_candidates))

            target_symbol_id: str | None = None
            target_file_id: str | None = None
            confidence = pending["confidence"]
            if pending["target_kind"]:
                same_file_target = symbol_ids_by_file_name_kind.get(
                    (
                        pending["relative_path"],
                        pending["target_text"],
                        pending["target_kind"],
                    )
                )
                if same_file_target is not None:
                    target_symbol_id = same_file_target
                else:
                    target_candidates = symbol_ids_by_name_kind.get(
                        (pending["target_text"], pending["target_kind"]), set()
                    )
                    if len(target_candidates) == 1:
                        target_symbol_id = next(iter(target_candidates))
                    elif pending["kind"] == "calls" or len(target_candidates) > 1:
                        confidence = AMBIGUOUS_CANDIDATE

            if pending["target_path"] and "$" not in pending["target_path"]:
                target_path = pending["target_path"]
                if pending["kind"] == "kbuild_contains" and target_path.endswith(".o"):
                    target_path = target_path[:-2] + ".c"
                path_candidates = include_candidates(
                    pending["relative_path"], target_path, available_paths
                )
                if len(path_candidates) == 1:
                    target_file_id = file_ids_by_path[path_candidates[0]]
                    if confidence != AMBIGUOUS_CANDIDATE:
                        confidence = min(
                            (confidence, SOURCE_INFERRED),
                            key=(
                                "source_exact",
                                "source_inferred",
                                "ambiguous_candidate",
                            ).index,
                        )
                elif len(path_candidates) > 1:
                    confidence = AMBIGUOUS_CANDIDATE

            relation_id = stable_id(
                "relation",
                pending["file_id"],
                pending["kind"],
                pending["target_text"],
                str(pending["start_line"]),
                str(pending["end_line"]),
                source_symbol_id or "",
                target_file_id or "",
                target_symbol_id or "",
            )
            relation_cursor = connection.execute(
                """
                INSERT OR IGNORE INTO relation(
                    id, snapshot_id, source_file_id, source_symbol_id,
                    target_file_id, target_symbol_id, condition_id, kind,
                    target_text, start_line, end_line, confidence, generator)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relation_id,
                    snapshot_id,
                    pending["file_id"],
                    source_symbol_id,
                    target_file_id,
                    target_symbol_id,
                    pending["condition_id"],
                    pending["kind"],
                    pending["target_text"],
                    pending["start_line"],
                    pending["end_line"],
                    confidence,
                    pending["generator"],
                ),
            )
            relation_count += relation_cursor.rowcount

        connection.execute("DROP TABLE pending_relation_stage")

        if chunk_count < 1:
            raise RuntimeError("ingest produced no chunks")
        validated_at = utc_now()
        connection.execute(
            """
            UPDATE snapshot
            SET state = 'validated', file_count = ?, blob_count = ?,
                chunk_count = ?, structured_chunk_count = ?,
                fallback_chunk_count = ?, parse_error_count = ?,
                symbol_occurrence_count = ?, relation_count = ?,
                condition_count = ?, analysis_cache_hit_count = ?,
                analysis_cache_miss_count = ?, seed_file_count = ?,
                dependency_file_count = ?, dependency_unresolved_count = ?,
                dependency_ambiguous_count = ?,
                dependency_expansion_truncated = ?, byte_count = ?
            WHERE id = ? AND state = 'building'
            """,
            (
                len(scan.files),
                len(unique_blobs),
                chunk_count,
                structured_chunk_count,
                fallback_chunk_count,
                parse_error_count,
                symbol_occurrence_count,
                relation_count,
                source_condition_count,
                analysis_cache_hit_count,
                analysis_cache_miss_count,
                dependency_expansion.seed_file_count,
                dependency_expansion.dependency_file_count,
                dependency_expansion.unresolved_reference_count,
                dependency_expansion.ambiguous_reference_count,
                int(dependency_expansion.truncated),
                scan.byte_count,
                snapshot_id,
            ),
        )
        connection.execute(
            "INSERT INTO snapshot_event(snapshot_id, state, recorded_at) VALUES (?, 'validated', ?)",
            (snapshot_id, validated_at),
        )
        superseded = connection.execute(
            "SELECT id FROM snapshot WHERE repository_id = ? AND state = 'active'",
            (repository_id,),
        ).fetchall()
        for old in superseded:
            connection.execute(
                "UPDATE snapshot SET state = 'superseded' WHERE id = ?",
                (old["id"],),
            )
            connection.execute(
                "INSERT INTO snapshot_event(snapshot_id, state, recorded_at) VALUES (?, 'superseded', ?)",
                (old["id"], validated_at),
            )
        connection.execute(
            """
            UPDATE snapshot
            SET state = 'active', activated_at = ?
            WHERE id = ? AND state = 'validated'
            """,
            (validated_at, snapshot_id),
        )
        connection.execute(
            "INSERT INTO snapshot_event(snapshot_id, state, recorded_at) VALUES (?, 'active', ?)",
            (snapshot_id, validated_at),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return {
        "id": snapshot_id,
        "repository": project,
        "revision": revision,
        "source_digest": source_digest,
        "manifest_digest": scan.manifest_digest,
        "index_profile_digest": profile_digest,
        "state": "active",
        "file_count": len(scan.files),
        "blob_count": len(unique_blobs),
        "chunk_count": chunk_count,
        "structured_chunk_count": structured_chunk_count,
        "fallback_chunk_count": fallback_chunk_count,
        "parse_error_count": parse_error_count,
        "symbol_occurrence_count": symbol_occurrence_count,
        "relation_count": relation_count,
        "condition_count": source_condition_count,
        "analysis_cache_hit_count": analysis_cache_hit_count,
        "analysis_cache_miss_count": analysis_cache_miss_count,
        "seed_file_count": dependency_expansion.seed_file_count,
        "dependency_file_count": dependency_expansion.dependency_file_count,
        "dependency_unresolved_count": (
            dependency_expansion.unresolved_reference_count
        ),
        "dependency_ambiguous_count": (
            dependency_expansion.ambiguous_reference_count
        ),
        "dependency_expansion_truncated": dependency_expansion.truncated,
        "byte_count": scan.byte_count,
        "skipped_file_count": scan.skipped_file_count,
        "idempotent": False,
        "reactivated": False,
    }
