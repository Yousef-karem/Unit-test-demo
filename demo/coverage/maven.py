from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from demo.config import GENERATED_PATTERN
from demo.coverage.java_version import normalize_java_version
from demo.coverage.runner import get_maven_runner_config, run_maven


def maven_executable() -> str:
    names = ["mvn.cmd", "mvn.bat", "mvn"] if sys.platform == "win32" else ["mvn"]
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return "mvn"


def maven_compiler_property_args(version: str) -> List[str]:
    normalized = normalize_java_version(version)
    try:
        major = int(normalized)
    except ValueError:
        major = 8
    if major >= 9:
        return [f"-Dmaven.compiler.release={normalized}"]
    return [
        f"-Dmaven.compiler.source={normalized}",
        f"-Dmaven.compiler.target={normalized}",
    ]


def _maven_base_args() -> List[str]:
    args = [
        "-Drat.skip=true",
        "-Dcheckstyle.skip=true",
        "-Denforcer.skip=true",
        "-q",
        "-Dstyle.color=never",
    ]
    cfg = get_maven_runner_config()
    if cfg.compiler_java_version:
        args.extend(maven_compiler_property_args(cfg.compiler_java_version))
    return args


def _generated_test_surefire_args(test_filter: str | None = None) -> List[str]:
    test_expr = test_filter or GENERATED_PATTERN
    return [
        f"-Dtest={test_expr}",
        f"-Dsurefire.includes=**/{GENERATED_PATTERN}.java",
    ]


def project_has_jacoco(project_root: Path) -> bool:
    return "<artifactId>jacoco-maven-plugin</artifactId>" in (project_root / "pom.xml").read_text()


def run_maven_test_compile(project_root: Path, test_filter: str | None = None) -> Tuple[str, int]:
    cmd = _maven_base_args() + _generated_test_surefire_args(test_filter) + ["test-compile"]
    return run_maven(cmd, project_root)


def run_maven_tests(project_root: Path, test_filter: str | None = None) -> Tuple[str, int]:
    if project_has_jacoco(project_root):
        cmd = _maven_base_args() + _generated_test_surefire_args(test_filter) + ["test"]
    else:
        cmd = _maven_base_args() + [
            "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent",
            *_generated_test_surefire_args(test_filter),
            "test",
        ]
    return run_maven(cmd, project_root)


def run_maven_report(project_root: Path) -> Tuple[str, int]:
    cmd = _maven_base_args() + ["org.jacoco:jacoco-maven-plugin:report"]
    return run_maven(cmd, project_root)


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _path_from_maven_log(raw_path: str) -> Path:
    path = raw_path.strip().replace("/", "\\") if sys.platform == "win32" else raw_path.strip()
    if sys.platform == "win32":
        path = re.sub(r"^[\\/]+([A-Za-z]:\\)", r"\1", path)
    return Path(path)


def extract_first_failing_test_path(log: str) -> Optional[Path]:
    log = strip_ansi(log)
    m = re.search(
        r"(?m)^\[ERROR\]\s+(.+?[\\/]src[\\/]test[\\/]java[\\/].+?[\\/]LLM_Generated.+?Test\.java):\[\d+,\d+\]",
        log,
    )
    if not m:
        m = re.search(
            r"(?m)^\[ERROR\]\s+(.+?[\\/]src[\\/]test[\\/]java[\\/].+?[\\/]LLM_Generated.+?Test\.java)",
            log,
        )
    if not m:
        return None
    return _path_from_maven_log(m.group(1))


def extract_failing_test_paths(log: str) -> List[Path]:
    log = strip_ansi(log)
    matches = re.findall(
        r"(?m)^\[ERROR\]\s+(.+?[\\/]src[\\/]test[\\/]java[\\/].+?[\\/]LLM_Generated.+?Test\.java):\[\d+,\d+\]",
        log,
    )
    if not matches:
        matches = re.findall(
            r"(?m)^\[ERROR\]\s+(.+?[\\/]src[\\/]test[\\/]java[\\/].+?[\\/]LLM_Generated.+?Test\.java)",
            log,
        )
    return [_path_from_maven_log(m) for m in matches]


def write_failure_artifacts(failing_path: Path, errors: str, failures_dir: Path, suffix: str) -> None:
    """
    Writes BOTH:
      - <TestName>__<suffix>.java  (copy of file)
      - <TestName>__<suffix>.txt  (last 80 lines of errors)
    """
    failures_dir.mkdir(parents=True, exist_ok=True)
    test_name = failing_path.stem
    java_out = failures_dir / f"{test_name}__{suffix}.java"
    txt_out = failures_dir / f"{test_name}__{suffix}.txt"
    try:
        shutil.copyfile(failing_path, java_out)
    except (FileNotFoundError, OSError):
        pass
    lines = strip_ansi(errors).splitlines()
    snippet = "\n".join(lines[-80:]) if lines else errors
    txt_out.write_text(snippet, encoding="utf-8")
