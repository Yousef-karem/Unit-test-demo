from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Tuple

from demo.config import GENERATED_PREFIX


def gradle_init_script_text() -> str:
    return f"""
allprojects {{
  apply plugin: 'jacoco'
}}

tasks.withType(Test).configureEach {{
  useJUnitPlatform()
  include "**/{GENERATED_PREFIX}*Test*"
}}

tasks.withType(JacocoReport).configureEach {{
  reports {{
    xml.required = true
    html.required = true
  }}
}}
""".strip()


def run_gradle_jacoco(project_root: Path, demo_root: Path) -> Tuple[str, int]:
    init_script_path = demo_root / "gradle_init.gradle"
    init_script_path.write_text(gradle_init_script_text(), encoding="utf-8")
    cmd = [
        "./gradlew",
        "-q",
        "-I",
        str(init_script_path),
        "test",
        "--tests",
        f"*{GENERATED_PREFIX}*Test",
        "jacocoTestReport",
    ]
    p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
    log = (p.stdout or "") + "\n" + (p.stderr or "")
    return log, p.returncode
