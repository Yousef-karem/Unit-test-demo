from __future__ import annotations

from demo.semantic.models import TestSpec


def build_testability_guidance(spec: TestSpec) -> str:
    hints = spec.testability_hints or {}
    lines = ["TESTABILITY CONSTRAINTS:"]
    added = False

    if hints.get("usesIO"):
        lines.append(
            "- Method performs IO (e.g. System.out). Capture stdout with ByteArrayOutputStream and "
            "PrintStream; use System.setOut in @Before and restore the original PrintStream in @After. "
            "NEVER use System.out.toString() to assert printed output."
        )
        added = True
    if hints.get("usesDB"):
        lines.append("- Method touches a database: use in-memory or fake implementations; do not hit a real database.")
        added = True
    if hints.get("usesNetwork"):
        lines.append("- Method uses network: stub external calls; do not perform real network I/O in unit tests.")
        added = True
    if hints.get("usesTime"):
        lines.append("- Method uses time: avoid flaky time-dependent assertions; use fixed inputs where possible.")
        added = True
    if hints.get("usesRandomness"):
        lines.append("- Method uses randomness: seed or stub random sources for deterministic assertions.")
        added = True
    if hints.get("probablyPure"):
        lines.append("- Method appears pure: assert directly on return values with concrete expected results.")
        added = True
    if hints.get("hasThrowStatements"):
        if spec.junit_version == "4":
            lines.append("- Method throws exceptions: include @Test(expected = ExceptionClass.class) for invalid inputs.")
        else:
            lines.append("- Method throws exceptions: use assertThrows(ExceptionClass.class, () -> ...) for invalid inputs.")
        added = True

    if not added:
        return ""
    return "\n".join(lines)


def build_assertion_strategy(spec: TestSpec) -> str:
    lines = ["ASSERTION STRATEGY:"]
    is_void = spec.return_type in ("void", "") or spec.is_void
    uses_io = spec.testability_hints.get("usesIO", False)

    if spec.is_static and spec.method_name:
        lines.append(
            f"- Call `{spec.class_name}.{spec.method_name}(...)` statically; "
            "do not rely on instance construction for the target method."
        )
    elif not spec.is_static and spec.method_name:
        lines.append(
            f"- Instantiate `{spec.class_name}` and invoke `{spec.method_name}(...)` on the instance."
        )

    if is_void and uses_io:
        lines.append(
            "- Method returns void with stdout side effects: assert on captured output content "
            "(e.g. out.toString().contains(\"expected fragment\"))."
        )
    elif is_void:
        lines.append(
            "- Method returns void: verify behavior via observable side effects shown in the method source "
            "(mutated arguments, captured IO, or thrown exceptions)."
        )
    else:
        lines.append(
            f"- Method returns `{spec.return_type}`: use assertEquals / assertNotNull on the return value."
        )

    if spec.literal_outputs:
        lines.append("EXPECTED OUTPUT PATTERNS (from source literals):")
        for lit in spec.literal_outputs:
            lines.append(f'  - Output may contain: "{lit}"')

    return "\n".join(lines)


def build_private_method_sources_guidance(spec: TestSpec) -> str:
    if not spec.private_method_sources:
        return ""
    lines = [
        "PRIVATE METHOD SOURCES (context for indirect testing only — do NOT call directly from tests):",
    ]
    for sig, source in spec.private_method_sources.items():
        lines.append(f"- `{sig}`:")
        for src_line in source.splitlines()[:12]:
            lines.append(f"    {src_line}")
        if source.count("\n") > 12:
            lines.append("    ...")
    return "\n".join(lines)


def build_method_source_guidance(spec: TestSpec) -> str:
    if spec.method_source:
        return f"METHOD SOURCE (exact code under test):\n{spec.method_source}"
    return ""
