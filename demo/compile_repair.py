from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from demo.coverage.maven import run_maven_test_compile, strip_ansi
from demo.llm.prompt_writer import (
    OllamaRepairTimeout,
    ollama_repair_test,
    ollama_write_compile_repair_prompt,
    set_ollama_repair_timeout,
)
from demo.prompt_generation.helpers import resolve_related_sources
from demo.semantic.extractor import SemanticExtractor
from demo.test_method_edit import apply_shell_compile_fixes, summarize_compile_errors
from demo.targets import _extract_imports_context_from_text
from demo.utils import (
    ensure_junit_imports,
    enforce_test_class_name,
    remove_invented_api_stubs,
    rewrite_interface_mocks_to_concrete,
    validate_java_test_output,
)


@dataclass
class CompileFailure:
    failing_path: Path
    errors: str


@dataclass
class CompileRepairResult:
    failing_path: Path
    action: str
    fixed_code: Optional[str] = None
    errors_tail: str = ""
    method_names: List[str] = field(default_factory=list)
    repair_prompt: str = ""
    repair_prompt_generated: bool = False
    elapsed_seconds: float = 0.0
    repair_attempts: int = 0


@dataclass
class CompileRepairContext:
    project_root: Path
    demo_root: Path
    model: str
    java_version: str
    junit_version: str
    max_compile_repair_attempts: int
    test_target_map: Dict[str, Dict]
    ast_analysis: Optional[Dict]
    compile_repair_threads: int
    repo_types_text: str
    ollama_repair_timeout: int = 300
    ollama_repair_concurrency: int = 1
    ollama_semaphore: Optional[threading.Semaphore] = field(default=None, repr=False)


def _constructor_info(source_text: str, class_name: str) -> str:
    if not source_text or not class_name:
        return ""
    ctor_re = re.compile(rf"\b{re.escape(class_name)}\s*\(([^)]*)\)\s*\{{")
    signatures: List[str] = []
    for match in ctor_re.finditer(source_text):
        params = (match.group(1) or "").strip()
        signatures.append(f"{class_name}({params})" if params else f"{class_name}()")
    if not signatures:
        return f"{class_name}() — no explicit constructors found; use default no-arg constructor"
    return "; ".join(signatures)


def group_compile_failures(failing_paths: List[Path], compile_log: str) -> List[CompileFailure]:
    grouped: Dict[str, CompileFailure] = {}
    for path in failing_paths:
        key = str(path)
        if key not in grouped:
            grouped[key] = CompileFailure(failing_path=path, errors=compile_log)
    return list(grouped.values())


def _errors_for_file(compile_errors: str, file_name: str) -> str:
    log = strip_ansi(compile_errors)
    lines = log.splitlines()
    matched: List[str] = []
    capture = False
    for line in lines:
        if file_name in line and "[ERROR]" in line:
            capture = True
            matched.append(line)
            continue
        if capture:
            if line.lstrip().startswith("[ERROR]") and file_name not in line:
                capture = False
            elif line.lstrip().startswith("[ERROR]"):
                matched.append(line)
                if "required:" in line or "found:" in line or "symbol:" in line or "reason:" in line:
                    continue
                capture = False
            elif line.strip() and (line.startswith("  ") or "required:" in line or "found:" in line or "symbol:" in line):
                matched.append(line)
            else:
                capture = False
    return "\n".join(matched) if matched else compile_errors


def _verify_file_compiles(
    project_root: Path,
    demo_root: Path,
    test_path: Path,
    code: str,
) -> tuple[bool, str]:
    """Isolate other generated tests and run mvn test-compile for this file only."""
    from demo.pipeline import isolate_generated_tests_except

    isolation_root = demo_root / "isolation" / "compile_repair_verify"
    original = test_path.read_text(encoding="utf-8", errors="ignore") if test_path.exists() else ""
    test_path.write_text(code, encoding="utf-8")
    try:
        with isolate_generated_tests_except(project_root, test_path, isolation_root):
            compile_log, compile_rc = run_maven_test_compile(project_root)
        if compile_rc == 0:
            return True, compile_log
        return False, _errors_for_file(compile_log, test_path.name)
    finally:
        if original:
            test_path.write_text(original, encoding="utf-8")
        elif test_path.exists():
            test_path.write_text(code, encoding="utf-8")


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


def _apply_deterministic_fixes(
    file_content: str,
    *,
    source_bundle: str,
    related_sources: str,
    compile_errors: str,
    source_text: str,
    test_class: str,
    junit_version: str,
) -> str:
    fixed = remove_invented_api_stubs(file_content, source_bundle)
    fixed = rewrite_interface_mocks_to_concrete(fixed, related_sources)
    fixed = apply_shell_compile_fixes(fixed, compile_errors, source_text)
    fixed = enforce_test_class_name(fixed, test_class)
    fixed = ensure_junit_imports(fixed, junit_version)
    return fixed


def _ollama_call(ctx: CompileRepairContext, fn, *args, **kwargs):
    sem = ctx.ollama_semaphore
    if sem is not None:
        with sem:
            return fn(*args, **kwargs)
    return fn(*args, **kwargs)


def _llm_repair_attempt(
    *,
    ctx: CompileRepairContext,
    failing_path: Path,
    test_class: str,
    file_content_work: str,
    file_errors: str,
    error_summary: str,
    source_text: str,
    package_imports: str,
    constructor_info: str,
    related_sources: str,
    semantic_hints: str,
    attempt: int,
) -> tuple[Optional[str], str, bool]:
    repair_prompt_text = ""
    try:
        prompt_result = _ollama_call(
            ctx,
            ollama_write_compile_repair_prompt,
            model=ctx.model,
            compiler_errors=file_errors,
            file_content=file_content_work,
            source_text=source_text,
            package_imports=package_imports,
            constructor_info=constructor_info,
            repository_types=ctx.repo_types_text,
            related_type_sources=related_sources,
            semantic_hints=semantic_hints,
            error_summary=error_summary,
            java_version=ctx.java_version,
            junit_version=ctx.junit_version,
        )
        repair_prompt_text = (prompt_result.get("repair_prompt") or "").strip()
    except Exception as exc:
        return None, f"Prompt generation error: {exc}", False

    prompts_dir = ctx.demo_root / "compile" / "repair_prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"{test_class}.json").write_text(
        json.dumps(
            {
                "test_class": test_class,
                "attempt": attempt,
                "repair_prompt": repair_prompt_text,
                "compiler_errors": file_errors,
                "error_summary": error_summary,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    try:
        fixed_code = _ollama_call(
            ctx,
            ollama_repair_test,
            model=ctx.model,
            compiler_errors=file_errors,
            file_content=file_content_work,
            source_text=source_text,
            package_imports=package_imports,
            constructor_info=constructor_info,
            repository_types=ctx.repo_types_text,
            related_type_sources=related_sources,
            java_version=ctx.java_version,
            junit_version=ctx.junit_version,
            repair_prompt=repair_prompt_text or None,
        )
    except OllamaRepairTimeout as exc:
        return None, str(exc), bool(repair_prompt_text)

    return fixed_code, repair_prompt_text, bool(repair_prompt_text)


def _repair_one_file(compile_failure: CompileFailure, ctx: CompileRepairContext) -> CompileRepairResult:
    worker_start = time.perf_counter()
    failing_path = compile_failure.failing_path
    compile_errors = compile_failure.errors

    if not failing_path.exists():
        return CompileRepairResult(
            failing_path=failing_path,
            action="skipped_missing_file",
            errors_tail=compile_errors,
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    try:
        file_content = failing_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return CompileRepairResult(
            failing_path=failing_path,
            action="skipped_read_error",
            errors_tail=str(exc),
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    test_class = failing_path.stem
    target = ctx.test_target_map.get(test_class, {})
    related_sources = resolve_related_sources(ctx.project_root, ctx.ast_analysis, target)
    source_text = ""
    package_imports = ""
    constructor_info = ""
    class_name = target.get("class_name", "")
    src_path = target.get("source_file")
    if src_path:
        try:
            source_text = Path(src_path).read_text(encoding="utf-8", errors="ignore")
            package_imports = "\n".join(_extract_imports_context_from_text(source_text))
            constructor_info = _constructor_info(source_text, class_name)
        except OSError:
            pass

    source_bundle = f"{related_sources}\n{source_text}"
    file_errors = _errors_for_file(compile_errors, failing_path.name)
    error_summary = summarize_compile_errors(compile_errors, failing_path.name)

    file_content_work = _apply_deterministic_fixes(
        file_content,
        source_bundle=source_bundle,
        related_sources=related_sources,
        compile_errors=compile_errors,
        source_text=source_text,
        test_class=test_class,
        junit_version=ctx.junit_version,
    )
    deterministic_applied = file_content_work != file_content

    ok, verify_log = _verify_file_compiles(ctx.project_root, ctx.demo_root, failing_path, file_content_work)
    if ok:
        action = "deterministic_fix" if deterministic_applied else "already_compiles"
        return CompileRepairResult(
            failing_path=failing_path,
            action=action,
            fixed_code=file_content_work,
            errors_tail=file_errors,
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    if ctx.max_compile_repair_attempts <= 0:
        if deterministic_applied:
            return CompileRepairResult(
                failing_path=failing_path,
                action="deterministic_fix",
                fixed_code=file_content_work,
                errors_tail=verify_log or file_errors,
                elapsed_seconds=round(time.perf_counter() - worker_start, 2),
            )
        return CompileRepairResult(
            failing_path=failing_path,
            action="rejected_no_llm_repair",
            errors_tail=verify_log or file_errors,
            elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        )

    has_mockito = bool((target.get("test_libraries") or {}).get("mockito", True))
    semantic_hints = _semantic_hints(
        target,
        ctx.ast_analysis,
        ctx.project_root,
        related_sources,
        ctx.java_version,
        ctx.junit_version,
        has_mockito,
    )

    repair_prompt_text = ""
    repair_prompt_generated = False
    llm_attempts = 0
    last_errors = verify_log or file_errors

    for attempt in range(1, ctx.max_compile_repair_attempts + 1):
        llm_attempts += 1
        last_errors = _errors_for_file(last_errors, failing_path.name) or last_errors
        error_summary = summarize_compile_errors(last_errors, failing_path.name)

        fixed_code, prompt_or_err, prompt_ok = _llm_repair_attempt(
            ctx=ctx,
            failing_path=failing_path,
            test_class=test_class,
            file_content_work=file_content_work,
            file_errors=last_errors,
            error_summary=error_summary,
            source_text=source_text,
            package_imports=package_imports,
            constructor_info=constructor_info,
            related_sources=related_sources,
            semantic_hints=semantic_hints,
            attempt=attempt,
        )
        if prompt_ok:
            repair_prompt_generated = True
            repair_prompt_text = prompt_or_err

        if fixed_code is None:
            if "timed out" in prompt_or_err.lower():
                return CompileRepairResult(
                    failing_path=failing_path,
                    action="rejected_repair_timeout",
                    errors_tail=f"{last_errors}\n\n{prompt_or_err}",
                    repair_prompt=repair_prompt_text,
                    repair_prompt_generated=repair_prompt_generated,
                    elapsed_seconds=round(time.perf_counter() - worker_start, 2),
                    repair_attempts=llm_attempts,
                )
            return CompileRepairResult(
                failing_path=failing_path,
                action="rejected_prompt_generation_failed",
                errors_tail=f"{last_errors}\n\n{prompt_or_err}",
                repair_prompt=repair_prompt_text,
                repair_prompt_generated=repair_prompt_generated,
                elapsed_seconds=round(time.perf_counter() - worker_start, 2),
                repair_attempts=llm_attempts,
            )

        fixed_code = _apply_deterministic_fixes(
            fixed_code,
            source_bundle=source_bundle,
            related_sources=related_sources,
            compile_errors=last_errors,
            source_text=source_text,
            test_class=test_class,
            junit_version=ctx.junit_version,
        )

        invalid_reason = validate_java_test_output(fixed_code, test_class)
        if not fixed_code.strip() or invalid_reason:
            last_errors = last_errors if not invalid_reason else f"{last_errors}\n\n{invalid_reason}"
            file_content_work = fixed_code or file_content_work
            continue

        ok, verify_log = _verify_file_compiles(ctx.project_root, ctx.demo_root, failing_path, fixed_code)
        if ok:
            action = "fixed"
            if deterministic_applied:
                action = "deterministic_fix+fixed"
            return CompileRepairResult(
                failing_path=failing_path,
                action=action,
                fixed_code=fixed_code,
                errors_tail=last_errors,
                repair_prompt=repair_prompt_text,
                repair_prompt_generated=repair_prompt_generated,
                elapsed_seconds=round(time.perf_counter() - worker_start, 2),
                repair_attempts=llm_attempts,
            )

        last_errors = verify_log
        file_content_work = fixed_code

    return CompileRepairResult(
        failing_path=failing_path,
        action="rejected_still_not_compiling",
        fixed_code=None,
        errors_tail=last_errors,
        repair_prompt=repair_prompt_text,
        repair_prompt_generated=repair_prompt_generated,
        elapsed_seconds=round(time.perf_counter() - worker_start, 2),
        repair_attempts=llm_attempts,
    )


def run_parallel_compile_repair(
    failing_paths: List[Path],
    compile_log: str,
    ctx: CompileRepairContext,
) -> List[CompileRepairResult]:
    set_ollama_repair_timeout(ctx.ollama_repair_timeout)
    if ctx.ollama_semaphore is None and ctx.ollama_repair_concurrency > 0:
        ctx.ollama_semaphore = threading.Semaphore(ctx.ollama_repair_concurrency)

    grouped = group_compile_failures(failing_paths, compile_log)
    if not grouped:
        return []

    threads = max(1, ctx.compile_repair_threads)
    results: List[CompileRepairResult] = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {
            pool.submit(_repair_one_file, compile_failure, ctx): compile_failure
            for compile_failure in grouped
        }
        for fut in as_completed(futures):
            results.append(fut.result())
    return results
