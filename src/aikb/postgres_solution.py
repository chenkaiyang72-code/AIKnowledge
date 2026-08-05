from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, text

from aikb.ingestion import stable_id
from aikb.solution import (
    ResolvedSolutionScope,
    SolutionManifest,
    SolutionPublishResult,
)


class PostgresSolutionPublisher:
    """Atomically publish a fixed repository/snapshot combination to the team store."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def publish(self, manifest: SolutionManifest) -> SolutionPublishResult:
        digest = manifest.digest()
        solution_id = stable_id("solution", manifest.name)
        solution_snapshot_id = stable_id(
            "solution_snapshot", solution_id, manifest.revision, digest
        )
        canonical_manifest = manifest.canonical_json()
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
                {"name": f"solution:{manifest.name}"},
            )
            members: list[dict[str, Any]] = []
            for ordinal, member in enumerate(manifest.members):
                row = connection.execute(
                    text(
                        "SELECT r.id AS repository_id, s.id AS snapshot_id, s.state "
                        "FROM snapshot s JOIN repository r ON r.id=s.repository_id "
                        "WHERE s.id=:snapshot_id AND r.name=:repository"
                    ),
                    {
                        "snapshot_id": member.snapshot_id,
                        "repository": member.repository,
                    },
                ).mappings().first()
                if row is None:
                    raise ValueError(
                        "solution member is not published in PostgreSQL: "
                        f"{member.repository}@{member.snapshot_id}"
                    )
                if row["state"] not in {"active", "superseded"}:
                    raise ValueError(
                        f"solution member snapshot is incomplete: {member.snapshot_id}"
                    )
                members.append(
                    {
                        **dict(row),
                        "role": member.role,
                        "ordinal": ordinal,
                        "required": member.required,
                    }
                )

            connection.execute(
                text(
                    "INSERT INTO solution(id,name,description) "
                    "VALUES (:id,:name,:description) "
                    "ON CONFLICT (name) DO UPDATE SET description=excluded.description"
                ),
                {
                    "id": solution_id,
                    "name": manifest.name,
                    "description": manifest.description,
                },
            )
            stored_solution_id = connection.execute(
                text("SELECT id FROM solution WHERE name=:name"),
                {"name": manifest.name},
            ).scalar_one()
            if stored_solution_id != solution_id:
                raise RuntimeError("solution identity conflicts with PostgreSQL")

            existing = connection.execute(
                text(
                    "SELECT state,member_count,manifest_json "
                    "FROM solution_snapshot WHERE id=:id FOR UPDATE"
                ),
                {"id": solution_snapshot_id},
            ).mappings().first()
            if existing is not None:
                stored_manifest = json.dumps(
                    existing["manifest_json"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if (
                    existing["member_count"] != len(members)
                    or stored_manifest != canonical_manifest
                ):
                    raise RuntimeError(
                        "solution snapshot identity conflicts with PostgreSQL"
                    )
                if existing["state"] == "active":
                    return self._result(
                        manifest,
                        solution_snapshot_id,
                        digest,
                        idempotent=True,
                        reactivated=False,
                        superseded=(),
                    )
                if existing["state"] != "superseded":
                    raise RuntimeError(
                        "solution snapshot exists in incomplete PostgreSQL state "
                        f"{existing['state']}"
                    )
                superseded = self._activate(
                    connection, solution_id, solution_snapshot_id
                )
                return self._result(
                    manifest,
                    solution_snapshot_id,
                    digest,
                    idempotent=True,
                    reactivated=True,
                    superseded=superseded,
                )

            connection.execute(
                text(
                    "INSERT INTO solution_snapshot(" 
                    "id,solution_id,revision,manifest_digest,manifest_json,state,member_count) "
                    "VALUES (:id,:solution_id,:revision,:digest,CAST(:manifest AS jsonb),"
                    "'building',:member_count)"
                ),
                {
                    "id": solution_snapshot_id,
                    "solution_id": solution_id,
                    "revision": manifest.revision,
                    "digest": digest,
                    "manifest": canonical_manifest,
                    "member_count": len(members),
                },
            )
            self._event(connection, solution_snapshot_id, "building")
            connection.execute(
                text(
                    "INSERT INTO solution_snapshot_member(" 
                    "solution_snapshot_id,repository_id,snapshot_id,role,ordinal,required) "
                    "VALUES (:solution_snapshot_id,:repository_id,:snapshot_id,:role,"
                    ":ordinal,:required)"
                ),
                [
                    {"solution_snapshot_id": solution_snapshot_id, **member}
                    for member in members
                ],
            )
            stored_count = connection.execute(
                text(
                    "SELECT count(*) FROM solution_snapshot_member "
                    "WHERE solution_snapshot_id=:id"
                ),
                {"id": solution_snapshot_id},
            ).scalar_one()
            if stored_count != len(members):
                raise RuntimeError("PostgreSQL solution member validation failed")
            connection.execute(
                text("UPDATE solution_snapshot SET state='validated' WHERE id=:id"),
                {"id": solution_snapshot_id},
            )
            self._event(connection, solution_snapshot_id, "validated")
            superseded = self._activate(
                connection, solution_id, solution_snapshot_id
            )
        return self._result(
            manifest,
            solution_snapshot_id,
            digest,
            idempotent=False,
            reactivated=False,
            superseded=superseded,
        )

    @staticmethod
    def _event(connection: Any, snapshot_id: str, state: str) -> None:
        connection.execute(
            text(
                "INSERT INTO solution_snapshot_event(solution_snapshot_id,state) "
                "VALUES (:id,:state)"
            ),
            {"id": snapshot_id, "state": state},
        )

    def _activate(
        self,
        connection: Any,
        solution_id: str,
        solution_snapshot_id: str,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            text(
                "SELECT id FROM solution_snapshot WHERE solution_id=:solution_id "
                "AND state='active' AND id<>:id ORDER BY id FOR UPDATE"
            ),
            {"solution_id": solution_id, "id": solution_snapshot_id},
        ).scalars().all()
        superseded = tuple(rows)
        connection.execute(
            text(
                "UPDATE solution_snapshot SET state='superseded' "
                "WHERE solution_id=:solution_id AND state='active' AND id<>:id"
            ),
            {"solution_id": solution_id, "id": solution_snapshot_id},
        )
        for previous_id in superseded:
            self._event(connection, previous_id, "superseded")
        connection.execute(
            text(
                "UPDATE solution_snapshot SET state='active',activated_at=now() "
                "WHERE id=:id"
            ),
            {"id": solution_snapshot_id},
        )
        self._event(connection, solution_snapshot_id, "active")
        return superseded

    @staticmethod
    def _result(
        manifest: SolutionManifest,
        solution_snapshot_id: str,
        digest: str,
        *,
        idempotent: bool,
        reactivated: bool,
        superseded: tuple[str, ...],
    ) -> SolutionPublishResult:
        return SolutionPublishResult(
            solution=manifest.name,
            solution_snapshot_id=solution_snapshot_id,
            revision=manifest.revision,
            manifest_digest=digest,
            state="active",
            member_count=len(manifest.members),
            idempotent=idempotent,
            reactivated=reactivated,
            superseded_solution_snapshot_ids=superseded,
        )


def resolve_postgres_solution_scope(
    engine: Engine,
    solution: str,
    solution_snapshot_id: str | None = None,
    allowed_repositories: set[str] | None = None,
) -> ResolvedSolutionScope:
    clauses = ["sol.name=:solution"]
    parameters: dict[str, Any] = {"solution": solution}
    if solution_snapshot_id:
        clauses.append("ss.id=:snapshot_id")
        parameters["snapshot_id"] = solution_snapshot_id
    else:
        clauses.append("ss.state='active'")
    with engine.connect() as connection:
        snapshot = connection.execute(
            text(
                "SELECT ss.id,ss.revision,ss.manifest_digest,ss.state "
                "FROM solution_snapshot ss JOIN solution sol ON sol.id=ss.solution_id "
                f"WHERE {' AND '.join(clauses)}"
            ),
            parameters,
        ).mappings().first()
        if snapshot is None:
            if solution_snapshot_id:
                raise ValueError("requested solution snapshot was not found")
            raise ValueError("solution has no active snapshot")
        members = connection.execute(
            text(
                "SELECT r.name AS repository,s.id AS snapshot_id,s.revision,"
                "s.source_digest,s.manifest_digest,s.index_profile_digest,s.state,"
                "member.role,member.ordinal,member.required "
                "FROM solution_snapshot_member member "
                "JOIN repository r ON r.id=member.repository_id "
                "JOIN snapshot s ON s.id=member.snapshot_id "
                "WHERE member.solution_snapshot_id=:id ORDER BY member.ordinal"
            ),
            {"id": snapshot["id"]},
        ).mappings().all()
    visible = [
        dict(row)
        for row in members
        if allowed_repositories is None or row["repository"] in allowed_repositories
    ]
    if not visible:
        raise ValueError("solution has no repositories visible to this principal")
    return ResolvedSolutionScope(
        solution=solution,
        solution_snapshot_id=snapshot["id"],
        revision=snapshot["revision"],
        manifest_digest=snapshot["manifest_digest"],
        state=snapshot["state"],
        snapshots=tuple(visible),
        partial_visibility=len(visible) != len(members),
    )
