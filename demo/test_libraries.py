from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

JUnitVersion = Literal["4", "5"]

_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")


def _read_build_text(project_root: Path) -> str:
    parts: list[str] = []
    for name in _BUILD_FILES:
        path = project_root / name
        if not path.exists():
            continue
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts)


def _junit_from_build(text: str) -> JUnitVersion | None:
    if not text:
        return None

    lower = text.lower()
    has_junit5 = bool(
        re.search(r"junit-jupiter", lower)
        or re.search(r"org\.junit\.jupiter", lower)
    )
    has_junit4 = bool(
        re.search(
            r"<groupId>\s*junit\s*</groupId>\s*<artifactId>\s*junit\s*</artifactId>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        or re.search(r"['\"]junit:junit:", text)
        or re.search(r"testImplementation\s*\(?\s*['\"]junit:junit:", text)
    )

    if has_junit5:
        return "5"
    if has_junit4:
        return "4"
    return None


def _junit_from_existing_tests(project_root: Path) -> JUnitVersion | None:
    test_root = project_root / "src" / "test" / "java"
    if not test_root.exists():
        return None

    saw_junit5 = False
    saw_junit4 = False
    for path in test_root.rglob("*.java"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"import\s+org\.junit\.jupiter\.", text):
            saw_junit5 = True
        if re.search(r"import\s+org\.junit\.(Test|Before|After|Assert)\b", text):
            saw_junit4 = True
        if re.search(r"import\s+static\s+org\.junit\.Assert\.", text):
            saw_junit4 = True

    if saw_junit5 and not saw_junit4:
        return "5"
    if saw_junit4 and not saw_junit5:
        return "4"
    if saw_junit5:
        return "5"
    return None


def detect_junit_version(project_root: Path) -> JUnitVersion:
    from_build = _junit_from_build(_read_build_text(project_root))
    if from_build is not None:
        return from_build

    from_tests = _junit_from_existing_tests(project_root)
    if from_tests is not None:
        return from_tests

    return "5"
