from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from aikb.catalog import Catalog, SearchHit
from aikb.storage import (
    LexicalSearchResult,
    ReadCatalog,
    SourceLocation,
)


ZOEKT_EXPORT_VERSION = 1
MAX_ZOEKT_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ZOEKT_TERMS = 32


class ZoektError(RuntimeError):
    pass


class ZoektUnavailable(ZoektError):
    pass


class ZoektProtocolError(ZoektError):
    pass


@dataclass(frozen=True)
class ZoektExportResult:
    snapshot_id: str
    repository: str
    revision: str
    zoekt_repository: str
    output: Path
    source: Path
    metadata: Path
    manifest: Path
    export_digest: str
    file_count: int
    byte_count: int
    idempotent: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "repository": self.repository,
            "revision": self.revision,
            "zoekt_repository": self.zoekt_repository,
            "output": str(self.output),
            "source": str(self.source),
            "metadata": str(self.metadata),
            "manifest": str(self.manifest),
            "export_digest": self.export_digest,
            "file_count": self.file_count,
            "byte_count": self.byte_count,
            "idempotent": self.idempotent,
        }


def zoekt_repository_name(snapshot_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", snapshot_id):
        raise ValueError("snapshot ID cannot be represented as a Zoekt repository")
    return f"aikb-snapshot-{snapshot_id}"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _safe_parts(path: str) -> tuple[str, ...]:
    if "\\" in path:
        raise ValueError(f"snapshot path must use forward slashes: {path!r}")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or not parsed.parts:
        raise ValueError(f"snapshot path must be relative: {path!r}")
    if any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError(f"snapshot path contains an unsafe segment: {path!r}")
    return parsed.parts


def _load_export_rows(
    catalog: Catalog,
    snapshot_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    snapshot = _SnapshotLoader.load_snapshot(catalog, snapshot_id)
    repository_row = catalog.connection.execute(
        "SELECT * FROM repository WHERE id=?", (snapshot["repository_id"],)
    ).fetchone()
    if repository_row is None:
        raise ValueError(f"repository not found for snapshot: {snapshot['id']}")
    rows = [
        dict(row)
        for row in catalog.connection.execute(
            """
            SELECT f.path,f.blob_id,f.size_bytes,b.compression,b.compressed_content
            FROM source_file AS f JOIN blob AS b ON b.id=f.blob_id
            WHERE f.snapshot_id=? ORDER BY f.path
            """,
            (snapshot["id"],),
        )
    ]
    if len(rows) != snapshot["file_count"]:
        raise RuntimeError(
            "snapshot export source is inconsistent: "
            f"expected {snapshot['file_count']} files, got {len(rows)}"
        )
    return snapshot, dict(repository_row), rows


class _SnapshotLoader:
    """Shared state check without importing the PostgreSQL optional dependency."""

    @staticmethod
    def load_snapshot(
        catalog: Catalog,
        snapshot_id: str | None,
    ) -> dict[str, Any]:
        if snapshot_id:
            row = catalog.connection.execute(
                "SELECT * FROM snapshot WHERE id=?", (snapshot_id,)
            ).fetchone()
        else:
            rows = catalog.connection.execute(
                "SELECT * FROM snapshot WHERE state='active' ORDER BY id"
            ).fetchall()
            if len(rows) != 1:
                raise ValueError(
                    "snapshot-id is required unless the catalog has exactly one "
                    "active snapshot"
                )
            row = rows[0]
        if row is None:
            raise ValueError(f"snapshot not found: {snapshot_id}")
        if row["state"] not in {"active", "superseded"}:
            raise ValueError(
                "snapshot must already be validated, "
                f"got state={row['state']}"
            )
        return dict(row)


def _expected_manifest(
    snapshot: dict[str, Any],
    repository: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "export_version": ZOEKT_EXPORT_VERSION,
        "snapshot_id": snapshot["id"],
        "repository": repository["name"],
        "revision": snapshot["revision"],
        "source_digest": snapshot["source_digest"],
        "manifest_digest": snapshot["manifest_digest"],
        "index_profile_digest": snapshot["index_profile_digest"],
        "zoekt_repository": zoekt_repository_name(snapshot["id"]),
        "file_count": snapshot["file_count"],
        "byte_count": snapshot["byte_count"],
        "files": [
            {
                "path": row["path"],
                "blob_id": row["blob_id"],
                "size_bytes": row["size_bytes"],
            }
            for row in rows
        ],
    }
    return {
        **payload,
        "export_digest": hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest(),
    }


def _metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "Name": manifest["zoekt_repository"],
        "URL": f"aiknowledge://repository/{manifest['repository']}",
        "Branches": [
            {"Name": "snapshot", "Version": manifest["revision"]}
        ],
        "Metadata": {
            "aikb.repository": manifest["repository"],
            "aikb.snapshot_id": manifest["snapshot_id"],
            "aikb.manifest_digest": manifest["manifest_digest"],
            "aikb.index_profile_digest": manifest["index_profile_digest"],
        },
    }


def _validate_existing_export(output: Path, expected: dict[str, Any]) -> None:
    manifest_path = output / "manifest.json"
    try:
        actual = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"existing Zoekt export has no valid manifest: {output}"
        ) from error
    if actual != expected:
        raise RuntimeError(
            f"existing Zoekt export does not match snapshot: {output}"
        )
    try:
        actual_metadata = json.loads(
            (output / "zoekt.meta.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("existing Zoekt export metadata is invalid") from error
    if actual_metadata != _metadata(expected):
        raise RuntimeError("existing Zoekt export metadata does not match snapshot")
    source = output / "source"
    expected_paths = {item["path"] for item in expected["files"]}
    actual_paths = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_paths != expected_paths:
        raise RuntimeError("existing Zoekt export file set is inconsistent")
    for item in expected["files"]:
        target = source.joinpath(*_safe_parts(item["path"]))
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"existing Zoekt export path is unsafe: {target}")
        data = target.read_bytes()
        if len(data) != item["size_bytes"]:
            raise RuntimeError(f"existing Zoekt export size mismatch: {target}")
        if hashlib.sha256(data).hexdigest() != item["blob_id"]:
            raise RuntimeError(f"existing Zoekt export digest mismatch: {target}")


def export_snapshot_for_zoekt(
    catalog: Catalog,
    output: Path,
    snapshot_id: str | None = None,
) -> ZoektExportResult:
    snapshot, repository, rows = _load_export_rows(catalog, snapshot_id)
    output = output.resolve()
    expected = _expected_manifest(snapshot, repository, rows)
    if output.exists():
        if not output.is_dir() or output.is_symlink():
            raise RuntimeError(f"Zoekt export target is not a safe directory: {output}")
        _validate_existing_export(output, expected)
        return _export_result(output, expected, True)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    ).resolve()
    try:
        temporary.chmod(0o755)
        source = temporary / "source"
        source.mkdir()
        byte_count = 0
        for row in rows:
            if row["compression"] != "zlib":
                raise RuntimeError(
                    f"unsupported blob compression: {row['compression']}"
                )
            data = zlib.decompress(row["compressed_content"])
            if len(data) != row["size_bytes"]:
                raise RuntimeError(f"blob size mismatch: {row['blob_id']}")
            if hashlib.sha256(data).hexdigest() != row["blob_id"]:
                raise RuntimeError(f"blob digest mismatch: {row['blob_id']}")
            target = source.joinpath(*_safe_parts(row["path"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            byte_count += len(data)
        if byte_count != expected["byte_count"]:
            raise RuntimeError(
                "snapshot export byte count mismatch: "
                f"expected {expected['byte_count']}, got {byte_count}"
            )
        (temporary / "zoekt.meta.json").write_text(
            json.dumps(_metadata(expected), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (temporary / "manifest.json").write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raced = False
        try:
            temporary.rename(output)
        except FileExistsError:
            _validate_existing_export(output, expected)
            raced = True
        return _export_result(output, expected, raced)
    finally:
        if temporary.exists():
            if (
                temporary.parent != output.parent
                or not temporary.name.startswith(f".{output.name}.tmp-")
                or temporary.is_symlink()
            ):
                raise RuntimeError(
                    f"refusing to remove unsafe temporary export: {temporary}"
                )
            shutil.rmtree(temporary)


def _export_result(
    output: Path,
    manifest: dict[str, Any],
    idempotent: bool,
) -> ZoektExportResult:
    return ZoektExportResult(
        snapshot_id=manifest["snapshot_id"],
        repository=manifest["repository"],
        revision=manifest["revision"],
        zoekt_repository=manifest["zoekt_repository"],
        output=output,
        source=output / "source",
        metadata=output / "zoekt.meta.json",
        manifest=output / "manifest.json",
        export_digest=manifest["export_digest"],
        file_count=manifest["file_count"],
        byte_count=manifest["byte_count"],
        idempotent=idempotent,
    )


class ZoektClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = MAX_ZOEKT_RESPONSE_BYTES,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Zoekt URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Zoekt URL must not contain credentials, query, or fragment")
        if timeout_seconds <= 0:
            raise ValueError("Zoekt timeout must be positive")
        if max_response_bytes < 1:
            raise ValueError("Zoekt response limit must be positive")
        base_path = parsed.path.rstrip("/")
        self.search_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, f"{base_path}/api/search", "", "")
        )
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    def search_locations(
        self,
        query: str,
        snapshots: list[dict[str, Any]],
        top_k: int,
    ) -> list[SourceLocation]:
        if top_k < 1 or top_k > 100:
            raise ValueError("top-k must be between 1 and 100")
        terms = list(dict.fromkeys(re.findall(r"[\w]+", query)))[:MAX_ZOEKT_TERMS]
        if not terms:
            raise ValueError("query must contain at least one searchable term")
        scopes = {
            zoekt_repository_name(row["snapshot_id"]): row for row in snapshots
        }
        content_query = " or ".join(
            f'content:"{_quote_value(term)}"' for term in terms
        )
        repository_query = " or ".join(
            f"repo:^{name}$" for name in scopes
        )
        zoekt_query = f"({content_query}) ({repository_query})"
        payload = {
            "Q": zoekt_query,
            "Opts": {
                "MaxDocDisplayCount": min(top_k * 5, 500),
                "MaxMatchDisplayCount": min(top_k * 20, 2_000),
                "NumContextLines": 0,
                "UseBM25Scoring": True,
            },
        }
        response = self._post(payload)
        result = response.get("Result")
        if not isinstance(result, dict):
            raise ZoektProtocolError("Zoekt response has no Result object")
        files = result.get("Files") or []
        if not isinstance(files, list):
            raise ZoektProtocolError("Zoekt Result.Files must be a list")
        locations: list[SourceLocation] = []
        seen: set[tuple[str, str, int]] = set()
        for file_index, item in enumerate(files):
            if not isinstance(item, dict):
                raise ZoektProtocolError("Zoekt file match must be an object")
            internal_repository = item.get("Repository")
            scope = scopes.get(internal_repository)
            if scope is None:
                raise ZoektProtocolError(
                    "Zoekt returned a repository outside the requested scope"
                )
            version = item.get("Version")
            if version and version != scope["revision"]:
                raise ZoektProtocolError(
                    "Zoekt returned a version that does not match the snapshot"
                )
            path = item.get("FileName")
            if not isinstance(path, str):
                raise ZoektProtocolError("Zoekt FileName must be a string")
            normalized_path = PurePosixPath(*_safe_parts(path)).as_posix()
            line_matches = item.get("LineMatches", [])
            if not isinstance(line_matches, list):
                raise ZoektProtocolError("Zoekt LineMatches must be a list")
            file_score = _number(item.get("Score"), default=-float(file_index + 1))
            for line_index, line_match in enumerate(line_matches):
                if not isinstance(line_match, dict):
                    raise ZoektProtocolError("Zoekt line match must be an object")
                line = line_match.get("LineNumber")
                if not isinstance(line, int) or line < 1:
                    continue
                key = (scope["snapshot_id"], normalized_path, line)
                if key in seen:
                    continue
                seen.add(key)
                line_score = _number(line_match.get("Score"), default=file_score)
                score = max(file_score, line_score)
                locations.append(
                    SourceLocation(
                        repository=scope["repository"],
                        snapshot_id=scope["snapshot_id"],
                        path=normalized_path,
                        line=line,
                        rank=-score + (line_index / 1_000_000),
                    )
                )
                if len(locations) >= top_k * 5:
                    return locations
        return locations

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.search_url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                data = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                raise ZoektUnavailable(f"Zoekt HTTP {error.code}") from error
            raise ZoektProtocolError(f"Zoekt rejected query with HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ZoektUnavailable(f"Zoekt is unavailable: {error}") from error
        if len(data) > self.max_response_bytes:
            raise ZoektProtocolError("Zoekt response exceeded configured size limit")
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ZoektProtocolError("Zoekt returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise ZoektProtocolError("Zoekt response must be an object")
        return decoded


def _quote_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _number(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


class ZoektReadCatalog:
    """Routes lexical reads to Zoekt and all authoritative reads to a catalog."""

    def __init__(
        self,
        catalog: ReadCatalog,
        client: ZoektClient,
        fallback_on_unavailable: bool = True,
    ):
        self.catalog = catalog
        self.client = client
        self.fallback_on_unavailable = fallback_on_unavailable

    def resolve_snapshots(
        self,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.catalog.resolve_snapshots(repository, snapshot_id)

    def search_lexical(
        self,
        query: str,
        top_k: int = 10,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> LexicalSearchResult:
        snapshots = self.catalog.resolve_snapshots(repository, snapshot_id)
        try:
            locations = self.client.search_locations(query, snapshots, top_k)
        except ZoektUnavailable:
            if not self.fallback_on_unavailable:
                raise
            return self.catalog.search_lexical(
                query, top_k, repository, snapshot_id
            )
        hits = self.catalog.resolve_location_chunks(locations, top_k)
        return LexicalSearchResult(channel="lexical_zoekt", hits=tuple(hits))

    def search(
        self,
        query: str,
        top_k: int = 10,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]:
        return list(
            self.search_lexical(
                query, top_k, repository, snapshot_id
            ).hits
        )

    def resolve_location_chunks(
        self,
        locations: list[SourceLocation],
        top_k: int = 10,
    ) -> list[SearchHit]:
        return self.catalog.resolve_location_chunks(locations, top_k)

    def search_symbol_chunks(
        self,
        names: list[str],
        top_k: int = 20,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]:
        return self.catalog.search_symbol_chunks(
            names, top_k, repository, snapshot_id
        )

    def search_relation_chunks(
        self,
        names: list[str],
        top_k: int = 20,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> list[SearchHit]:
        return self.catalog.search_relation_chunks(
            names, top_k, repository, snapshot_id
        )

    def find_symbol(
        self,
        name: str,
        top_k: int = 50,
        repository: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        return self.catalog.find_symbol(name, top_k, repository, snapshot_id)
