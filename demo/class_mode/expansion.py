from __future__ import annotations

from copy import deepcopy
from typing import Dict, List

from demo.class_mode.slicing import (
    allowed_methods_for_slice,
    build_class_slice_snippet,
    classify_public_methods,
    resolve_effective_slice_kinds,
)


def expand_class_target(target: Dict, *, slices: int = 3) -> List[Dict]:
    if target.get("method_name") is not None:
        return [target]

    class_info = (target.get("ast") or {}).get("class")
    if not class_info:
        return [target]

    slice_kinds = resolve_effective_slice_kinds(class_info, slices)
    if not slice_kinds:
        return [target]

    package = target.get("package") or ""
    class_name = target.get("class_name") or ""
    fqcn = class_info.get("className") or (f"{package}.{class_name}" if package else class_name)
    simple, complex_methods = classify_public_methods(class_info)

    expanded: List[Dict] = []
    total = len(slice_kinds)
    for index, slice_kind in enumerate(slice_kinds):
        snippet = build_class_slice_snippet(
            fqcn,
            class_info,
            slice_kind,
            simple,
            complex_methods,
            all_slice_kinds=slice_kinds,
        )
        slice_target = deepcopy(target)
        slice_target.update(
            {
                "class_prompt_slice": slice_kind,
                "class_prompt_slice_index": index,
                "class_prompt_slice_total": total,
                "snippet": snippet,
                "signature": f"(class: {class_name} — {slice_kind.replace('_', ' ')} slice)",
                "class_prompt_slice_allowed_methods": allowed_methods_for_slice(
                    slice_kind,
                    simple,
                    complex_methods,
                    all_slice_kinds=slice_kinds,
                ),
            }
        )
        expanded.append(slice_target)
    return expanded


def expand_class_targets(targets: List[Dict], *, slices: int = 3) -> List[Dict]:
    expanded: List[Dict] = []
    for target in targets:
        expanded.extend(expand_class_target(target, slices=slices))
    return expanded
