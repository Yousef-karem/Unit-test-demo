from __future__ import annotations

import re
from typing import Dict

from demo.config import GENERATED_PREFIX

_SLICE_SUFFIX = {
    "lifecycle": "Lifecycle",
    "behavior": "Behavior",
    "edge_cases": "EdgeCases",
    "behavior_a": "BehaviorA",
    "behavior_b": "BehaviorB",
}


def apply_slice_test_class_name(target: Dict, llm_name: str) -> str:
    """Return a deterministic test class name for a class-mode prompt slice."""
    slice_kind = target.get("class_prompt_slice") or ""
    if not slice_kind:
        return llm_name

    suffix = _SLICE_SUFFIX.get(slice_kind, "Slice")
    class_name = re.sub(r"[^A-Za-z0-9]+", "", target.get("class_name") or "Class")
    return f"{GENERATED_PREFIX}{class_name}_{suffix}Test"
