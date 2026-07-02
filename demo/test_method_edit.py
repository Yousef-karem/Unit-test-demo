from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from demo.coverage.maven import strip_ansi


def add_throws_exception_to_test_methods(code: str, compiler_errors: str) -> str:
    if "unreported exception" not in compiler_errors:
        return code

    def fix_signature(match: re.Match) -> str:
        signature = match.group(0)
        if " throws " in signature:
            return signature
        return signature[:-1].rstrip() + " throws Exception {"

    return re.sub(
        r"(?m)^\s*(?:public\s+)?void\s+[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)\s*\{",
        fix_signature,
        code,
    )


@dataclass
class MethodPruneResult:
    pruned_methods: List[str]
    remaining_methods: List[str]
    code: str
    whole_file_reject: bool = False
    reason: str = ""


_IMPORT_RULES: Tuple[Tuple[str, str], ...] = (
    (r"\bPrintStream\b", "import java.io.PrintStream;"),
    (r"\bByteArrayOutputStream\b", "import java.io.ByteArrayOutputStream;"),
    (r"\bNullPointerException\b", "import java.lang.NullPointerException;"),
    (r"\bIOException\b", "import java.io.IOException;"),
    (r"\bException\b", "import java.lang.Exception;"),
)

_METHOD_NAME_RE = re.compile(r"\bvoid\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _iter_test_method_spans(lines: List[str]) -> List[Tuple[str, int, int]]:
    """Return (method_name, start_line_idx, end_line_idx_exclusive) for each @Test method."""
    spans: List[Tuple[str, int, int]] = []
    i = 0
    while i < len(lines):
        if not re.match(r"\s*@Test\b", lines[i]):
            i += 1
            continue
        start = i
        i += 1
        method_name = ""
        while i < len(lines):
            sig = _METHOD_NAME_RE.search(lines[i])
            if sig:
                method_name = sig.group(1)
            if "{" in lines[i]:
                break
            i += 1
        if i >= len(lines):
            break
        depth = 0
        while i < len(lines):
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
            if depth <= 0:
                break
        if method_name:
            spans.append((method_name, start, i))
    return spans


def fix_expected_exception_annotations(code: str) -> str:
    def repl(match: re.Match) -> str:
        exc = match.group(1)
        if exc.endswith(".class"):
            return match.group(0)
        return f"@Test(expected = {exc}.class)"

    return re.sub(
        r"@Test\s*\(\s*expected\s*=\s*([A-Za-z_][\w.]*)\s*\)",
        repl,
        code,
    )


def count_test_methods(code: str) -> int:
    return len(extract_test_methods(code))


def extract_test_methods(code: str) -> Dict[str, str]:
    lines = code.splitlines()
    methods: Dict[str, str] = {}
    for method_name, start, end in _iter_test_method_spans(lines):
        methods[method_name] = "\n".join(lines[start:end])
    return methods


def method_line_ranges(code: str) -> Dict[str, Tuple[int, int]]:
    lines = code.splitlines()
    ranges: Dict[str, Tuple[int, int]] = {}
    for method_name, start, end in _iter_test_method_spans(lines):
        ranges[method_name] = (start + 1, end)
    return ranges


def remove_test_methods(code: str, method_names: Iterable[str]) -> str:
    to_remove = set(method_names)
    if not to_remove:
        return code
    lines = code.splitlines()
    remove_spans = {
        (start, end)
        for method_name, start, end in _iter_test_method_spans(lines)
        if method_name in to_remove
    }
    kept: List[str] = []
    i = 0
    while i < len(lines):
        skipped = False
        for start, end in remove_spans:
            if i == start:
                i = end
                skipped = True
                break
        if skipped:
            continue
        kept.append(lines[i])
        i += 1
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def compile_errors_for_file(log: str, file_path: Path) -> List[Tuple[int, int]]:
    log = strip_ansi(log)
    needle = file_path.name
    errors: List[Tuple[int, int]] = []
    pattern = re.compile(
        rf"(?m)^\[ERROR\]\s+.*{re.escape(needle)}:\[(\d+),(\d+)\]"
    )
    for match in pattern.finditer(log):
        errors.append((int(match.group(1)), int(match.group(2))))
    return errors


def methods_for_error_lines(code: str, log: str, file_path: Path) -> Set[str]:
    ranges = method_line_ranges(code)
    error_lines = {line for line, _col in compile_errors_for_file(log, file_path)}
    matched: Set[str] = set()
    for method_name, (start, end) in ranges.items():
        if any(start <= line <= end for line in error_lines):
            matched.add(method_name)
    return matched


def unmapped_error_lines(code: str, log: str, file_path: Path) -> List[int]:
    ranges = method_line_ranges(code)
    method_lines: Set[int] = set()
    for start, end in ranges.values():
        method_lines.update(range(start, end + 1))
    unmapped: List[int] = []
    for line, _col in compile_errors_for_file(log, file_path):
        if line not in method_lines:
            unmapped.append(line)
    return unmapped


def summarize_compile_errors(compile_log: str, file_name: str) -> str:
    """Build a concise, explicit error summary for LLM repair prompts."""
    log = strip_ansi(compile_log)
    lines = [ln for ln in log.splitlines() if file_name in ln]
    error_lines = [ln for ln in lines if "[ERROR]" in ln]
    hints: List[str] = []
    joined = "\n".join(lines)
    if (
        "cannot be applied to given types" in joined
        and ("required: no arguments" in joined or "found:" in joined)
    ):
        for match in re.finditer(r"constructor (\w+) in class", joined):
            cls = match.group(1)
            block = joined[match.start() : match.start() + 500]
            if "required: no arguments" in block or re.search(r"found:\s*\w+", block):
                hints.append(
                    f"- WRONG_CONSTRUCTOR: `{cls}` only has a no-argument constructor; "
                    f"replace every `new {cls}(...)` with `new {cls}()`."
                )
    if "unreported exception" in joined:
        hints.append("- ADD_THROWS: add `throws Exception` to @Test method signatures that call throwing code.")
    for match in re.finditer(r"symbol:\s+method (\w+)\(", joined):
        typo = match.group(1)
        hints.append(f"- UNKNOWN_METHOD: compiler cannot resolve method `{typo}(...)` — fix typo or use a real API from source.")
    if "Console.in" in joined or "variable in" in joined:
        hints.append(
            "- STDOUT_HELPER: do not use `Console.in` to restore System.out; "
            "save `PrintStream originalOut = System.out` before setOut and restore in finally."
        )
    for match in re.finditer(r"symbol:\s+class (\w+)", joined):
        inner = match.group(1)
        hints.append(
            f"- NESTED_CLASS: `{inner}` is likely a nested type — use the qualified name "
            f"(e.g. `Outer.{inner}`) or `Outer.{inner}` in declarations and `new Outer.{inner}(...)`."
        )
    if not hints:
        return "\n".join(error_lines[-12:]) if error_lines else compile_log[-2000:]
    return "ERROR SUMMARY (follow these exactly):\n" + "\n".join(hints) + "\n\nRaw compiler lines:\n" + "\n".join(error_lines[-12:])


def _public_method_names(source_text: str) -> Set[str]:
    if not source_text:
        return set()
    return set(
        re.findall(
            r"\b(?:public|protected)\s+(?:static\s+)?(?:[\w.<>\[\]]+\s+)+(\w+)\s*\(",
            source_text,
        )
    )


def fix_method_name_typos(code: str, source_text: str) -> str:
    known = _public_method_names(source_text)
    if not known:
        return code

    def closest(name: str) -> Optional[str]:
        if name in known:
            return name
        candidates = [m for m in known if m.startswith(name[:3]) or name.startswith(m[:3])]
        if len(candidates) == 1:
            return candidates[0]
        for m in known:
            if len(name) == len(m) and sum(a != b for a, b in zip(name, m)) == 1:
                return m
        return None

    def repl(match: re.Match) -> str:
        obj = match.group(1)
        method = match.group(2)
        fixed = closest(method)
        if fixed and fixed != method:
            return f"{obj}.{fixed}("
        return match.group(0)

    return re.sub(r"(\b\w+)\.(\w+)\s*\(", repl, code)


def fix_wrong_constructors_from_source(code: str, source_text: str) -> str:
    """Rewrite `new Foo(args)` -> `new Foo()` when source only declares no-arg constructors."""
    if not source_text:
        return code
    fixed = code
    for class_match in re.finditer(r"\bclass\s+(\w+)", source_text):
        cls = class_match.group(1)
        ctor_params = re.findall(rf"\b{re.escape(cls)}\s*\(([^)]*)\)\s*\{{", source_text)
        if not ctor_params:
            continue
        if all(not p.strip() for p in ctor_params):
            fixed = re.sub(rf"\bnew\s+{re.escape(cls)}\s*\([^)]+\)", f"new {cls}()", fixed)
    return fixed


def fix_wrong_constructors(code: str, compile_log: str) -> str:
    log = strip_ansi(compile_log)
    if "required: no arguments" not in log:
        return code
    no_arg_classes: Set[str] = set()
    for match in re.finditer(
        r"constructor (\w+) in class [\w.]+\.\1 cannot be applied to given types;\s*\n\s*required: no arguments",
        log,
    ):
        no_arg_classes.add(match.group(1))
    if not no_arg_classes:
        for match in re.finditer(
            r"constructor (\w+) in class [\w.]+ cannot be applied",
            log,
        ):
            ctx = log[match.start() : match.start() + 400]
            if "required: no arguments" in ctx:
                no_arg_classes.add(match.group(1))
    fixed = code
    for cls in no_arg_classes:
        fixed = re.sub(rf"\bnew\s+{re.escape(cls)}\s*\([^)]*\)", f"new {cls}()", fixed)
    return fixed


def nested_class_map(source_text: str) -> Dict[str, str]:
    """Map inner class simple names to Outer.Inner from production source."""
    if not source_text:
        return {}
    outer_match = re.search(r"\bclass\s+(\w+)", source_text)
    if not outer_match:
        return {}
    outer = outer_match.group(1)
    nested: Dict[str, str] = {}
    for match in re.finditer(r"\b(?:public\s+)?(?:static\s+)?class\s+(\w+)", source_text):
        inner = match.group(1)
        if inner != outer:
            nested[inner] = f"{outer}.{inner}"
    return nested


def fix_nested_class_references(code: str, source_text: str) -> str:
    """Qualify nested types (e.g. Aresta -> Grafo.Aresta) when used as bare symbols."""
    nested = nested_class_map(source_text)
    if not nested:
        return code
    fixed = code
    for inner, qualified in nested.items():
        fixed = re.sub(
            rf"(?<!\.)\b{re.escape(inner)}\b",
            qualified,
            fixed,
        )
    return fixed


def _zero_arg_public_methods(source_text: str) -> List[str]:
    if not source_text:
        return []
    return re.findall(r"\bpublic\s+(?:[\w.<>\[\]]+\s+)+(\w+)\s*\(\s*\)\s*\{", source_text)


def fix_invented_getter_names(code: str, source_text: str) -> str:
    """Map invented JavaBean getters to real zero-arg methods from source (e.g. getPeso -> peso)."""
    methods = _zero_arg_public_methods(source_text)
    if not methods:
        return code
    fixed = code
    for method in methods:
        for prefix in ("get", "is"):
            if not method:
                continue
            invented = prefix + method[0].upper() + method[1:]
            fixed = fixed.replace(f".{invented}(", f".{method}(")
    # Common LLM shorthand for edge destination vertex in graph tests.
    if ".getV(" in fixed and "v2" in methods:
        fixed = fixed.replace(".getV(", ".v2(")
    return fixed


def fix_field_access_as_methods(code: str, source_text: str) -> str:
    """Rewrite private field reads to accessor calls when a matching public method exists."""
    methods = set(_zero_arg_public_methods(source_text))
    fixed = code
    for name in sorted(methods, key=len, reverse=True):
        fixed = re.sub(rf"\.{re.escape(name)}\b(?!\s*\()", f".{name}()", fixed)
    return fixed


def fix_stdout_capture_helper(code: str) -> str:
    if "Console.in" not in code and "java.io.Console.in" not in code:
        return code
    # Replace broken restore with a valid PrintStream (minimal fix for compile)
    fixed = re.sub(
        r"System\.setOut\s*\(\s*java\.io\.Console\.in\s*\)",
        "System.setOut(new java.io.PrintStream(System.out))",
        code,
    )
    # Prefer wrapping runnable.run() blocks when we see the common broken pattern
    pattern = re.compile(
        r"(?ms)(System\.setOut\(new java\.io\.PrintStream\((\w+)\)\);\s*)"
        r"(.*?)"
        r"(System\.setOut\([^)]+\);)",
    )

    def wrap(match: re.Match) -> str:
        stream_var = match.group(2)
        middle = match.group(3)
        return (
            f"java.io.PrintStream originalOut = System.out;\n"
            f"        try {{\n"
            f"            System.setOut(new java.io.PrintStream({stream_var}));\n"
            f"{middle}"
            f"        }} finally {{\n"
            f"            System.setOut(originalOut);\n"
            f"        }}"
        )

    return pattern.sub(wrap, fixed)


def apply_shell_compile_fixes(code: str, compile_log: str, source_text: str = "") -> str:
    fixed = fix_expected_exception_annotations(code)
    fixed = fix_wrong_constructors(fixed, compile_log)
    fixed = fix_wrong_constructors_from_source(fixed, source_text)
    if source_text:
        fixed = fix_nested_class_references(fixed, source_text)
        fixed = fix_invented_getter_names(fixed, source_text)
        fixed = fix_field_access_as_methods(fixed, source_text)
        fixed = fix_method_name_typos(fixed, source_text)
    fixed = fix_stdout_capture_helper(fixed)
    fixed = add_throws_exception_to_test_methods(fixed, compile_log)
    existing = set(re.findall(r"^\s*import\s+[^;]+;", fixed, flags=re.MULTILINE))
    needed: List[str] = []
    for pattern, import_line in _IMPORT_RULES:
        if re.search(pattern, fixed) and import_line not in existing:
            needed.append(import_line)
            existing.add(import_line)
    if not needed:
        return fixed
    lines = fixed.splitlines()
    insert_at = 0
    for idx, line in enumerate(lines):
        if line.strip().startswith("package "):
            insert_at = idx + 1
        elif line.strip().startswith("import "):
            insert_at = idx + 1
    for offset, import_line in enumerate(needed):
        lines.insert(insert_at + offset, import_line)
    return "\n".join(lines) + ("\n" if fixed.endswith("\n") else "")


def archive_rejected_methods(
    demo_root: Path,
    stage: str,
    test_path: Path,
    methods: Iterable[str],
    errors: str,
    *,
    action: str = "rejected",
) -> List[str]:
    file_text = test_path.read_text(encoding="utf-8", errors="ignore")
    method_map = extract_test_methods(file_text)
    archived: List[str] = []
    pkg = ""
    pkg_match = re.search(r"^\s*package\s+([a-zA-Z0-9_.]+)\s*;", file_text, re.MULTILINE)
    if pkg_match:
        pkg = pkg_match.group(1)
    rel_pkg = Path(pkg.replace(".", "/")) if pkg else Path()
    base = demo_root / "rejected" / stage / "methods" / rel_pkg / test_path.stem
    base.mkdir(parents=True, exist_ok=True)
    trimmed = errors.strip()
    for method_name in methods:
        snippet = method_map.get(method_name, "")
        if not snippet:
            continue
        dest = base / f"{method_name}.java"
        dest.write_text(snippet, encoding="utf-8")
        dest.with_suffix(".failed.txt").write_text(
            f"action={action}\nmethod={method_name}\nclass={test_path.stem}\n\n{trimmed}",
            encoding="utf-8",
        )
        archived.append(method_name)
    return archived


def prune_failing_compile_methods(
    test_path: Path,
    compile_log: str,
    demo_root: Path,
    *,
    action: str = "rejected_after_compile_prune",
) -> MethodPruneResult:
    if not test_path.exists():
        return MethodPruneResult([], [], "", whole_file_reject=True, reason="missing_file")

    code = test_path.read_text(encoding="utf-8", errors="ignore")
    fixed = apply_shell_compile_fixes(code, compile_log)
    if fixed != code:
        code = fixed
        test_path.write_text(code, encoding="utf-8")

    failing_methods = methods_for_error_lines(code, compile_log, test_path)
    unmapped = unmapped_error_lines(code, compile_log, test_path)

    if not failing_methods and unmapped:
        return MethodPruneResult(
            [],
            list(extract_test_methods(code).keys()),
            code,
            whole_file_reject=True,
            reason="unmapped_class_shell_errors",
        )

    if not failing_methods:
        return MethodPruneResult([], list(extract_test_methods(code).keys()), code)

    archive_rejected_methods(demo_root, "compile", test_path, failing_methods, compile_log, action=action)
    pruned_code = remove_test_methods(code, failing_methods)
    remaining = list(extract_test_methods(pruned_code).keys())
    if not remaining:
        return MethodPruneResult(
            list(failing_methods),
            [],
            pruned_code,
            whole_file_reject=True,
            reason="all_methods_pruned",
        )
    test_path.write_text(pruned_code, encoding="utf-8")
    return MethodPruneResult(list(failing_methods), remaining, pruned_code)


def prune_failing_runtime_methods(
    test_path: Path,
    method_names: Iterable[str],
    errors: str,
    demo_root: Path,
    *,
    action: str = "rejected_after_runtime_prune",
) -> MethodPruneResult:
    if not test_path.exists():
        return MethodPruneResult([], [], "", whole_file_reject=True, reason="missing_file")
    names = [m for m in method_names if m]
    if not names:
        return MethodPruneResult([], list(extract_test_methods(test_path.read_text(encoding="utf-8", errors="ignore")).keys()), test_path.read_text(encoding="utf-8", errors="ignore"))

    code = test_path.read_text(encoding="utf-8", errors="ignore")
    archive_rejected_methods(demo_root, "runtime", test_path, names, errors, action=action)
    pruned_code = remove_test_methods(code, names)
    remaining = list(extract_test_methods(pruned_code).keys())
    if not remaining:
        return MethodPruneResult(list(names), [], pruned_code, whole_file_reject=True, reason="all_methods_pruned")
    test_path.write_text(pruned_code, encoding="utf-8")
    return MethodPruneResult(list(names), remaining, pruned_code)


def survivor_method_map(paths: Iterable[str]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for path_str in paths:
        path = Path(path_str)
        if not path.exists():
            continue
        result[str(path)] = list(extract_test_methods(path.read_text(encoding="utf-8", errors="ignore")).keys())
    return result
