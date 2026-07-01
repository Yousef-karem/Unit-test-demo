from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from demo.config import DEFAULT_DOCKER_MAVEN_IMAGE, GENERATED_PREFIX
from demo.coverage.java_version import coerce_supported_version
from demo.coverage.maven import (
    run_maven_report,
    run_maven_test_compile,
    run_maven_tests,
    strip_ansi,
)
from demo.coverage.parse import parse_jacoco_xml, parse_surefire_reports, parse_surefire_summary
from demo.coverage.runner import configure_maven_runner, docker_image_name, ensure_docker_available
from demo.pipeline import (
    add_throws_exception_to_test_methods,
    isolate_non_generated_test_files,
    prune_generated_snapshots,
    restore_isolated_test_files,
    sync_generated_snapshot,
    sync_generated_snapshots,
    write_test_file,
)


def _resolve_path(path_str: str, base: Path) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def resolve_run_root(from_run: str) -> Path:
    run_root = Path(from_run).expanduser()
    if not run_root.is_absolute():
        run_root = (Path.cwd() / run_root).resolve()
    if not run_root.exists():
        raise FileNotFoundError(f"Run directory not found: {run_root}")
    demo_root = run_root / "DemoTestCases"
    if not demo_root.is_dir():
        raise FileNotFoundError(f"Missing DemoTestCases/ under run directory: {run_root}")
    return run_root


def find_project_root(run_root: Path, demo_root: Path) -> Path:
    summary_path = demo_root / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        project_root = _resolve_path(summary["project_root"], Path.cwd())
        if (project_root / "pom.xml").exists():
            return project_root

    repo_root = run_root / "repo"
    if not repo_root.is_dir():
        raise FileNotFoundError(f"Missing repo/ under run directory: {run_root}")

    pom_files = sorted(repo_root.rglob("pom.xml"))
    if not pom_files:
        raise FileNotFoundError(f"No pom.xml found under {repo_root}")
    return pom_files[0].parent


def _package_from_generated_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = re.search(r"^\s*package\s+([a-zA-Z0-9_.]+)\s*;", text, re.MULTILINE)
    return m.group(1) if m else ""


def _refresh_generated_snapshots_from_repo(demo_root: Path) -> None:
    """Prefer repo survivor copies over stale DemoTestCases/generated/ snapshots."""
    summary_path = demo_root / "summary.json"
    if not summary_path.exists():
        return
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for path_str in summary.get("survivor_test_files_in_repo") or []:
        path = _resolve_path(path_str, Path.cwd())
        if path.exists():
            sync_generated_snapshot(demo_root, path)


def ensure_generated_tests(project_root: Path, demo_root: Path) -> List[str]:
    """Install only survivor snapshots from DemoTestCases/generated/ into the repo."""
    _refresh_generated_snapshots_from_repo(demo_root)

    generated_paths: List[str] = []
    generated_dir = demo_root / "generated"

    allowed_stems: set[str] = set()
    if generated_dir.is_dir():
        for src in sorted(generated_dir.glob(f"{GENERATED_PREFIX}*Test.java")):
            allowed_stems.add(src.stem)
            test_class = src.stem
            pkg = _package_from_generated_file(src)
            code = src.read_text(encoding="utf-8", errors="ignore")
            dest = write_test_file(project_root, pkg, test_class, code)
            generated_paths.append(str(dest))

    for test_file in list(project_root.rglob(f"{GENERATED_PREFIX}*Test.java")):
        if test_file.stem not in allowed_stems:
            try:
                test_file.unlink(missing_ok=True)
            except OSError:
                pass

    if not generated_paths:
        summary_path = demo_root / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            for path_str in summary.get("survivor_test_files_in_repo") or []:
                path = _resolve_path(path_str, Path.cwd())
                if path.exists():
                    generated_paths.append(str(path))

    return list(dict.fromkeys(str(Path(p).resolve()) for p in generated_paths))


def _apply_checked_exception_fixes(
    project_root: Path,
    demo_root: Path,
    generated_paths: List[str],
    compile_log: str,
) -> bool:
    if "unreported exception" not in compile_log:
        return False
    changed = False
    for path_str in generated_paths:
        test_path = Path(path_str)
        if not test_path.exists():
            continue
        code = test_path.read_text(encoding="utf-8", errors="ignore")
        fixed = add_throws_exception_to_test_methods(code, compile_log)
        if fixed == code:
            continue
        test_path.write_text(fixed, encoding="utf-8")
        sync_generated_snapshot(demo_root, test_path)
        changed = True
    return changed


def configure_maven_from_run(demo_root: Path, args) -> None:
    config_path = demo_root / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Missing run config: {config_path}")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    saved_args = config.get("args", {})

    use_docker = getattr(args, "docker_maven", False) or bool(saved_args.get("docker_maven"))
    docker_java_version = coerce_supported_version(config.get("docker_java_version", "8"))
    compiler_java_version = config.get("maven_compiler_java_version")
    docker_image = (
        getattr(args, "docker_maven_image", None)
        or saved_args.get("docker_maven_image")
        or DEFAULT_DOCKER_MAVEN_IMAGE
    )
    maven_cache_volume = (
        getattr(args, "docker_maven_cache_volume", None)
        or saved_args.get("docker_maven_cache_volume")
    )

    configure_maven_runner(
        use_docker=use_docker,
        java_version=docker_java_version,
        docker_image=docker_image,
        maven_cache_volume=maven_cache_volume,
        compiler_java_version=compiler_java_version,
    )

    if use_docker:
        ensure_docker_available()
        print(
            "Using Docker Maven image:",
            docker_image_name(),
            f"(Java {docker_java_version}, cache volume: {maven_cache_volume})",
        )
    elif compiler_java_version:
        print(f"Maven compiler properties will use Java {compiler_java_version}")


def run_coverage_from_run(args) -> None:
    run_root = resolve_run_root(args.from_run)
    demo_root = run_root / "DemoTestCases"
    project_root = find_project_root(run_root, demo_root)

    print(f"Re-running coverage for existing run: {run_root}")
    print(f"Project root: {project_root}")

    configure_maven_from_run(demo_root, args)

    generated_paths = ensure_generated_tests(project_root, demo_root)
    if not generated_paths:
        raise RuntimeError(
            "No survivor generated tests found in this run. "
            f"Expected files under {demo_root / 'generated'} (after compile/runtime filtering) "
            "or survivor paths in summary.json."
        )

    prune_generated_snapshots(demo_root, generated_paths)
    sync_generated_snapshots(demo_root, generated_paths)
    (demo_root / "written_paths.json").write_text(
        json.dumps(generated_paths, indent=2), encoding="utf-8"
    )

    print(f"Found {len(generated_paths)} generated test file(s).")

    isolation_root = demo_root / "isolation" / "non_generated_tests"
    isolated_tests = isolate_non_generated_test_files(project_root, generated_paths, isolation_root)
    if isolated_tests:
        print(f"Isolated {len(isolated_tests)} pre-existing non-generated test files.")
    (demo_root / "isolation").mkdir(parents=True, exist_ok=True)
    (demo_root / "isolation" / "moved_tests.json").write_text(
        json.dumps(isolated_tests, indent=2), encoding="utf-8"
    )

    compile_log_path = demo_root / "compile" / "compile_log.txt"
    compile_log_path.parent.mkdir(parents=True, exist_ok=True)
    compile_log, compile_rc = run_maven_test_compile(project_root)
    if compile_rc != 0 and _apply_checked_exception_fixes(
        project_root, demo_root, generated_paths, compile_log
    ):
        compile_log, compile_rc = run_maven_test_compile(project_root)
    with compile_log_path.open("w", encoding="utf-8") as f:
        f.write(compile_log)

    if compile_rc != 0:
        tail = "\n".join(strip_ansi(compile_log).splitlines()[-40:])
        restore_isolated_test_files(isolated_tests)
        raise RuntimeError(f"Maven test-compile failed.\n{tail}")

    (demo_root / "runtime").mkdir(parents=True, exist_ok=True)
    test_log, test_rc = run_maven_tests(project_root)
    (demo_root / "runtime" / "test_log.txt").write_text(test_log, encoding="utf-8")

    report_log, _report_rc = run_maven_report(project_root)
    build_log = (test_log or "") + "\n" + (report_log or "")
    (demo_root / "coverage" / "build_log.txt").write_text(build_log, encoding="utf-8")

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
    coverage: Dict[str, float] = parse_jacoco_xml(xml_path) if xml_path else zero_coverage

    report_dir = demo_root / "coverage" / "report"
    if report_dir.exists():
        shutil.rmtree(report_dir)
    if html_path:
        shutil.copytree(project_root / "target" / "site" / "jacoco", report_dir)
    if xml_path:
        shutil.copyfile(xml_path, demo_root / "coverage" / "jacoco.xml")

    runtime_counts = parse_surefire_summary(test_log) if test_log else None
    if runtime_counts is None:
        runtime_counts = parse_surefire_reports(project_root / "target" / "surefire-reports") or {}

    generated_paths = [p for p in generated_paths if Path(p).exists()]
    no_report_path = demo_root / "coverage" / "no_report_reason.txt"
    if (report_dir / "index.html").exists():
        if no_report_path.exists():
            no_report_path.unlink()
    else:
        if not jacoco_exec_found:
            reason = "jacoco.exec not found (tests did not execute far enough)"
        else:
            reason = "coverage report not found"
        no_report_path.write_text(reason, encoding="utf-8")

    summary_path = demo_root / "summary.json"
    summary: Dict = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))

    summary.update(
        {
            "project_root": str(project_root),
            "coverage_rerun_at": datetime.now().isoformat(timespec="seconds"),
            "compile_survivors": len(generated_paths),
            "compile_blocked": False,
            "compile_blocked_reason": None,
            "runtime_survivors": len(generated_paths),
            "survivor_test_files_in_repo": generated_paths,
            "jacoco_exec_found": jacoco_exec_found,
            "coverage": coverage,
            "tests_run": runtime_counts.get("tests_run"),
            "failures": runtime_counts.get("failures"),
            "errors": runtime_counts.get("errors"),
            "skipped": runtime_counts.get("skipped"),
            "coverage_report_index": str((report_dir / "index.html").resolve())
            if (report_dir / "index.html").exists()
            else None,
            "coverage_rerun_test_rc": test_rc,
        }
    )
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    restore_isolated_test_files(isolated_tests)

    print("\n=== COVERAGE RERUN DONE ===")
    print(f"Summary: {summary_path}")
    if summary.get("coverage_report_index"):
        print("Coverage HTML:", summary["coverage_report_index"])
    elif no_report_path.exists():
        print(f"Coverage report not found. Check {no_report_path}")

    if coverage:
        print("Coverage:")
        print(f"- Line:        {coverage['line_coverage']*100:.2f}%")
        print(f"- Instruction: {coverage['instruction_coverage']*100:.2f}%")
        print(f"- Branch:      {coverage['branch_coverage']*100:.2f}%")

    if test_rc != 0:
        print(f"Warning: Maven tests exited with code {test_rc}")
