from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


def run(cmd: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True, capture_output=True)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def safe_repo_dirname(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def repo_name_from_arg(repo: str) -> str:
    from demo.repo import github_repo_label, is_github_url

    if is_github_url(repo):
        return safe_name(github_repo_label(repo))
    p = Path(repo).expanduser().resolve()
    parts = p.parts
    for i, part in enumerate(parts):
        if (
            part == "demo_out"
            and i + 3 < len(parts)
            and parts[i + 2] == "runs"
            and parts[-1] == "repo"
        ):
            return safe_name(parts[i + 1])
    return safe_name(p.name)


def _best_fenced_java_block(text: str) -> Optional[str]:
    blocks = re.findall(r"```(?:java)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if not blocks:
        return None
    for block in blocks:
        if re.search(r"(?m)^\s*(package\s+[\w.]+;|import\s+[\w.*]+;|(?:public\s+)?class\s+\w+)", block):
            return block
    return blocks[0]


def _truncate_after_balanced_class(text: str) -> str:
    class_match = re.search(r"(?m)^\s*(?:public\s+)?class\s+\w+", text)
    if not class_match:
        return text.rstrip()

    first_brace = text.find("{", class_match.end())
    if first_brace == -1:
        return text.rstrip()

    depth = 0
    for i in range(first_brace, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[: i + 1].rstrip()
    return text.rstrip()


def sanitize_java_output(text: str) -> str:
    cleaned = (text or "").strip()
    fenced = _best_fenced_java_block(cleaned)
    if fenced is not None:
        cleaned = fenced.strip()

    # Drop prose before the first real Java declaration. Avoid matching words
    # like "class" inside explanation paragraphs.
    start_re = re.compile(
        r"(?m)^\s*(package\s+[\w.]+;|import\s+[\w.*]+;|(?:public\s+)?class\s+\w+)"
    )
    m = start_re.search(cleaned)
    if m:
        cleaned = cleaned[m.start():]

    cleaned = "\n".join(
        line for line in cleaned.splitlines()
        if not line.strip().startswith(("```", "### "))
    ).strip()
    return _truncate_after_balanced_class(cleaned)


def declared_api_names(source_bundle: str) -> set[str]:
    names: set[str] = set()
    for m in re.finditer(
        r"\b(?:public|protected|private)\s+[\w\<\>\[\],.]+\s+(\w+)\s*\(",
        source_bundle,
    ):
        names.add(m.group(1))
    for m in re.finditer(
        r"\bpublic\s+(?!class|interface|enum|static\s+void\s+main)([\w\<\>\[\],.]+\s+)+(\w+)\s*[;=]",
        source_bundle,
    ):
        names.add(m.group(2))
    return names


def find_concrete_impls(related_sources: str, type_name: str) -> List[str]:
    return re.findall(
        rf"\bclass\s+(\w+)\s+implements\s+[^{{;]*\b{re.escape(type_name)}\b",
        related_sources,
    )


def _has_int_constructor(related_sources: str, class_name: str) -> bool:
    return bool(re.search(rf"\bpublic\s+{re.escape(class_name)}\s*\(\s*int\s+", related_sources))


def _keys_for_test_method(method_name: str, var_count: int = 2) -> List[int]:
    lower = method_name.lower()
    if var_count == 1:
        if "one" in lower:
            return [5]
        return [5]
    if any(k in lower for k in ("secondislarger", "second")):
        return [3, 5]
    if any(k in lower for k in ("firstislarger", "first")):
        return [5, 3]
    if any(k in lower for k in ("less", "smaller")):
        return [20]
    if "equal" in lower:
        return [10]
    if any(k in lower for k in ("greater", "larger")):
        return [5]
    return [5, 3]


def rewrite_interface_mocks_to_concrete(code: str, related_sources: str) -> str:
    mock_decls = re.findall(r"@Mock\s+(?:private\s+)?([A-Z]\w+)\s+(\w+)\s*;", code)
    if not mock_decls:
        return code

    var_to_impl: Dict[str, str] = {}
    for type_name, var in mock_decls:
        impls = find_concrete_impls(related_sources, type_name)
        if impls and _has_int_constructor(related_sources, impls[0]):
            var_to_impl[var] = impls[0]
    if not var_to_impl:
        return code

    kept: List[str] = []
    for line in code.splitlines():
        if any(re.search(rf"@Mock\s+(?:private\s+)?\w+\s+{re.escape(var)}\s*;", line) for var in var_to_impl):
            continue
        if line.strip().startswith("when("):
            continue
        if "MockitoAnnotations" in line:
            continue
        if re.match(r"\s*@Mock\b", line):
            continue
        kept.append(line)

    result: List[str] = []
    i = 0
    while i < len(kept):
        line = kept[i]
        result.append(line)
        if re.match(r"\s*@Test\b", line):
            i += 1
            method_name = ""
            while i < len(kept) and "{" not in kept[i]:
                sig = re.search(r"\bvoid\s+(\w+)\s*\(", kept[i])
                if sig:
                    method_name = sig.group(1)
                result.append(kept[i])
                i += 1
            if i >= len(kept):
                break
            result.append(kept[i])
            indent = re.match(r"(\s*)", kept[i]).group(1) + "    "
            i += 1
            block: List[str] = []
            depth = 1
            while i < len(kept) and depth > 0:
                block.append(kept[i])
                depth += kept[i].count("{") - kept[i].count("}")
                i += 1
            block_text = "\n".join(block)
            used_vars = [var for var in var_to_impl if re.search(rf"\b{re.escape(var)}\b", block_text)]
            if used_vars:
                keys = _keys_for_test_method(method_name, len(used_vars))
                for idx, var in enumerate(used_vars):
                    impl = var_to_impl[var]
                    result.append(f"{indent}{impl} {var} = new {impl}({keys[min(idx, len(keys)-1)]});")
            result.extend(block)
            continue
        i += 1
    return "\n".join(result)


def remove_invented_api_stubs(code: str, source_bundle: str) -> str:
    declared = declared_api_names(source_bundle)
    kept: List[str] = []
    for line in code.splitlines():
        stub = re.search(r"when\([^)]*\.(\w+)\s*\(", line)
        if stub and stub.group(1) not in declared:
            continue
        call = re.search(r"\.(\w+)\s*\(", line)
        if call and call.group(1) not in declared and "when(" not in line:
            # Drop direct calls to invented methods (e.g. mockItem.getKey()).
            if re.search(r"\b(mock|Mockito)", line):
                continue
        kept.append(line)
    return "\n".join(kept)


def validate_test_coverage_quality(
    code: str, target: dict, related_sources: str
) -> Optional[str]:
    sig = target.get("signature") or ""
    for type_name in re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", sig):
        impls = find_concrete_impls(related_sources, type_name)
        if not impls:
            continue
        uses_mock = bool(
            re.search(rf"@Mock\b[^\n]*\b{type_name}\b", code)
            or re.search(rf"when\([^)]*\b{type_name}\b", code)
        )
        uses_concrete = any(f"new {impl}" in code for impl in impls)
        if uses_mock and not uses_concrete:
            primary = impls[0]
            return (
                f"mocks {type_name} instead of real {primary} instances — "
                f"use `new {primary}(...)` so production code executes"
            )
    return None


def enforce_test_class_name(code: str, expected_class_name: str) -> str:
    if re.search(
        rf"(?m)^\s*(?:public\s+)?class\s+{re.escape(expected_class_name)}\b",
        code,
    ):
        return code
    return re.sub(
        r"(?m)^(\s*(?:public\s+)?class\s+)\w+",
        rf"\1{expected_class_name}",
        code,
        count=1,
    )


def ensure_junit_imports(code: str, junit_version: str = "5") -> str:
    if not code.strip():
        return code

    required: List[str] = []
    if junit_version == "4":
        if "@Test" in code and "import org.junit.Test;" not in code:
            required.append("import org.junit.Test;")
        if "@Before" in code and "import org.junit.Before;" not in code:
            required.append("import org.junit.Before;")
        if "@After" in code and "import org.junit.After;" not in code:
            required.append("import org.junit.After;")
        uses_assert = any(
            token in code
            for token in (
                "assertEquals",
                "assertArrayEquals",
                "assertTrue",
                "assertFalse",
                "assertNull",
                "assertNotNull",
                "assertSame",
                "assertNotSame",
            )
        )
        if uses_assert and "import static org.junit.Assert" not in code:
            required.append("import static org.junit.Assert.*;")
    else:
        if "@Test" in code and "import org.junit.jupiter.api.Test;" not in code:
            required.append("import org.junit.jupiter.api.Test;")
        if "@BeforeEach" in code and "import org.junit.jupiter.api.BeforeEach;" not in code:
            required.append("import org.junit.jupiter.api.BeforeEach;")
        if "@AfterEach" in code and "import org.junit.jupiter.api.AfterEach;" not in code:
            required.append("import org.junit.jupiter.api.AfterEach;")

    if not required:
        return code

    lines = code.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("package "):
            insert_at = i + 1
        elif line.strip().startswith("import "):
            insert_at = i + 1

    prefix_blank = insert_at > 0 and insert_at < len(lines) and lines[insert_at].strip()
    to_insert = required + ([""] if prefix_blank else [])
    return "\n".join(lines[:insert_at] + to_insert + lines[insert_at:])


def ensure_junit5_imports(code: str) -> str:
    return ensure_junit_imports(code, "5")


def validate_junit_framework(code: str, junit_version: str) -> Optional[str]:
    if junit_version == "4":
        if re.search(r"\borg\.junit\.jupiter\b", code):
            return "project uses JUnit 4; remove org.junit.jupiter imports and use org.junit.Test / org.junit.Assert"
        if re.search(r"@BeforeEach\b|@AfterEach\b", code):
            return "project uses JUnit 4; use @Before/@After instead of @BeforeEach/@AfterEach"
        return None

    if re.search(r"import\s+org\.junit\.Test\b", code):
        return "project uses JUnit 5; use org.junit.jupiter.api.Test instead of org.junit.Test"
    if re.search(r"import\s+static\s+org\.junit\.Assert\.", code):
        return "project uses JUnit 5; use org.junit.jupiter.api.Assertions instead of org.junit.Assert"
    return None


def validate_java_test_output(code: str, expected_class_name: Optional[str] = None) -> Optional[str]:
    cleaned = (code or "").strip()
    if not cleaned:
        return "empty output"
    if "```" in cleaned:
        return "contains markdown code fences"
    if re.search(r"(?m)^\s*#{1,6}\s+", cleaned):
        return "contains markdown headings"
    if re.search(r"\b(Here'?s|breakdown|explanation|Below is)\b", cleaned, flags=re.IGNORECASE):
        return "contains explanatory prose"
    if not re.search(r"(?m)^\s*(?:public\s+)?class\s+\w+", cleaned):
        return "missing Java test class declaration"
    if expected_class_name and not re.search(
        rf"(?m)^\s*(?:public\s+)?class\s+{re.escape(expected_class_name)}\b", cleaned
    ):
        return f"missing expected test class {expected_class_name}"
    if "@Test" not in cleaned:
        return "missing @Test methods"
    return None


def ensure_unique_run_class_name(base: str, used: set[str], index: int) -> str:
    if base not in used:
        return base

    if base.endswith("Test"):
        prefix = base[:-4]
        suffix = "Test"
    else:
        prefix = base
        suffix = ""

    candidate = f"{prefix}_M{index}{suffix}"
    counter = 1
    while candidate in used:
        candidate = f"{prefix}_M{index}_{counter}{suffix}"
        counter += 1
    return candidate
