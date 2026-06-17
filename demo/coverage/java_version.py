from __future__ import annotations

import re
from pathlib import Path

SUPPORTED_DOCKER_JAVA_VERSIONS = ("8", "11", "17", "21")
FALLBACK_JAVA_VERSION = "8"

_PROPERTY_PATTERNS = (
    r"<maven\.compiler\.release>\s*([^<\s]+)\s*</maven\.compiler\.release>",
    r"<java\.version>\s*([^<\s]+)\s*</java\.version>",
    r"<maven\.compiler\.source>\s*([^<\s]+)\s*</maven\.compiler\.source>",
)

_COMPILER_PLUGIN_PATTERNS = (
    r"<artifactId>maven-compiler-plugin</artifactId>.*?<release>\s*([^<\s]+)\s*</release>",
    r"<artifactId>maven-compiler-plugin</artifactId>.*?<source>\s*([^<\s]+)\s*</source>",
)


def normalize_java_version(raw: str) -> str:
    value = raw.strip()
    if value.startswith("1."):
        return value.split(".", 1)[1].split(".")[0]
    return value.split(".")[0]


def coerce_supported_version(version: str) -> str:
    normalized = normalize_java_version(version)
    if normalized in SUPPORTED_DOCKER_JAVA_VERSIONS:
        return normalized
    print(
        f"Java version {version!r} is not supported for Docker Maven "
        f"(supported: {', '.join(SUPPORTED_DOCKER_JAVA_VERSIONS)}); "
        f"using fallback Java {FALLBACK_JAVA_VERSION}."
    )
    return FALLBACK_JAVA_VERSION


def java_version_guidance(version: str) -> str:
    normalized = normalize_java_version(version)
    try:
        major = int(normalized)
    except ValueError:
        major = int(FALLBACK_JAVA_VERSION)

    lines = [f"Generated tests must compile under Java {normalized}."]
    if major <= 8:
        lines.append(
            "Do not use var, records, text blocks, switch expressions, or other post-Java-8 syntax."
        )
    elif major <= 11:
        lines.append(
            "Do not use records, text blocks, pattern matching switch, or other post-Java-11 syntax unless they appear in the source snippet."
        )
    elif major <= 16:
        lines.append(
            "Do not use records, text blocks, or sealed types unless they appear in the source snippet."
        )
    else:
        lines.append(
            "Use modern syntax only when appropriate for this Java level and when supported by the source snippet."
        )
    lines.append(f"Use syntax and APIs valid for Java {normalized} only.")
    return " ".join(lines)


def _first_match(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def detect_java_version(project_root: Path) -> str | None:
    pom = project_root / "pom.xml"
    if not pom.exists():
        return None

    text = pom.read_text(encoding="utf-8", errors="ignore")
    raw = _first_match(text, _PROPERTY_PATTERNS)
    if raw is None:
        raw = _first_match(text, _COMPILER_PLUGIN_PATTERNS)
    if raw is None:
        return None

    return normalize_java_version(raw)


def resolve_project_java_version(project_root: Path) -> str:
    detected = detect_java_version(project_root)
    if detected is None:
        print(
            f"Java version not found in {project_root / 'pom.xml'}; "
            f"using fallback Java {FALLBACK_JAVA_VERSION} for this project."
        )
        return FALLBACK_JAVA_VERSION
    return detected


def resolve_docker_java_version(project_root: Path) -> str:
    return coerce_supported_version(resolve_project_java_version(project_root))
