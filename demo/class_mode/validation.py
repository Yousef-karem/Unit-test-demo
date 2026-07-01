from __future__ import annotations

import re
from typing import Dict, List, Optional


def validate_slice_test_coverage(code: str, target: Dict) -> Optional[str]:
    """Ensure generated tests reference at least one method allowed for this slice."""
    allowed: List[str] = target.get("class_prompt_slice_allowed_methods") or []
    if not allowed:
        return None

    class_name = target.get("class_name") or ""
    for method_name in allowed:
        if re.search(rf"\b{re.escape(class_name)}\s*\.\s*{re.escape(method_name)}\s*\(", code):
            return None
        if re.search(rf"\b{re.escape(method_name)}\s*\(", code):
            return None

    return (
        f"slice `{target.get('class_prompt_slice')}` requires tests that call at least one of: "
        + ", ".join(allowed[:8])
    )
