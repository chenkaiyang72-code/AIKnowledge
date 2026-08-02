from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


REQUIRED_QUESTION_FIELDS = {
    "id",
    "question",
    "category",
    "synthetic",
    "query_terms",
    "required_evidence",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_questions(path: Path) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                question = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(question, dict):
                raise ValueError(f"{path}:{line_number}: question must be an object")
            missing = REQUIRED_QUESTION_FIELDS - question.keys()
            if missing:
                raise ValueError(
                    f"{path}:{line_number}: missing fields: {', '.join(sorted(missing))}"
                )
            if not question["query_terms"]:
                raise ValueError(f"{path}:{line_number}: query_terms must not be empty")
            questions.append(question)
    if not questions:
        raise ValueError(f"{path} contains no questions")
    return questions


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def kernel_version(source: Path) -> str:
    makefile = source / "Makefile"
    values: dict[str, str] = {}
    wanted = {"VERSION", "PATCHLEVEL", "SUBLEVEL", "EXTRAVERSION"}
    with makefile.open("r", encoding="utf-8") as stream:
        for line in stream:
            match = re.match(r"^(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)\s*=\s*(.*)$", line)
            if match:
                values[match.group(1)] = match.group(2).strip()
            if wanted <= values.keys():
                break
    missing = wanted - values.keys()
    if missing:
        raise ValueError(f"cannot read kernel version; missing {sorted(missing)}")
    base = f'{values["VERSION"]}.{values["PATCHLEVEL"]}.{values["SUBLEVEL"]}'
    return f'{base}{values["EXTRAVERSION"]}'


def inspect_source(scope: dict[str, Any], source: Path, archive: Path | None) -> dict[str, Any]:
    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    required_paths = ["Makefile", "Kconfig", "arch", "drivers", "fs", "kernel", "mm"]
    missing = [item for item in required_paths if not (source / item).exists()]
    if missing:
        raise ValueError(f"source tree is incomplete; missing: {missing}")

    actual_version = kernel_version(source)
    expected_version = scope["source"]["version"]
    if actual_version != expected_version:
        raise ValueError(f"version mismatch: expected {expected_version}, got {actual_version}")

    result: dict[str, Any] = {
        "scope_id": scope["scope_id"],
        "source_version": actual_version,
        "source_exists": True,
        "git_metadata": (source / ".git").exists(),
        "build_profile": {
            "config": (source / ".config").exists(),
            "compile_commands": (source / "compile_commands.json").exists(),
            "module_symvers": (source / "Module.symvers").exists(),
        },
    }
    if archive is not None:
        actual_hash = sha256_file(archive)
        expected_hash = scope["source"]["archive_sha256"]
        if actual_hash.lower() != expected_hash.lower():
            raise ValueError(
                f"archive SHA-256 mismatch: expected {expected_hash}, got {actual_hash}"
            )
        result["archive"] = {"path": str(archive), "sha256": actual_hash}
    return result


def _relative_path(path_text: str, source: Path) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        path = source / path
    try:
        return path.resolve().relative_to(source.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def search_term(
    source: Path,
    term: str,
    include_roots: Iterable[str],
    exclude_globs: Iterable[str],
    max_hits: int = 2_000,
) -> tuple[list[dict[str, Any]], bool]:
    if not term or len(term) > 128:
        raise ValueError("each query term must contain 1..128 characters")
    rg = shutil.which("rg")
    if rg is None:
        raise RuntimeError("ripgrep (rg) is required but was not found on PATH")

    command = [
        rg,
        "--json",
        "--line-number",
        "--column",
        "--color=never",
        "--fixed-strings",
        "--max-count=20",
    ]
    for pattern in exclude_globs:
        command.extend(["--glob", f"!{pattern}"])
    search_roots = [source / root for root in include_roots]
    search_roots = [root for root in search_roots if root.exists()]
    if not search_roots:
        search_roots = [source]
    command.extend(["--", term, *(str(root) for root in search_roots)])

    hits: list[dict[str, Any]] = []
    truncated = False
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for raw_line in process.stdout:
        event = json.loads(raw_line)
        if event.get("type") != "match":
            continue
        data = event["data"]
        hits.append(
            {
                "path": _relative_path(data["path"]["text"], source),
                "line": data["line_number"],
                "column": data["submatches"][0]["start"] + 1,
                "text": data["lines"]["text"].rstrip("\r\n"),
                "term": term,
            }
        )
        if len(hits) >= max_hits:
            truncated = True
            process.kill()
            break
    stderr = ""
    if process.stderr is not None:
        stderr = process.stderr.read().strip()
    return_code = process.wait()
    process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    if not truncated and return_code not in (0, 1):
        raise RuntimeError(f"ripgrep failed for {term!r}: {stderr}")
    return hits, truncated


def rank_question(
    source: Path,
    question: dict[str, Any],
    include_roots: Iterable[str],
    exclude_globs: Iterable[str],
    top_k: int,
) -> dict[str, Any]:
    by_path: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"terms": set(), "hits": [], "hit_count": 0}
    )
    truncated_terms: list[str] = []
    for term in question["query_terms"]:
        hits, truncated = search_term(source, term, include_roots, exclude_globs)
        if truncated:
            truncated_terms.append(term)
        for hit in hits:
            entry = by_path[hit["path"]]
            entry["terms"].add(term)
            entry["hit_count"] += 1
            if len(entry["hits"]) < 8:
                entry["hits"].append(hit)

    ranked: list[dict[str, Any]] = []
    for path, entry in by_path.items():
        term_coverage = len(entry["terms"])
        score = term_coverage * 100 + min(entry["hit_count"], 50)
        ranked.append(
            {
                "path": path,
                "score": score,
                "matched_terms": sorted(entry["terms"]),
                "hit_count": entry["hit_count"],
                "hits": entry["hits"],
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["path"]))
    return {
        "id": question["id"],
        "question": question["question"],
        "synthetic": question["synthetic"],
        "truncated_terms": truncated_terms,
        "results": ranked[:top_k],
    }


def evaluate_results(
    questions: list[dict[str, Any]], results: list[dict[str, Any]], top_k: int
) -> dict[str, Any]:
    result_by_id = {item["id"]: item for item in results}
    total_evidence = 0
    found_evidence = 0
    reciprocal_ranks: list[float] = []
    per_question: list[dict[str, Any]] = []

    for question in questions:
        expected_paths = {
            evidence["path"] for evidence in question.get("required_evidence", [])
        }
        ranked_paths = [
            item["path"] for item in result_by_id[question["id"]]["results"][:top_k]
        ]
        hits = expected_paths.intersection(ranked_paths)
        total_evidence += len(expected_paths)
        found_evidence += len(hits)
        ranks = [ranked_paths.index(path) + 1 for path in expected_paths if path in ranked_paths]
        reciprocal_rank = 1.0 / min(ranks) if ranks else 0.0
        if expected_paths:
            reciprocal_ranks.append(reciprocal_rank)
        per_question.append(
            {
                "id": question["id"],
                "expected_paths": sorted(expected_paths),
                "found_paths": sorted(hits),
                "reciprocal_rank": reciprocal_rank,
            }
        )

    return {
        f"evidence_recall_at_{top_k}": (
            found_evidence / total_evidence if total_evidence else None
        ),
        "mrr": (
            sum(reciprocal_ranks) / len(reciprocal_ranks)
            if reciprocal_ranks
            else None
        ),
        "evidence_found": found_evidence,
        "evidence_total": total_evidence,
        "per_question": per_question,
    }


def run_baseline(
    scope: dict[str, Any],
    questions: list[dict[str, Any]],
    source: Path,
    top_k: int,
) -> dict[str, Any]:
    results = [
        rank_question(
            source,
            question,
            scope.get("include_roots", []),
            scope.get("exclude_globs", []),
            top_k,
        )
        for question in questions
    ]
    return {
        "schema_version": 1,
        "scope_id": scope["scope_id"],
        "retriever": {
            "name": "ripgrep-fixed-string",
            "top_k": top_k,
            "ranking": "term_coverage_then_hit_count",
        },
        "metrics": evaluate_results(questions, results, top_k),
        "questions": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIKnowledge Phase 0A evaluation CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="verify a source snapshot")
    inspect_parser.add_argument("--scope", type=Path, required=True)
    inspect_parser.add_argument("--source", type=Path, required=True)
    inspect_parser.add_argument("--archive", type=Path)

    baseline_parser = subparsers.add_parser("baseline", help="run lexical baseline")
    baseline_parser.add_argument("--scope", type=Path, required=True)
    baseline_parser.add_argument("--questions", type=Path, required=True)
    baseline_parser.add_argument("--source", type=Path, required=True)
    baseline_parser.add_argument("--output", type=Path, required=True)
    baseline_parser.add_argument("--top-k", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scope = load_json(args.scope)
        if args.command == "inspect":
            report = inspect_source(scope, args.source, args.archive)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.top_k < 1 or args.top_k > 100:
            raise ValueError("top-k must be between 1 and 100")
        questions = load_questions(args.questions)
        inspect_source(scope, args.source, None)
        report = run_baseline(scope, questions, args.source, args.top_k)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
