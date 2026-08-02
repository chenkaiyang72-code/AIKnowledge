from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any


TREE_SITTER_GENERATOR = "tree-sitter-c-v3"
FALLBACK_GENERATOR = "line-window-v1"
STRUCTURED_LANGUAGES = {"c", "c-header"}
STRUCTURAL_NODE_TYPES = {
    "function_definition": "function",
    "declaration": "declaration",
    "type_definition": "type",
    "preproc_def": "macro",
    "preproc_function_def": "macro",
    "preproc_call": "macro_invocation",
    "expression_statement": "top_level_expression",
}
CONTAINER_NODE_TYPES = {
    "translation_unit",
    "ERROR",
    "preproc_if",
    "preproc_ifdef",
    "preproc_elif",
    "preproc_else",
}


@dataclass(frozen=True)
class CodeChunk:
    kind: str
    start_line: int
    end_line: int
    content: str
    symbol: str | None
    generator: str


@dataclass(frozen=True)
class ParseOutcome:
    chunks: list[CodeChunk]
    parse_status: str
    syntax_error_count: int


def tree_sitter_versions() -> dict[str, str | None]:
    try:
        import tree_sitter
        import tree_sitter_c

        return {
            "tree_sitter": version("tree-sitter"),
            "tree_sitter_c": version("tree-sitter-c"),
        }
    except (ImportError, PackageNotFoundError):
        return {"tree_sitter": None, "tree_sitter_c": None}


def _fallback_chunks(
    text: str,
    chunk_lines: int,
    overlap: int,
) -> list[CodeChunk]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return []
    chunks: list[CodeChunk] = []
    start = 0
    while start < len(lines):
        end = min(start + chunk_lines, len(lines))
        chunks.append(
            CodeChunk(
                kind="line_window",
                start_line=start + 1,
                end_line=end,
                content="".join(lines[start:end]),
                symbol=None,
                generator=FALLBACK_GENERATOR,
            )
        )
        if end == len(lines):
            break
        start = end - overlap
    return chunks


def _extract_structured_chunks(
    node: Any,
    data: bytes,
    max_structured_lines: int,
    overlap: int,
) -> list[CodeChunk]:
    if node.type not in CONTAINER_NODE_TYPES:
        return []
    chunks: list[CodeChunk] = []
    leading_comments: list[tuple[int, int, int]] = []
    for child_index in range(node.named_child_count):
        child = node.named_child(child_index)
        if child is None:
            continue
        if child.type == "comment":
            if leading_comments and child.start_point.row - leading_comments[-1][2] > 2:
                leading_comments = []
            leading_comments.append(
                (child.start_byte, child.start_point.row, child.end_point.row)
            )
            continue
        if child.type in STRUCTURAL_NODE_TYPES:
            if (
                leading_comments
                and child.start_point.row - leading_comments[-1][2] <= 2
            ):
                start_byte = leading_comments[0][0]
                start_row = leading_comments[0][1]
            else:
                start_byte = child.start_byte
                start_row = child.start_point.row
            content = data[start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
            kind = STRUCTURAL_NODE_TYPES[child.type]
            chunks.extend(
                _split_large_chunk(
                    content=content,
                    kind=kind,
                    symbol=_symbol_from_content(kind, content),
                    start_line=start_row + 1,
                    max_lines=max_structured_lines,
                    overlap=overlap,
                )
            )
            leading_comments = []
            continue
        leading_comments = []
        if child.type in CONTAINER_NODE_TYPES:
            chunks.extend(
                _extract_structured_chunks(
                    child, data, max_structured_lines, overlap
                )
            )
    return chunks


def _symbol_from_content(kind: str, content: str) -> str | None:
    if kind == "macro":
        match = re.search(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)", content)
        return match.group(1) if match else None
    if kind in {"macro_invocation", "top_level_expression"}:
        match = re.search(r"\b([A-Za-z_]\w*)\s*\(", content)
        return match.group(1) if match else None
    if kind == "type":
        match = re.search(r"\b([A-Za-z_]\w*)\s*;\s*$", content)
        return match.group(1) if match else None
    if kind == "function":
        header = content.split("{", 1)[0]
        matches = re.findall(r"\b([A-Za-z_]\w*)\s*\(", header)
        return matches[-1] if matches else None
    if kind == "declaration":
        call = re.search(r"\b([A-Z_][A-Z0-9_]*)\s*\(", content)
        if call:
            return call.group(1)
        function_names = re.findall(r"\b([A-Za-z_]\w*)\s*\(", content)
        if function_names:
            return function_names[-1]
        identifiers = re.findall(r"\b([A-Za-z_]\w*)\b", content)
        ignored = {
            "const",
            "extern",
            "static",
            "struct",
            "union",
            "enum",
            "unsigned",
            "signed",
            "long",
            "short",
            "int",
            "char",
            "void",
        }
        candidates = [item for item in identifiers if item not in ignored]
        return candidates[-1] if candidates else None
    return None


def _split_large_chunk(
    content: str,
    kind: str,
    symbol: str | None,
    start_line: int,
    max_lines: int,
    overlap: int,
) -> list[CodeChunk]:
    lines = content.splitlines(keepends=True)
    if len(lines) <= max_lines:
        return [
            CodeChunk(
                kind=kind,
                start_line=start_line,
                end_line=start_line + max(len(lines), 1) - 1,
                content=content,
                symbol=symbol,
                generator=TREE_SITTER_GENERATOR,
            )
        ]
    chunks: list[CodeChunk] = []
    start = 0
    part = 1
    while start < len(lines):
        end = min(start + max_lines, len(lines))
        chunks.append(
            CodeChunk(
                kind=f"{kind}_part",
                start_line=start_line + start,
                end_line=start_line + end - 1,
                content="".join(lines[start:end]),
                symbol=symbol,
                generator=TREE_SITTER_GENERATOR,
            )
        )
        if end == len(lines):
            break
        part += 1
        start = end - overlap
    return chunks


def build_chunks(
    data: bytes,
    language: str,
    chunk_lines: int,
    overlap: int,
    max_structured_lines: int = 240,
) -> ParseOutcome:
    text = data.decode("utf-8", errors="replace")
    if language not in STRUCTURED_LANGUAGES:
        return ParseOutcome(_fallback_chunks(text, chunk_lines, overlap), "not_applicable", 0)
    try:
        import tree_sitter_c
        from tree_sitter import Language, Parser
    except ImportError:
        return ParseOutcome(_fallback_chunks(text, chunk_lines, overlap), "fallback", 0)

    language_definition = Language(tree_sitter_c.language())
    # Keep the parser alive for the full lifetime of the tree and its nodes.
    parser = Parser(language_definition)
    tree = parser.parse(data)
    root = tree.root_node
    # Treat this as a file-level parse anomaly flag. Walking every malformed
    # descendant is both expensive on macro-heavy kernel code and unnecessary
    # for deciding whether a file needs fallback or review.
    error_count = int(root.has_error)
    chunks = _extract_structured_chunks(
        root, data, max_structured_lines, overlap
    )
    if not chunks:
        return ParseOutcome(
            _fallback_chunks(text, chunk_lines, overlap), "fallback", error_count
        )
    return ParseOutcome(chunks, "structured", error_count)
