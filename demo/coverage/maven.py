from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from demo.config import GENERATED_PATTERN


def maven_executable() -> str:
    names = ["mvn.cmd", "mvn.bat", "mvn"] if sys.platform == "win32" else ["mvn"]
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return "mvn"


def project_has_jacoco(project_root: Path) -> bool:
    return "<artifactId>jacoco-maven-plugin</artifactId>" in (project_root / "pom.xml").read_text()


def run_maven_tests(project_root: Path) -> Tuple[str, int]:
    if project_has_jacoco(project_root):
        cmd = [
            maven_executable(),
            "-Drat.skip=true",
            "-Dcheckstyle.skip=true",
            "-Denforcer.skip=true",
            "-q",
            "-Dstyle.color=never",
            f"-Dtest={GENERATED_PATTERN}",
            "test",
        ]
    else:
        cmd = [
            maven_executable(),
            "-Drat.skip=true",
            "-Dcheckstyle.skip=true",
            "-Denforcer.skip=true",
            "-q",
            "-Dstyle.color=never",
            "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent",
            f"-Dtest={GENERATED_PATTERN}",
            "test",
        ]
    p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
    log = (p.stdout or "") + "\n" + (p.stderr or "")
    return log, p.returncode


def run_maven_report(project_root: Path) -> Tuple[str, int]:
    cmd = [
        maven_executable(),
        "-Drat.skip=true",
        "-Dcheckstyle.skip=true",
        "-Denforcer.skip=true",
        "-q",
        "-Dstyle.color=never",
        "org.jacoco:jacoco-maven-plugin:report",
    ]
    p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
    log = (p.stdout or "") + "\n" + (p.stderr or "")
    return log, p.returncode


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _path_from_maven_log(raw_path: str) -> Path:
    path = raw_path.strip().replace("/", "\\") if sys.platform == "win32" else raw_path.strip()
    if sys.platform == "win32":
        # Maven on Windows can report /C:/... or \C:\... depending on shell/tooling.
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
        # still write errors
        pass
    lines = strip_ansi(errors).splitlines()
    snippet = "\n".join(lines[-80:]) if lines else errors
    txt_out.write_text(snippet, encoding="utf-8")
