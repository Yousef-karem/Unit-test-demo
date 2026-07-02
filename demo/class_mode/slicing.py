from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from demo.static_analysis import (
    JavadocFieldInfo,
    _render_javadoc_info,
    _render_method_javadoc,
    compact_ast_tree,
    extract_class_javadoc,
    extract_constructor_javadoc,
    extract_field_javadoc,
    extract_method_javadoc,
)

MethodEntry = Tuple[str, Dict]
SliceKind = str

# ---------------------------------------------------------------------------
# Trivial local variable names that should NOT be counted as "shared fields"
# when building the lightweight method dependency graph.
# ---------------------------------------------------------------------------
_TRIVIAL_LOCALS: Set[str] = {
    "i", "j", "k", "n", "m", "s", "e", "v", "x", "y", "z",
    "t", "c", "b", "a", "p", "q", "r", "idx", "len", "size",
    "tmp", "temp", "val", "obj", "res", "ret", "result", "flag",
    "key", "it", "iter", "node", "cur", "curr", "prev", "next",
    "start", "end", "left", "right", "mid",
}

# ---------------------------------------------------------------------------
# Keyword → functional bucket mapping (first camelCase word of method name)
# ---------------------------------------------------------------------------
_BUCKET_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("insertion",    ["add", "insert", "put", "push", "append", "enqueue", "insere", "adiciona"]),
    ("deletion",     ["remove", "delete", "pop", "dequeue", "clear", "drop", "retira", "erase"]),
    ("search",       ["find", "search", "get", "lookup", "fetch", "contains", "existe",
                      "exists", "check", "has", "query", "list", "lista"]),
    ("update",       ["update", "set", "modify", "change", "replace", "edit", "alter", "atualiza"]),
    ("traversal",    ["traverse", "visit", "walk", "iterate", "print", "imprime", "display",
                      "show", "render", "dump", "proximo", "prox", "primeiro"]),
    ("persistence",  ["save", "load", "read", "write", "persist", "store", "export",
                      "import", "serialize", "deserialize"]),
    ("computation",  ["compute", "calc", "calculate", "process", "transform", "convert",
                      "parse", "format", "build", "generate", "create", "make",
                      "transposto", "grafo"]),
]


def _render_field_javadoc(lines: List[str], jdoc: JavadocFieldInfo, indent: str = "  ") -> None:
    if jdoc.summary:
        lines.append(f"{indent}doc: {jdoc.summary}")
    if jdoc.description:
        lines.append(f"{indent}description: {jdoc.description}")
    if jdoc.deprecated:
        lines.append(f"{indent}@deprecated: {jdoc.deprecated}")


def _method_name(method_info: Dict, signature: str) -> str:
    return method_info.get("name") or method_info.get("methodName") or signature.split("(", 1)[0]


def _is_public_method(method_info: Dict) -> bool:
    modifiers = (method_info.get("ast") or {}).get("modifiers") or []
    return "private" not in modifiers


def _is_complex_method(method_info: Dict) -> bool:
    ast = method_info.get("ast") or {}
    control_flow = ast.get("controlFlow") or {}
    hints = ast.get("testabilityHints") or {}
    metrics = ast.get("metrics") or {}

    if any(
        control_flow.get(flag)
        for flag in ("hasIf", "hasSwitch", "hasLoop", "hasTryCatch")
    ):
        return True
    if any(
        hints.get(flag)
        for flag in ("usesIO", "usesDB", "usesNetwork", "usesTime", "usesRandomness", "hasThrowStatements")
    ):
        return True
    if int(metrics.get("cyclomaticComplexity") or 1) > 3:
        return True

    javadoc = method_info.get("javadoc") or {}
    for tag in javadoc.get("tags") or []:
        tag_name = (tag.get("tagName") or tag.get("name") or "").lower()
        if tag_name in {"throws", "exception", "deprecated"}:
            return True
    return False


# ---------------------------------------------------------------------------
# Lifecycle detection
# ---------------------------------------------------------------------------

_LIFECYCLE_PREFIXES = (
    "init", "setup", "setUp", "configure", "initialize", "initialise",
    "build", "create", "make", "of", "from", "getInstance", "newInstance",
    "construct", "prepare", "open", "start",
)

_LIFECYCLE_ANNOTATIONS = {"postconstruct", "beforeeach", "before", "setup"}

_LIFECYCLE_JAVADOC_KEYWORDS = (
    "initializ", "sets up", "construct", "creat", "factory", "builder",
    "bootstrap", "configur",
)


def _is_lifecycle_method(method_info: Dict, signature: str) -> bool:
    """Return True if the method is related to object initialization / lifecycle setup."""
    name = _method_name(method_info, signature).lower()

    # 1. Name prefix match
    for prefix in _LIFECYCLE_PREFIXES:
        if name == prefix.lower() or name.startswith(prefix.lower()):
            return True

    # 2. Annotations on the method
    ast = method_info.get("ast") or {}
    annotations = [a.lower() for a in (ast.get("annotations") or [])]
    if any(a in _LIFECYCLE_ANNOTATIONS for a in annotations):
        return True

    # 3. Javadoc summary keywords
    javadoc = method_info.get("javadoc") or {}
    summary = (
        javadoc.get("summary") or javadoc.get("firstSentence") or javadoc.get("description") or ""
    )
    if isinstance(summary, list):
        summary = " ".join(
            (p.get("text") or "") if isinstance(p, dict) else str(p) for p in summary
        )
    summary_lower = str(summary).lower()
    if any(kw in summary_lower for kw in _LIFECYCLE_JAVADOC_KEYWORDS):
        return True

    return False


def _lifecycle_methods(class_info: Dict) -> List[MethodEntry]:
    """Return all public lifecycle-related MethodEntry items from class_info."""
    result: List[MethodEntry] = []
    for sig, info in (class_info.get("methods") or {}).items():
        if _is_public_method(info) and _is_lifecycle_method(info, sig):
            result.append((sig, info))
    return result


# ---------------------------------------------------------------------------
# Functional grouping helpers
# ---------------------------------------------------------------------------

def _method_leading_word(name: str) -> str:
    """Extract the first camelCase word from a method name, lowercased."""
    # Split on camelCase boundaries: insertNode → ["insert", "Node"]
    parts = re.sub(r"([A-Z])", r"_\1", name).split("_")
    return (parts[0] or (parts[1] if len(parts) > 1 else name)).lower()


def _functional_bucket(name: str) -> str:
    """Map a method name to a semantic functional bucket."""
    leading = _method_leading_word(name)
    name_lower = name.lower()
    for bucket, keywords in _BUCKET_KEYWORDS:
        for kw in keywords:
            if name_lower == kw or name_lower.startswith(kw) or leading == kw:
                return bucket
    return "utility"


def _method_shared_fields(method_info: Dict) -> Set[str]:
    """Return the set of non-trivial variable names read or written by a method."""
    ast = method_info.get("ast") or {}
    names: Set[str] = set()
    for var_name in (ast.get("readVariables") or []):
        if var_name and var_name not in _TRIVIAL_LOCALS:
            names.add(var_name)
    for var_name in (ast.get("writtenVariables") or []):
        if var_name and var_name not in _TRIVIAL_LOCALS:
            names.add(var_name)
    return names


def _method_calls(method_info: Dict) -> Set[str]:
    """Return the set of method names called by this method (simple name only)."""
    ast = method_info.get("ast") or {}
    deps = ast.get("dependencies") or {}
    calls: Set[str] = set()
    for call in (deps.get("calls") or []):
        # call is like "ds.Grafo.listaAdjVazia(int)" → extract simple name
        simple = call.split(".")[-1].split("(")[0]
        if simple:
            calls.add(simple)
    return calls


def _assign_functional_groups(
    simple: Sequence[MethodEntry],
    complex_methods: Sequence[MethodEntry],
    class_info: Dict,
    slice_kinds: Sequence[SliceKind],
) -> Dict[SliceKind, List[MethodEntry]]:
    """
    Assign non-lifecycle public methods to functional groups matching the
    available (non-lifecycle) slice kinds.

    Returns a mapping  slice_kind -> List[MethodEntry].
    """
    # All non-lifecycle methods in encounter order
    all_methods: List[MethodEntry] = list(simple) + list(complex_methods)

    if not all_methods:
        return {}

    # --- Step 1: primary bucket assignment by name -------------------------
    bucket_map: Dict[str, List[MethodEntry]] = {}
    for entry in all_methods:
        sig, info = entry
        name = _method_name(info, sig)
        bucket = _functional_bucket(name)
        bucket_map.setdefault(bucket, []).append(entry)

    # --- Step 2: refine using shared field analysis (lightweight MDG) ------
    # Build field sets per method
    field_sets: Dict[str, Set[str]] = {
        sig: _method_shared_fields(info) for sig, info in all_methods
    }

    # Build call sets per method (simple names)
    call_sets: Dict[str, Set[str]] = {
        sig: _method_calls(info) for sig, info in all_methods
    }
    # Also index all method simple-names for reverse lookup
    method_simple_names: Dict[str, str] = {
        sig: _method_name(info, sig) for sig, info in all_methods
    }

    # For each bucket, check if any method in it is called by methods in another
    # bucket that shares fields → merge those two buckets
    # We do one pass to keep it O(n²) but simple
    bucket_of: Dict[str, str] = {}
    for bucket, entries in bucket_map.items():
        for sig, _ in entries:
            bucket_of[sig] = bucket

    changed = True
    while changed:
        changed = False
        sigs = list(bucket_of.keys())
        for i in range(len(sigs)):
            for j in range(i + 1, len(sigs)):
                sig_a, sig_b = sigs[i], sigs[j]
                b_a, b_b = bucket_of[sig_a], bucket_of[sig_b]
                if b_a == b_b:
                    continue
                shared_fields = field_sets.get(sig_a, set()) & field_sets.get(sig_b, set())
                name_b = method_simple_names.get(sig_b, "")
                name_a = method_simple_names.get(sig_a, "")
                # Merge if they share non-trivial fields OR one calls the other
                calls_ab = name_b in call_sets.get(sig_a, set())
                calls_ba = name_a in call_sets.get(sig_b, set())
                if shared_fields or calls_ab or calls_ba:
                    # Merge smaller bucket into larger
                    size_a = sum(1 for s, b in bucket_of.items() if b == b_a)
                    size_b = sum(1 for s, b in bucket_of.items() if b == b_b)
                    keep, drop = (b_a, b_b) if size_a >= size_b else (b_b, b_a)
                    for sig in list(bucket_of):
                        if bucket_of[sig] == drop:
                            bucket_of[sig] = keep
                    changed = True
                    break
            if changed:
                break

    # Rebuild bucket_map from updated bucket_of
    bucket_map = {}
    for sig, info in all_methods:
        b = bucket_of.get(sig, "utility")
        bucket_map.setdefault(b, []).append((sig, info))

    # --- Step 3: determine available non-lifecycle slice slots -------------
    non_lifecycle_kinds = [k for k in slice_kinds if k != "lifecycle"]
    num_slots = len(non_lifecycle_kinds)

    if num_slots == 0:
        return {}

    # Sort buckets by size descending so largest groups get their own slot
    sorted_buckets = sorted(bucket_map.items(), key=lambda kv: len(kv[1]), reverse=True)

    # --- Step 4: merge excess buckets into the closest slot ----------------
    # If we have more buckets than slots, merge smallest into adjacent ones
    while len(sorted_buckets) > num_slots:
        # Merge the two smallest consecutive buckets
        sorted_buckets = sorted(sorted_buckets, key=lambda kv: len(kv[1]))
        name1, methods1 = sorted_buckets[0]
        name2, methods2 = sorted_buckets[1]
        merged_name = name1 if len(methods1) >= len(methods2) else name2
        sorted_buckets = [
            (n, m) for n, m in sorted_buckets if n not in (name1, name2)
        ]
        sorted_buckets.append((merged_name, methods1 + methods2))

    # Sort final buckets by their first method's position in all_methods
    # to keep a stable, predictable order
    all_sigs = [sig for sig, _ in all_methods]

    def _bucket_first_pos(bucket_entry: Tuple[str, List[MethodEntry]]) -> int:
        _, entries = bucket_entry
        positions = [all_sigs.index(sig) for sig, _ in entries if sig in all_sigs]
        return min(positions) if positions else 9999

    sorted_buckets.sort(key=_bucket_first_pos)

    # --- Step 5: map slice_kinds to bucket groups --------------------------
    result: Dict[SliceKind, List[MethodEntry]] = {}
    for i, kind in enumerate(non_lifecycle_kinds):
        if i < len(sorted_buckets):
            _, methods = sorted_buckets[i]
            # complex methods always go to the LAST non-lifecycle slot (edge_cases logic)
            if kind == "edge_cases" and i == len(non_lifecycle_kinds) - 1:
                complex_sigs = {sig for sig, _ in complex_methods}
                methods = [e for e in methods if e[0] in complex_sigs] or methods
            result[kind] = methods
        else:
            result[kind] = []

    # Special case: if the last slot is "edge_cases" and complex methods exist,
    # ensure complex methods end up there (may already be satisfied above).
    if "edge_cases" in result and complex_methods:
        complex_sigs = {sig for sig, _ in complex_methods}
        for kind, entries in result.items():
            if kind != "edge_cases":
                result[kind] = [e for e in entries if e[0] not in complex_sigs]
        existing = {sig for sig, _ in result["edge_cases"]}
        for entry in complex_methods:
            if entry[0] not in existing:
                result["edge_cases"].append(entry)

    return result


# ---------------------------------------------------------------------------
# Public API — unchanged signatures
# ---------------------------------------------------------------------------

def classify_public_methods(class_info: Dict) -> Tuple[List[MethodEntry], List[MethodEntry]]:
    """
    Return (simple, complex_methods) — both lists contain only non-lifecycle
    public methods.  Lifecycle methods are handled separately by
    _lifecycle_methods() and appear in the 'lifecycle' slice.
    """
    simple: List[MethodEntry] = []
    complex_methods: List[MethodEntry] = []
    for signature, method_info in (class_info.get("methods") or {}).items():
        if not _is_public_method(method_info):
            continue
        if _is_lifecycle_method(method_info, signature):
            continue          # ← excluded; will appear only in lifecycle slice
        if _is_complex_method(method_info):
            complex_methods.append((signature, method_info))
        else:
            simple.append((signature, method_info))
    return simple, complex_methods


def resolve_effective_slice_kinds(class_info: Dict, requested: int) -> List[SliceKind]:
    if requested <= 1:
        return []

    simple, complex_methods = classify_public_methods(class_info)
    lifecycle = _lifecycle_methods(class_info)
    public_count = len(simple) + len(complex_methods)

    # Trivial class — no slicing needed
    if public_count <= 1 and not lifecycle:
        return []

    if requested >= 4 and public_count >= 13 and len(simple) >= 4:
        return ["lifecycle", "behavior_a", "behavior_b", "edge_cases"]
    if requested >= 3:
        return ["lifecycle", "behavior", "edge_cases"]
    return ["lifecycle", "behavior"]


def methods_for_slice(
    slice_kind: SliceKind,
    simple: Sequence[MethodEntry],
    complex_methods: Sequence[MethodEntry],
    *,
    all_slice_kinds: Sequence[SliceKind] = (),
    class_info: Optional[Dict] = None,
) -> List[MethodEntry]:
    """
    Return the MethodEntry list for *slice_kind*.

    When *class_info* is provided the improved functional grouping is used.
    When it is absent the function falls back to the original simple/complex
    split so that any external caller that omits class_info keeps working.
    """
    if slice_kind == "lifecycle":
        if class_info is not None:
            return _lifecycle_methods(class_info)
        # Fallback: lifecycle = simple + complex (original behaviour)
        return list(simple) + list(complex_methods)

    # Functional grouping when class_info is available
    if class_info is not None:
        groups = _assign_functional_groups(simple, complex_methods, class_info, all_slice_kinds)
        if slice_kind in groups:
            return groups[slice_kind]

    # ---- Original fallback logic (unchanged) ----
    if slice_kind == "behavior":
        selected = list(simple)
        if "edge_cases" not in all_slice_kinds:
            selected.extend(complex_methods)
        return selected
    if slice_kind == "edge_cases":
        return list(complex_methods)
    if slice_kind == "behavior_a":
        half = max(1, len(simple) // 2)
        return list(simple[:half])
    if slice_kind == "behavior_b":
        half = max(1, len(simple) // 2)
        return list(simple[half:])
    return []


def allowed_methods_for_slice(
    slice_kind: SliceKind,
    simple: Sequence[MethodEntry],
    complex_methods: Sequence[MethodEntry],
    *,
    all_slice_kinds: Sequence[SliceKind] = (),
    class_info: Optional[Dict] = None,
) -> List[str]:
    if slice_kind == "lifecycle":
        return []
    return [_method_name(info, sig) for sig, info in methods_for_slice(
        slice_kind, simple, complex_methods,
        all_slice_kinds=all_slice_kinds,
        class_info=class_info,
    )]


# ---------------------------------------------------------------------------
# Snippet rendering helpers
# ---------------------------------------------------------------------------

def _relevant_field_names(methods: Sequence[MethodEntry]) -> Optional[Set[str]]:
    """
    Return the set of non-trivial field/variable names referenced by *methods*,
    or None if no method has variable info (→ caller should include all fields).
    """
    names: Set[str] = set()
    has_data = False
    for _, info in methods:
        ast = info.get("ast") or {}
        rv = ast.get("readVariables") or []
        wv = ast.get("writtenVariables") or []
        if rv or wv:
            has_data = True
        names.update(v for v in rv if v and v not in _TRIVIAL_LOCALS)
        names.update(v for v in wv if v and v not in _TRIVIAL_LOCALS)
    return names if has_data else None


def _append_class_context(lines: List[str], class_info: Dict) -> None:
    class_jdoc = extract_class_javadoc(class_info)
    if class_jdoc:
        lines.append("javadoc:")
        _render_javadoc_info(lines, class_jdoc)
    else:
        if class_info.get("domainKind"):
            lines.append(f"domainKind: {class_info['domainKind']}")
        if class_info.get("annotations"):
            lines.append(f"annotations: {', '.join(class_info['annotations'])}")
        if class_info.get("extendsClass"):
            lines.append(f"extends: {class_info['extendsClass']}")
        if class_info.get("implementsList"):
            lines.append(f"implements: {', '.join(class_info['implementsList'])}")

    if class_info.get("autowiredComponents"):
        lines.append("autowiredComponents:")
        lines.extend(f"- {item}" for item in class_info["autowiredComponents"][:20])


def _append_fields(
    lines: List[str],
    class_info: Dict,
    relevant_names: Optional[Set[str]] = None,
) -> None:
    """
    Append field declarations to *lines*.

    When *relevant_names* is provided, only fields whose name appears in that
    set are emitted.  Pass ``None`` to include all fields (original behaviour).
    """
    fields = class_info.get("fields") or []
    if not fields:
        return
    filtered = [
        fld for fld in fields
        if relevant_names is None or fld.get("name", "") in relevant_names
    ]
    if not filtered:
        return
    lines.append("fields:")
    for fld in filtered[:30]:
        field_jdoc = extract_field_javadoc(fld)
        if field_jdoc:
            field_type = fld.get("resolvedType") or fld.get("type") or "Object"
            lines.append(f"- {fld.get('name', '')} ({field_type})")
            _render_field_javadoc(lines, field_jdoc)
        else:
            mods = " ".join(fld.get("modifiers") or [])
            field_type = fld.get("resolvedType") or fld.get("type") or "Object"
            anns = fld.get("annotations") or []
            ann_text = f" @{','.join(anns)}" if anns else ""
            lines.append(f"- {mods} {field_type} {fld.get('name', '')}{ann_text}".strip())


def _append_constructors(lines: List[str], class_info: Dict, include: bool = True) -> None:
    if not include:
        return
    constructors = class_info.get("constructors") or []
    if not constructors:
        return
    lines.append("constructors:")
    for ctor in constructors[:12]:
        lines.append(f"- {ctor.get('signature', 'constructor')}")
        ctor_jdoc = extract_constructor_javadoc(ctor)
        if ctor_jdoc:
            _render_method_javadoc(lines, ctor_jdoc, indent="  ")
        else:
            snippet = (ctor.get("sourceSnippet") or "").strip()
            if snippet:
                lines.append(f"  snippet: {snippet[:240]}")


def _append_method_ast_details(lines: List[str], method_info: Dict, indent: str = "  ") -> None:
    ast = method_info.get("ast") or {}
    if not ast:
        return

    def append(line: str) -> None:
        lines.append(f"{indent}{line}" if line else line)

    control_flow = ast.get("controlFlow") or {}
    metrics = ast.get("metrics") or {}
    hints = ast.get("testabilityHints") or {}
    data_flow = ast.get("dataFlow") or {}
    smells = ast.get("smells") or {}

    cf_parts = [f"{k}={v}" for k, v in control_flow.items() if v not in (None, "", [], {})]
    if cf_parts:
        append("controlFlow: " + ", ".join(cf_parts))

    metric_parts = [f"{k}={v}" for k, v in metrics.items() if v not in (None, "", [], {})]
    if metric_parts:
        append("metrics: " + ", ".join(metric_parts))

    hint_parts = [f"{k}={v}" for k, v in hints.items() if v not in (None, "", [], {})]
    if hint_parts:
        append("testabilityHints: " + ", ".join(hint_parts))

    flow_parts = [f"{k}={v}" for k, v in data_flow.items() if v not in (None, "", [], {})]
    if flow_parts:
        append("dataFlow: " + ", ".join(flow_parts))

    smell_parts = [f"{k}={v}" for k, v in smells.items() if v not in (None, "", [], {})]
    if smell_parts:
        append("smells: " + ", ".join(smell_parts))

    deps = ast.get("dependencies") or {}
    if deps.get("calls"):
        append("calls:")
        for call in deps["calls"][:30]:
            append(f"- {call}")
    if deps.get("usesTypes"):
        append("usesTypes: " + ", ".join(deps["usesTypes"][:40]))

    variables = ast.get("variables") or []
    if variables:
        append("variables:")
        for var in variables[:30]:
            flags = []
            if var.get("read"):
                flags.append("read")
            if var.get("written"):
                flags.append("written")
            append(
                f"- {var.get('kind', 'VAR')} {var.get('type', '')} "
                f"{var.get('name', '')} {'/'.join(flags)}".strip()
            )

    tree = compact_ast_tree(ast.get("astTree"), max_nodes=80)
    if tree:
        append("astTree:")
        for tree_line in tree:
            append(tree_line)


def _append_method_full(lines: List[str], signature: str, method_info: Dict) -> None:
    return_type = method_info.get("returnType") or "void"
    method_jdoc = extract_method_javadoc(method_info)
    lines.append(f"- {return_type} {signature}")
    if method_jdoc:
        _render_method_javadoc(lines, method_jdoc, indent="  ")
    else:
        _append_method_ast_details(lines, method_info, indent="  ")


def _append_methods(lines: List[str], methods: Sequence[MethodEntry]) -> None:
    if not methods:
        return
    lines.append("methods:")
    for signature, method_info in methods:
        _append_method_full(lines, signature, method_info)


def build_class_slice_snippet(
    fqcn: str,
    class_info: Dict,
    slice_kind: SliceKind,
    simple: Sequence[MethodEntry],
    complex_methods: Sequence[MethodEntry],
    *,
    all_slice_kinds: Sequence[SliceKind] = (),
) -> str:
    lines = [
        "STATIC ANALYSIS SUMMARY (class-mode slice; not raw source)",
        f"class: {fqcn}",
        f"slice: {slice_kind}",
        f"kind: {class_info.get('kind', 'class')}",
    ]

    _append_class_context(lines, class_info)

    # Resolve this slice's methods using the improved strategy
    slice_methods = methods_for_slice(
        slice_kind, simple, complex_methods,
        all_slice_kinds=all_slice_kinds,
        class_info=class_info,
    )

    # Scope fields to only those referenced by this slice's methods
    relevant = _relevant_field_names(slice_methods)
    _append_fields(lines, class_info, relevant_names=relevant)

    # Include constructors:
    #  - Always for the lifecycle slice (tests need to know how to build the object)
    #  - For other slices only when the slice's methods actually reference fields
    #    (meaning they need an instance, so the constructor matters)
    include_ctors = slice_kind == "lifecycle" or bool(relevant)
    _append_constructors(lines, class_info, include=include_ctors)

    _append_methods(lines, slice_methods)

    return "\n".join(lines)


def slice_focus_instruction(slice_kind: SliceKind) -> str:
    instructions = {
        # ---- original slice kinds (unchanged) ----
        "lifecycle": (
            "Generate 3-5 @Test methods focused on object creation, constructor contracts, "
            "field/wiring initialization, and @BeforeEach/@AfterEach setup. "
            "Use the complete field, constructor, and method documentation below to build realistic instances. "
            "Prefer constructor and field-level assertions; only call methods when needed to verify initialization."
        ),
        "behavior": (
            "Generate 3-5 @Test methods for documented happy-path behavior and public API contracts. "
            "Each @Test should call a distinct listed production method where possible. "
            "Use the full Javadoc contracts (@param, @return) and constructor/field context below. "
            "Prefer real collaborators over mocks."
        ),
        "behavior_a": (
            "Generate 3-5 @Test methods for the first functional group of the public API listed below. "
            "Each @Test should call a distinct listed method. Use complete method documentation below. "
            "Prefer real collaborators over mocks."
        ),
        "behavior_b": (
            "Generate 3-5 @Test methods for the second functional group of the public API listed below. "
            "Each @Test should call a distinct listed method. Use complete method documentation below. "
            "Prefer real collaborators over mocks."
        ),
        "edge_cases": (
            "Generate 3-5 @Test methods targeting branches, invalid inputs, exceptions, and side effects "
            "for the complex methods listed below. Use complete Javadoc and AST metadata "
            "(controlFlow, testabilityHints, @throws) to cover error paths and conditional branches explicitly."
        ),
        # ---- functional group slice kinds ----
        "insertion": (
            "Generate 3-5 @Test methods covering insertion / addition operations listed below. "
            "Verify that the data structure or collection grows correctly after each insert, "
            "including boundary cases such as inserting into an empty structure or inserting duplicates."
        ),
        "deletion": (
            "Generate 3-5 @Test methods covering removal / deletion operations listed below. "
            "Verify that the target element is absent after removal, that the structure shrinks "
            "correctly, and that removing from an empty or missing element is handled safely."
        ),
        "search": (
            "Generate 3-5 @Test methods covering search / query / existence-check operations listed below. "
            "Test both positive (element found) and negative (element absent) cases, "
            "as well as boundary inputs such as empty collections or extreme values."
        ),
        "update": (
            "Generate 3-5 @Test methods covering update / modification operations listed below. "
            "Verify that values change correctly, that the old state is replaced, and that "
            "updating a non-existent key/element behaves as documented."
        ),
        "traversal": (
            "Generate 3-5 @Test methods covering traversal / iteration / display operations listed below. "
            "Verify correct ordering, completeness of traversal, and output format where applicable. "
            "Test on empty structures and non-trivial structures."
        ),
        "persistence": (
            "Generate 3-5 @Test methods covering persistence / I/O / serialization operations listed below. "
            "Verify round-trip correctness (save then load), error handling for missing resources, "
            "and that the persisted form matches expectations."
        ),
        "computation": (
            "Generate 3-5 @Test methods covering computation / transformation / conversion operations "
            "listed below. Verify correctness of returned values against known inputs, "
            "including boundary and edge-case inputs."
        ),
        "utility": (
            "Generate 3-5 @Test methods for the utility / helper operations listed below. "
            "Each @Test should target a distinct method. Verify return values, side effects, "
            "and any documented contracts (@param, @return, @throws)."
        ),
    }
    return instructions.get(slice_kind, instructions["behavior"])
