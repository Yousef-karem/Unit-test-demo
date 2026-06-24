from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import re
import xml.etree.ElementTree as ET


@dataclass
class UncoveredBranch:
    class_name: str
    method_signature: str
    behavior: str
    missed_branches: int


@dataclass
class UncoveredStatement:
    kind: str
    code: str
    behavior: str
    behavior_type: str
    missed_branches: int = 0
    missed_instructions: int = 0
    context: List[str] = field(default_factory=list)


@dataclass
class UncoveredMethod:
    package_name: str
    class_name: str
    method_name: str
    source_file: str
    uncovered_lines: List[int] = field(default_factory=list)
    missed_branch_lines: Dict[int, int] = field(default_factory=dict)
    uncovered_statements: List[UncoveredStatement] = field(default_factory=list)
    uncovered_source: str = ""
    signature: str = ""

    @property
    def display_class_name(self) -> str:
        return f"{self.package_name}.{self.class_name}" if self.package_name else self.class_name


class CoverageAnalyzer:
    def __init__(self, xml_path: Path, project_root: Path, ast_analysis: Optional[Dict] = None):
        self.xml_path = xml_path
        self.project_root = project_root
        self.ast_analysis = ast_analysis or {}
        self.tree = ET.parse(str(xml_path))
        self.root = self.tree.getroot()
        self._class_index = self._build_class_index()

    def getLineCoverage(self) -> float:
        return self._coverage_for_counter("LINE")

    def getBranchCoverage(self) -> float:
        return self._coverage_for_counter("BRANCH")

    def getInstructionCoverage(self) -> float:
        return self._coverage_for_counter("INSTRUCTION")

    def getUncoveredMethods(self) -> List[UncoveredMethod]:
        grouped: Dict[Tuple[str, str, str], UncoveredMethod] = {}

        for package in self.root.findall("package"):
            package_name = (package.attrib.get("name") or "").replace("/", ".")
            for sourcefile in package.findall("sourcefile"):
                filename = sourcefile.attrib.get("name", "")
                if not filename:
                    continue
                class_info = self._class_for_sourcefile(package_name, filename)
                if not class_info:
                    continue

                uncovered_lines = []
                for line in sourcefile.findall("line"):
                    nr = int(line.attrib.get("nr", "0"))
                    mi = int(line.attrib.get("mi", "0"))
                    mb = int(line.attrib.get("mb", "0"))
                    if nr and (mi > 0 or mb > 0):
                        uncovered_lines.append((nr, mi, mb))

                if not uncovered_lines:
                    continue

                for nr, missed_instructions, missed_branches in uncovered_lines:
                    method = self._method_for_line(class_info, nr)
                    if method is None:
                        continue
                    if self._is_ignorable_empty_constructor(class_info, method):
                        continue

                    method_name = method["name"]
                    signature = method.get("signature", method_name)
                    key = (package_name, class_info["class_name"], signature)
                    item = grouped.get(key)
                    if item is None:
                        item = UncoveredMethod(
                            package_name=package_name,
                            class_name=class_info["class_name"],
                            method_name=method_name,
                            source_file=str((self.project_root / class_info["file_path"]).resolve()),
                            signature=signature,
                        )
                        grouped[key] = item
                    item.uncovered_lines.append(nr)
                    if missed_branches > 0:
                        item.missed_branch_lines[nr] = item.missed_branch_lines.get(nr, 0) + missed_branches
                    self._add_uncovered_statement(
                        item=item,
                        method=method,
                        line=nr,
                        missed_instructions=missed_instructions,
                        missed_branches=missed_branches,
                    )

        for item in grouped.values():
            item.uncovered_lines = sorted(set(item.uncovered_lines))
            item.missed_branch_lines = dict(sorted(item.missed_branch_lines.items()))
            item.uncovered_source = self._extract_source_lines(item.source_file, item.uncovered_lines)
            item.uncovered_statements = self._dedupe_and_sort_statements(item.uncovered_statements)

        return sorted(
            grouped.values(),
            key=self._priority_key,
        )

    def getUncoveredBranches(self) -> List[UncoveredBranch]:
        branches: List[UncoveredBranch] = []
        for method in self.getUncoveredMethods():
            for stmt in method.uncovered_statements:
                if stmt.missed_branches <= 0:
                    continue
                branches.append(
                    UncoveredBranch(
                        class_name=method.display_class_name,
                        method_signature=method.signature,
                        behavior=stmt.behavior,
                        missed_branches=stmt.missed_branches,
                    )
                )
        return branches

    def compact_uncovered_text(self, limit: int = 12) -> str:
        methods = self.getUncoveredMethods()[:limit]
        if not methods:
            return "(no uncovered executable lines found)"
        blocks: List[str] = []
        for method in methods:
            branch_count = sum(method.missed_branch_lines.values())
            blocks.append(
                "\n".join(
                    [
                        f"Class: {method.display_class_name}",
                        f"Method: {method.signature}",
                        f"Missed Branches: {branch_count}",
                        "Missing Behaviors:",
                        self._format_statement_list(method.uncovered_statements),
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _coverage_for_counter(self, counter_type: str) -> float:
        counter = self.root.find(f"counter[@type='{counter_type}']")
        if counter is None:
            return 0.0
        missed = int(counter.attrib.get("missed", "0"))
        covered = int(counter.attrib.get("covered", "0"))
        total = missed + covered
        return covered / total if total else 0.0

    def _build_class_index(self) -> Dict[Tuple[str, str], Dict]:
        index: Dict[Tuple[str, str], Dict] = {}
        for fqcn, class_info in (self.ast_analysis.get("classes") or {}).items():
            file_path = (class_info.get("filePath") or "").replace("\\", "/")
            if not file_path or "/src/test/" in f"/{file_path}":
                continue
            package_name, class_name = self._split_fqcn(fqcn)
            filename = Path(file_path).name
            methods = []
            for signature, method in (class_info.get("methods") or {}).items():
                start = method.get("startLine")
                end = method.get("endLine")
                if start is None or end is None:
                    continue
                methods.append(
                    {
                        "name": method.get("name") or signature.split("(", 1)[0],
                        "signature": signature,
                        "start": int(start),
                        "end": int(end),
                        "kind": "method",
                        "sourceSnippet": "",
                        "astTree": (method.get("ast") or {}).get("astTree"),
                        "executableStatements": [],
                    }
                )
            for ctor in class_info.get("constructors") or []:
                start = ctor.get("startLine")
                end = ctor.get("endLine")
                if start is None or end is None:
                    continue
                methods.append(
                    {
                        "name": "<init>",
                        "signature": ctor.get("signature", "<init>()"),
                        "start": int(start),
                        "end": int(end),
                        "kind": "constructor",
                        "sourceSnippet": ctor.get("sourceSnippet") or "",
                        "astTree": None,
                        "executableStatements": [],
                    }
                )
            methods.sort(key=lambda m: (m["start"], m["end"]))
            source_path = self.project_root / file_path
            try:
                source_lines = source_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                source_lines = []
            for method in methods:
                method["executableStatements"] = self._executable_statements(method, source_lines)
            index[(package_name, filename)] = {
                "class_name": class_name,
                "file_path": file_path,
                "methods": methods,
                "constructors": class_info.get("constructors") or [],
            }
        return index

    def _class_for_sourcefile(self, package_name: str, filename: str) -> Optional[Dict]:
        return self._class_index.get((package_name, filename))

    def _method_for_line(self, class_info: Dict, line: int) -> Optional[Dict]:
        for method in class_info.get("methods") or []:
            if method["start"] <= line <= method["end"]:
                return method
        return None

    def _is_ignorable_empty_constructor(self, class_info: Dict, method: Dict) -> bool:
        if method.get("kind") != "constructor" or method.get("name") != "<init>":
            return False
        snippet = method.get("sourceSnippet") or ""
        if not snippet:
            # No explicit constructor in AST means JaCoCo is reporting the implicit empty constructor.
            return True
        body_match = re.search(r"\{(?P<body>.*)\}\s*$", snippet, flags=re.DOTALL)
        if not body_match:
            return False
        body = re.sub(r"//.*|/\*.*?\*/", "", body_match.group("body"), flags=re.DOTALL).strip()
        return body == ""

    def _add_uncovered_statement(
        self,
        item: UncoveredMethod,
        method: Dict,
        line: int,
        missed_instructions: int,
        missed_branches: int,
    ) -> None:
        statements = method.get("executableStatements") or []
        owner = self._owning_statement(statements, line)
        if owner is None:
            code = self._extract_plain_source_lines(item.source_file, [line])
            if not code or re.fullmatch(r"[{};]+", code.strip()):
                return
            behavior, behavior_type = self._describe_behavior("Statement", code)
            item.uncovered_statements.append(
                UncoveredStatement(
                    kind="Statement",
                    code=code,
                    behavior=behavior,
                    behavior_type=behavior_type,
                    missed_branches=missed_branches,
                    missed_instructions=missed_instructions,
                    context=[code] if code else [],
                )
            )
            return

        behavior, behavior_type = self._describe_behavior(owner["kind"], owner["code"])
        idx = statements.index(owner)
        context = [
            self._compact_statement(s)
            for s in statements[max(0, idx - 2) : min(len(statements), idx + 3)]
        ]
        item.uncovered_statements.append(
            UncoveredStatement(
                kind=owner["kind"],
                code=self._compact_code(owner["code"]),
                behavior=behavior,
                behavior_type=behavior_type,
                missed_branches=missed_branches,
                missed_instructions=missed_instructions,
                context=context,
            )
        )

    def _executable_statements(self, method: Dict, source_lines: List[str]) -> List[Dict]:
        ast_tree = method.get("astTree")
        if not ast_tree or not source_lines:
            return []
        nodes = self._flatten_executable_nodes(ast_tree)
        statements: List[Dict] = []
        for node in nodes:
            span = self._find_node_span(
                source_lines=source_lines,
                start_line=method["start"],
                end_line=method["end"],
                code=node.get("code") or "",
            )
            if span is None:
                continue
            statements.append(
                {
                    "kind": node.get("kind", "Statement"),
                    "code": node.get("code") or "",
                    "start": span[0],
                    "end": span[1],
                }
            )
        statements.sort(key=lambda s: (s["start"], s["end"], len(s["code"])))
        return statements

    def _flatten_executable_nodes(self, node: Dict) -> List[Dict]:
        executable_kinds = {
            "IfStmt",
            "SwitchStmt",
            "SwitchEntry",
            "CatchClause",
            "ThrowStmt",
            "ReturnStmt",
            "ForStmt",
            "ForeachStmt",
            "ForEachStmt",
            "WhileStmt",
            "DoStmt",
            "ExpressionStmt",
            "TryStmt",
            "AssertStmt",
            "BreakStmt",
            "ContinueStmt",
        }
        result: List[Dict] = []
        if node.get("kind") in executable_kinds and node.get("code"):
            result.append(node)
        for child in node.get("children") or []:
            result.extend(self._flatten_executable_nodes(child))
        return result

    def _find_node_span(
        self,
        source_lines: List[str],
        start_line: int,
        end_line: int,
        code: str,
    ) -> Optional[Tuple[int, int]]:
        wanted = self._normalize_code(code)
        if not wanted:
            return None
        best: Optional[Tuple[int, int]] = None
        for start in range(start_line, end_line + 1):
            chunk = ""
            for end in range(start, end_line + 1):
                if 1 <= end <= len(source_lines):
                    chunk = f"{chunk}\n{source_lines[end - 1]}" if chunk else source_lines[end - 1]
                normalized = self._normalize_code(chunk)
                if normalized == wanted or wanted in normalized:
                    candidate = (start, end)
                    if best is None or (candidate[1] - candidate[0]) < (best[1] - best[0]):
                        best = candidate
                    break
        return best

    def _owning_statement(self, statements: List[Dict], line: int) -> Optional[Dict]:
        candidates = [s for s in statements if s["start"] <= line <= s["end"]]
        if not candidates:
            return None
        return min(candidates, key=lambda s: (s["end"] - s["start"], self._kind_rank(s["kind"])))

    def _kind_rank(self, kind: str) -> int:
        ranks = {
            "ThrowStmt": 0,
            "ReturnStmt": 0,
            "CatchClause": 1,
            "SwitchEntry": 1,
            "IfStmt": 2,
            "WhileStmt": 2,
            "ForStmt": 2,
            "ForeachStmt": 2,
            "ForEachStmt": 2,
            "DoStmt": 2,
            "SwitchStmt": 3,
            "TryStmt": 4,
            "ExpressionStmt": 5,
        }
        return ranks.get(kind, 9)

    def _describe_behavior(self, kind: str, code: str) -> Tuple[str, str]:
        compact = self._compact_code(code)
        lower = compact.lower()
        condition = self._extract_condition(compact)

        if kind == "CatchClause" or " catch " in f" {lower} ":
            caught = self._extract_caught_exception(compact)
            return f"{caught} catch block" if caught else "catch block", "Exception path"
        if kind == "ThrowStmt" or lower.startswith("throw "):
            thrown = self._extract_thrown_exception(compact)
            return f"{thrown} path" if thrown else "exception path", "Exception path"
        if "== null" in lower or "!= null" in lower:
            return condition or "null condition", "Null handling"
        if "isempty()" in lower or "size() == 0" in lower or ".length == 0" in lower:
            return condition or "empty collection condition", "Empty collection"
        if kind in {"ForStmt", "ForeachStmt", "ForEachStmt", "WhileStmt", "DoStmt"}:
            return condition or "loop execution path", "Loop path"
        if kind in {"SwitchStmt", "SwitchEntry"}:
            if "default" in lower:
                return "default switch branch", "Switch branch"
            return compact.split(":", 1)[0].strip() or "switch branch", "Switch branch"
        if kind == "IfStmt":
            behavior_type = "Boolean branch"
            if any(token in lower for token in ("full", "cheia", "capacity", "length")):
                behavior_type = "Boundary condition"
            if any(token in lower for token in ("erro", "error", "invalid")):
                behavior_type = "Error handling"
            return condition or compact, behavior_type
        if any(op in compact for op in ("==", "<=", ">=", "<", ">")):
            return condition or compact, "Boundary condition"
        if lower.startswith("return "):
            return compact, "Boolean branch" if "?" in compact or "true" in lower or "false" in lower else "Boundary condition"
        return compact or "uncovered statement", "Boundary condition"

    def _extract_condition(self, code: str) -> str:
        keyword_match = re.search(r"\b(if|while|for)\s*\(", code)
        if not keyword_match:
            return ""
        keyword = keyword_match.group(1)
        open_idx = code.find("(", keyword_match.end() - 1)
        depth = 0
        close_idx = -1
        for idx in range(open_idx, len(code)):
            ch = code[idx]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_idx = idx
                    break
        if close_idx == -1:
            return ""
        condition = code[open_idx + 1 : close_idx].strip()
        if keyword == "for":
            parts = [part.strip() for part in condition.split(";")]
            if len(parts) == 3 and parts[1]:
                condition = parts[1]
        return self._compact_code(condition)

    def _extract_thrown_exception(self, code: str) -> str:
        match = re.search(r"new\s+([A-Za-z_][A-Za-z0-9_.]*)", code)
        return match.group(1) if match else ""

    def _extract_caught_exception(self, code: str) -> str:
        match = re.search(r"catch\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)", code)
        return match.group(1) if match else ""

    def _dedupe_and_sort_statements(self, statements: List[UncoveredStatement]) -> List[UncoveredStatement]:
        by_key: Dict[Tuple[str, str], UncoveredStatement] = {}
        for stmt in statements:
            key = (stmt.kind, stmt.code)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = stmt
                continue
            existing.missed_branches += stmt.missed_branches
            existing.missed_instructions += stmt.missed_instructions
        return sorted(
            by_key.values(),
            key=lambda s: (-s.missed_branches, s.behavior_type, s.behavior, s.code),
        )

    def _priority_key(self, method: UncoveredMethod) -> Tuple[int, int, int, str, str, str]:
        missed_branches = sum(method.missed_branch_lines.values())
        missing_behaviors = len({s.behavior for s in method.uncovered_statements})
        executable_statements = len(method.uncovered_statements)
        return (
            -missed_branches,
            -missing_behaviors,
            -executable_statements,
            method.package_name,
            method.class_name,
            method.signature,
        )

    def _format_statement_list(self, statements: List[UncoveredStatement]) -> str:
        if not statements:
            return "(none)"
        return "\n".join(
            f"- behavior: {s.behavior}; type: {s.behavior_type}; missed branches: {s.missed_branches}; context: "
            + " | ".join(s.context[:5])
            for s in statements
        )

    def _compact_statement(self, statement: Dict) -> str:
        return f"{statement.get('kind', 'Statement')}: {self._compact_code(statement.get('code') or '')}"

    def _compact_code(self, code: str, limit: int = 220) -> str:
        compact = re.sub(r"\s+", " ", code or "").strip()
        if len(compact) > limit:
            return compact[: limit - 3] + "..."
        return compact

    def _normalize_code(self, code: str) -> str:
        without_comments = re.sub(r"//.*|/\*.*?\*/", "", code or "", flags=re.DOTALL)
        normalized = re.sub(r"\s+", " ", without_comments).strip()
        normalized = re.sub(r"\s+([();,])", r"\1", normalized)
        normalized = re.sub(r"([({])\s+", r"\1", normalized)
        return normalized

    def _extract_source_lines(self, source_file: str, line_numbers: List[int]) -> str:
        try:
            lines = Path(source_file).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return ""
        selected = []
        for nr in line_numbers:
            if 1 <= nr <= len(lines):
                selected.append(f"{nr}: {lines[nr - 1].rstrip()}")
        return "\n".join(selected)

    def _extract_plain_source_lines(self, source_file: str, line_numbers: List[int]) -> str:
        try:
            lines = Path(source_file).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return ""
        selected = []
        for nr in line_numbers:
            if 1 <= nr <= len(lines):
                selected.append(lines[nr - 1].strip())
        return " ".join(selected)

    @staticmethod
    def _split_fqcn(fqcn: str) -> Tuple[str, str]:
        if "." not in fqcn:
            return "", fqcn
        return fqcn.rsplit(".", 1)
