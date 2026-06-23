from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List



from demo.config import (
    DEFAULT_DOCKER_MAVEN_CACHE_VOLUME,
    DEFAULT_DOCKER_MAVEN_IMAGE,
    DEMO_OUT,
    GENERATED_PATTERN,
    GENERATED_PREFIX,
)
from demo.coverage.maven import (
    extract_failing_test_paths,
    extract_first_failing_test_path,
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
from demo.llm.prompt_writer import (
    ollama_repair_test,
    ollama_runtime_repair_test,
    ollama_write_prompt,
)
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
    run_ast_analysis,
    targets_from_analysis,
)
from demo.targets import _extract_imports_context_from_text, extract_targets
from demo.utils import (
    ensure_unique_run_class_name,
    ensure_junit_imports,
    enforce_test_class_name,
    find_concrete_impls,
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
TYPE_NAME_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")
SKIP_RELATED_TYPES = frozenset(
    {
        "String", "Integer", "Boolean", "Long", "Double", "Float", "Short", "Byte",
        "Character", "Object", "Class", "Void", "Override", "Test", "BeforeEach",
        "AfterEach", "Mock", "InjectMocks", "Collection", "List", "Map", "Set",
        "Optional", "Arrays", "Collections", "Assertions",
    }
)


def collect_related_type_sources(project_root: Path, target: Dict) -> str:
    own_class = target.get("class_name") or ""
    text = " ".join(
        [
            target.get("signature") or "",
            target.get("snippet") or "",
            own_class,
        ]
    )
    related_names = {
        name for name in TYPE_NAME_RE.findall(text)
        if name not in SKIP_RELATED_TYPES and name != own_class
    }
    if not related_names:
        return ""

    by_name: Dict[str, str] = {}
    for f in list_java_files(project_root):
        if f.stem not in related_names or f.stem in by_name:
            continue
        try:
            rel = f.relative_to(project_root)
            by_name[f.stem] = f"// {rel}\n{f.read_text(encoding='utf-8', errors='ignore')}"
        except (OSError, ValueError):
            continue

    # Pull in concrete classes that implement referenced interfaces.
    interface_names = [
        name for name, src in by_name.items()
        if re.search(rf"\binterface\s+{re.escape(name)}\b", src)
    ]
    if interface_names:
        for f in list_java_files(project_root):
            if f.stem in by_name:
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for iface in interface_names:
                if re.search(
                    rf"\bclass\s+{re.escape(f.stem)}\s+implements\s+[^{{;]*\b{re.escape(iface)}\b",
                    txt,
                ):
                    rel = f.relative_to(project_root)
                    by_name[f.stem] = f"// {rel}\n{txt}"
                    break

    return "\n\n".join(by_name[name] for name in sorted(by_name))


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

        # Keep generated tests in place.
        if resolved in generated_set or test_file.name.startswith(GENERATED_PREFIX):
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
    This mirrors CubeTester-style focused refinement: compiler/runtime logs should
    describe the specific generated test being repaired, not unrelated failures.
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


def looks_like_java_test_file(text: str) -> bool:
    sample = (text or "").lstrip()
    return bool(
        re.search(r"(?m)^\s*package\s+[\w.]+\s*;", sample)
        or re.search(r"(?m)^\s*import\s+", sample)
        or re.search(r"\bclass\s+\w+Test\b", sample)
    )


def build_direct_generation_prompt(
    target: Dict,
    test_class: str,
    project_types_text: str,
    java_version: str,
    junit_version: str,
    has_mockito: bool,
) -> str:
    pkg = target.get("package") or "(default)"
    mockito_rule = (
        "Mockito is available, but do NOT mock domain/value interfaces when a concrete implementation exists. "
        "Prefer real objects so production code executes."
        if has_mockito
        else "Mockito is not available; do not import or use org.mockito, @Mock, when, verify, or MockitoAnnotations."
    )
    junit_rule = (
        "Use org.junit.Test and static org.junit.Assert.*. Do not use JUnit Jupiter."
        if junit_version == "4"
        else "Use org.junit.jupiter.api.Test and org.junit.jupiter.api.Assertions.*. Do not use org.junit.Test."
    )
    return f"""
Output ONLY a complete Java test file. No markdown. No explanations.
Generate test class exactly: {test_class}
Package: {pkg}
Target Java version: {java_version}. {java_version_guidance(java_version)}
Target framework: JUnit {junit_version}. {junit_rule}
{mockito_rule}

Hard rules:
- Every @Test must call real production code from target class {target.get("class_name")}.
- Do not mock the class under test.
- For interface parameters, use a concrete implementation from the project type context when present.
- Do not put null as the first element of arrays unless the test explicitly expects NullPointerException.
- For Item[] or similar arrays, create non-null concrete Item implementations for all normal-path tests.
- Use only methods, fields, and constructors shown in the source/AST/project type context.
- At least 3 @Test methods with concrete assertions.

Target:
- class: {target.get("class_name")}
- method: {target.get("method_name") or "(class mode)"}
- signature: {target.get("signature") or "(entire class)"}

Source/AST summary:
{target.get("snippet") or ""}

Project type context:
{project_types_text}
""".strip()


# ----------------------------
# Pipeline
# ----------------------------

def run_pipeline(args) -> None:
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
    max_refinement_iterations = max(0, int(getattr(args, "max_refinement_iterations", 5)))

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

    # 4) Collect targets
    analysis_mode = getattr(args, "analysis_mode", "ast")
    ast_analysis: Dict | None = None
    if analysis_mode == "ast":
        analysis_path = demo_root / "analysis.json"
        analyzer_jar = Path(args.analyzer_jar) if getattr(args, "analyzer_jar", None) else None
        ast_analysis = run_ast_analysis(
            project_root=project_root,
            output_path=analysis_path,
            analyzer_jar=analyzer_jar,
            classpath=getattr(args, "analysis_classpath", None),
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

    has_mockito = project_has_mockito(project_root)
    for t in targets:
        t["test_libraries"] = {
            "junit": junit_version,
            "mockito": has_mockito,
        }

    # Save config and target list for reproducibility
    config = {
        "args": vars(args),
        "resolved_java_version": project_java_version,
        "docker_java_version": docker_java_version,
        "maven_compiler_java_version": compiler_java_version,
        "resolved_junit_version": junit_version,
        "selected_packages": selected,
        "models": {"ollama": args.ollama_model, "gpt": args.gpt_model},
        "analysis_mode": analysis_mode,
        "max_refinement_iterations": max_refinement_iterations,
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
    project_type_context = (
        project_type_context_from_analysis(ast_analysis)
        if ast_analysis is not None
        else list_project_type_context(project_root)
    )
    # Keep the context bounded (avoid huge prompts)
    project_types_text = "\n".join(project_type_context[:800]) or ", ".join(project_types[:800])
    generation_quality_log: List[Dict] = []

    for i, t in enumerate(targets, 1):
        g = ollama_write_prompt(args.gpt_model, t, project_types_text, java_version=project_java_version)

        test_class = g.get("test_class_name", "")
        test_class = ensure_unique_test_class_name(test_class, t, args.mode)
        test_class = ensure_unique_run_class_name(test_class, used_test_class_names, i)
        used_test_class_names.add(test_class)

        test_target_map[test_class] = t

        (demo_root / "prompts" / f"{test_class}.json").write_text(
            json.dumps({"test_class_name": test_class, "prompt": g["prompt"]}, indent=2),
            encoding="utf-8",
        )

        print(f"[{i}/{len(targets)}] Generating {test_class} ...")
        related_sources = (
            related_type_sources_from_analysis(ast_analysis, t)
            if ast_analysis is not None
            else collect_related_type_sources(project_root, t)
        )
        base_prompt = g.get("prompt", "")
        if looks_like_java_test_file(base_prompt):
            base_prompt = build_direct_generation_prompt(
                target=t,
                test_class=test_class,
                project_types_text=project_types_text,
                java_version=project_java_version,
                junit_version=junit_version,
                has_mockito=has_mockito,
            )
        if related_sources:
            impl_hints: List[str] = []
            sig_text = t.get("signature") or ""
            for type_name in TYPE_NAME_RE.findall(sig_text):
                for impl in find_concrete_impls(related_sources, type_name):
                    impl_hints.append(
                        f"For `{type_name}` parameters, use `new {impl}(...)` — do NOT @Mock `{type_name}`."
                    )
            base_prompt = (
                f"{base_prompt}\n\n"
                "Related type sources (use ONLY APIs shown here; do not invent methods):\n"
                f"{related_sources}"
            )
            if impl_hints:
                base_prompt += "\n\n" + "\n".join(dict.fromkeys(impl_hints))
        prompt_text = (
            f"Generate a JUnit {junit_version} test class named exactly `{test_class}`.\n"
            f"Target Java version: {project_java_version}. "
            f"{java_version_guidance(project_java_version)}\n\n{base_prompt}"
        )
        code = ""
        invalid_reason = ""
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
                or validate_test_coverage_quality(code, t, related_sources)
                or ""
            )
            if not invalid_reason and not (t.get("test_libraries") or {}).get("mockito", True):
                if re.search(r"\borg\.mockito\b|@Mock\b|@InjectMocks\b|\bMockito\b|\bwhen\s*\(", code):
                    invalid_reason = (
                        f"project has no Mockito dependency; rewrite as plain JUnit {junit_version} with no Mockito"
                    )
            if not invalid_reason:
                break
            prompt_text = (
                f"Generate a JUnit {junit_version} test class named exactly `{test_class}`.\n"
                f"Target Java version: {project_java_version}. "
                f"{java_version_guidance(project_java_version)}\n\n{base_prompt}\n\n"
                f"Previous output was invalid because: {invalid_reason}. "
                "Return ONLY the complete Java test file, with no markdown or explanation."
            )

        (demo_root / "generated" / f"{test_class}.java").write_text(code, encoding="utf-8")
        if invalid_reason:
            rel = Path(t["package"].replace(".", "/")) / f"{test_class}.java" if t["package"] else Path(f"{test_class}.java")
            dest = rejected_compile_root / "invalid_generation" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            (dest.with_suffix(".txt")).write_text(invalid_reason, encoding="utf-8")
            generation_quality_log.append(
                {
                    "test_class": test_class,
                    "target": t,
                    "reason": invalid_reason,
                    "action": "rejected_invalid_generation",
                }
            )
            continue

        out_path = write_test_file(project_root, t["package"], test_class, code)
        generated_paths.append(str(out_path))

    written_paths = list(generated_paths)
    (demo_root / "written_paths.json").write_text(json.dumps(written_paths, indent=2), encoding="utf-8")
    (demo_root / "generation_quality_log.json").write_text(
        json.dumps(generation_quality_log, indent=2), encoding="utf-8"
    )

    # Isolate pre-existing non-generated tests so compile/runtime can focus on
    # generated tests only.
    isolation_root = demo_root / "isolation" / "non_generated_tests"
    isolated_tests = isolate_non_generated_test_files(project_root, generated_paths, isolation_root)
    if isolated_tests:
        print(f"Isolated {len(isolated_tests)} pre-existing non-generated test files.")
    (demo_root / "isolation").mkdir(parents=True, exist_ok=True)
    (demo_root / "isolation" / "moved_tests.json").write_text(
        json.dumps(isolated_tests, indent=2), encoding="utf-8"
    )

    # 6) Compile repair loop: GPT repair first, then compile gate
    print("\nCompile stage: compiling ONLY generated tests:", GENERATED_PATTERN)

    compile_gate_log: List[Dict] = []
    repair_log: List[Dict] = []
    retry_counts: Dict[str, int] = {}
    compile_blocked = False
    compile_blocked_reason = ""

    repo_types_text = ", ".join(list_repository_types(project_root))

    compile_log_path = demo_root / "compile" / "compile_log.txt"

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
        snippet = "\n".join(strip_ansi(errors).splitlines()[-80:])
        compile_gate_log.append(
            {"file": str(failing_path), "moved_to": str(dest), "errors": snippet, "action": action}
        )
        return str(dest)

    # --- Phase A: focused compile refinement ---
    print("Running Maven with RAT, Checkstyle, and Enforcer skipped for coverage-only execution.")
    for _ in range(max(1, len(generated_paths) * (max_refinement_iterations + 1))):
        last_compile_log, compile_rc = run_maven_test_compile(project_root)

        # FIX #1: always append compile log during REPAIR phase too
        with compile_log_path.open("a", encoding="utf-8") as f:
            f.write(last_compile_log)
            f.write("\n" + ("-" * 80) + "\n")

        if compile_rc == 0:
            break

        failing_path = extract_first_failing_test_path(last_compile_log)
        if not failing_path:
            compile_blocked = True
            compile_blocked_reason = (
                "maven test-compile failed due to non-generated test compile errors"
            )
            break
        failing_path = resolve_maven_test_path(project_root, failing_path)

        # save "before repair" artifacts
        with isolate_generated_tests_except(
            project_root,
            failing_path,
            demo_root / "isolation" / "compile_refinement",
        ):
            focused_compile_log, focused_compile_rc = run_maven_test_compile(
                project_root, test_filter=failing_path.stem
            )
        if focused_compile_rc != 0:
            last_compile_log = focused_compile_log

        write_failure_artifacts(failing_path, last_compile_log, demo_root / "failures", "compile_before")

        fp = str(failing_path)
        retries = retry_counts.get(fp, 0)

        # exceeded retries -> move to rejected so summaries and artifacts reflect it.
        if retries >= max_refinement_iterations:
            write_failure_artifacts(failing_path, last_compile_log, demo_root / "failures", "compile_final")
            moved_to = move_to_rejected_compile(
                failing_path,
                last_compile_log,
                action=f"deleted_after_{max_refinement_iterations}_repairs",
            )
            repair_log.append(
                {
                    "file": fp,
                    "moved_to": moved_to,
                    "errors_tail": concise_error_log(last_compile_log),
                    "action": f"deleted_after_{max_refinement_iterations}_repairs",
                }
            )
            generated_paths = [p for p in generated_paths if Path(p).exists()]
            continue

        # attempt GPT repair
        try:
            file_content = failing_path.read_text(encoding="utf-8", errors="ignore")
        except FileNotFoundError:
            generated_paths = [p for p in generated_paths if Path(p).exists()]
            continue

        test_class = failing_path.stem
        target = test_target_map.get(test_class, {})
        source_text = ""
        package_imports = ""
        constructor_info = ""

        src_path = target.get("source_file")
        if src_path:
            try:
                source_text = Path(src_path).read_text(encoding="utf-8", errors="ignore")
                package_imports = "\n".join(_extract_imports_context_from_text(source_text))
                constructor_info = extract_constructor_info(source_text, target.get("class_name", ""))
            except OSError:
                pass

        related_sources = (
            related_type_sources_from_analysis(ast_analysis, target)
            if ast_analysis is not None
            else collect_related_type_sources(project_root, target)
        )
        source_bundle = f"{related_sources}\n{source_text}"
        stub_fixed = remove_invented_api_stubs(file_content, source_bundle)
        stub_fixed = rewrite_interface_mocks_to_concrete(stub_fixed, related_sources)
        stub_fixed = add_throws_exception_to_test_methods(stub_fixed, last_compile_log)
        if stub_fixed != file_content:
            failing_path.write_text(stub_fixed, encoding="utf-8")
            repair_log.append(
                {
                    "file": fp,
                    "action": "deterministic_stub_removal",
                    "errors_tail": concise_error_log(last_compile_log, max_lines=20),
                }
            )
            continue

        fixed_code = ollama_repair_test(
            model=args.gpt_model,
            compiler_errors=last_compile_log,
            file_content=file_content,
            source_text=source_text,
            package_imports=package_imports,
            constructor_info=constructor_info,
            repository_types=repo_types_text,
            related_type_sources=related_sources,
            java_version=project_java_version,
            junit_version=junit_version,
        )

        fixed_code = enforce_test_class_name(fixed_code, test_class)
        fixed_code = ensure_junit_imports(fixed_code, junit_version)
        fixed_code = remove_invented_api_stubs(fixed_code, source_bundle)
        fixed_code = rewrite_interface_mocks_to_concrete(fixed_code, related_sources)
        fixed_code = add_throws_exception_to_test_methods(fixed_code, last_compile_log)

        invalid_fix_reason = validate_java_test_output(fixed_code, test_class)
        if not fixed_code.strip() or invalid_fix_reason:
            write_failure_artifacts(failing_path, last_compile_log, demo_root / "failures", "compile_final")
            retry_counts[fp] = retries + 1
            repair_log.append(
                {
                    "file": fp,
                    "invalid_fix_reason": invalid_fix_reason,
                    "errors_tail": concise_error_log(last_compile_log),
                    "action": "invalid_or_empty_fix",
                }
            )
            continue

        failing_path.write_text(fixed_code, encoding="utf-8")
        retry_counts[fp] = retries + 1
        repair_log.append(
            {
                "file": fp,
                "errors_tail": concise_error_log(last_compile_log),
                "action": "fixed",
            }
        )

    # Save repair log in both compile/ and coverage/ (handy for your demo)
    (demo_root / "compile" / "repair_log.json").write_text(json.dumps(repair_log, indent=2), encoding="utf-8")
    (demo_root / "coverage" / "repair_log.json").write_text(json.dumps(repair_log, indent=2), encoding="utf-8")

    # Survivors after GPT repair stage:
    generated_paths = [p for p in generated_paths if Path(p).exists()]

    # --- Phase B: Compile gate loop (move remaining failing tests) ---
    for _ in range(10):
        last_compile_log, compile_rc = run_maven_test_compile(project_root)

        with compile_log_path.open("a", encoding="utf-8") as f:
            f.write(last_compile_log)
            f.write("\n" + ("-" * 80) + "\n")

        if compile_rc == 0:
            break

        failing_paths = extract_failing_test_paths(last_compile_log)
        failing_paths = [resolve_maven_test_path(project_root, p) for p in failing_paths]
        if not failing_paths:
            compile_blocked = True
            compile_blocked_reason = (
                "maven test-compile failed due to non-generated test compile errors"
            )
            break

        for failing_path in failing_paths:
            move_to_rejected_compile(failing_path, last_compile_log, action="rejected")

        generated_paths = [p for p in generated_paths if Path(p).exists()]
        if not generated_paths:
            break

    (demo_root / "compile" / "compile_gate_log.json").write_text(
        json.dumps(compile_gate_log, indent=2), encoding="utf-8"
    )

    # Survivors after compile stage:
    generated_paths = [p for p in generated_paths if Path(p).exists()]
    compile_survivors = len(generated_paths)
    compile_rejected_files = list((demo_root / "rejected" / "compile").rglob(f"{GENERATED_PREFIX}*Test.java"))
    compile_rejected = len(compile_rejected_files)

    early_stop = (compile_survivors == 0) or compile_blocked

    # 7) Runtime stage: run tests with JaCoCo agent; runtime repair on failures
    test_log = ""
    test_rc = 0
    report_log = ""
    report_rc = 0

    runtime_gate_log: List[Dict] = []
    runtime_repair_log: List[Dict] = []
    runtime_retries: Dict[str, int] = {}
    rejected_runtime_root = demo_root / "rejected" / "runtime"

    if not early_stop:
        print("\nRuntime stage: running ONLY generated tests with JaCoCo agent")

        def move_to_rejected_runtime(failing_path: Path, errors: str, action: str) -> None:
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
            runtime_gate_log.append(
                {
                    "file": str(failing_path),
                    "moved_to": str(dest),
                    "action": action,
                    "errors_tail": "\n".join(strip_ansi(errors).splitlines()[-80:]),
                }
            )

        for _ in range(max(1, len(generated_paths) * (max_refinement_iterations + 1))):
            test_log, test_rc = run_maven_tests(project_root)
            (demo_root / "runtime" / "test_log.txt").write_text(test_log, encoding="utf-8")

            failures = extract_runtime_failures(project_root / "target" / "surefire-reports")
            if test_rc == 0 or not failures:
                break

            changed_any = False
            for f in failures:
                class_name = f.get("class_name", "")
                method_name = f.get("method_name", "")
                stack_trace = f.get("stack_trace", "")
                rel = Path("src/test/java") / Path(class_name.replace(".", "/") + ".java")
                failing_path = project_root / rel
                fp = str(failing_path)
                retry_key = f"{fp}::{method_name or '*'}"

                if not failing_path.exists():
                    continue

                test_filter = class_name.rsplit(".", 1)[-1]
                if method_name:
                    test_filter = f"{test_filter}#{method_name}"
                with isolate_generated_tests_except(
                    project_root,
                    failing_path,
                    demo_root / "isolation" / "runtime_refinement",
                ):
                    focused_test_log, focused_test_rc = run_maven_tests(
                        project_root, test_filter=test_filter
                    )
                if focused_test_rc != 0:
                    stack_trace = stack_trace or concise_error_log(focused_test_log)

                write_failure_artifacts(failing_path, stack_trace, demo_root / "failures", "runtime_before")

                retries = runtime_retries.get(retry_key, 0)
                if retries >= max_refinement_iterations:
                    write_failure_artifacts(failing_path, stack_trace, demo_root / "failures", "runtime_final")
                    move_to_rejected_runtime(failing_path, stack_trace, action="rejected")
                    runtime_repair_log.append(
                        {
                            "file": fp,
                            "method": method_name or None,
                            "action": "rejected",
                            "errors_tail": concise_error_log(stack_trace),
                        }
                    )
                    changed_any = True
                    continue

                try:
                    file_content = failing_path.read_text(encoding="utf-8", errors="ignore")
                except FileNotFoundError:
                    continue

                test_class = failing_path.stem
                target = test_target_map.get(test_class, {})
                runtime_source_text = ""
                runtime_related_sources = (
                    related_type_sources_from_analysis(ast_analysis, target)
                    if ast_analysis is not None
                    else collect_related_type_sources(project_root, target)
                )
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
                    deterministic_fix = ensure_junit_imports(deterministic_fix, junit_version)
                    failing_path.write_text(deterministic_fix, encoding="utf-8")
                    runtime_retries[retry_key] = retries + 1
                    runtime_repair_log.append(
                        {
                            "file": fp,
                            "method": method_name or None,
                            "action": "deterministic_runtime_rewrite",
                            "errors_tail": concise_error_log(stack_trace),
                        }
                    )
                    changed_any = True
                    continue

                # FIX #2: keyword call (avoids args/positional confusion)
                fixed_code = ollama_runtime_repair_test(
                    model=args.gpt_model,
                    stack_trace=stack_trace,
                    file_content=file_content,
                    failing_method=method_name,
                    source_text=runtime_source_text,
                    related_type_sources=runtime_related_sources,
                    java_version=project_java_version,
                    junit_version=junit_version,
                )

                if not fixed_code.strip():
                    write_failure_artifacts(failing_path, stack_trace, demo_root / "failures", "runtime_final")
                    move_to_rejected_runtime(failing_path, stack_trace, action="rejected_empty_fix")
                    runtime_repair_log.append(
                        {
                            "file": fp,
                            "method": method_name or None,
                            "action": "rejected_empty_fix",
                            "errors_tail": concise_error_log(stack_trace),
                        }
                    )
                    changed_any = True
                    continue

                fixed_code = enforce_test_class_name(fixed_code, test_class)
                fixed_code = ensure_junit_imports(fixed_code, junit_version)
                fixed_code = remove_invented_api_stubs(fixed_code, source_bundle)
                fixed_code = rewrite_interface_mocks_to_concrete(fixed_code, runtime_related_sources)

                failing_path.write_text(fixed_code, encoding="utf-8")
                runtime_retries[retry_key] = retries + 1
                runtime_repair_log.append(
                    {
                        "file": fp,
                        "method": method_name or None,
                        "action": "fixed",
                        "errors_tail": concise_error_log(stack_trace),
                    }
                )
                changed_any = True

            if not changed_any:
                break

        (demo_root / "runtime" / "runtime_gate_log.json").write_text(
            json.dumps(runtime_gate_log, indent=2), encoding="utf-8"
        )
        (demo_root / "runtime" / "runtime_repair_log.json").write_text(
            json.dumps(runtime_repair_log, indent=2), encoding="utf-8"
        )

        # Always attempt report (even if some tests failed)
        report_log, report_rc = run_maven_report(project_root)

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
    if early_stop:
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
    runtime_counts = parse_surefire_summary(test_log) if test_log else None
    if runtime_counts is None:
        runtime_counts = parse_surefire_reports(project_root / "target" / "surefire-reports") or {}

    # Survivors after runtime stage (what remains in repo)
    generated_paths = [p for p in generated_paths if Path(p).exists()]
    runtime_survivors = len(generated_paths)
    runtime_rejected_files = list((demo_root / "rejected" / "runtime").rglob(f"{GENERATED_PREFIX}*Test.java"))
    runtime_rejected = len(runtime_rejected_files)

    coverage_quality_issue = None
    if (
        not early_stop
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

    # No-report reason
    if not (report_dir / "index.html").exists():
        if early_stop:
            if compile_blocked:
                reason = compile_blocked_reason
            else:
                reason = "0 generated tests compiled"
        elif not jacoco_exec_found:
            reason = "jacoco.exec not found (tests did not execute far enough)"
        else:
            reason = "coverage report not found"
        (demo_root / "coverage" / "no_report_reason.txt").write_text(reason, encoding="utf-8")

    summary = {
        "repo": args.repo,
        "project_root": str(project_root),
        "build": "maven",
        "mode": args.mode,
        "selected_packages": selected,
        "ollama_model": args.ollama_model,
        "gpt_model": args.gpt_model,
        "max_refinement_iterations": max_refinement_iterations,
        "generated_total": len(written_paths) + len(generation_quality_log),
        "generated_written": len(written_paths),
        "generation_rejected": len(generation_quality_log),
        "compile_survivors": compile_survivors,
        "compile_rejected": int(compile_rejected),
        "compile_blocked": compile_blocked,
        "compile_blocked_reason": compile_blocked_reason or None,
        "runtime_survivors": runtime_survivors,
        "runtime_rejected": int(runtime_rejected),
        "isolated_non_generated_tests": len(isolated_tests),
        "isolated_tests_manifest": str((demo_root / "isolation" / "moved_tests.json").resolve()),
        "survivor_test_files_in_repo": generated_paths,
        "rejected_compile_dir": str((demo_root / "rejected" / "compile").resolve()),
        "rejected_runtime_dir": str((demo_root / "rejected" / "runtime").resolve()),
        "jacoco_exec_found": jacoco_exec_found,
        "coverage": coverage,
        "coverage_quality_issue": coverage_quality_issue,
        "tests_run": runtime_counts.get("tests_run"),
        "failures": runtime_counts.get("failures"),
        "errors": runtime_counts.get("errors"),
        "skipped": runtime_counts.get("skipped"),
        "coverage_report_index": str((report_dir / "index.html").resolve())
        if (report_dir / "index.html").exists()
        else None,
        "note": "Tests were written locally into src/test/java but NOT committed or pushed.",
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
