from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from openai import OpenAI

from demo.config import (
    DEMO_OUT,
    GENERATED_PATTERN,
    GENERATED_PREFIX,
)
from demo.coverage.maven import (
    extract_failing_test_paths,
    extract_first_failing_test_path,
    run_maven_report,
    run_maven_tests,
    strip_ansi,
    write_failure_artifacts,
)
from demo.coverage.parse import (
    extract_runtime_failures,
    parse_jacoco_xml,
    parse_surefire_reports,
    parse_surefire_summary,
)
from demo.llm.gpt import (
    gpt_repair_test,
    gpt_runtime_repair_test,
    gpt_write_prompt,
)
from demo.llm.ollama import ollama_generate
from demo.packages import (
    choose_packages_interactive,
    discover_packages,
    file_in_selected_packages,
    list_java_files,
)
from demo.repo import clone_or_update, detect_build_system
from demo.targets import _extract_imports_context_from_text, extract_targets
from demo.utils import (
    ensure_unique_run_class_name,
    load_env_file,
    repo_name_from_arg,
    sanitize_java_output,
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


def write_test_file(project_root: Path, target_pkg: str, test_class_name: str, code: str) -> Path:
    base = project_root / "src" / "test" / "java"
    if target_pkg and target_pkg != "(default)":
        base = base / Path(target_pkg.replace(".", "/"))
    base.mkdir(parents=True, exist_ok=True)

    path = base / f"{test_class_name}.java"
    path.write_text(code, encoding="utf-8")
    return path


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

    demo_root = run_root / "DemoTestCases"
    (demo_root / "prompts").mkdir(parents=True, exist_ok=True)
    (demo_root / "generated").mkdir(parents=True, exist_ok=True)
    (demo_root / "coverage").mkdir(parents=True, exist_ok=True)
    (demo_root / "failures").mkdir(parents=True, exist_ok=True)
    (demo_root / "compile").mkdir(parents=True, exist_ok=True)
    (demo_root / "runtime").mkdir(parents=True, exist_ok=True)
    (demo_root / "rejected" / "compile").mkdir(parents=True, exist_ok=True)
    (demo_root / "rejected" / "runtime").mkdir(parents=True, exist_ok=True)

    # 2) Detect build system
    build = args.build
    if build == "auto":
        build = detect_build_system(project_root)
    if build != "maven":
        raise RuntimeError("This demo pipeline.py currently supports Maven repos only (pom.xml).")

    # 3) Discover packages
    pkgs = discover_packages(project_root)
    selected: List[str] = ["*"]
    if args.select_packages:
        selected = choose_packages_interactive(pkgs)
    elif args.packages and args.packages.strip().upper() != "ALL":
        selected = [p.strip() for p in args.packages.split(",") if p.strip()] or ["*"]

    # 4) Collect targets
    java_files = list_java_files(project_root)
    java_files = [f for f in java_files if file_in_selected_packages(f, project_root, selected)]
    java_files = java_files[: args.max_files]

    targets: List[Dict] = []
    skip_keywords = ("application", "config", "filter", "security", "interceptor")
    for f in java_files:
        for t in extract_targets(f, args.mode):
            if args.skip_framework_classes:
                cls_name = (t.get("class_name") or "").lower()
                if any(k in cls_name for k in skip_keywords):
                    continue
            targets.append(t)
            if len(targets) >= args.max_targets:
                break
        if len(targets) >= args.max_targets:
            break

    if not targets:
        raise RuntimeError("No targets found (check src/main/java and selected packages).")

    # Save config and target list for reproducibility
    config = {
        "args": vars(args),
        "selected_packages": selected,
        "models": {"ollama": args.ollama_model, "gpt": args.gpt_model},
    }
    (demo_root / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (demo_root / "targets.json").write_text(json.dumps(targets, indent=2), encoding="utf-8")

    # 5) GPT writes prompts; Ollama generates tests; write into repo
    load_env_file(Path(__file__).resolve().parents[1] / ".env")
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    generated_paths: List[str] = []
    test_target_map: Dict[str, Dict] = {}
    used_test_class_names: set[str] = set()

    project_types = list_project_types(project_root)
    # Keep the context bounded (avoid huge prompts)
    project_types_text = ", ".join(project_types[:800])

    for i, t in enumerate(targets, 1):
        g = gpt_write_prompt(client, args.gpt_model, t, project_types_text)

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
        code = sanitize_java_output(ollama_generate(args.ollama_model, g["prompt"]))

        (demo_root / "generated" / f"{test_class}.java").write_text(code, encoding="utf-8")

        out_path = write_test_file(project_root, t["package"], test_class, code)
        generated_paths.append(str(out_path))

    written_paths = list(generated_paths)
    (demo_root / "written_paths.json").write_text(json.dumps(written_paths, indent=2), encoding="utf-8")

    # 6) Compile repair loop: GPT repair first, then compile gate
    print("\nCompile stage: compiling ONLY generated tests:", GENERATED_PATTERN)

    compile_gate_log: List[Dict] = []
    repair_log: List[Dict] = []
    retry_counts: Dict[str, int] = {}

    repo_types_text = ", ".join(list_repository_types(project_root))

    compile_log_path = demo_root / "compile" / "compile_log.txt"
    rejected_compile_root = demo_root / "rejected" / "compile"

    # --- Phase A: GPT compile-repair first (up to 10 iterations) ---
    print("Running Maven with RAT, Checkstyle, and Enforcer skipped for coverage-only execution.")
    for _ in range(10):
        cmd = [
            "mvn",
            "-Drat.skip=true",
            "-Dcheckstyle.skip=true",
            "-Denforcer.skip=true",
            "-q",
            "-Dstyle.color=never",
            f"-Dtest={GENERATED_PATTERN}",
            "test-compile",
        ]
        p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
        last_compile_log = (p.stdout or "") + "\n" + (p.stderr or "")

        # FIX #1: always append compile log during REPAIR phase too
        with compile_log_path.open("a", encoding="utf-8") as f:
            f.write(last_compile_log)
            f.write("\n" + ("-" * 80) + "\n")

        if p.returncode == 0:
            break

        failing_path = extract_first_failing_test_path(last_compile_log)
        if not failing_path:
            break

        # save "before repair" artifacts
        write_failure_artifacts(failing_path, last_compile_log, demo_root / "failures", "compile_before")

        fp = str(failing_path)
        retries = retry_counts.get(fp, 0)

        # exceeded retries -> delete (repair phase does NOT move to rejected)
        if retries >= 2:
            write_failure_artifacts(failing_path, last_compile_log, demo_root / "failures", "compile_final")
            try:
                failing_path.unlink()
            except FileNotFoundError:
                pass
            repair_log.append(
                {
                    "file": fp,
                    "errors_tail": "\n".join(strip_ansi(last_compile_log).splitlines()[-80:]),
                    "action": "deleted_after_2_repairs",
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

        fixed_code = gpt_repair_test(
            client=client,
            gpt_model=args.gpt_model,
            compiler_errors=last_compile_log,
            file_content=file_content,
            source_text=source_text,
            package_imports=package_imports,
            constructor_info=constructor_info,
            repository_types=repo_types_text,
        )

        if not fixed_code.strip():
            write_failure_artifacts(failing_path, last_compile_log, demo_root / "failures", "compile_final")
            try:
                failing_path.unlink()
            except FileNotFoundError:
                pass
            repair_log.append(
                {
                    "file": fp,
                    "errors_tail": "\n".join(strip_ansi(last_compile_log).splitlines()[-80:]),
                    "action": "deleted_empty_fix",
                }
            )
            generated_paths = [p for p in generated_paths if Path(p).exists()]
            continue

        failing_path.write_text(fixed_code, encoding="utf-8")
        retry_counts[fp] = retries + 1
        repair_log.append(
            {
                "file": fp,
                "errors_tail": "\n".join(strip_ansi(last_compile_log).splitlines()[-80:]),
                "action": "fixed",
            }
        )

    # Save repair log in both compile/ and coverage/ (handy for your demo)
    (demo_root / "compile" / "repair_log.json").write_text(json.dumps(repair_log, indent=2), encoding="utf-8")
    (demo_root / "coverage" / "repair_log.json").write_text(json.dumps(repair_log, indent=2), encoding="utf-8")

    # Survivors after GPT repair stage:
    generated_paths = [p for p in generated_paths if Path(p).exists()]

    def move_to_rejected_compile(failing_path: Path, errors: str, action: str) -> None:
        try:
            rel = failing_path.relative_to(project_root)
        except ValueError:
            rel = Path("src/test/java") / failing_path.name
        dest = rejected_compile_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(failing_path), str(dest))
        except FileNotFoundError:
            return
        snippet = "\n".join(strip_ansi(errors).splitlines()[-80:])
        compile_gate_log.append(
            {"file": str(failing_path), "moved_to": str(dest), "errors": snippet, "action": action}
        )

    # --- Phase B: Compile gate loop (move remaining failing tests) ---
    for _ in range(10):
        cmd = [
            "mvn",
            "-Drat.skip=true",
            "-Dcheckstyle.skip=true",
            "-Denforcer.skip=true",
            "-q",
            "-Dstyle.color=never",
            f"-Dtest={GENERATED_PATTERN}",
            "test-compile",
        ]
        p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
        last_compile_log = (p.stdout or "") + "\n" + (p.stderr or "")

        with compile_log_path.open("a", encoding="utf-8") as f:
            f.write(last_compile_log)
            f.write("\n" + ("-" * 80) + "\n")

        if p.returncode == 0:
            break

        failing_paths = extract_failing_test_paths(last_compile_log)
        if not failing_paths:
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

    early_stop = compile_survivors == 0

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

        for _ in range(10):
            test_log, test_rc = run_maven_tests(project_root)
            (demo_root / "runtime" / "test_log.txt").write_text(test_log, encoding="utf-8")

            failures = extract_runtime_failures(project_root / "target" / "surefire-reports")
            if test_rc == 0 or not failures:
                break

            changed_any = False
            for f in failures:
                class_name = f.get("class_name", "")
                stack_trace = f.get("stack_trace", "")
                rel = Path("src/test/java") / Path(class_name.replace(".", "/") + ".java")
                failing_path = project_root / rel
                fp = str(failing_path)

                if not failing_path.exists():
                    continue

                write_failure_artifacts(failing_path, stack_trace, demo_root / "failures", "runtime_before")

                retries = runtime_retries.get(fp, 0)
                if retries >= 2:
                    write_failure_artifacts(failing_path, stack_trace, demo_root / "failures", "runtime_final")
                    move_to_rejected_runtime(failing_path, stack_trace, action="rejected")
                    runtime_repair_log.append(
                        {
                            "file": fp,
                            "action": "rejected",
                            "errors_tail": "\n".join(strip_ansi(stack_trace).splitlines()[-80:]),
                        }
                    )
                    changed_any = True
                    continue

                try:
                    file_content = failing_path.read_text(encoding="utf-8", errors="ignore")
                except FileNotFoundError:
                    continue

                # FIX #2: keyword call (avoids args/positional confusion)
                fixed_code = gpt_runtime_repair_test(
                    client=client,
                    gpt_model=args.gpt_model,
                    stack_trace=stack_trace,
                    file_content=file_content,
                )

                if not fixed_code.strip():
                    write_failure_artifacts(failing_path, stack_trace, demo_root / "failures", "runtime_final")
                    move_to_rejected_runtime(failing_path, stack_trace, action="rejected_empty_fix")
                    runtime_repair_log.append(
                        {
                            "file": fp,
                            "action": "rejected_empty_fix",
                            "errors_tail": "\n".join(strip_ansi(stack_trace).splitlines()[-80:]),
                        }
                    )
                    changed_any = True
                    continue

                failing_path.write_text(fixed_code, encoding="utf-8")
                runtime_retries[fp] = retries + 1
                runtime_repair_log.append(
                    {
                        "file": fp,
                        "action": "fixed",
                        "errors_tail": "\n".join(strip_ansi(stack_trace).splitlines()[-80:]),
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
    coverage: Dict[str, float] = parse_jacoco_xml(xml_path) if xml_path else {}

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

    # No-report reason
    if not (report_dir / "index.html").exists():
        if early_stop:
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
        "generated_total": len(written_paths),
        "compile_survivors": compile_survivors,
        "compile_rejected": int(compile_rejected),
        "runtime_survivors": runtime_survivors,
        "runtime_rejected": int(runtime_rejected),
        "survivor_test_files_in_repo": generated_paths,
        "rejected_compile_dir": str((demo_root / "rejected" / "compile").resolve()),
        "rejected_runtime_dir": str((demo_root / "rejected" / "runtime").resolve()),
        "jacoco_exec_found": jacoco_exec_found,
        "coverage": coverage,
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
