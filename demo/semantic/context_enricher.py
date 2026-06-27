from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from demo.static_analysis import method_ast_summary
from demo.targets import extract_imports_context


def slice_source_lines(source_file: str | Path, start_line: int | None, end_line: int | None) -> str:
    if start_line is None or end_line is None:
        return ""
    try:
        lines = Path(source_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    if start_line < 1 or end_line < start_line:
        return ""
    return "\n".join(lines[start_line - 1 : end_line])


def enrich_method_source(
    target: Dict,
    class_info: Dict,
    method_info: Dict,
    fqcn: str,
    signature_key: str,
) -> str:
    snippet = (method_info.get("sourceSnippet") or "").strip()
    if snippet:
        return snippet

    source_file = target.get("source_file")
    start = method_info.get("startLine")
    end = method_info.get("endLine")
    if start is None:
        start = target.get("start_line")
    if end is None:
        end = target.get("end_line")
    if source_file and start is not None and end is not None:
        sliced = slice_source_lines(source_file, int(start), int(end))
        if sliced.strip():
            return sliced

    if method_info and signature_key:
        return method_ast_summary(fqcn, signature_key, method_info)
    return target.get("snippet") or ""


def enrich_imports_context(target: Dict) -> str:
    return extract_imports_context(target)


def enrich_private_method_sources(
    class_info: Dict,
    private_keys: List[str],
    source_file: str | Path | None,
) -> Dict[str, str]:
    sources: Dict[str, str] = {}
    methods = class_info.get("methods") or {}
    for key in private_keys:
        method_info = methods.get(key)
        if not method_info:
            called_name = key.split("(")[0]
            for sig, info in methods.items():
                if sig.split("(")[0] == called_name:
                    method_info = info
                    break
        if not method_info:
            continue

        snippet = (method_info.get("sourceSnippet") or "").strip()
        if not snippet and source_file:
            start = method_info.get("startLine")
            end = method_info.get("endLine")
            if start is not None and end is not None:
                snippet = slice_source_lines(source_file, int(start), int(end))
        if snippet:
            sources[key] = snippet
    return sources


_PRINTLN_LITERAL_RE = re.compile(
    r'println\s*\(\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*(?:\+|\)|,)',
    re.MULTILINE,
)


def _literals_from_ast_tree(node: Optional[Dict], found: List[str]) -> None:
    if not node:
        return
    code = node.get("code") or ""
    for match in _PRINTLN_LITERAL_RE.finditer(code):
        literal = match.group(1).replace('\\"', '"').replace("\\n", "\n")
        if literal and literal not in found:
            found.append(literal)
    for child in node.get("children") or []:
        _literals_from_ast_tree(child, found)


def extract_literal_outputs(ast_dict: Dict, method_source: str = "") -> List[str]:
    found: List[str] = []
    _literals_from_ast_tree(ast_dict.get("astTree"), found)

    text = method_source or ""
    for match in _PRINTLN_LITERAL_RE.finditer(text):
        literal = match.group(1).replace('\\"', '"')
        if literal and literal not in found:
            found.append(literal)

    for match in re.finditer(r'System\.out\.println\s*\(\s*"([^"]+)"\s*\+', text):
        literal = match.group(1)
        if literal and literal not in found:
            found.append(literal)

    return found
