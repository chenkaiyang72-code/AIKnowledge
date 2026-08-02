from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


RELATION_GENERATOR = "source-relations-v2"
SOURCE_EXACT = "source_exact"
SOURCE_INFERRED = "source_inferred"
AMBIGUOUS_CANDIDATE = "ambiguous_candidate"
STRUCTURED_LANGUAGES = {"c", "c-header"}


@dataclass(frozen=True)
class SourceCondition:
    expression: str
    start_line: int
    end_line: int
    depth: int
    generator: str = RELATION_GENERATOR


@dataclass(frozen=True)
class SymbolOccurrence:
    name: str
    kind: str
    role: str
    start_line: int
    end_line: int
    namespace_scope: str
    signature: str | None
    condition: SourceCondition | None
    confidence: str = SOURCE_EXACT
    generator: str = RELATION_GENERATOR


@dataclass(frozen=True)
class SourceRelation:
    kind: str
    target_text: str
    start_line: int
    end_line: int
    source_name: str | None = None
    source_kind: str | None = None
    target_kind: str | None = None
    target_path: str | None = None
    condition: SourceCondition | None = None
    confidence: str = SOURCE_EXACT
    generator: str = RELATION_GENERATOR


@dataclass(frozen=True)
class SourceFacts:
    conditions: list[SourceCondition]
    occurrences: list[SymbolOccurrence]
    relations: list[SourceRelation]


@dataclass(frozen=True)
class DependencyReference:
    kind: str
    target: str
    line: int


@dataclass
class _ConditionFrame:
    parent_expression: str | None
    branch_expressions: list[str]
    current_expression: str
    start_line: int
    depth: int


_DIRECTIVE_PATTERN = re.compile(
    r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b\s*(.*?)\s*$"
)
_INCLUDE_PATTERN = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]')
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_]\w*$")


def _combine_conditions(parent: str | None, branch: str) -> str:
    if parent and branch:
        return f"({parent}) && ({branch})"
    return parent or branch


def _directive_expression(kind: str, value: str) -> str:
    value = value.strip()
    if kind == "ifdef":
        return f"defined({value})"
    if kind == "ifndef":
        return f"!defined({value})"
    return value or "1"


def extract_preprocessor_conditions(text: str) -> list[SourceCondition]:
    lines = text.splitlines()
    stack: list[_ConditionFrame] = []
    conditions: list[SourceCondition] = []

    def active_expression() -> str | None:
        if not stack:
            return None
        frame = stack[-1]
        return _combine_conditions(frame.parent_expression, frame.current_expression)

    def close_branch(end_line: int) -> None:
        if not stack or end_line < stack[-1].start_line:
            return
        expression = active_expression()
        if expression:
            conditions.append(
                SourceCondition(
                    expression=expression,
                    start_line=stack[-1].start_line,
                    end_line=end_line,
                    depth=stack[-1].depth,
                )
            )

    for line_number, line in enumerate(lines, start=1):
        match = _DIRECTIVE_PATTERN.match(line)
        if not match:
            continue
        kind, value = match.groups()
        if kind in {"if", "ifdef", "ifndef"}:
            parent = active_expression()
            expression = _directive_expression(kind, value)
            stack.append(
                _ConditionFrame(
                    parent_expression=parent,
                    branch_expressions=[expression],
                    current_expression=expression,
                    start_line=line_number + 1,
                    depth=len(stack) + 1,
                )
            )
        elif kind == "elif" and stack:
            close_branch(line_number - 1)
            frame = stack[-1]
            expression = _directive_expression(kind, value)
            previous = " || ".join(f"({item})" for item in frame.branch_expressions)
            frame.current_expression = f"!({previous}) && ({expression})"
            frame.branch_expressions.append(expression)
            frame.start_line = line_number + 1
        elif kind == "else" and stack:
            close_branch(line_number - 1)
            frame = stack[-1]
            previous = " || ".join(f"({item})" for item in frame.branch_expressions)
            frame.current_expression = f"!({previous})"
            frame.start_line = line_number + 1
        elif kind == "endif" and stack:
            close_branch(line_number - 1)
            stack.pop()

    last_line = len(lines)
    while stack:
        close_branch(last_line)
        stack.pop()
    return conditions


def _condition_at(
    conditions: list[SourceCondition], line: int
) -> SourceCondition | None:
    matches = [item for item in conditions if item.start_line <= line <= item.end_line]
    if not matches:
        return None
    return max(matches, key=lambda item: item.depth)


def _node_text(node: Any, data: bytes) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _symbol_from_declarator(node: Any, data: bytes) -> str | None:
    if node is None:
        return None
    if node.type in {"identifier", "type_identifier", "field_identifier"}:
        value = _node_text(node, data)
        return value if _IDENTIFIER_PATTERN.match(value) else None
    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        found = _symbol_from_declarator(declarator, data)
        if found:
            return found
    for child in node.named_children:
        found = _symbol_from_declarator(child, data)
        if found:
            return found
    return None


def _normalized_signature(content: str) -> str:
    header = content.split("{", 1)[0]
    return re.sub(r"\s+", " ", header).strip()[:1_000]


def _is_file_scoped(content: str, kind: str) -> bool:
    if kind in {"macro", "type"}:
        return True
    return bool(re.search(r"\bstatic\b", content.split("{", 1)[0]))


def _call_target(function_node: Any, data: bytes) -> tuple[str, str]:
    text = _node_text(function_node, data).strip()
    if function_node.type == "identifier" and _IDENTIFIER_PATTERN.match(text):
        return text, SOURCE_INFERRED
    identifiers = re.findall(r"[A-Za-z_]\w*", text)
    if identifiers:
        return identifiers[-1], AMBIGUOUS_CANDIDATE
    return text[:512], AMBIGUOUS_CANDIDATE


def _extract_c_facts(data: bytes, text: str) -> SourceFacts:
    conditions = extract_preprocessor_conditions(text)
    occurrences: list[SymbolOccurrence] = []
    relations: list[SourceRelation] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _INCLUDE_PATTERN.match(line)
        if match:
            delimiter, target = match.groups()
            relations.append(
                SourceRelation(
                    kind="includes",
                    target_text=target,
                    target_path=target,
                    start_line=line_number,
                    end_line=line_number,
                    condition=_condition_at(conditions, line_number),
                    confidence=(
                        SOURCE_EXACT if delimiter == '"' else SOURCE_INFERRED
                    ),
                )
            )

    try:
        import tree_sitter_c
        from tree_sitter import Language, Parser
    except ImportError:
        return SourceFacts(conditions, occurrences, relations)

    parser = Parser(Language(tree_sitter_c.language()))
    root = parser.parse(data).root_node

    def walk(node: Any, current_function: str | None = None) -> None:
        next_function = current_function
        content = _node_text(node, data)
        start_line = node.start_point.row + 1
        end_line = max(start_line, node.end_point.row + 1)
        condition = _condition_at(conditions, start_line)

        if node.type == "function_definition":
            name = _symbol_from_declarator(node.child_by_field_name("declarator"), data)
            if name:
                occurrences.append(
                    SymbolOccurrence(
                        name=name,
                        kind="function",
                        role="definition",
                        start_line=start_line,
                        end_line=end_line,
                        namespace_scope=(
                            "file" if _is_file_scoped(content, "function") else "repository"
                        ),
                        signature=_normalized_signature(content),
                        condition=condition,
                    )
                )
                next_function = name
        elif node.type == "declaration":
            name = _symbol_from_declarator(node.child_by_field_name("declarator"), data)
            if name:
                is_function = "(" in content.split(";", 1)[0]
                kind = "function" if is_function else "variable"
                if current_function and not is_function:
                    namespace_scope = f"function:{current_function}"
                else:
                    namespace_scope = (
                        "file" if _is_file_scoped(content, kind) else "repository"
                    )
                occurrences.append(
                    SymbolOccurrence(
                        name=name,
                        kind=kind,
                        role="declaration",
                        start_line=start_line,
                        end_line=end_line,
                        namespace_scope=namespace_scope,
                        signature=_normalized_signature(content),
                        condition=condition,
                    )
                )
        elif node.type == "type_definition":
            name = _symbol_from_declarator(node.child_by_field_name("declarator"), data)
            if not name:
                names = re.findall(r"\b([A-Za-z_]\w*)\s*;\s*$", content)
                name = names[-1] if names else None
            if name:
                occurrences.append(
                    SymbolOccurrence(
                        name=name,
                        kind="type",
                        role="definition",
                        start_line=start_line,
                        end_line=end_line,
                        namespace_scope=(
                            f"function:{current_function}"
                            if current_function
                            else "file"
                        ),
                        signature=_normalized_signature(content),
                        condition=condition,
                    )
                )
        elif node.type in {"preproc_def", "preproc_function_def"}:
            match = re.search(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)", content)
            if match:
                occurrences.append(
                    SymbolOccurrence(
                        name=match.group(1),
                        kind="macro",
                        role="definition",
                        start_line=start_line,
                        end_line=end_line,
                        namespace_scope="file",
                        signature=_normalized_signature(content),
                        condition=condition,
                    )
                )
        elif node.type == "call_expression":
            function_node = node.child_by_field_name("function")
            if function_node is not None:
                target, confidence = _call_target(function_node, data)
                if target:
                    relations.append(
                        SourceRelation(
                            kind="calls",
                            source_name=current_function,
                            source_kind="function" if current_function else None,
                            target_text=target,
                            target_kind="function",
                            start_line=start_line,
                            end_line=end_line,
                            condition=condition,
                            confidence=confidence,
                        )
                    )

        for child in node.named_children:
            walk(child, next_function)

    walk(root)
    return SourceFacts(conditions, occurrences, relations)


def _extract_kconfig_facts(text: str) -> SourceFacts:
    occurrences: list[SymbolOccurrence] = []
    relations: list[SourceRelation] = []
    current_symbol: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        definition = re.match(r"^\s*(?:menu)?config\s+([A-Za-z0-9_]+)\b", line)
        if definition:
            current_symbol = definition.group(1)
            occurrences.append(
                SymbolOccurrence(
                    name=current_symbol,
                    kind="config",
                    role="definition",
                    start_line=line_number,
                    end_line=line_number,
                    namespace_scope="repository",
                    signature=line.strip(),
                    condition=None,
                )
            )
            continue
        depends = re.match(r"^\s*depends\s+on\s+(.+?)\s*$", line)
        if depends and current_symbol:
            expression = depends.group(1)
            for target in sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", expression))):
                relations.append(
                    SourceRelation(
                        kind="depends_on_config",
                        source_name=current_symbol,
                        source_kind="config",
                        target_text=target,
                        target_kind="config",
                        start_line=line_number,
                        end_line=line_number,
                        confidence=SOURCE_EXACT,
                    )
                )
        selected = re.match(
            r"^\s*(select|imply)\s+([A-Z][A-Z0-9_]+)(?:\s+if\s+(.+))?\s*$",
            line,
        )
        if selected and current_symbol:
            relation_kind, target, condition_text = selected.groups()
            condition = (
                SourceCondition(condition_text, line_number, line_number, 1)
                if condition_text
                else None
            )
            relations.append(
                SourceRelation(
                    kind=f"{relation_kind}s_config",
                    source_name=current_symbol,
                    source_kind="config",
                    target_text=target,
                    target_kind="config",
                    start_line=line_number,
                    end_line=line_number,
                    condition=condition,
                    confidence=SOURCE_EXACT,
                )
            )
        sourced = re.match(r'^\s*(?:rsource|source)\s+"([^"]+)"', line)
        if sourced:
            relations.append(
                SourceRelation(
                    kind="includes_config",
                    target_text=sourced.group(1),
                    target_path=sourced.group(1),
                    start_line=line_number,
                    end_line=line_number,
                    confidence=SOURCE_EXACT,
                )
            )
    conditions = sorted(
        {item.condition for item in relations if item.condition is not None},
        key=lambda item: (item.start_line, item.expression),
    )
    return SourceFacts(conditions, occurrences, relations)


def _extract_kbuild_facts(text: str) -> SourceFacts:
    conditions: list[SourceCondition] = []
    relations: list[SourceRelation] = []
    assignment = re.compile(
        r"^\s*([A-Za-z0-9_./-]+)-\$\((CONFIG_[A-Za-z0-9_]+)\)\s*\+?=\s*(.+?)\s*$"
    )
    unconditional = re.compile(r"^\s*([A-Za-z0-9_./-]+)-(y|m)\s*\+?=\s*(.+?)\s*$")
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = assignment.match(line)
        condition: SourceCondition | None = None
        if match:
            owner, config, targets_text = match.groups()
            condition = SourceCondition(config, line_number, line_number, 1)
            conditions.append(condition)
        else:
            match = unconditional.match(line)
            if not match:
                continue
            owner, mode, targets_text = match.groups()
            config = f"KBUILD_{mode.upper()}"
        for target in targets_text.split():
            if target.startswith("$(") or target in {"+=", ":="}:
                continue
            relations.append(
                SourceRelation(
                    kind="kbuild_contains",
                    source_name=owner,
                    source_kind="kbuild_target",
                    target_text=target,
                    target_path=target,
                    start_line=line_number,
                    end_line=line_number,
                    condition=condition,
                    confidence=SOURCE_EXACT,
                )
            )
    return SourceFacts(conditions, [], relations)


def extract_source_facts(data: bytes, language: str) -> SourceFacts:
    text = data.decode("utf-8", errors="replace")
    if language in STRUCTURED_LANGUAGES:
        return _extract_c_facts(data, text)
    if language == "kconfig":
        return _extract_kconfig_facts(text)
    if language == "kbuild":
        return _extract_kbuild_facts(text)
    return SourceFacts([], [], [])


def extract_dependency_references(
    data: bytes, language: str
) -> list[DependencyReference]:
    text = data.decode("utf-8", errors="replace")
    references: list[DependencyReference] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if language in STRUCTURED_LANGUAGES:
            match = _INCLUDE_PATTERN.match(line)
            if match:
                references.append(
                    DependencyReference("include", match.group(2), line_number)
                )
        elif language == "kconfig":
            match = re.match(r'^\s*(?:rsource|source)\s+"([^"]+)"', line)
            if match:
                references.append(
                    DependencyReference("kconfig_source", match.group(1), line_number)
                )
        elif language == "kbuild":
            match = re.match(r"^\s*[^#:=]+?(?:\+?=|:=)\s*(.+?)\s*$", line)
            if not match:
                continue
            for target in match.group(1).split():
                if target.endswith((".o", ".a", "/")) or target in {
                    "Makefile",
                    "Kbuild",
                    "Kconfig",
                }:
                    references.append(
                        DependencyReference("kbuild_target", target, line_number)
                    )
    return references


def include_candidates(
    source_path: str,
    target: str,
    available_paths: set[str],
) -> list[str]:
    target = target.replace("\\", "/")
    candidates: set[str] = set()
    relative = (PurePosixPath(source_path).parent / target).as_posix()
    normalized = posixpath.normpath(relative)
    if normalized in available_paths:
        candidates.add(normalized)
    if target in available_paths:
        candidates.add(target)
    include_path = f"include/{target}"
    if include_path in available_paths:
        candidates.add(include_path)
    suffix = f"/{target}"
    for path in available_paths:
        if path.endswith(suffix):
            candidates.add(path)
    return sorted(candidates)
