from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from demo.coverage.java_version import java_version_guidance
from demo.packages import list_java_files
from demo.static_analysis import related_type_sources_from_analysis
from demo.utils import find_concrete_impls

TYPE_NAME_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")
SKIP_RELATED_TYPES = frozenset(
    {
        "String", "Integer", "Boolean", "Long", "Double", "Float", "Short", "Byte",
        "Character", "Object", "Class", "Void", "Override", "Test", "BeforeEach",
        "AfterEach", "Mock", "InjectMocks", "Collection", "List", "Map", "Set",
        "Optional", "Arrays", "Collections", "Assertions",
    }
)


def collect_related_type_sources(project_root: Path, target: Dict) -> str:
    own_class = target.get("class_name") or ""
    text = " ".join(
        [
            target.get("signature") or "",
            target.get("snippet") or "",
            own_class,
        ]
    )
    related_names = {
        name for name in TYPE_NAME_RE.findall(text)
        if name not in SKIP_RELATED_TYPES and name != own_class
    }
    if not related_names:
        return ""

    by_name: Dict[str, str] = {}
    for f in list_java_files(project_root):
        if f.stem not in related_names or f.stem in by_name:
            continue
        try:
            rel = f.relative_to(project_root)
            by_name[f.stem] = f"// {rel}\n{f.read_text(encoding='utf-8', errors='ignore')}"
        except (OSError, ValueError):
            continue

    interface_names = [
        name for name, src in by_name.items()
        if re.search(rf"\binterface\s+{re.escape(name)}\b", src)
    ]
    if interface_names:
        for f in list_java_files(project_root):
            if f.stem in by_name:
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for iface in interface_names:
                if re.search(
                    rf"\bclass\s+{re.escape(f.stem)}\s+implements\s+[^{{;]*\b{re.escape(iface)}\b",
                    txt,
                ):
                    rel = f.relative_to(project_root)
                    by_name[f.stem] = f"// {rel}\n{txt}"
                    break

    return "\n\n".join(by_name[name] for name in sorted(by_name))


def resolve_related_sources(
    project_root: Path,
    ast_analysis: Dict | None,
    target: Dict,
) -> str:
    if ast_analysis is not None:
        return related_type_sources_from_analysis(ast_analysis, target, project_root=project_root)
    return collect_related_type_sources(project_root, target)


def looks_like_java_test_file(text: str) -> bool:
    sample = (text or "").lstrip()
    return bool(
        re.search(r"(?m)^\s*package\s+[\w.]+\s*;", sample)
        or re.search(r"(?m)^\s*import\s+", sample)
        or re.search(r"\bclass\s+\w+Test\b", sample)
    )


def build_direct_generation_prompt(
    target: Dict,
    test_class: str,
    project_types_text: str,
    java_version: str,
    junit_version: str,
    has_mockito: bool,
) -> str:
    pkg = target.get("package") or "(default)"
    mockito_rule = (
        "Mockito is available, but do NOT mock domain/value interfaces when a concrete implementation exists. "
        "Prefer real objects so production code executes."
        if has_mockito
        else "Mockito is not available; do not import or use org.mockito, @Mock, when, verify, or MockitoAnnotations."
    )
    junit_rule = (
        "Use org.junit.Test and static org.junit.Assert.*. Do not use JUnit Jupiter."
        if junit_version == "4"
        else "Use org.junit.jupiter.api.Test and org.junit.jupiter.api.Assertions.*. Do not use org.junit.Test."
    )
    return f"""
Output ONLY a complete Java test file. No markdown. No explanations.
Generate test class exactly: {test_class}
Package: {pkg}
Target Java version: {java_version}. {java_version_guidance(java_version)}
Target framework: JUnit {junit_version}. {junit_rule}
{mockito_rule}

Hard rules:
- Every @Test must call real production code from target class {target.get("class_name")}.
- Do not mock the class under test.
- For interface parameters, use a concrete implementation from the project type context when present.
- Do not put null as the first element of arrays unless the test explicitly expects NullPointerException.
- For Item[] or similar arrays, create non-null concrete Item implementations for all normal-path tests.
- Use only methods, fields, and constructors shown in the source/AST/project type context.
- At least 3 @Test methods with concrete assertions.

Target:
- class: {target.get("class_name")}
- method: {target.get("method_name") or "(class mode)"}
- signature: {target.get("signature") or "(entire class)"}

Source/AST summary:
{target.get("snippet") or ""}

Project type context:
{project_types_text}
""".strip()


def append_related_sources(prompt: str, related_sources: str, target: Dict) -> str:
    if not related_sources:
        return prompt

    impl_hints: List[str] = []
    sig_text = target.get("signature") or ""
    for type_name in TYPE_NAME_RE.findall(sig_text):
        for impl in find_concrete_impls(related_sources, type_name):
            impl_hints.append(
                f"For `{type_name}` parameters, use `new {impl}(...)` — do NOT @Mock `{type_name}`."
            )
    updated = (
        f"{prompt}\n\n"
        "Related type sources (use ONLY APIs shown here; do not invent methods):\n"
        f"{related_sources}"
    )
    if impl_hints:
        updated += "\n\n" + "\n".join(dict.fromkeys(impl_hints))
    return updated
