from __future__ import annotations

from demo.prompter.rules import get_java_guidance, get_junit_prompt_rules
from demo.prompter.templates import USER_PROMPT_TEMPLATE
from demo.semantic.models import TestSpec


def build_static_prompt(spec: TestSpec) -> str:
    rules = get_junit_prompt_rules(spec.junit_version, spec.has_mockito)
    java_guidance = get_java_guidance(spec.java_version)

    if spec.constructor_sigs:
        c_lines = [
            f"To construct the class under test `{spec.class_name}`, use one of the constructors shown in its constructors block:"
        ]
        for sig in spec.constructor_sigs:
            c_lines.append(f"  - `{sig}`")
        constructor_guidance = "\n".join(c_lines)
    else:
        constructor_guidance = (
            f"To construct the class under test `{spec.class_name}`, use its default constructor (no arguments)."
        )

    cf = spec.control_flow_characteristics
    b_lines = ["BRANCH & LOGIC COVERAGE GOALS:"]
    if cf.get("has_loops"):
        b_lines.append(
            "- The target code contains loops. Test boundary states (0, 1, and multiple iterations) of loops."
        )
    if cf.get("has_conditionals"):
        b_lines.append(
            "- The target code contains conditional paths. Cover all branches (equal, less-than, greater-than, true, false, and boundary checks)."
        )
    if cf.get("has_exceptions"):
        b_lines.append(
            "- The target code throws exceptions. Ensure you include a test verifying that the exception is thrown on invalid/edge inputs."
        )

    if len(b_lines) == 1:
        b_lines.append("- Test regular behavior, edge cases, and boundary values.")
    branch_guidance = "\n".join(b_lines)

    if spec.private_method_delegation:
        p_lines = [
            "PRIVATE METHOD DELEGATION (INDIRECT TESTING):",
            f"The public entry point `{spec.signature}` delegates logic to these private helper methods:",
        ]
        for m in spec.private_method_delegation:
            p_lines.append(f"  - `{m}`")
        p_lines.append(
            "Since private methods cannot be called directly in unit tests, you MUST test them indirectly. "
            "Choose inputs to the public method that cover the internal branches of these private helper methods."
        )
        private_method_guidance = "\n".join(p_lines)
    else:
        private_method_guidance = ""

    if spec.collaborator_strategy:
        col_lines = ["COLLABORATOR STRATEGIES (Use these specific approaches for dependencies):"]
        for col in spec.collaborator_strategy:
            prefix = "Mock (Mockito)" if col.strategy == "mock" else "Use REAL object"
            col_lines.append(f"- {prefix} `{col.type_name}`: {col.details}")
        collaborator_guidance = "\n".join(col_lines)
    else:
        collaborator_guidance = "No collaborator dependencies detected."

    if spec.related_sources:
        related_sources_guidance = (
            "RELATED TYPE SOURCES (Use ONLY APIs/constructors defined in this source context; do not invent methods):\n"
            f"{spec.related_sources}"
        )
    else:
        related_sources_guidance = ""

    return USER_PROMPT_TEMPLATE.format(
        junit_version=spec.junit_version,
        test_class_name=spec.test_class_name,
        java_version=spec.java_version,
        java_version_guidance=java_guidance,
        package_name=spec.package_name,
        class_name=spec.class_name,
        domain_kind=spec.domain_kind,
        signature=spec.signature,
        snippet=spec.snippet,
        library_limit=rules["library_limit"],
        dependency_rule=rules["dependency_rule"],
        framework_rule=rules["framework_rule"],
        junit_imports=rules["junit_imports"],
        junit_visibility=rules["junit_visibility"],
        junit_forbidden=rules["junit_forbidden"],
        constructor_guidance=constructor_guidance,
        branch_guidance=branch_guidance,
        private_method_guidance=private_method_guidance,
        collaborator_guidance=collaborator_guidance,
        related_sources_guidance=related_sources_guidance,
    )
