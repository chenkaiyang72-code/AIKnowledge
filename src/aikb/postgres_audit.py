from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from aikb.ingestion import stable_id
from aikb.postgres_catalog import PostgresPrincipalContext


@dataclass(frozen=True)
class MCPAuditRecord:
    request_id: str
    tool_name: str
    outcome: str
    scope_summary: dict[str, Any]
    result_summary: dict[str, Any]
    query_hash: str | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id or len(self.request_id) > 128:
            raise ValueError("audit request ID is invalid")
        if self.tool_name not in {
            "aikb_scope_resolve",
            "aikb_context_search",
            "aikb_context_get",
        }:
            raise ValueError("audit tool name is invalid")
        if self.outcome not in {"success", "error"}:
            raise ValueError("audit outcome is invalid")


class PostgresMCPAuditWriter:
    """Append metadata-only MCP audit events through the reader RLS role."""

    def __init__(self, engine: Engine, principal: PostgresPrincipalContext):
        self.engine = engine
        self.principal = principal

    def append(self, record: MCPAuditRecord) -> str:
        event_id = stable_id(
            "mcp_audit",
            self.principal.security_domain_id,
            self.principal.principal_id,
            record.request_id,
            record.tool_name,
        )
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE aikb_reader"))
            connection.execute(
                text("SELECT set_config('aikb.principal_id',:value,true)"),
                {"value": self.principal.principal_id},
            )
            connection.execute(
                text("SELECT set_config('aikb.security_domain_id',:value,true)"),
                {"value": self.principal.security_domain_id},
            )
            connection.execute(
                text(
                    "INSERT INTO mcp_audit_event(id,principal_id,security_domain_id,"
                    "request_id,tool_name,outcome,query_hash,trace_id,scope_summary,"
                    "result_summary) VALUES (:id,:principal,:domain,:request,:tool,"
                    ":outcome,:query_hash,:trace_id,CAST(:scope AS jsonb),"
                    "CAST(:result AS jsonb)) ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": event_id,
                    "principal": self.principal.principal_id,
                    "domain": self.principal.security_domain_id,
                    "request": record.request_id,
                    "tool": record.tool_name,
                    "outcome": record.outcome,
                    "query_hash": record.query_hash,
                    "trace_id": record.trace_id,
                    "scope": json.dumps(record.scope_summary, sort_keys=True),
                    "result": json.dumps(record.result_summary, sort_keys=True),
                },
            )
        return event_id
