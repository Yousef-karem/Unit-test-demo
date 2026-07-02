from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set



from demo.class_mode.expansion import expand_class_targets
from demo.class_mode.naming import apply_slice_test_class_name
from demo.class_mode.validation import validate_slice_test_coverage
from demo.config import (
    DEFAULT_COVERAGE_THRESHOLD,
    DEFAULT_DOCKER_MAVEN_CACHE_VOLUME,
    DEFAULT_DOCKER_MAVEN_IMAGE,
    DEFAULT_GENERATION_THREADS,
    DEFAULT_MAX_COMPILE_REPAIR_ATTEMPTS,
    DEFAULT_MAX_ITERATION_REFINEMENTS,
    DEFAULT_MAX_RUNTIME_REPAIR_ATTEMPTS,
    DEFAULT_MAX_STAGNATION_ITERATIONS,
    DEFAULT_COMPILE_REPAIR_THREADS,
    DEFAULT_OLLAMA_REPAIR_CONCURRENCY,
    DEFAULT_OLLAMA_REPAIR_TIMEOUT,
    DEFAULT_RUNTIME_REPAIR_THREADS,
    DEMO_OUT,
    GENERATED_PATTERN,
    GENERATED_PREFIX,
)
from demo.coverage.maven import (
    extract_failing_test_paths,
    run_maven_report,
    run_maven_test_compile,
    run_maven_tests,
    strip_ansi,
    write_failure_artifacts,
)
from demo.coverage.java_version import (
    coerce_supported_version,
    detect_java_version,
    java_version_guidance,
    resolve_project_java_version,
)
from demo.coverage.runner import configure_maven_runner, docker_image_name, ensure_docker_available
from demo.coverage.parse import (
    extract_runtime_failures,
    parse_jacoco_xml,
    parse_surefire_reports,
    parse_surefire_summary,
)
from demo.coverage.refinement import CoverageRefinement
from demo.compile_repair import CompileRepairContext, run_parallel_compile_repair
from demo.runtime_repair import (
    RuntimeRepairContext,
    combined_stack_trace,
    group_failures_by_file,
    run_parallel_runtime_repair,
)
from demo.test_method_edit import (
    prune_failing_compile_methods,
    prune_failing_runtime_methods,
    survivor_method_map,
)
from demo.prompt_generation.factory import create_prompt_generator
from demo.prompt_generation.helpers import resolve_related_sources
from demo.prompt_generation.models import PromptGenerationContext
from demo.prompt_generation.protocol import PromptGenerator
from demo.llm.ollama import ollama_generate
from demo.packages import (
    choose_packages_interactive,
    discover_packages,
    file_in_selected_packages,
    list_java_files,
)
from demo.repo import clone_or_update, detect_build_system
from demo.test_libraries import detect_junit_version
from demo.static_analysis import (
    project_type_context_from_analysis,
    related_type_sources_from_analysis,
    run_incremental_ast_analysis,
    run_ast_analysis,
    targets_from_analysis,
)
from demo.targets import _extract_imports_context_from_text, extract_targets
from demo.utils import (
    ensure_unique_run_class_name,
    ensure_junit_imports,
    enforce_test_class_name,
    load_env_file,
    remove_invented_api_stubs,
    repo_name_from_arg,
    rewrite_interface_mocks_to_concrete,
    sanitize_java_output,
    validate_java_test_output,
    validate_junit_framework,
    validate_test_coverage_quality,
)

# ----------------------------
# Naming helpers
# ----------------------------

def stable_suffix_for_target(t: Dict) -> str:
    key = "|".join(
        [
            str(t.get("source_file", "")),
            str(t.get("class_name", "")),
            str(t.get("method_name", "")),
            str(t.get("signature", "")),
        ]
    )
    return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:8]


def ensure_unique_test_class_name(base: str, t: Dict, mode: str) -> str:
    if not (base.startswith(GENERATED_PREFIX) and base.endswith("Test")):
        suffix = (t.get("class_name", "") + (t.get("method_name") or ""))
        suffix = re.sub(r"[^A-Za-z0-9]+", "", suffix)
        base = f"{GENERATED_PREFIX}{suffix}Test"

    # For method-mode, force uniqueness per target
    if mode == "method":
        suffix = stable_suffix_for_target(t)
        if base.endswith("Test"):
            base = base[:-4] + "_" + suffix + "Test"
    return base


# ----------------------------
# Project type discovery helpers
# ----------------------------

TYPE_DECL_RE = re.compile(r"\b(class|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
def list_project_types(project_root: Path) -> List[str]:
    types = set()
    for f in list_java_files(project_root):
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in TYPE_DECL_RE.finditer(txt):
            types.add(m.group(2))
    return sorted(types)


def _extract_type_api_summary(source_text: str, type_name: str) -> str:
    parts: List[str] = []
    if re.search(rf"\binterface\s+{re.escape(type_name)}\b", source_text):
        for m in re.finditer(r"public\s+[^;]+;", source_text):
            parts.append(m.group(0).strip())
    elif re.search(rf"\bclass\s+{re.escape(type_name)}\b", source_text):
        for m in re.finditer(rf"\bpublic\s+(?:static\s+)?[\w\<\>\[\]]+\s+{re.escape(type_name)}\s*\([^)]*\)", source_text):
            params = (m.group(0).split("(", 1)[1].rsplit(")", 1)[0]).strip()
            parts.append(f"constructor({params})")
        for m in re.finditer(r"public\s+(?!class|interface|enum)[^;=]+[;=]", source_text):
            decl = m.group(0).strip().rstrip(";")
            if decl and not decl.startswith("public static void main"):
                parts.append(decl)
    return "; ".join(parts[:12])


def list_project_type_context(project_root: Path) -> List[str]:
    context: List[str] = []
    for f in list_java_files(project_root):
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in TYPE_DECL_RE.finditer(txt):
            kind, name = m.group(1), m.group(2)
            detail = f"{kind} {name}"
            implements_m = re.search(rf"\bclass\s+{re.escape(name)}\b[^{'{'}]*\bimplements\s+([^{'{'}]+)", txt)
            if implements_m:
                detail += f" implements {implements_m.group(1).strip()}"
            api = _extract_type_api_summary(txt, name)
            if api:
                detail += f" api: {api}"
            constructors = extract_constructor_info(txt, name)
            if constructors:
                detail += f" constructors: {constructors}"
            context.append(detail)
    return sorted(context)


def list_repository_types(project_root: Path) -> List[str]:
    names = set()
    repo_re = re.compile(r"\b(class|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*Repository)\b")
    for f in list_java_files(project_root):
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in repo_re.finditer(txt):
            names.add(m.group(2))
    return sorted(names)


def project_has_mockito(project_root: Path) -> bool:
    candidates = [
        project_root / "pom.xml",
        project_root / "build.gradle",
        project_root / "build.gradle.kts",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            if "mockito" in path.read_text(encoding="utf-8", errors="ignore").lower():
                return True
        except OSError:
            continue
    return False


def extract_constructor_info(source_text: str, class_name: str) -> str:
    if not source_text or not class_name:
        return ""
    ctor_re = re.compile(rf"\b{re.escape(class_name)}\s*\(([^)]*)\)\s*\{{")
    params: List[str] = []
    for m in ctor_re.finditer(source_text):
        p = (m.group(1) or "").strip()
        if p:
            params.append(p)
    return "; ".join(params)


def is_interface_target(target: Dict) -> bool:
    src = target.get("source_file")
    cls = target.get("class_name", "")
    if not src or not cls:
        return False
    try:
        txt = Path(src).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return bool(re.search(rf"\binterface\s+{re.escape(cls)}\b", txt))


def write_test_file(project_root: Path, target_pkg: str, test_class_name: str, code: str) -> Path:
    base = project_root / "src" / "test" / "java"
    if target_pkg and target_pkg != "(default)":
        base = base / Path(target_pkg.replace(".", "/"))
    base.mkdir(parents=True, exist_ok=True)

    path = base / f"{test_class_name}.java"
    path.write_text(code, encoding="utf-8")
    return path


def remove_generated_snapshot(demo_root: Path, test_class: str) -> None:
    """Remove a test snapshot from DemoTestCases/generated/ when it is rejected."""
    snapshot = demo_root / "generated" / f"{test_class}.java"
    try:
        snapshot.unlink(missing_ok=True)
    except OSError:
        pass


def prune_generated_snapshots(demo_root: Path, survivor_paths: List[str]) -> None:
    """Keep only survivor test snapshots in DemoTestCases/generated/."""
    survivor_stems = {Path(p).stem for p in survivor_paths if Path(p).exists()}
    generated_dir = demo_root / "generated"
    if not generated_dir.is_dir():
        return
    for snapshot in generated_dir.glob(f"{GENERATED_PREFIX}*Test.java"):
        if snapshot.stem not in survivor_stems:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass


def sync_generated_snapshot(demo_root: Path, test_path: Path) -> None:
    """Copy the repo survivor test back into DemoTestCases/generated/."""
    if not test_path.exists():
        return
    generated_dir = demo_root / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(test_path, generated_dir / test_path.name)


def sync_generated_snapshots(demo_root: Path, survivor_paths: List[str]) -> None:
    """Refresh DemoTestCases/generated/ from the final survivor files in the repo."""
    for path_str in survivor_paths:
        sync_generated_snapshot(demo_root, Path(path_str))


def resolve_survivor_paths(paths: List[str]) -> List[str]:
    resolved: List[str] = []
    seen: Set[str] = set()
    for path_str in paths:
        path = Path(path_str)
        if not path.is_absolute():
            path = path.resolve()
        else:
            try:
                path = path.resolve()
            except OSError:
                pass
        if not path.exists():
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(key)
    return resolved


def prune_repo_generated_tests(project_root: Path, survivor_paths: List[str]) -> None:
    """Remove LLM-generated tests from the repo that are not compile/runtime survivors."""
    survivor_stems = {Path(p).stem for p in survivor_paths}
    for test_file in list(project_root.rglob(f"{GENERATED_PREFIX}*Test.java")):
        if test_file.stem not in survivor_stems:
            try:
                test_file.unlink(missing_ok=True)
            except OSError:
                pass


def prepare_survivors_for_runtime(
    project_root: Path,
    demo_root: Path,
    survivor_paths: List[str],
) -> tuple[List[str], bool, str]:
    """Keep only survivor tests in repo/generated and verify they compile before runtime."""
    survivor_paths = resolve_survivor_paths(survivor_paths)
    if not survivor_paths:
        return [], False, "no survivor tests remain in repo"

    prune_repo_generated_tests(project_root, survivor_paths)
    prune_generated_snapshots(demo_root, survivor_paths)
    sync_generated_snapshots(demo_root, survivor_paths)

    compile_log, compile_rc = run_maven_test_compile(project_root)
    if compile_rc != 0 and "unreported exception" in compile_log:
        changed = False
        for path_str in survivor_paths:
            test_path = Path(path_str)
            if not test_path.exists():
                continue
            code = test_path.read_text(encoding="utf-8", errors="ignore")
            fixed = add_throws_exception_to_test_methods(code, compile_log)
            if fixed != code:
                test_path.write_text(fixed, encoding="utf-8")
                sync_generated_snapshot(demo_root, test_path)
                changed = True
        if changed:
            survivor_paths = resolve_survivor_paths(survivor_paths)
            compile_log, compile_rc = run_maven_test_compile(project_root)

    if compile_rc != 0:
        tail = "\n".join(strip_ansi(compile_log).splitlines()[-20:])
        return survivor_paths, False, f"survivor tests do not compile before runtime:\n{tail}"

    return survivor_paths, True, ""


def isolate_non_generated_test_files(
    project_root: Path, generated_paths: List[str], backup_root: Path
) -> List[Dict[str, str]]:
    """
    Move non-generated *Test.java files out of src/test/java for this run.
    This prevents unrelated pre-existing test compile failures from blocking
    generated-test coverage runs.
    """
    test_root = project_root / "src" / "test" / "java"
    if not test_root.exists():
        return []

    generated_set = {Path(p).resolve() for p in generated_paths}
    moved: List[Dict[str, str]] = []

    for test_file in test_root.rglob("*Test.java"):
        try:
            resolved = test_file.resolve()
        except OSError:
            resolved = test_file

        # Keep only this run's generated tests in place.
        if resolved in generated_set:
            continue

        rel = test_file.relative_to(test_root)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(test_file), str(dest))
        moved.append({"from": str(test_file), "to": str(dest)})

    return moved


def isolate_stale_generated_tests(
    project_root: Path, keep_paths: List[str], backup_root: Path
) -> List[Dict[str, str]]:
    """
    Move LLM_Generated*Test.java files left over from prior runs that are not
    part of the current run's generated_paths.
    """
    test_root = project_root / "src" / "test" / "java"
    if not test_root.exists():
        return []

    keep_set = {Path(p).resolve() for p in keep_paths}
    moved: List[Dict[str, str]] = []

    for test_file in test_root.rglob(f"{GENERATED_PREFIX}*Test.java"):
        try:
            resolved = test_file.resolve()
        except OSError:
            resolved = test_file
        if resolved in keep_set:
            continue
        rel = test_file.relative_to(test_root)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(test_file), str(dest))
        moved.append({"from": str(test_file), "to": str(dest)})

    return moved


def restore_isolated_test_files(moved: List[Dict[str, str]]) -> None:
    for item in moved:
        src = Path(item["to"])
        dst = Path(item["from"])
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


@contextmanager
def isolate_generated_tests_except(
    project_root: Path, keep_path: Path, backup_root: Path
) -> Iterator[List[Dict[str, str]]]:
    """
    Temporarily move every generated test except keep_path out of src/test/java.
    Temporarily isolate other generated tests so compiler/runtime logs refer only
    to the specific generated test being repaired, not unrelated failures.
    """
    test_root = project_root / "src" / "test" / "java"
    moved: List[Dict[str, str]] = []
    if not test_root.exists():
        yield moved
        return

    try:
        keep_resolved = keep_path.resolve()
    except OSError:
        keep_resolved = keep_path

    for test_file in test_root.rglob(f"{GENERATED_PREFIX}*Test.java"):
        try:
            resolved = test_file.resolve()
        except OSError:
            resolved = test_file
        if resolved == keep_resolved:
            continue
        rel = test_file.relative_to(test_root)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(test_file), str(dest))
        moved.append({"from": str(test_file), "to": str(dest)})

    try:
        yield moved
    finally:
        restore_isolated_test_files(moved)


def concise_error_log(log: str, max_lines: int = 80) -> str:
    lines = strip_ansi(log).splitlines()
    important = [
        line
        for line in lines
        if (
            "[ERROR]" in line
            or "Failed tests:" in line
            or "Errors:" in line
            or "Failures:" in line
            or "Exception" in line
            or "Caused by:" in line
            or "cannot find symbol" in line
        )
    ]
    selected = important or lines
    return "\n".join(selected[-max_lines:])


def concise_compile_error_log(log: str, failing_path: Path, max_lines: int = 30) -> str:
    """Extract compile errors scoped to a specific test file for shorter repair prompts."""
    log = strip_ansi(log)
    needle = failing_path.name
    scoped = [line for line in log.splitlines() if needle in line or "[ERROR]" in line]
    if not scoped:
        scoped = [
            line
            for line in log.splitlines()
            if "[ERROR]" in line or "cannot find symbol" in line or "error:" in line.lower()
        ]
    selected = scoped or log.splitlines()
    return "\n".join(selected[-max_lines:])


def concise_runtime_error_log(
    log_or_trace: str,
    class_name: str = "",
    method_name: str = "",
    max_lines: int = 30,
) -> str:
    """Extract Surefire failure details for a specific test method."""
    log = strip_ansi(log_or_trace)
    lines = log.splitlines()
    if not class_name and not method_name:
        return concise_error_log(log, max_lines=max_lines)

    simple_class = class_name.rsplit(".", 1)[-1] if class_name else ""
    selected: List[str] = []
    capture = False
    for line in lines:
        if method_name and (
            f"{simple_class}.{method_name}" in line
            or (">>> FAILURE!" in line and method_name in line)
        ):
            capture = True
        elif simple_class and simple_class in line and (
            "FAILURE" in line or "ERROR" in line or "Exception" in line
        ):
            capture = True
        if capture:
            selected.append(line)
            if len(selected) >= max_lines:
                break
    if not selected:
        selected = [
            line
            for line in lines
            if "AssertionError" in line
            or "Exception" in line
            or "Failed tests:" in line
            or "FAILURE" in line
        ]
    return "\n".join((selected or lines)[-max_lines:])


def count_passed_generated_tests(reports_dir: Path) -> int:
    """Count passing test cases in LLM_Generated* Surefire reports."""
    if not reports_dir.exists():
        return 0
    import xml.etree.ElementTree as ET

    passed = 0
    for xml_path in reports_dir.glob("TEST-*.xml"):
        if GENERATED_PREFIX not in xml_path.name:
            continue
        try:
            tree = ET.parse(str(xml_path))
        except ET.ParseError:
            continue
        root = tree.getroot()
        for case in root.findall(".//testcase"):
            classname = case.attrib.get("classname", "")
            if GENERATED_PREFIX not in classname:
                continue
            if case.find("failure") is None and case.find("error") is None:
                passed += 1
    return passed


def resolve_maven_test_path(project_root: Path, reported_path: Path) -> Path:
    if reported_path.exists():
        return reported_path

    parts = list(reported_path.parts)
    lowered = [p.lower() for p in parts]
    try:
        src_i = lowered.index("src")
        if lowered[src_i : src_i + 3] == ["src", "test", "java"]:
            return project_root / Path(*parts[src_i:])
    except ValueError:
        pass
    return reported_path


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


def patch_obsolete_tools_jar_dependency(project_root: Path) -> bool:
    """Remove old JDK 8 tools.jar system dependencies that break on modern JDKs."""
    pom = project_root / "pom.xml"
    if not pom.is_file():
        return False
    try:
        text = pom.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if "tools.jar" not in text and "<artifactId>tools</artifactId>" not in text:
        return False

    dep_re = re.compile(r"\s*<dependency>[\s\S]*?</dependency>", re.IGNORECASE)

    def keep_or_remove(match: re.Match) -> str:
        block = match.group(0)
        is_tools = (
            re.search(r"<groupId>\s*com\.sun\s*</groupId>", block, re.IGNORECASE)
            and re.search(r"<artifactId>\s*tools\s*</artifactId>", block, re.IGNORECASE)
        )
        return "" if is_tools else block

    patched = dep_re.sub(keep_or_remove, text)
    if patched == text:
        return False
    pom.write_text(patched, encoding="utf-8")
    return True


@dataclass
class TargetGenerationResult:
    index: int
    test_class: str
    target: Dict
    out_path: Optional[str] = None
    quality_log_entry: Optional[Dict] = None
    error: Optional[str] = None
    elapsed_seconds: Optional[float] = None
def read_changed_java_path_set(path: Path) -> set[str]:
    changed: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return changed
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#") or not item.endswith(".java"):
            continue
        changed.add(item.replace("\\", "/"))
    return changed


def filter_targets_to_changed_files(targets: List[Dict], project_root: Path, changed_files: Path) -> List[Dict]:
    changed = read_changed_java_path_set(changed_files)
    if not changed:
        return targets
    root = project_root.resolve()
    filtered: List[Dict] = []
    for target in targets:
        source = target.get("source_file")
        if not source:
            continue
        try:
            rel = Path(source).resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            rel = str(source).replace("\\", "/")
        if rel in changed:
            filtered.append(target)
    return filtered


def git_output(project_root: Path, args: List[str]) -> str:
    p = subprocess.run(
        ["git", "-C", str(project_root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


def current_git_commit(project_root: Path) -> str | None:
    commit = git_output(project_root, ["rev-parse", "HEAD"])
    return commit or None


def base_commit_from_analysis(path: Path) -> str | None:
    if path.is_dir():
        path = path / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    commit = data.get("generatedFromCommit")
    return str(commit) if commit else None


def latest_previous_analysis_base(repo_name: str, current_run_root: Path) -> Path | None:
    runs_root = DEMO_OUT / repo_name / "runs"
    if not runs_root.is_dir():
        return None
    candidates: List[Path] = []

    def add_candidate(path: Path) -> None:
        try:
            if path.resolve().is_relative_to(current_run_root.resolve()):
                return
        except (OSError, ValueError):
            pass
        candidates.append(path)

    # Prefer package-sharded analysis because it scales better and is the normal
    # artifact when --analysis-full-output is disabled.
    for manifest in runs_root.glob("*/DemoTestCases/*-shards/manifest.json"):
        add_candidate(manifest.parent)
    for path in runs_root.glob("*/DemoTestCases/analysis.json"):
        add_candidate(path)

    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def resolve_incremental_base_analysis(args, repo_name: str, run_root: Path) -> Path:
    explicit = getattr(args, "analysis_base", None)
    if explicit:
        return Path(explicit)
    detected = latest_previous_analysis_base(repo_name, run_root)
    if detected is None:
        raise RuntimeError(
            "--analysis-incremental needs a base analysis. Pass --analysis-base, "
            "or run one AST analysis first so package shards exist."
        )
    print(f"Using previous AST base: {detected}")
    return detected


def resolve_incremental_diff_base(args, project_root: Path, base_analysis: Path) -> str:
    explicit = getattr(args, "analysis_diff_base", None)
    if explicit:
        return explicit
    base_commit = base_commit_from_analysis(base_analysis)
    if base_commit:
        return base_commit
    previous = git_output(project_root, ["rev-parse", "--verify", "HEAD~1"])
    if previous:
        return previous
    raise RuntimeError(
        "Could not infer incremental diff base. Pass --analysis-diff-base "
        "(for example origin/master, HEAD~1, or a specific commit)."
    )


def write_git_diff_lists(project_root: Path, demo_root: Path, diff_base: str, diff_head: str = "HEAD") -> tuple[Path, Path]:
    changed_path = demo_root / "changed.txt"
    deleted_path = demo_root / "deleted.txt"
    diff_range = diff_base if "..." in diff_base else f"{diff_base}...{diff_head}"
    changed = git_output(
        project_root,
        ["diff", "--name-only", "--diff-filter=ACMRT", diff_range, "--", "*.java"],
    )
    deleted = git_output(
        project_root,
        ["diff", "--name-only", "--diff-filter=D", diff_range, "--", "*.java"],
    )
    changed_path.write_text((changed + "\n") if changed else "", encoding="utf-8")
    deleted_path.write_text((deleted + "\n") if deleted else "", encoding="utf-8")
    print(f"Incremental diff range: {diff_range}")
    print(f"Incremental changed Java files: {len([x for x in changed.splitlines() if x.strip()])}")
    print(f"Incremental deleted Java files: {len([x for x in deleted.splitlines() if x.strip()])}")
    return changed_path, deleted_path


def looks_like_java_test_file(text: str) -> bool:
    sample = (text or "").lstrip()
    return bool(
        re.search(r"(?m)^\s*package\s+[\w.]+\s*;", sample)
        or re.search(r"(?m)^\s*import\s+", sample)
        or re.search(r"\bclass\s+\w+Test\b", sample)
    )


def _elapsed_since(start: float) -> float:
    return round(time.perf_counter() - start, 2)


def _print_timing_summary(timing: Dict) -> None:
    threads = timing.get("generation_threads", 1)
    targets = timing.get("target_count", 0)
    print("\nTiming (seconds):")
    print(f"  Setup:              {timing.get('setup_seconds', 0):.2f}")
    print(f"  Analysis:           {timing.get('analysis_seconds', 0):.2f}")
    print(f"  Generation:        {timing.get('generation_seconds', 0):.2f}  ({threads} threads, {targets} targets)")
    print(f"  Compile/repair:    {timing.get('compile_repair_seconds', 0):.2f}")
    print(f"  Runtime/coverage:  {timing.get('runtime_coverage_seconds', 0):.2f}")
    print(f"  Total:            {timing.get('total_seconds', 0):.2f}")


def _generate_one_target(
    *,
    index: int,
    total: int,
    t: Dict,
    args,
    project_root: Path,
    demo_root: Path,
    rejected_compile_root: Path,
    ast_analysis,
    project_java_version: str,
    junit_version: str,
    has_mockito: bool,
    project_types: List[str],
    used_test_class_names: Set[str],
    name_lock: threading.Lock,
    print_lock: threading.Lock,
    prompt_generator: PromptGenerator,
) -> TargetGenerationResult:
    worker_start = time.perf_counter()
    try:
        project_type_context = (
            project_type_context_from_analysis(ast_analysis, t)
            if ast_analysis is not None
            else list_project_type_context(project_root)
        )
        project_types_text = "\n".join(project_type_context[:250]) or ", ".join(project_types[:250])
        generated = prompt_generator.generate(
            PromptGenerationContext(
                target=t,
                project_root=project_root,
                ast_analysis=ast_analysis,
                project_java_version=project_java_version,
                junit_version=junit_version,
                has_mockito=has_mockito,
                project_types_text=project_types_text,
                target_mode=args.mode,
            )
        )

        test_class = ensure_unique_test_class_name(generated.test_class_name, t, args.mode)
        if t.get("class_prompt_slice"):
            test_class = apply_slice_test_class_name(t, test_class)
        with name_lock:
            test_class = ensure_unique_run_class_name(test_class, used_test_class_names, index)
            used_test_class_names.add(test_class)

        (demo_root / "prompts" / f"{test_class}.json").write_text(
            json.dumps(
                {
                    "test_class_name": test_class,
                    "prompt": generated.prompt,
                    **generated.metadata,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        with print_lock:
            print(f"[{index}/{total}] Generating {test_class} ...")

        related_sources = resolve_related_sources(project_root, ast_analysis, t)
        base_prompt = generated.prompt
        prompt_text = (
            f"Generate a JUnit {junit_version} test class named exactly `{test_class}`.\n"
            f"Target Java version: {project_java_version}. "
            f"{java_version_guidance(project_java_version)}\n\n{base_prompt}"
        )
        code = ""
        invalid_reason = ""
        out_path: Optional[Path] = None
        use_generation_compile_gate = not getattr(args, "skip_generation_compile_gate", True)
        source_bundle = f"{related_sources}\n{t.get('snippet') or ''}"
        for attempt in range(3):
            code = sanitize_java_output(ollama_generate(args.ollama_model, prompt_text))
            code = enforce_test_class_name(code, test_class)
            code = ensure_junit_imports(code, junit_version)
            code = remove_invented_api_stubs(code, source_bundle)
            code = rewrite_interface_mocks_to_concrete(code, related_sources)
            invalid_reason = (
                validate_java_test_output(code, test_class)
                or validate_junit_framework(code, junit_version)
                or validate_slice_test_coverage(code, t)
                or validate_test_coverage_quality(code, t, related_sources)
                or ""
            )
            if not invalid_reason and not (t.get("test_libraries") or {}).get("mockito", True):
                if re.search(r"\borg\.mockito\b|@Mock\b|@InjectMocks\b|\bMockito\b|\bwhen\s*\(", code):
                    invalid_reason = (
                        f"project has no Mockito dependency; rewrite as plain JUnit {junit_version} with no Mockito"
                    )
            if not invalid_reason and use_generation_compile_gate:
                with print_lock:
                    print(f"[{index}/{total}] Compile gate: {test_class} ...")
                trial_path = write_test_file(project_root, t["package"], test_class, code)
                compile_log, compile_rc = run_maven_test_compile(project_root, test_filter=test_class)
                if compile_rc != 0:
                    tail = concise_compile_error_log(compile_log, trial_path, max_lines=15)
                    invalid_reason = f"compilation failed:\n{tail}"
                    try:
                        trial_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                else:
                    out_path = trial_path
                    break
            elif not invalid_reason:
                break
            prompt_text = (
                f"Generate a JUnit {junit_version} test class named exactly `{test_class}`.\n"
                f"Target Java version: {project_java_version}. "
                f"{java_version_guidance(project_java_version)}\n\n{base_prompt}\n\n"
                f"Previous output was invalid because: {invalid_reason}. "
                "Return ONLY the complete Java test file, with no markdown or explanation."
            )

        if invalid_reason:
            rel = Path(t["package"].replace(".", "/")) / f"{test_class}.java" if t["package"] else Path(f"{test_class}.java")
            dest = rejected_compile_root / "invalid_generation" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            (dest.with_suffix(".txt")).write_text(invalid_reason, encoding="utf-8")
            elapsed = _elapsed_since(worker_start)
            return TargetGenerationResult(
                index=index,
                test_class=test_class,
                target=t,
                quality_log_entry={
                    "test_class": test_class,
                    "target": t,
                    "reason": invalid_reason,
                    "action": "rejected_invalid_generation",
                    "elapsed_seconds": elapsed,
                },
                elapsed_seconds=elapsed,
            )

        (demo_root / "generated" / f"{test_class}.java").write_text(code, encoding="utf-8")
        out_path = out_path or write_test_file(project_root, t["package"], test_class, code)
        return TargetGenerationResult(
            index=index,
            test_class=test_class,
            target=t,
            out_path=str(out_path),
            elapsed_seconds=_elapsed_since(worker_start),
        )
    except Exception as exc:
        return TargetGenerationResult(
            index=index,
            test_class="",
            target=t,
            error=str(exc),
            elapsed_seconds=_elapsed_since(worker_start),
        )


# ----------------------------
# Pipeline
# ----------------------------

def run_pipeline(args, prompt_generator: PromptGenerator | None = None) -> None:
    prompt_gen = prompt_generator or create_prompt_generator(args)
    run_started = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="seconds")
    stage_started = run_started
    timing: Dict[str, float] = {}

    # 1) Per-repo run root + clone/open repo
    DEMO_OUT.mkdir(exist_ok=True)
    repo_name = repo_name_from_arg(args.repo)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = DEMO_OUT / repo_name / "runs" / timestamp
    run_root.mkdir(parents=True, exist_ok=True)
    project_root = clone_or_update(args.repo, run_root / "repo", args.branch)
    if patch_obsolete_tools_jar_dependency(project_root):
        print("Patched obsolete com.sun:tools/tools.jar dependency for modern JDK compatibility.")

    demo_root = run_root / "DemoTestCases"
    (demo_root / "prompts").mkdir(parents=True, exist_ok=True)
    (demo_root / "generated").mkdir(parents=True, exist_ok=True)
    (demo_root / "coverage").mkdir(parents=True, exist_ok=True)
    (demo_root / "failures").mkdir(parents=True, exist_ok=True)
    (demo_root / "compile").mkdir(parents=True, exist_ok=True)
    (demo_root / "runtime").mkdir(parents=True, exist_ok=True)
    (demo_root / "rejected" / "compile").mkdir(parents=True, exist_ok=True)
    (demo_root / "rejected" / "runtime").mkdir(parents=True, exist_ok=True)
    rejected_compile_root = demo_root / "rejected" / "compile"
    max_refinement_iterations = max(
        0,
        int(getattr(args, "max_refinement_iterations", DEFAULT_MAX_ITERATION_REFINEMENTS)),
    )
    max_stagnation_iterations = max(
        0,
        int(getattr(args, "max_stagnation_iterations", DEFAULT_MAX_STAGNATION_ITERATIONS)),
    )
    max_runtime_repair_attempts = max(
        0,
        int(getattr(args, "max_runtime_repair_attempts", DEFAULT_MAX_RUNTIME_REPAIR_ATTEMPTS)),
    )
    runtime_repair_threads = max(
        1,
        int(getattr(args, "runtime_repair_threads", DEFAULT_RUNTIME_REPAIR_THREADS) or DEFAULT_RUNTIME_REPAIR_THREADS),
    )
    max_compile_repair_attempts = max(
        0,
        int(getattr(args, "max_compile_repair_attempts", DEFAULT_MAX_COMPILE_REPAIR_ATTEMPTS)),
    )
    compile_repair_threads = max(
        1,
        int(getattr(args, "compile_repair_threads", DEFAULT_COMPILE_REPAIR_THREADS) or DEFAULT_COMPILE_REPAIR_THREADS),
    )
    ollama_repair_timeout = max(
        30,
        int(getattr(args, "ollama_repair_timeout", DEFAULT_OLLAMA_REPAIR_TIMEOUT) or DEFAULT_OLLAMA_REPAIR_TIMEOUT),
    )
    ollama_repair_concurrency = max(
        1,
        int(
            getattr(args, "ollama_repair_concurrency", DEFAULT_OLLAMA_REPAIR_CONCURRENCY)
            or DEFAULT_OLLAMA_REPAIR_CONCURRENCY
        ),
    )

    # 2) Detect build system
    build = args.build
    if build == "auto":
        build = detect_build_system(project_root)
    if build != "maven":
        raise RuntimeError("This demo pipeline.py currently supports Maven repos only (pom.xml).")

    use_docker_maven = getattr(args, "docker_maven", False)
    project_java_version = resolve_project_java_version(project_root)
    docker_java_version = coerce_supported_version(project_java_version)
    pom_java_version = detect_java_version(project_root)
    compiler_java_version = project_java_version if pom_java_version is None else None
    docker_image = getattr(args, "docker_maven_image", None) or DEFAULT_DOCKER_MAVEN_IMAGE
    maven_cache_volume = getattr(args, "docker_maven_cache_volume", DEFAULT_DOCKER_MAVEN_CACHE_VOLUME)
    configure_maven_runner(
        use_docker=use_docker_maven,
        java_version=docker_java_version,
        docker_image=docker_image,
        maven_cache_volume=maven_cache_volume,
        compiler_java_version=compiler_java_version,
    )
    if use_docker_maven:
        ensure_docker_available()
        print(
            "Using Docker Maven image:",
            docker_image_name(),
            f"(Java {docker_java_version}, project Java {project_java_version}, cache volume: {maven_cache_volume})",
        )
    else:
        print(f"Detected project Java version: {project_java_version}")
    if compiler_java_version:
        print(
            f"Maven compiler properties will use Java {compiler_java_version} "
            "(pom.xml has no explicit Java version)."
        )

    junit_version = detect_junit_version(project_root)
    print(f"Detected JUnit version: {junit_version}")

    # 3) Discover packages
    pkgs = discover_packages(project_root)
    selected: List[str] = ["*"]
    if args.select_packages:
        selected = choose_packages_interactive(pkgs)
    elif args.packages and args.packages.strip().upper() != "ALL":
        selected = [p.strip() for p in args.packages.split(",") if p.strip()] or ["*"]

    timing["setup_seconds"] = _elapsed_since(stage_started)
    stage_started = time.perf_counter()

    # 4) Collect targets
    analysis_mode = getattr(args, "analysis_mode", "ast")
    ast_analysis: Dict | None = None
    resolved_analysis_base: str | None = None
    resolved_changed_files: str | None = None
    resolved_deleted_files: str | None = None
    resolved_diff_base: str | None = None
    if analysis_mode == "ast":
        analysis_path = demo_root / "analysis.json"
        analyzer_jar = Path(args.analyzer_jar) if getattr(args, "analyzer_jar", None) else None
        analysis_incremental = bool(getattr(args, "analysis_incremental", False))
        current_commit = current_git_commit(project_root)
        analysis_shards_dir = (
            Path(args.analysis_shards_dir).resolve()
            if getattr(args, "analysis_shards_dir", None)
            else demo_root / f"{repo_name}-shards"
        )
        if analysis_incremental:
            base_analysis_path = resolve_incremental_base_analysis(args, repo_name, run_root)
            resolved_analysis_base = str(base_analysis_path)
            changed_files_arg = getattr(args, "analysis_changed_files", None)
            deleted_files_arg = getattr(args, "analysis_deleted_files", None)
            if changed_files_arg:
                changed_files_path = Path(changed_files_arg)
                deleted_files_path = Path(deleted_files_arg) if deleted_files_arg else None
            else:
                diff_base = resolve_incremental_diff_base(args, project_root, base_analysis_path)
                resolved_diff_base = diff_base
                changed_files_path, deleted_files_path = write_git_diff_lists(project_root, demo_root, diff_base)
            resolved_changed_files = str(changed_files_path)
            resolved_deleted_files = str(deleted_files_path) if deleted_files_path else None
            ast_analysis = run_incremental_ast_analysis(
                project_root=project_root,
                output_path=analysis_path,
                base_analysis=base_analysis_path,
                changed_files=changed_files_path,
                deleted_files=deleted_files_path,
                analyzer_jar=analyzer_jar,
                classpath=getattr(args, "analysis_classpath", None),
                output_dir=analysis_shards_dir,
                threads=getattr(args, "analysis_threads", None),
                batch_size=getattr(args, "analysis_batch_size", None),
                ast_tree=getattr(args, "analysis_ast_tree", None),
                commit=current_commit,
                full_output=getattr(args, "analysis_full_output", True),
            )
        else:
            ast_analysis = run_ast_analysis(
                project_root=project_root,
                output_path=analysis_path,
                analyzer_jar=analyzer_jar,
                classpath=getattr(args, "analysis_classpath", None),
                output_dir=analysis_shards_dir,
                threads=getattr(args, "analysis_threads", None),
                batch_size=getattr(args, "analysis_batch_size", None),
                ast_tree=getattr(args, "analysis_ast_tree", None),
                commit=current_commit,
                full_output=getattr(args, "analysis_full_output", True),
            )
        targets = targets_from_analysis(
            analysis=ast_analysis,
            project_root=project_root,
            mode=args.mode,
            selected_packages=selected,
            max_files=args.max_files,
            max_targets=args.max_targets,
            skip_framework_classes=args.skip_framework_classes,
        )
        if analysis_incremental:
            targets = filter_targets_to_changed_files(
                targets,
                project_root,
                changed_files_path,
            )
    else:
        java_files = list_java_files(project_root)
        java_files = [f for f in java_files if file_in_selected_packages(f, project_root, selected)]

        targets: List[Dict] = []
        skip_keywords = ("application", "config", "filter", "security", "interceptor")
        scanned_files = 0
        for f in java_files:
            scanned_files += 1
            for t in extract_targets(f, args.mode):
                if args.skip_framework_classes:
                    cls_name = (t.get("class_name") or "").lower()
                    if any(k in cls_name for k in skip_keywords):
                        continue
                if is_interface_target(t):
                    continue
                targets.append(t)
                if len(targets) >= args.max_targets:
                    break
            if len(targets) >= args.max_targets:
                break
            if scanned_files >= args.max_files and targets:
                break

    if not targets:
        raise RuntimeError("No targets found (check src/main/java and selected packages).")

    timing["analysis_seconds"] = _elapsed_since(stage_started)
    stage_started = time.perf_counter()

    has_mockito = project_has_mockito(project_root)
    for t in targets:
        t["test_libraries"] = {
            "junit": junit_version,
            "mockito": has_mockito,
        }

    if (
        args.mode == "class"
        and getattr(args, "class_prompt_slices", 1) > 1
        and analysis_mode == "ast"
    ):
        targets = expand_class_targets(targets, slices=args.class_prompt_slices)
        if len(targets) > args.max_targets:
            targets = targets[: args.max_targets]

    # Save config and target list for reproducibility
    config = {
        "args": vars(args),
        "resolved_java_version": project_java_version,
        "docker_java_version": docker_java_version,
        "maven_compiler_java_version": compiler_java_version,
        "resolved_junit_version": junit_version,
        "selected_packages": selected,
        "models": {"ollama": args.ollama_model, "gpt": args.gpt_model},
        "prompt_mode": getattr(args, "prompt_mode", "llm"),
        "analysis_mode": analysis_mode,
        "analysis_incremental": bool(getattr(args, "analysis_incremental", False)),
        "analysis_base": getattr(args, "analysis_base", None),
        "analysis_changed_files": getattr(args, "analysis_changed_files", None),
        "analysis_deleted_files": getattr(args, "analysis_deleted_files", None),
        "analysis_diff_base": getattr(args, "analysis_diff_base", None),
        "resolved_analysis_base": resolved_analysis_base,
        "resolved_analysis_changed_files": resolved_changed_files,
        "resolved_analysis_deleted_files": resolved_deleted_files,
        "resolved_analysis_diff_base": resolved_diff_base,
        "max_refinement_iterations": max_refinement_iterations,
        "max_stagnation_iterations": max_stagnation_iterations,
        "max_runtime_repair_attempts": max_runtime_repair_attempts,
        "runtime_repair_threads": runtime_repair_threads,
        "max_compile_repair_attempts": max_compile_repair_attempts,
        "compile_repair_threads": compile_repair_threads,
        "ollama_repair_timeout": ollama_repair_timeout,
        "ollama_repair_concurrency": ollama_repair_concurrency,
        "test_libraries": {"junit": junit_version, "mockito": has_mockito},
    }
    (demo_root / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (demo_root / "targets.json").write_text(json.dumps(targets, indent=2), encoding="utf-8")

    # 5) Ollama writes prompts; Ollama generates tests; write into repo
    load_env_file(Path(__file__).resolve().parents[1] / ".env")

    generated_paths: List[str] = []
    test_target_map: Dict[str, Dict] = {}
    used_test_class_names: set[str] = set()

    project_types = list_project_types(project_root)
    project_types_text = (
        "\n".join(list_project_type_context(project_root)[:250])
        or ", ".join(project_types[:250])
    )
    generation_quality_log: List[Dict] = []

    generation_threads = max(
        1,
        getattr(args, "generation_threads", DEFAULT_GENERATION_THREADS) or DEFAULT_GENERATION_THREADS,
    )
    name_lock = threading.Lock()
    print_lock = threading.Lock()
    total_targets = len(targets)
    generation_elapsed: List[float] = []

    with ThreadPoolExecutor(max_workers=generation_threads) as pool:
        futures = [
            pool.submit(
                _generate_one_target,
                index=i,
                total=total_targets,
                t=t,
                args=args,
                project_root=project_root,
                demo_root=demo_root,
                rejected_compile_root=rejected_compile_root,
                ast_analysis=ast_analysis,
                project_java_version=project_java_version,
                junit_version=junit_version,
                has_mockito=has_mockito,
                project_types=project_types,
                used_test_class_names=used_test_class_names,
                name_lock=name_lock,
                print_lock=print_lock,
                prompt_generator=prompt_gen,
            )
            for i, t in enumerate(targets, 1)
        ]
        for fut in as_completed(futures):
            result = fut.result()
            if result.elapsed_seconds is not None:
                generation_elapsed.append(result.elapsed_seconds)
            if result.error:
                generation_quality_log.append(
                    {
                        "test_class": result.test_class or f"target_{result.index}",
                        "target": result.target,
                        "reason": result.error,
                        "action": "generation_exception",
                        "elapsed_seconds": result.elapsed_seconds,
                    }
                )
                with print_lock:
                    print(f"[{result.index}/{total_targets}] Generation failed: {result.error}")
                continue
            if result.test_class:
                test_target_map[result.test_class] = result.target
            if result.quality_log_entry:
                generation_quality_log.append(result.quality_log_entry)
            elif result.out_path:
                generated_paths.append(result.out_path)

    timing["generation_seconds"] = _elapsed_since(stage_started)
    if generation_elapsed:
        timing["generation_seconds_avg"] = round(sum(generation_elapsed) / len(generation_elapsed), 2)
        timing["generation_seconds_max"] = round(max(generation_elapsed), 2)
    print(
        f"\nGeneration finished in {timing['generation_seconds']:.2f}s "
        f"({generation_threads} threads, {total_targets} targets)"
    )
    stage_started = time.perf_counter()

    written_paths = list(generated_paths)
    (demo_root / "written_paths.json").write_text(json.dumps(written_paths, indent=2), encoding="utf-8")
    (demo_root / "generation_quality_log.json").write_text(
        json.dumps(generation_quality_log, indent=2), encoding="utf-8"
    )
    
    # Create isolation directory before writing any files into it.
    (demo_root / "isolation").mkdir(parents=True, exist_ok=True)

    (demo_root / "isolation").mkdir(parents=True, exist_ok=True)

    stale_root = demo_root / "isolation" / "stale_generated_tests"
    stale_moved = isolate_stale_generated_tests(project_root, generated_paths, stale_root)
    if stale_moved:
        print(f"Isolated {len(stale_moved)} stale LLM_Generated test files from prior runs.")
    (demo_root / "isolation" / "stale_moved_tests.json").write_text(
        json.dumps(stale_moved, indent=2), encoding="utf-8"
    )

    # Isolate pre-existing non-generated tests so compile/runtime can focus on
    # generated tests only.
    isolation_root = demo_root / "isolation" / "non_generated_tests"
    isolated_tests = isolate_non_generated_test_files(project_root, generated_paths, isolation_root)
    if isolated_tests:
        print(f"Isolated {len(isolated_tests)} pre-existing non-generated test files.")
    (demo_root / "isolation" / "moved_tests.json").write_text(
        json.dumps(isolated_tests, indent=2), encoding="utf-8"
    )

    # 6) Compile repair: parallel single-shot + method-level prune
    print("\nCompile stage: compiling ONLY generated tests:", GENERATED_PATTERN)
    print("Running Maven with RAT, Checkstyle, and Enforcer skipped for coverage-only execution.")

    compile_gate_log: List[Dict] = []
    repair_log: List[Dict] = []
    compile_repair_attempts = 0
    compile_repair_seconds = 0.0
    compile_rejected_methods = 0
    compile_blocked = False
    compile_blocked_reason = ""

    repo_types_text = ", ".join(list_repository_types(project_root))
    compile_log_path = demo_root / "compile" / "compile_log.txt"
    (demo_root / "compile" / "repair_prompts").mkdir(parents=True, exist_ok=True)

    def move_to_rejected_compile(failing_path: Path, errors: str, action: str) -> str:
        try:
            rel = failing_path.relative_to(project_root)
        except ValueError:
            rel = Path("src/test/java") / failing_path.name
        dest = rejected_compile_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(failing_path), str(dest))
        except FileNotFoundError:
            return ""
        remove_generated_snapshot(demo_root, failing_path.stem)
        snippet = "\n".join(strip_ansi(errors).splitlines()[-80:])
        compile_gate_log.append(
            {"file": str(failing_path), "moved_to": str(dest), "errors": snippet, "action": action}
        )
        return str(dest)

    def handle_compile_method_prune(
        failing_path: Path,
        compile_log: str,
        *,
        action: str,
    ) -> None:
        nonlocal compile_rejected_methods
        if not failing_path.exists():
            return
        write_failure_artifacts(failing_path, compile_log, demo_root / "failures", "compile_final")
        prune_result = prune_failing_compile_methods(
            failing_path,
            compile_log,
            demo_root,
            action=action,
        )
        fp = str(failing_path)
        if prune_result.pruned_methods:
            compile_rejected_methods += len(prune_result.pruned_methods)
            repair_log.append(
                {
                    "file": fp,
                    "action": f"{action}_methods_pruned",
                    "pruned_methods": prune_result.pruned_methods,
                    "remaining_methods": prune_result.remaining_methods,
                    "errors_tail": concise_compile_error_log(compile_log, failing_path),
                }
            )
        if prune_result.whole_file_reject or not prune_result.remaining_methods:
            move_to_rejected_compile(failing_path, compile_log, action=action)
            repair_log.append(
                {
                    "file": fp,
                    "action": action,
                    "errors_tail": concise_compile_error_log(compile_log, failing_path),
                    "reason": prune_result.reason or "whole_file_reject",
                }
            )
        elif prune_result.pruned_methods:
            sync_generated_snapshot(demo_root, failing_path)

    print("Compile attempt 1/1: mvn test-compile ...", flush=True)
    last_compile_log, compile_rc = run_maven_test_compile(project_root)
    compile_log_path.write_text(last_compile_log, encoding="utf-8")

    failing_paths: List[Path] = []
    if compile_rc != 0:
        failing_paths = extract_failing_test_paths(last_compile_log)
        failing_paths = [resolve_maven_test_path(project_root, p) for p in failing_paths]
        failing_paths = list(dict.fromkeys(str(p.resolve()) for p in failing_paths if p.exists()))
        failing_paths = [Path(p) for p in failing_paths]
        if not failing_paths:
            compile_blocked = True
            compile_blocked_reason = (
                "maven test-compile failed due to non-generated test compile errors"
            )
        else:
            for failing_path in failing_paths:
                if failing_path.exists():
                    write_failure_artifacts(
                        failing_path, last_compile_log, demo_root / "failures", "compile_before"
                    )

    any_compile_fix_applied = False
    if failing_paths and not compile_blocked:
        repair_started = time.perf_counter()
        repair_ctx = CompileRepairContext(
            project_root=project_root,
            demo_root=demo_root,
            model=args.gpt_model,
            java_version=project_java_version,
            junit_version=junit_version,
            max_compile_repair_attempts=max_compile_repair_attempts,
            test_target_map=test_target_map,
            ast_analysis=ast_analysis,
            compile_repair_threads=compile_repair_threads,
            repo_types_text=repo_types_text,
            ollama_repair_timeout=ollama_repair_timeout,
            ollama_repair_concurrency=ollama_repair_concurrency,
        )
        print(
            f"Compile repair: {len(failing_paths)} failing file(s), "
            f"{compile_repair_threads} thread(s), "
            f"Ollama timeout {ollama_repair_timeout}s, "
            f"concurrency {ollama_repair_concurrency} ...",
            flush=True,
        )
        repair_results = run_parallel_compile_repair(failing_paths, last_compile_log, repair_ctx)
        compile_repair_seconds = round(time.perf_counter() - repair_started, 2)

        for result in repair_results:
            fp = str(result.failing_path)
            if result.action in (
                "fixed",
                "deterministic_fix",
                "deterministic_fix+fixed",
                "already_compiles",
            ) and result.fixed_code:
                result.failing_path.write_text(result.fixed_code, encoding="utf-8")
                sync_generated_snapshot(demo_root, result.failing_path)
                any_compile_fix_applied = True
                compile_repair_attempts += 1
                repair_log.append(
                    {
                        "file": fp,
                        "action": result.action,
                        "errors_tail": concise_compile_error_log(result.errors_tail, result.failing_path),
                        "repair_prompt_generated": result.repair_prompt_generated,
                        "elapsed_seconds": result.elapsed_seconds,
                    }
                )
            elif result.action.startswith("rejected"):
                compile_repair_attempts += 1
                if result.failing_path.exists():
                    handle_compile_method_prune(
                        result.failing_path,
                        result.errors_tail or last_compile_log,
                        action=result.action,
                    )
                else:
                    repair_log.append(
                        {
                            "file": fp,
                            "action": result.action,
                            "errors_tail": result.errors_tail,
                            "repair_prompt_generated": result.repair_prompt_generated,
                            "elapsed_seconds": result.elapsed_seconds,
                        }
                    )

    if any_compile_fix_applied and not compile_blocked:
        print("Compile verify: mvn test-compile ...", flush=True)
        last_compile_log, compile_rc = run_maven_test_compile(project_root)
        with compile_log_path.open("a", encoding="utf-8") as f:
            f.write("\n" + ("-" * 80) + "\n")
            f.write(last_compile_log)

    if compile_rc != 0 and not compile_blocked:
        remaining_paths = extract_failing_test_paths(last_compile_log)
        remaining_paths = [resolve_maven_test_path(project_root, p) for p in remaining_paths]
        remaining_paths = list(dict.fromkeys(str(p.resolve()) for p in remaining_paths if p.exists()))
        remaining_paths = [Path(p) for p in remaining_paths]
        if not remaining_paths:
            compile_blocked = True
            compile_blocked_reason = (
                "maven test-compile failed due to non-generated test compile errors"
            )
        else:
            for failing_path in remaining_paths:
                handle_compile_method_prune(
                    failing_path,
                    last_compile_log,
                    action="rejected_after_compile_verify",
                )

    if not compile_blocked:
        final_compile_log, final_compile_rc = run_maven_test_compile(project_root)
        if final_compile_rc != 0:
            compile_blocked = True
            compile_blocked_reason = "generated tests still do not compile after repair and method pruning"

    (demo_root / "compile" / "repair_log.json").write_text(json.dumps(repair_log, indent=2), encoding="utf-8")
    (demo_root / "compile" / "compile_repair_log.json").write_text(
        json.dumps(repair_log, indent=2), encoding="utf-8"
    )
    (demo_root / "coverage" / "repair_log.json").write_text(json.dumps(repair_log, indent=2), encoding="utf-8")
    (demo_root / "compile" / "compile_gate_log.json").write_text(
        json.dumps(compile_gate_log, indent=2), encoding="utf-8"
    )

    generated_paths = resolve_survivor_paths(generated_paths)
    compile_survivors = len(generated_paths)
    compile_rejected_files = list((demo_root / "rejected" / "compile").rglob(f"{GENERATED_PREFIX}*Test.java"))
    compile_rejected = len(compile_rejected_files)

    timing["compile_repair_seconds"] = _elapsed_since(stage_started)
    if compile_repair_seconds:
        timing["compile_repair_seconds_detail"] = compile_repair_seconds
    stage_started = time.perf_counter()

    early_stop = compile_survivors == 0
    runtime_blocked = False
    runtime_blocked_reason = ""

    # 7) Runtime stage: run tests with JaCoCo agent; runtime repair on failures
    test_log = ""
    test_rc = 0
    report_log = ""
    report_rc = 0

    runtime_gate_log: List[Dict] = []
    runtime_repair_log: List[Dict] = []
    runtime_repair_attempts = 0
    runtime_repair_seconds = 0.0
    runtime_rejected_methods = 0
    tests_passed_first_run: Optional[int] = None
    tests_passed_after_repair: Optional[int] = None
    runtime_rejected_details: List[Dict] = []
    rejected_runtime_root = demo_root / "rejected" / "runtime"
    (demo_root / "runtime" / "repair_prompts").mkdir(parents=True, exist_ok=True)

    runtime_ready = False
    if not early_stop:
        generated_paths, runtime_ready, runtime_blocked_reason = prepare_survivors_for_runtime(
            project_root,
            demo_root,
            generated_paths,
        )
        compile_survivors = len(generated_paths)
        if not runtime_ready:
            runtime_blocked = True
            print(f"\nRuntime skipped: {runtime_blocked_reason.splitlines()[0]}", flush=True)
        elif compile_blocked:
            print(
                f"\nCompile had partial failures ({compile_blocked_reason}); "
                f"continuing with {compile_survivors} survivor test file(s).",
                flush=True,
            )

    if not early_stop and runtime_ready:
        print("\nRuntime stage: running ONLY generated tests with JaCoCo agent")

        def move_to_rejected_runtime(
            failing_path: Path,
            errors: str,
            action: str,
            *,
            method_name: str = "",
            iterations: int = 1,
        ) -> None:
            try:
                rel = failing_path.relative_to(project_root)
            except ValueError:
                rel = Path("src/test/java") / failing_path.name
            dest = rejected_runtime_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(failing_path), str(dest))
            except FileNotFoundError:
                return
            remove_generated_snapshot(demo_root, failing_path.stem)
            trimmed = concise_runtime_error_log(
                errors,
                class_name=failing_path.stem,
                method_name=method_name,
            )
            failed_txt = dest.with_suffix(".failed.txt")
            failed_txt.write_text(
                f"action={action}\niterations={iterations}\nmethod={method_name or '*'}\n\n{trimmed}",
                encoding="utf-8",
            )
            runtime_gate_log.append(
                {
                    "file": str(failing_path),
                    "moved_to": str(dest),
                    "action": action,
                    "method": method_name or None,
                    "iterations": iterations,
                    "errors_tail": trimmed,
                }
            )

        def record_runtime_rejection(
            failing_path: Path,
            action: str,
            errors_tail: str,
            method_names: List[str],
            *,
            iterations: int = 1,
        ) -> None:
            nonlocal runtime_rejected_methods
            method_names = [m for m in method_names if m]
            method_label = ", ".join(method_names) if method_names else ""
            write_failure_artifacts(
                failing_path, errors_tail, demo_root / "failures", "runtime_final"
            )
            fp = str(failing_path)
            if method_names and failing_path.exists():
                prune_result = prune_failing_runtime_methods(
                    failing_path,
                    method_names,
                    errors_tail,
                    demo_root,
                    action=action,
                )
                if prune_result.pruned_methods:
                    runtime_rejected_methods += len(prune_result.pruned_methods)
                if prune_result.whole_file_reject or not prune_result.remaining_methods:
                    move_to_rejected_runtime(
                        failing_path,
                        errors_tail,
                        action=action,
                        method_name=method_label,
                        iterations=iterations,
                    )
                else:
                    sync_generated_snapshot(demo_root, failing_path)
                    runtime_gate_log.append(
                        {
                            "file": fp,
                            "action": f"{action}_methods_pruned",
                            "method": method_label or None,
                            "iterations": iterations,
                            "pruned_methods": prune_result.pruned_methods,
                            "remaining_methods": prune_result.remaining_methods,
                            "errors_tail": errors_tail,
                        }
                    )
            else:
                move_to_rejected_runtime(
                    failing_path,
                    errors_tail,
                    action=action,
                    method_name=method_label,
                    iterations=iterations,
                )
            runtime_repair_log.append(
                {
                    "file": fp,
                    "method": method_label or None,
                    "action": action,
                    "errors_tail": errors_tail,
                    "pruned_methods": method_names or None,
                }
            )
            runtime_rejected_details.append(
                {
                    "file": fp,
                    "method": method_label or None,
                    "action": action,
                    "iterations": iterations,
                    "errors_tail": errors_tail,
                }
            )

        print("Runtime attempt 1/1: mvn test ...", flush=True)
        test_log, test_rc = run_maven_tests(project_root)
        (demo_root / "runtime" / "test_log.txt").write_text(test_log, encoding="utf-8")

        tests_passed_first_run = count_passed_generated_tests(
            project_root / "target" / "surefire-reports"
        )

        failures = extract_runtime_failures(project_root / "target" / "surefire-reports")
        any_fix_applied = False

        if failures:
            for grouped in group_failures_by_file(failures, project_root):
                if grouped.failing_path.exists():
                    write_failure_artifacts(
                        grouped.failing_path,
                        combined_stack_trace(grouped),
                        demo_root / "failures",
                        "runtime_before",
                    )

            repair_started = time.perf_counter()
            repair_ctx = RuntimeRepairContext(
                project_root=project_root,
                demo_root=demo_root,
                model=args.gpt_model,
                java_version=project_java_version,
                junit_version=junit_version,
                max_runtime_repair_attempts=max_runtime_repair_attempts,
                test_target_map=test_target_map,
                ast_analysis=ast_analysis,
                runtime_repair_threads=runtime_repair_threads,
            )
            print(
                f"Runtime repair: {len(group_failures_by_file(failures, project_root))} failing file(s), "
                f"{runtime_repair_threads} thread(s) ...",
                flush=True,
            )
            repair_results = run_parallel_runtime_repair(failures, repair_ctx)
            runtime_repair_seconds = round(time.perf_counter() - repair_started, 2)

            for result in repair_results:
                fp = str(result.failing_path)
                method_label = ", ".join(result.method_names) if result.method_names else None
                if result.action in ("fixed", "deterministic_fix", "deterministic_fix+fixed") and result.fixed_code:
                    result.failing_path.write_text(result.fixed_code, encoding="utf-8")
                    sync_generated_snapshot(demo_root, result.failing_path)
                    any_fix_applied = True
                    runtime_repair_attempts += 1
                    runtime_repair_log.append(
                        {
                            "file": fp,
                            "method": method_label,
                            "action": result.action,
                            "errors_tail": result.errors_tail,
                            "repair_prompt_generated": result.repair_prompt_generated,
                            "elapsed_seconds": result.elapsed_seconds,
                        }
                    )
                elif result.action.startswith("rejected"):
                    if result.failing_path.exists():
                        record_runtime_rejection(
                            result.failing_path,
                            result.action,
                            result.errors_tail,
                            result.method_names,
                        )
                    runtime_repair_attempts += 1
                    if not result.failing_path.exists():
                        runtime_repair_log.append(
                            {
                                "file": fp,
                                "method": method_label,
                                "action": result.action,
                                "errors_tail": result.errors_tail,
                                "repair_prompt_generated": result.repair_prompt_generated,
                                "elapsed_seconds": result.elapsed_seconds,
                            }
                        )

        if any_fix_applied:
            print("Runtime verify: mvn test ...", flush=True)
            test_log, test_rc = run_maven_tests(project_root)
            (demo_root / "runtime" / "test_log_verify.txt").write_text(test_log, encoding="utf-8")
            remaining_failures = extract_runtime_failures(
                project_root / "target" / "surefire-reports"
            )
            for grouped in group_failures_by_file(remaining_failures, project_root):
                if not grouped.failing_path.exists():
                    continue
                stack_trace = combined_stack_trace(grouped)
                record_runtime_rejection(
                    grouped.failing_path,
                    "rejected_after_verify",
                    stack_trace,
                    [m.get("method_name", "") for m in grouped.methods],
                )

        tests_passed_after_repair = count_passed_generated_tests(
            project_root / "target" / "surefire-reports"
        )

        (demo_root / "runtime" / "runtime_gate_log.json").write_text(
            json.dumps(runtime_gate_log, indent=2), encoding="utf-8"
        )
        (demo_root / "runtime" / "runtime_repair_log.json").write_text(
            json.dumps(runtime_repair_log, indent=2), encoding="utf-8"
        )

        # Always attempt report (even if some tests failed)
        report_log, report_rc = run_maven_report(project_root)

        xml_after_runtime = project_root / "target" / "site" / "jacoco" / "jacoco.xml"
        survivor_paths = [p for p in generated_paths if Path(p).exists()]
        runtime_coverage_snapshot = (
            parse_jacoco_xml(xml_after_runtime) if xml_after_runtime.exists() else {}
        )
        coverage_below_threshold = bool(runtime_coverage_snapshot) and any(
            runtime_coverage_snapshot.get(metric, 0.0) < DEFAULT_COVERAGE_THRESHOLD
            for metric in ("line_coverage", "instruction_coverage", "branch_coverage")
        )
        if (
            report_rc == 0
            and xml_after_runtime.exists()
            and survivor_paths
            and max_refinement_iterations > 0
            and coverage_below_threshold
        ):
            def coverage_related_sources(target: Dict) -> str:
                return resolve_related_sources(project_root, ast_analysis, target)

            refinement = CoverageRefinement(
                project_root=project_root,
                demo_root=demo_root,
                model=args.ollama_model,
                java_version=project_java_version,
                junit_version=junit_version,
                has_mockito=has_mockito,
                ast_analysis=ast_analysis,
                generated_paths=survivor_paths,
                test_target_map=test_target_map,
                related_sources_provider=coverage_related_sources,
                project_types_text=project_types_text,
                max_iterations=max_refinement_iterations,
                max_stagnation=max_stagnation_iterations,
            )
            refinement.run(xml_after_runtime)
            generated_paths = resolve_survivor_paths(generated_paths)
            tests_passed_after_repair = count_passed_generated_tests(
                project_root / "target" / "surefire-reports"
            )

    timing["runtime_coverage_seconds"] = _elapsed_since(stage_started)
    if runtime_repair_seconds:
        timing["runtime_repair_seconds"] = runtime_repair_seconds

    # 8) Collect logs
    build_log = (test_log or "") + "\n" + (report_log or "")
    (demo_root / "coverage" / "build_log.txt").write_text(build_log, encoding="utf-8")

    # 9) Coverage paths
    xml = project_root / "target" / "site" / "jacoco" / "jacoco.xml"
    html = project_root / "target" / "site" / "jacoco" / "index.html"
    xml_path = xml if xml.exists() else None
    html_path = html if html.exists() else None

    jacoco_exec_found = (project_root / "target" / "jacoco.exec").exists()
    zero_coverage = {
        "line_coverage": 0.0,
        "instruction_coverage": 0.0,
        "branch_coverage": 0.0,
    }
    coverage: Dict[str, float] = parse_jacoco_xml(xml_path) if xml_path else {}
    if early_stop or runtime_blocked:
        coverage = zero_coverage
    elif not coverage:
        coverage = zero_coverage

    # Copy report into demo_out
    report_dir = demo_root / "coverage" / "report"
    if report_dir.exists():
        shutil.rmtree(report_dir)
    if html_path:
        src = project_root / "target" / "site" / "jacoco"
        shutil.copytree(src, report_dir)
    if xml_path:
        shutil.copyfile(xml_path, demo_root / "coverage" / "jacoco.xml")

    # Runtime counts
    refinement_log_path = demo_root / "coverage_refinement" / "coverage_refinement_log.json"
    runtime_counts = None
    if refinement_log_path.exists():
        runtime_counts = parse_surefire_reports(project_root / "target" / "surefire-reports")
    if runtime_counts is None:
        runtime_counts = parse_surefire_summary(test_log) if test_log else None
    if runtime_counts is None:
        runtime_counts = parse_surefire_reports(project_root / "target" / "surefire-reports") or {}

    # Survivors after runtime stage (what remains in repo)
    generated_paths = resolve_survivor_paths(generated_paths)
    prune_generated_snapshots(demo_root, generated_paths)
    sync_generated_snapshots(demo_root, generated_paths)
    survivor_test_methods = survivor_method_map(generated_paths)
    (demo_root / "written_paths.json").write_text(
        json.dumps(generated_paths, indent=2), encoding="utf-8"
    )
    runtime_survivors = len(generated_paths)
    runtime_rejected_files = list((demo_root / "rejected" / "runtime").rglob(f"{GENERATED_PREFIX}*Test.java"))
    runtime_rejected = len(runtime_rejected_files)

    coverage_quality_issue = None
    if (
        not early_stop
        and not runtime_blocked
        and runtime_survivors > 0
        and (runtime_counts.get("tests_run") or 0) > 0
        and coverage
        and all(coverage.get(k, 0.0) == 0.0 for k in zero_coverage)
    ):
        coverage_quality_issue = (
            "generated tests passed but covered 0 production lines; likely mock-only or non-executing tests"
        )
        (demo_root / "coverage" / "quality_gate.txt").write_text(
            coverage_quality_issue, encoding="utf-8"
        )

    coverage_refinement = None
    refinement_log_path = demo_root / "coverage_refinement" / "coverage_refinement_log.json"
    if refinement_log_path.exists():
        try:
            coverage_refinement = json.loads(refinement_log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            coverage_refinement = None

    # No-report reason
    if not (report_dir / "index.html").exists():
        if early_stop:
            reason = "0 generated tests compiled"
        elif runtime_blocked:
            reason = runtime_blocked_reason or "runtime skipped"
        elif not jacoco_exec_found:
            reason = "jacoco.exec not found (tests did not execute far enough)"
        else:
            reason = "coverage report not found"
        (demo_root / "coverage" / "no_report_reason.txt").write_text(reason, encoding="utf-8")

    timing["total_seconds"] = _elapsed_since(run_started)
    finished_at = datetime.now().isoformat(timespec="seconds")
    timing_summary = {
        "started_at": started_at,
        "finished_at": finished_at,
        "generation_threads": generation_threads,
        "target_count": total_targets,
        **timing,
    }

    summary = {
        "repo": args.repo,
        "project_root": str(project_root),
        "build": "maven",
        "mode": args.mode,
        "selected_packages": selected,
        "ollama_model": args.ollama_model,
        "gpt_model": args.gpt_model,
        "max_refinement_iterations": max_refinement_iterations,
        "max_stagnation_iterations": max_stagnation_iterations,
        "max_runtime_repair_attempts": max_runtime_repair_attempts,
        "runtime_repair_threads": runtime_repair_threads,
        "runtime_repair_seconds": runtime_repair_seconds,
        "max_compile_repair_attempts": max_compile_repair_attempts,
        "compile_repair_threads": compile_repair_threads,
        "ollama_repair_timeout": ollama_repair_timeout,
        "ollama_repair_concurrency": ollama_repair_concurrency,
        "compile_repair_seconds": compile_repair_seconds,
        "generated_total": len(written_paths) + len(generation_quality_log),
        "generated_written": len(written_paths),
        "generation_rejected": len(generation_quality_log),
        "compile_survivors": compile_survivors,
        "compile_rejected": int(compile_rejected),
        "compile_rejected_methods": int(compile_rejected_methods),
        "compile_repair_attempts": compile_repair_attempts,
        "compile_blocked": compile_blocked,
        "compile_blocked_reason": compile_blocked_reason or None,
        "runtime_blocked": runtime_blocked,
        "runtime_blocked_reason": runtime_blocked_reason or None,
        "runtime_survivors": runtime_survivors,
        "runtime_rejected": int(runtime_rejected),
        "runtime_rejected_methods": int(runtime_rejected_methods),
        "runtime_repair_attempts": runtime_repair_attempts,
        "runtime_rejected_details": runtime_rejected_details,
        "survivor_test_methods": survivor_test_methods,
        "tests_passed_first_run": tests_passed_first_run,
        "tests_passed_after_repair": tests_passed_after_repair,
        "isolated_non_generated_tests": len(isolated_tests),
        "isolated_stale_generated_tests": len(stale_moved),
        "isolated_tests_manifest": str((demo_root / "isolation" / "moved_tests.json").resolve()),
        "survivor_test_files_in_repo": generated_paths,
        "rejected_compile_dir": str((demo_root / "rejected" / "compile").resolve()),
        "rejected_runtime_dir": str((demo_root / "rejected" / "runtime").resolve()),
        "jacoco_exec_found": jacoco_exec_found,
        "coverage": coverage,
        "coverage_refinement": coverage_refinement,
        "coverage_quality_issue": coverage_quality_issue,
        "tests_run": runtime_counts.get("tests_run"),
        "failures": runtime_counts.get("failures"),
        "errors": runtime_counts.get("errors"),
        "skipped": runtime_counts.get("skipped"),
        "coverage_report_index": str((report_dir / "index.html").resolve())
        if (report_dir / "index.html").exists()
        else None,
        "note": "Tests were written locally into src/test/java but NOT committed or pushed.",
        "timing": timing_summary,
    }
    (demo_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Restore isolated test files back into cloned repo tree.
    restore_isolated_test_files(isolated_tests)

    print("\n=== DONE ===")
    print(f"Summary: {demo_root / 'summary.json'}")
    if summary["coverage_report_index"]:
        print("Coverage HTML:", summary["coverage_report_index"])
    else:
        print(f"Coverage report not found. Check {demo_root / 'coverage' / 'build_log.txt'}")

    if coverage:
        print("Coverage:")
        print(f"- Line:        {coverage['line_coverage']*100:.2f}%")
        print(f"- Instruction: {coverage['instruction_coverage']*100:.2f}%")
        print(f"- Branch:      {coverage['branch_coverage']*100:.2f}%")

    _print_timing_summary(timing_summary)


class Pipeline:
    def __init__(self, prompt_generator: PromptGenerator) -> None:
        self._prompt_generator = prompt_generator

    def run(self, args) -> None:
        run_pipeline(args, prompt_generator=self._prompt_generator)
