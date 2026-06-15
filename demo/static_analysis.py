from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ANALYZER_JAR = REPO_ROOT / "testnexus-analyzer-1.0.0.jar"
FALLBACK_ANALYZER_JAR = (
    REPO_ROOT
    / "Grad proj test"
    / "testnexus-analyzer"
    / "target"
    / "testnexus-analyzer-1.0.0.jar"
)

JAVA_TEST_PREFIXES = ("src/test/java/", "src\\test\\java\\")
JAVA_MAIN_PREFIXES = ("src/main/java/", "src\\main\\java\\")


def run_ast_analysis(
    project_root: Path,
    output_path: Path,
    analyzer_jar: Optional[Path] = None,
    classpath: Optional[str] = None,
) -> Dict:
    project_root = project_root.resolve()
    output_path = output_path.resolve()
    jar = (analyzer_jar or default_analyzer_jar()).resolve()
    if not jar.exists():
        raise RuntimeError(
            f"Analyzer JAR not found: {jar}. Put `testnexus-analyzer-1.0.0.jar` "
            "in the project root, or pass --analyzer-jar."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "java",
        "-jar",
        str(jar),
        "--mode",
        "full",
        "--project-root",
        str(project_root),
        "--output",
        str(output_path),
    ]
    cp = classpath or infer_project_classpath(project_root)
    if cp:
        cmd.extend(["--classpath", cp])

    p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(
            "Static analyzer failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )

    return json.loads(output_path.read_text(encoding="utf-8"))


def default_analyzer_jar() -> Path:
    if DEFAULT_ANALYZER_JAR.exists():
        return DEFAULT_ANALYZER_JAR
    return FALLBACK_ANALYZER_JAR


def infer_project_classpath(project_root: Path) -> str:
    entries: List[str] = []
    for rel in (
        "target/classes",
        "target/test-classes",
        "build/classes/java/main",
        "build/classes/java/test",
        "build/classes/kotlin/main",
        "build/classes/kotlin/test",
    ):
        p = project_root / rel
        if p.exists():
            entries.append(str(p.resolve()))

    # Keep this bounded but useful: Gradle/Maven dependency jars copied into the
    # project are enough for JavaParser symbol solving without invoking builds.
    for root_name in ("target", "build", "lib", "libs"):
        root = project_root / root_name
        if not root.exists():
            continue
        for jar in root.rglob("*.jar"):
            entries.append(str(jar.resolve()))

    return os.pathsep.join(dict.fromkeys(entries))


def targets_from_analysis(
    analysis: Dict,
    project_root: Path,
    mode: str,
    selected_packages: List[str],
    max_files: int,
    max_targets: int,
    skip_framework_classes: bool,
) -> List[Dict]:
    targets: List[Dict] = []
    seen_files: set[str] = set()
    skip_keywords = ("application", "config", "filter", "security", "interceptor")

    for fqcn, class_info in (analysis.get("classes") or {}).items():
        file_path = (class_info.get("filePath") or "").replace("\\", "/")
        if not file_path or file_path.startswith(JAVA_TEST_PREFIXES):
            continue
        if JAVA_MAIN_PREFIXES and not file_path.startswith(JAVA_MAIN_PREFIXES):
            continue

        package, class_name = split_fqcn(fqcn)
        if selected_packages != ["*"] and package not in selected_packages:
            continue
        if class_info.get("kind") == "interface":
            continue
        if skip_framework_classes and any(k in class_name.lower() for k in skip_keywords):
            continue

        if file_path not in seen_files:
            if len(seen_files) >= max_files and targets:
                break
            seen_files.add(file_path)

        source_file = str((project_root / file_path).resolve())
        if mode == "class":
            targets.append(
                {
                    "package": package,
                    "class_name": class_name,
                    "method_name": None,
                    "signature": None,
                    "snippet": class_ast_summary(fqcn, class_info),
                    "source_file": source_file,
                    "package_line": f"package {package};" if package else "",
                    "ast": {"class": class_info},
                    "analysis_source": "ast",
                }
            )
        else:
            for signature, method_info in (class_info.get("methods") or {}).items():
                target = target_from_method(
                    fqcn=fqcn,
                    class_info=class_info,
                    signature=signature,
                    method_info=method_info,
                    source_file=source_file,
                )
                targets.append(target)
                if len(targets) >= max_targets:
                    return targets

        if len(targets) >= max_targets:
            break

    return targets


def split_fqcn(fqcn: str) -> Tuple[str, str]:
    if "." not in fqcn:
        return "", fqcn
    package, class_name = fqcn.rsplit(".", 1)
    return package, class_name


def target_from_method(
    fqcn: str,
    class_info: Dict,
    signature: str,
    method_info: Dict,
    source_file: str,
) -> Dict:
    package, class_name = split_fqcn(fqcn)
    params = method_info.get("parameters") or []
    param_text = ", ".join(
        f"{p.get('type', 'Object')} {p.get('name', 'arg')}" for p in params
    )
    method_name = method_info.get("name") or signature.split("(", 1)[0]
    return_type = method_info.get("returnType") or "void"
    java_signature = f"public {return_type} {method_name}({param_text})"
    ast = method_info.get("ast") or {}
    return {
        "package": package,
        "class_name": class_name,
        "method_name": method_name,
        "signature": java_signature,
        "snippet": method_ast_summary(fqcn, signature, method_info),
        "source_file": source_file,
        "package_line": f"package {package};" if package else "",
        "ast": ast,
        "analysis_source": "ast",
        "dependencies": (ast.get("dependencies") or {}),
    }


def class_ast_summary(fqcn: str, class_info: Dict) -> str:
    lines = [
        "STATIC ANALYSIS SUMMARY (not raw source)",
        f"class: {fqcn}",
        f"kind: {class_info.get('kind', 'class')}",
    ]
    if class_info.get("extendsClass"):
        lines.append(f"extends: {class_info['extendsClass']}")
    if class_info.get("implementsList"):
        lines.append(f"implements: {', '.join(class_info['implementsList'])}")
    lines.append("methods:")
    for sig, method in (class_info.get("methods") or {}).items():
        lines.append(f"- {method.get('returnType', 'void')} {sig}")
    return "\n".join(lines)


def method_ast_summary(fqcn: str, signature: str, method_info: Dict) -> str:
    ast = method_info.get("ast") or {}
    lines = [
        "STATIC ANALYSIS SUMMARY (not raw source)",
        f"class: {fqcn}",
        f"method: {method_info.get('returnType', 'void')} {signature}",
    ]
    append_dict(lines, "controlFlow", ast.get("controlFlow"))
    append_dict(lines, "metrics", ast.get("metrics"))
    append_dict(lines, "testabilityHints", ast.get("testabilityHints"))
    append_dict(lines, "dataFlow", ast.get("dataFlow"))
    append_dict(lines, "smells", ast.get("smells"))
    deps = ast.get("dependencies") or {}
    if deps.get("calls"):
        lines.append("calls:")
        lines.extend(f"- {call}" for call in deps["calls"][:30])
    if deps.get("usesTypes"):
        lines.append("usesTypes: " + ", ".join(deps["usesTypes"][:40]))
    variables = ast.get("variables") or []
    if variables:
        lines.append("variables:")
        for var in variables[:30]:
            flags = []
            if var.get("read"):
                flags.append("read")
            if var.get("written"):
                flags.append("written")
            lines.append(
                f"- {var.get('kind', 'VAR')} {var.get('type', '')} "
                f"{var.get('name', '')} {'/'.join(flags)}".strip()
            )
    tree = compact_ast_tree(ast.get("astTree"), max_nodes=80)
    if tree:
        lines.append("astTree:")
        lines.extend(tree)
    return "\n".join(lines)


def append_dict(lines: List[str], label: str, data: Optional[Dict]) -> None:
    if not data:
        return
    parts = [f"{k}={v}" for k, v in data.items() if v not in (None, "", [], {})]
    if parts:
        lines.append(f"{label}: " + ", ".join(parts))


def compact_ast_tree(node: Optional[Dict], max_nodes: int, depth: int = 0) -> List[str]:
    if not node or max_nodes <= 0:
        return []
    code = re.sub(r"\s+", " ", node.get("code") or "").strip()
    if len(code) > 120:
        code = code[:117] + "..."
    line = f"{'  ' * depth}- {node.get('kind', 'Node')}: {code}"
    lines = [line]
    remaining = max_nodes - 1
    for child in node.get("children") or []:
        if remaining <= 0:
            break
        child_lines = compact_ast_tree(child, remaining, depth + 1)
        lines.extend(child_lines)
        remaining -= len(child_lines)
    return lines


def project_type_context_from_analysis(analysis: Dict) -> List[str]:
    context: List[str] = []
    for fqcn, class_info in (analysis.get("classes") or {}).items():
        if (class_info.get("filePath") or "").replace("\\", "/").startswith(JAVA_TEST_PREFIXES):
            continue
        _, name = split_fqcn(fqcn)
        detail = f"{class_info.get('kind', 'class')} {name}"
        if class_info.get("extendsClass"):
            detail += f" extends {class_info['extendsClass']}"
        if class_info.get("implementsList"):
            detail += f" implements {', '.join(class_info['implementsList'])}"
        methods = []
        for sig, method in (class_info.get("methods") or {}).items():
            methods.append(f"{method.get('returnType', 'void')} {sig}")
        if methods:
            detail += " api: " + "; ".join(methods[:12])
        context.append(detail)
    return sorted(context)


def related_type_sources_from_analysis(analysis: Dict, target: Dict) -> str:
    names = set(simple_names(type_names_from_target(target)))
    if not names:
        return ""

    chunks: List[str] = []
    for fqcn, class_info in (analysis.get("classes") or {}).items():
        _, class_name = split_fqcn(fqcn)
        if class_name not in names or class_name == target.get("class_name"):
            continue
        chunks.append(class_ast_summary(fqcn, class_info))
    return "\n\n".join(chunks)


def type_names_from_target(target: Dict) -> Iterable[str]:
    yield from re.findall(r"\b[A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)*\b", target.get("signature") or "")
    deps = target.get("dependencies") or {}
    for name in deps.get("usesTypes") or []:
        yield name
    for call in deps.get("calls") or []:
        if "." in call:
            yield call.rsplit(".", 1)[0]


def simple_names(names: Iterable[str]) -> Iterable[str]:
    for name in names:
        yield name.rsplit(".", 1)[-1]
