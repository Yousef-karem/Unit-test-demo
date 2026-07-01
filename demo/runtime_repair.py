from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from demo.llm.prompt_writer import (
    OllamaRepairTimeout,
    ollama_runtime_repair_test,
    ollama_write_runtime_repair_prompt,
)
from demo.prompt_generation.helpers import resolve_related_sources
from demo.semantic.extractor import SemanticExtractor
from demo.utils import (
    ensure_junit_imports,
    enforce_test_class_name,
    remove_invented_api_stubs,
    rewrite_interface_mocks_to_concrete,
    validate_java_test_output,
)


@dataclass
class RuntimeFailure:
    failing_path: Path
    class_name: str
    methods: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class RuntimeRepairResult:
    failing_path: Path
    action: str
    fixed_code: Optional[str] = None
    errors_tail: str = ""
    method_names: List[str] = field(default_factory=list)
    repair_prompt: str = ""
    repair_prompt_generated: bool = False
    elapsed_seconds: float = 0.0


@dataclass
class RuntimeRepairContext:
    project_root: Path
    demo_root: Path
    model: str
    java_version: str
    junit_version: str
    max_runtime_repair_attempts: int
    test_target_map: Dict[str, Dict]
    ast_analysis: Optional[Dict]
    runtime_repair_threads: int


def group_failures_by_file(
    failures: List[Dict[str, str]],
    project_root: Path,
) -> List[RuntimeFailure]:
    grouped: Dict[str, RuntimeFailure] = {}
    for failure in failures:
        class_name = failure.get("class_name", "")
        method_name = failure.get("method_name", "")
        stack_trace = failure.get("stack_trace", "")
        rel = Path("src/test/java") / Path(class_name.replace(".", "/") + ".java")
        failing_path = project_root / rel
        key = str(failing_path)
        if key not in grouped:
            grouped[key] = RuntimeFailure(
                failing_path=failing_path,
                class_name=class_name,
                methods=[],
            )
        grouped[key].methods.append(
            {"method_name": method_name, "stack_trace": stack_trace}
        )
    return list(grouped.values())


def combined_stack_trace(runtime_failure: RuntimeFailure) -> str:
    parts: List[str] = []
    for method in runtime_failure.methods:
        name = method.get("method_name", "")
        trace = method.get("stack_trace", "")
        if name:
            parts.append(f"--- {runtime_failure.class_name}.{name} ---\n{trace}")
        else:
            parts.append(trace)
    return "\n\n".join(parts)


def _failing_methods_label(runtime_failure: RuntimeFailure) -> str:
    names = [m.get("method_name", "") for m in runtime_failure.methods if m.get("method_name")]
    return ", ".join(names) if names else "(entire class)"


def _semantic_hints(
    target: Dict,
    ast_analysis: Optional[Dict],
    project_root: Path,
    related_sources: str,
    java_version: str,
    junit_version: str,
    has_mockito: bool,
) -> str:
    if ast_analysis is None:
        return ""
    try:
        spec = SemanticExtractor(ast_analysis).extract_spec(
            target,
            java_version=java_version,
            junit_version=junit_version,
            has_mockito=has_mockito,
            related_sources=related_sources,
            project_root=project_root,
        )
        return (
            f"SEMANTIC CONTEXT:\n"
            f"- Domain role: {spec.domain_kind}\n"
            f"- Signature: {spec.signature}\n"
            f"- Edge case guidance: {spec.edge_case_guidance or '(none)'}"
        )
    except Exception:
        return ""


def _repair_one_file(
    runtime_failure: RuntimeFailure,
    ctx: RuntimeRepairContext,
) -> RuntimeRepairResult:
    worker_start = time.perf_counter()
    failing_path = runtime_failure.failing_path
    method_names = [m.get("method_name", "") for m in runtime_failure.methods if m.get("method_name")]
    stack_trace = combined_stack_trace(runtime_failure)

    if not failing_path.exists():
        return RuntimeRepairResult(
            failing_path=failing_path,
            action="skipped_missing_file",
            errors_tail=stack_trace,
            method_names=method_names,
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    try:
        file_content = failing_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return RuntimeRepairResult(
            failing_path=failing_path,
            action="skipped_read_error",
            errors_tail=str(exc),
            method_names=method_names,
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    test_class = failing_path.stem
    target = ctx.test_target_map.get(test_class, {})
    runtime_related_sources = resolve_related_sources(
        ctx.project_root, ctx.ast_analysis, target
    )
    runtime_source_text = ""
    src_path = target.get("source_file")
    if src_path:
        try:
            runtime_source_text = Path(src_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pass

    source_bundle = f"{runtime_related_sources}\n{runtime_source_text}"
    deterministic_fix = remove_invented_api_stubs(file_content, source_bundle)
    deterministic_fix = rewrite_interface_mocks_to_concrete(
        deterministic_fix, runtime_related_sources
    )
    if deterministic_fix != file_content:
        deterministic_fix = enforce_test_class_name(deterministic_fix, test_class)
        deterministic_fix = ensure_junit_imports(deterministic_fix, ctx.junit_version)
        return RuntimeRepairResult(
            failing_path=failing_path,
            action="deterministic_fix",
            fixed_code=deterministic_fix,
            errors_tail=stack_trace,
            method_names=method_names,
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    if ctx.max_runtime_repair_attempts <= 0:
        return RuntimeRepairResult(
            failing_path=failing_path,
            action="rejected_no_llm_repair",
            errors_tail=stack_trace,
            method_names=method_names,
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    has_mockito = bool((target.get("test_libraries") or {}).get("mockito", True))
    semantic_hints = _semantic_hints(
        target,
        ctx.ast_analysis,
        ctx.project_root,
        runtime_related_sources,
        ctx.java_version,
        ctx.junit_version,
        has_mockito,
    )

    repair_prompt_text = ""
    try:
        prompt_result = ollama_write_runtime_repair_prompt(
            model=ctx.model,
            stack_trace=stack_trace,
            file_content=file_content,
            failing_methods=_failing_methods_label(runtime_failure),
            source_text=runtime_source_text,
            related_type_sources=runtime_related_sources,
            semantic_hints=semantic_hints,
            java_version=ctx.java_version,
            junit_version=ctx.junit_version,
        )
        repair_prompt_text = (prompt_result.get("repair_prompt") or "").strip()
    except Exception as exc:
        return RuntimeRepairResult(
            failing_path=failing_path,
            action="rejected_prompt_generation_failed",
            errors_tail=f"{stack_trace}\n\nPrompt generation error: {exc}",
            method_names=method_names,
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    prompts_dir = ctx.demo_root / "runtime" / "repair_prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"{test_class}.json").write_text(
        json.dumps(
            {
                "test_class": test_class,
                "failing_methods": method_names,
                "repair_prompt": repair_prompt_text,
                "stack_trace": stack_trace,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    try:
        fixed_code = ollama_runtime_repair_test(
            model=ctx.model,
            stack_trace=stack_trace,
            file_content=file_content,
            failing_method=_failing_methods_label(runtime_failure),
            source_text=runtime_source_text,
            related_type_sources=runtime_related_sources,
            java_version=ctx.java_version,
            junit_version=ctx.junit_version,
            repair_prompt=repair_prompt_text or None,
        )
    except OllamaRepairTimeout as exc:
        return RuntimeRepairResult(
            failing_path=failing_path,
            action="rejected_repair_timeout",
            errors_tail=f"{stack_trace}\n\n{exc}",
            method_names=method_names,
            repair_prompt=repair_prompt_text,
            repair_prompt_generated=bool(repair_prompt_text),
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    fixed_code = enforce_test_class_name(fixed_code, test_class)
    fixed_code = ensure_junit_imports(fixed_code, ctx.junit_version)
    fixed_code = remove_invented_api_stubs(fixed_code, source_bundle)
    fixed_code = rewrite_interface_mocks_to_concrete(fixed_code, runtime_related_sources)

    invalid_reason = validate_java_test_output(fixed_code, test_class)
    if not fixed_code.strip() or invalid_reason:
        return RuntimeRepairResult(
            failing_path=failing_path,
            action="rejected_empty_fix",
            errors_tail=stack_trace if not invalid_reason else f"{stack_trace}\n\n{invalid_reason}",
            method_names=method_names,
            repair_prompt=repair_prompt_text,
            repair_prompt_generated=bool(repair_prompt_text),
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    return RuntimeRepairResult(
        failing_path=failing_path,
        action="fixed",
        fixed_code=fixed_code,
        errors_tail=stack_trace,
        method_names=method_names,
        repair_prompt=repair_prompt_text,
        repair_prompt_generated=bool(repair_prompt_text),
        elapsed_seconds=round(time.perf_counter() - worker_start, 2),
    )


def run_parallel_runtime_repair(
    failures: List[Dict[str, str]],
    ctx: RuntimeRepairContext,
) -> List[RuntimeRepairResult]:
    grouped = group_failures_by_file(failures, ctx.project_root)
    if not grouped:
        return []

    threads = max(1, ctx.runtime_repair_threads)
    results: List[RuntimeRepairResult] = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {
            pool.submit(_repair_one_file, runtime_failure, ctx): runtime_failure
            for runtime_failure in grouped
        }
        for fut in as_completed(futures):
            results.append(fut.result())
    return results
