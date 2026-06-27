from __future__ import annotations

from typing import Any, Dict, List


def build_edge_case_guidance(
    method_info: Dict,
    control_flow: Dict[str, Any],
) -> str:
    lines = ["EDGE CASES & BOUNDARY CONDITIONS:"]
    params = method_info.get("parameters") or []
    param_names = {p.get("name", ""): p.get("type", "") for p in params if p.get("name")}

    has_loops = control_flow.get("has_loops", False)
    has_conditionals = control_flow.get("has_conditionals", False)
    has_exceptions = control_flow.get("has_exceptions", False)
    complexity = control_flow.get("cyclomatic_complexity", 1)

    added = False

    for name, ptype in param_names.items():
        base = (ptype or "").replace("[]", "").strip()
        if has_loops and base in ("int", "long", "Integer", "Long"):
            lines.append(
                f"- For loop bound parameter `{name}`: test {name}=0, {name}=1, {name}=2, and {name} at array/collection length."
            )
            added = True
        if has_conditionals and base in ("String", "java.lang.String", "CharSequence"):
            lines.append(
                f"- For string parameter `{name}`: empty string, single character, no match, and multiple matches."
            )
            added = True
        if "[]" in ptype or base.endswith("Array"):
            lines.append(
                f"- For array parameter `{name}`: empty array, length 1, and multiple elements; avoid null first element unless testing NullPointerException."
            )
            added = True

    if has_loops and not any("loop bound" in ln for ln in lines):
        lines.append("- Test loop boundary states: zero iterations, one iteration, and multiple iterations.")
        added = True

    if has_conditionals:
        lines.append("- Cover all conditional branches including true, false, and boundary equality checks.")
        added = True

    if has_exceptions:
        lines.append("- Include at least one test that triggers the exception path with invalid or edge inputs.")
        added = True

    if complexity > 3:
        min_tests = min(complexity, 8)
        lines.append(
            f"- Cyclomatic complexity is {complexity}; aim for at least {min_tests} @Test methods to cover distinct paths."
        )
        added = True

    if not added:
        lines.append("- Test regular behavior, edge cases, and boundary values.")

    return "\n".join(lines)
