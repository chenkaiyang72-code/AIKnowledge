from __future__ import annotations

import argparse
from pathlib import Path

from aikb.catalog import Catalog
from aikb.ingestion import ingest_source
from aikb.zoekt import export_snapshot_for_zoekt


def prepare(root: Path) -> dict[str, str]:
    root = root.resolve()
    if root.exists():
        raise RuntimeError(f"fixture root already exists: {root}")
    root.mkdir(parents=True)
    source = root / "linux"
    source.mkdir()
    (source / "Makefile").write_text(
        "VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 40\nEXTRAVERSION =\n",
        encoding="utf-8",
    )
    (source / "Kconfig").write_text('mainmenu "test"\n', encoding="utf-8")
    for directory in ["arch", "drivers", "fs", "kernel", "mm", "include"]:
        (source / directory).mkdir()
    (source / "kernel" / "live.c").write_text(
        "static int zoekt_live_marker(void) { return 42; }\n",
        encoding="utf-8",
    )
    scope = {
        "scope_id": "zoekt-live-ci",
        "source": {
            "project": "linux-zoekt-live-ci",
            "version": "6.18.40",
            "kind": "release_archive",
            "archive_name": "linux-zoekt-live-ci.tar.xz",
            "archive_sha256": "e" * 64,
            "git_commit": None,
        },
        "include_roots": ["kernel"],
        "exclude_globs": [],
        "index_policy": {
            "mode": "source_only",
            "execute_build": False,
            "requires_build_artifacts": False,
        },
    }
    database = root / "catalog.db"
    with Catalog(database) as catalog:
        catalog.initialize()
        snapshot = ingest_source(catalog, scope, source)
        export = export_snapshot_for_zoekt(
            catalog,
            root / "export",
            snapshot["id"],
        )
    index = root / "index"
    index.mkdir()
    return {
        "AIKB_TEST_ZOEKT_ROOT": str(root),
        "AIKB_TEST_ZOEKT_DB": str(database),
        "AIKB_TEST_ZOEKT_SNAPSHOT": snapshot["id"],
        "AIKB_TEST_ZOEKT_URL": "http://127.0.0.1:6070",
        "AIKB_TEST_ZOEKT_EXPORT": str(export.output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    values = prepare(args.root)
    with args.env_file.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
