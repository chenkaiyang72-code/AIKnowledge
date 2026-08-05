from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aikb.catalog import Catalog
from aikb.ingestion import stable_id
from aikb.retrieval import HybridHit, RetrievalResult, retrieve_hybrid
from aikb.storage import ReadCatalog


SOLUTION_MANIFEST_SCHEMA = "urn:aiknowledge:schema:solution-manifest:v1"
SOLUTION_RRF_K = 60


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SolutionMemberManifest(StrictModel):
    repository: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    required: bool = True


class SolutionManifest(StrictModel):
    schema_uri: Literal["urn:aiknowledge:schema:solution-manifest:v1"] = (
        SOLUTION_MANIFEST_SCHEMA
    )
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    description: str = ""
    members: list[SolutionMemberManifest] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_member_identity(self) -> SolutionManifest:
        repositories = [member.repository for member in self.members]
        roles = [member.role for member in self.members]
        snapshots = [member.snapshot_id for member in self.members]
        if len(set(repositories)) != len(repositories):
            raise ValueError("solution members must use unique repositories")
        if len(set(roles)) != len(roles):
            raise ValueError("solution members must use unique roles")
        if len(set(snapshots)) != len(snapshots):
            raise ValueError("solution members must use unique snapshots")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SolutionPublishResult:
    solution: str
    solution_snapshot_id: str
    revision: str
    manifest_digest: str
    state: str
    member_count: int
    idempotent: bool
    reactivated: bool
    superseded_solution_snapshot_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "solution": self.solution,
            "solution_snapshot_id": (
                None if self.partial_visibility else self.solution_snapshot_id
            ),
            "revision": self.revision,
            "manifest_digest": None if self.partial_visibility else self.manifest_digest,
            "state": self.state,
            "member_count": self.member_count,
            "idempotent": self.idempotent,
            "reactivated": self.reactivated,
            "superseded_solution_snapshot_ids": list(
                self.superseded_solution_snapshot_ids
            ),
        }


@dataclass(frozen=True)
class ResolvedSolutionScope:
    solution: str
    solution_snapshot_id: str
    revision: str
    manifest_digest: str
    state: str
    snapshots: tuple[dict[str, Any], ...]
    partial_visibility: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "solution": self.solution,
            "solution_snapshot_id": self.solution_snapshot_id,
            "revision": self.revision,
            "manifest_digest": self.manifest_digest,
            "state": self.state,
            "snapshots": [dict(row) for row in self.snapshots],
            "partial_visibility": self.partial_visibility,
        }


def load_solution_manifest(path: Path) -> SolutionManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"solution manifest must be valid JSON: {error}") from error
    return SolutionManifest.model_validate(payload)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _publish_result(
    manifest: SolutionManifest,
    solution_snapshot_id: str,
    manifest_digest: str,
    *,
    idempotent: bool,
    reactivated: bool,
    superseded: tuple[str, ...],
) -> SolutionPublishResult:
    return SolutionPublishResult(
        solution=manifest.name,
        solution_snapshot_id=solution_snapshot_id,
        revision=manifest.revision,
        manifest_digest=manifest_digest,
        state="active",
        member_count=len(manifest.members),
        idempotent=idempotent,
        reactivated=reactivated,
        superseded_solution_snapshot_ids=superseded,
    )


def publish_solution_snapshot(
    catalog: Catalog,
    manifest: SolutionManifest,
) -> SolutionPublishResult:
    """Validate and atomically activate an immutable multi-repository version set."""

    manifest_digest = manifest.digest()
    solution_id = stable_id("solution", manifest.name)
    solution_snapshot_id = stable_id(
        "solution_snapshot",
        solution_id,
        manifest.revision,
        manifest_digest,
    )
    now = _now()
    member_rows: list[dict[str, Any]] = []
    for ordinal, member in enumerate(manifest.members):
        row = catalog.connection.execute(
            """
            SELECT r.id AS repository_id, r.name AS repository,
                   s.id AS snapshot_id, s.state
            FROM snapshot AS s
            JOIN repository AS r ON r.id = s.repository_id
            WHERE s.id = ? AND r.name = ?
            """,
            (member.snapshot_id, member.repository),
        ).fetchone()
        if row is None:
            raise ValueError(
                "solution member does not resolve to the requested repository "
                f"and snapshot: {member.repository}@{member.snapshot_id}"
            )
        if row["state"] not in {"active", "superseded"}:
            raise ValueError(
                f"solution member snapshot is not published: {member.snapshot_id}"
            )
        member_rows.append(
            {
                **dict(row),
                "role": member.role,
                "ordinal": ordinal,
                "required": int(member.required),
            }
        )

    with catalog.connection:
        catalog.connection.execute(
            """
            INSERT INTO solution(id, name, description, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET description = excluded.description
            """,
            (solution_id, manifest.name, manifest.description, now),
        )
        stored_solution = catalog.connection.execute(
            "SELECT id FROM solution WHERE name = ?", (manifest.name,)
        ).fetchone()
        if stored_solution is None or stored_solution["id"] != solution_id:
            raise RuntimeError("solution identity conflicts with the stored catalog")

        existing = catalog.connection.execute(
            "SELECT state, member_count, manifest_json FROM solution_snapshot WHERE id = ?",
            (solution_snapshot_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["member_count"] != len(member_rows)
                or existing["manifest_json"] != manifest.canonical_json()
            ):
                raise RuntimeError("solution snapshot identity conflicts with stored data")
            if existing["state"] == "active":
                return _publish_result(
                    manifest,
                    solution_snapshot_id,
                    manifest_digest,
                    idempotent=True,
                    reactivated=False,
                    superseded=(),
                )
            if existing["state"] != "superseded":
                raise RuntimeError(
                    "solution snapshot exists in incomplete state "
                    f"{existing['state']}; manual repair required"
                )
            superseded_rows = catalog.connection.execute(
                """
                SELECT id FROM solution_snapshot
                WHERE solution_id = ? AND state = 'active' AND id <> ?
                ORDER BY id
                """,
                (solution_id, solution_snapshot_id),
            ).fetchall()
            superseded = tuple(row["id"] for row in superseded_rows)
            catalog.connection.execute(
                "UPDATE solution_snapshot SET state = 'superseded' "
                "WHERE solution_id = ? AND state = 'active' AND id <> ?",
                (solution_id, solution_snapshot_id),
            )
            for previous_id in superseded:
                catalog.connection.execute(
                    "INSERT INTO solution_snapshot_event(solution_snapshot_id, state, recorded_at) "
                    "VALUES (?, 'superseded', ?)",
                    (previous_id, now),
                )
            catalog.connection.execute(
                "UPDATE solution_snapshot SET state = 'active', activated_at = ? WHERE id = ?",
                (now, solution_snapshot_id),
            )
            catalog.connection.execute(
                "INSERT INTO solution_snapshot_event(solution_snapshot_id, state, recorded_at) "
                "VALUES (?, 'active', ?)",
                (solution_snapshot_id, now),
            )
            return _publish_result(
                manifest,
                solution_snapshot_id,
                manifest_digest,
                idempotent=True,
                reactivated=True,
                superseded=superseded,
            )

        catalog.connection.execute(
            """
            INSERT INTO solution_snapshot(
                id, solution_id, revision, manifest_digest, manifest_json,
                state, member_count, created_at)
            VALUES (?, ?, ?, ?, ?, 'building', ?, ?)
            """,
            (
                solution_snapshot_id,
                solution_id,
                manifest.revision,
                manifest_digest,
                manifest.canonical_json(),
                len(member_rows),
                now,
            ),
        )
        catalog.connection.execute(
            "INSERT INTO solution_snapshot_event(solution_snapshot_id, state, recorded_at) "
            "VALUES (?, 'building', ?)",
            (solution_snapshot_id, now),
        )
        catalog.connection.executemany(
            """
            INSERT INTO solution_snapshot_member(
                solution_snapshot_id, repository_id, snapshot_id,
                role, ordinal, required)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    solution_snapshot_id,
                    row["repository_id"],
                    row["snapshot_id"],
                    row["role"],
                    row["ordinal"],
                    row["required"],
                )
                for row in member_rows
            ],
        )
        stored_count = catalog.connection.execute(
            "SELECT COUNT(*) AS count FROM solution_snapshot_member "
            "WHERE solution_snapshot_id = ?",
            (solution_snapshot_id,),
        ).fetchone()["count"]
        if stored_count != len(member_rows):
            raise RuntimeError("solution snapshot member validation failed")
        catalog.connection.execute(
            "UPDATE solution_snapshot SET state = 'validated' WHERE id = ?",
            (solution_snapshot_id,),
        )
        catalog.connection.execute(
            "INSERT INTO solution_snapshot_event(solution_snapshot_id, state, recorded_at) "
            "VALUES (?, 'validated', ?)",
            (solution_snapshot_id, now),
        )
        superseded_rows = catalog.connection.execute(
            """
            SELECT id FROM solution_snapshot
            WHERE solution_id = ? AND state = 'active' AND id <> ?
            ORDER BY id
            """,
            (solution_id, solution_snapshot_id),
        ).fetchall()
        superseded = tuple(row["id"] for row in superseded_rows)
        catalog.connection.execute(
            "UPDATE solution_snapshot SET state = 'superseded' "
            "WHERE solution_id = ? AND state = 'active' AND id <> ?",
            (solution_id, solution_snapshot_id),
        )
        for previous_id in superseded:
            catalog.connection.execute(
                "INSERT INTO solution_snapshot_event(solution_snapshot_id, state, recorded_at) "
                "VALUES (?, 'superseded', ?)",
                (previous_id, now),
            )
        catalog.connection.execute(
            "UPDATE solution_snapshot SET state = 'active', activated_at = ? WHERE id = ?",
            (now, solution_snapshot_id),
        )
        catalog.connection.execute(
            "INSERT INTO solution_snapshot_event(solution_snapshot_id, state, recorded_at) "
            "VALUES (?, 'active', ?)",
            (solution_snapshot_id, now),
        )

    return _publish_result(
        manifest,
        solution_snapshot_id,
        manifest_digest,
        idempotent=False,
        reactivated=False,
        superseded=superseded,
    )


def resolve_solution_scope(
    catalog: Catalog,
    solution: str,
    solution_snapshot_id: str | None = None,
    allowed_repositories: set[str] | None = None,
) -> ResolvedSolutionScope:
    predicates = ["sol.name = ?"]
    parameters: list[Any] = [solution]
    if solution_snapshot_id:
        predicates.append("ss.id = ?")
        parameters.append(solution_snapshot_id)
    else:
        predicates.append("ss.state = 'active'")
    snapshot_row = catalog.connection.execute(
        f"""
        SELECT ss.id, ss.revision, ss.manifest_digest, ss.state
        FROM solution_snapshot AS ss
        JOIN solution AS sol ON sol.id = ss.solution_id
        WHERE {' AND '.join(predicates)}
        """,
        parameters,
    ).fetchone()
    if snapshot_row is None:
        if solution_snapshot_id:
            raise ValueError("requested solution snapshot was not found")
        raise ValueError("solution has no active snapshot")

    member_rows = catalog.connection.execute(
        """
        SELECT r.name AS repository, s.id AS snapshot_id, s.revision,
               s.source_digest, s.manifest_digest, s.index_profile_digest,
               s.state, member.role, member.ordinal, member.required
        FROM solution_snapshot_member AS member
        JOIN repository AS r ON r.id = member.repository_id
        JOIN snapshot AS s ON s.id = member.snapshot_id
        WHERE member.solution_snapshot_id = ?
        ORDER BY member.ordinal
        """,
        (snapshot_row["id"],),
    ).fetchall()
    visible_rows = [
        dict(row)
        for row in member_rows
        if allowed_repositories is None or row["repository"] in allowed_repositories
    ]
    if not visible_rows:
        raise ValueError("solution has no repositories visible to this principal")
    return ResolvedSolutionScope(
        solution=solution,
        solution_snapshot_id=snapshot_row["id"],
        revision=snapshot_row["revision"],
        manifest_digest=snapshot_row["manifest_digest"],
        state=snapshot_row["state"],
        snapshots=tuple(visible_rows),
        partial_visibility=len(visible_rows) != len(member_rows),
    )


def retrieve_solution_hybrid(
    catalog: ReadCatalog,
    query: str,
    scope: ResolvedSolutionScope,
    top_k: int = 20,
) -> RetrievalResult:
    """Retrieve each visible pinned member, then merge repository ranks fairly."""

    normalized_query = " ".join(query.split())
    if not normalized_query:
        raise ValueError("query must not be empty")
    if top_k < 1 or top_k > 100:
        raise ValueError("top-k must be between 1 and 100")

    routed_results: list[tuple[int, RetrievalResult]] = []
    channel_counts: dict[str, int] = {}
    identifiers: list[str] = []
    for ordinal, snapshot in enumerate(scope.snapshots):
        result = retrieve_hybrid(
            catalog=catalog,
            query=normalized_query,
            top_k=top_k,
            repository=snapshot["repository"],
            snapshot_id=snapshot["snapshot_id"],
        )
        routed_results.append((ordinal, result))
        for identifier in result.identifier_terms:
            if identifier not in identifiers:
                identifiers.append(identifier)
        for channel, count in result.channel_candidate_counts.items():
            channel_counts[channel] = channel_counts.get(channel, 0) + count

    ranked: list[tuple[float, int, float, HybridHit]] = []
    for ordinal, result in routed_results:
        for repository_rank, item in enumerate(result.hits, start=1):
            repository_rrf_score = 1.0 / (SOLUTION_RRF_K + repository_rank)
            ranked.append(
                (repository_rrf_score, ordinal, item.fused_score, item)
            )
    ranked.sort(
        key=lambda item: (
            -item[0],
            item[1],
            -item[2],
            item[3].hit.path,
            item[3].hit.start_line,
            item[3].hit.chunk_id,
        )
    )
    hits = tuple(
        HybridHit(
            hit=item.hit,
            fused_score=repository_rrf_score,
            contributions=item.contributions,
        )
        for repository_rrf_score, _ordinal, _local_score, item in ranked[:top_k]
    )
    return RetrievalResult(
        query=normalized_query,
        identifier_terms=tuple(identifiers),
        hits=hits,
        channel_candidate_counts=channel_counts,  # type: ignore[arg-type]
        rrf_k=SOLUTION_RRF_K,
    )
