from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Javadoc data structures
# ---------------------------------------------------------------------------
@dataclass
class JavadocParamInfo:
    """Documents a single ``@param`` tag."""

    name: str
    description: str


@dataclass
class JavadocThrowsInfo:
    """Documents a single ``@throws`` / ``@exception`` tag."""

    exception_type: str
    description: str


@dataclass
class JavadocInfo:
    """Structured Javadoc extracted from a class-level declaration."""

    summary: str = ""
    description: str = ""
    author: List[str] = field(default_factory=list)
    version: str = ""
    since: str = ""
    deprecated: str = ""
    see: List[str] = field(default_factory=list)


@dataclass
class JavadocMethodInfo:
    """Structured Javadoc extracted from a method or constructor declaration."""

    summary: str = ""
    description: str = ""
    params: List[JavadocParamInfo] = field(default_factory=list)
    returns: str = ""
    throws: List[JavadocThrowsInfo] = field(default_factory=list)
    deprecated: str = ""
    see: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)


@dataclass
class JavadocFieldInfo:
    """Structured Javadoc extracted from a field declaration."""

    summary: str = ""
    description: str = ""
    deprecated: str = ""
#================================end num1===================================

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
    output_dir: Optional[Path] = None,
    threads: Optional[int] = None,
    batch_size: Optional[int] = None,
    ast_tree: Optional[str] = None,
    commit: Optional[str] = None,
    full_output: bool = True,
) -> Dict:
    project_root = project_root.resolve()
    output_path = output_path.resolve()
    output_dir = output_dir.resolve() if output_dir is not None else None
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
    ]
    if full_output or output_dir is None:
        cmd.extend(["--output", str(output_path)])
    cp = classpath or infer_project_classpath(project_root)
    if cp:
        cmd.extend(["--classpath", cp])
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--output-dir", str(output_dir)])
    if threads is not None and threads > 0:
        cmd.extend(["--threads", str(threads)])
    if batch_size is not None and batch_size > 0:
        cmd.extend(["--batch-size", str(batch_size)])
    if ast_tree:
        cmd.extend(["--ast-tree", ast_tree])
    if commit:
        cmd.extend(["--commit", commit])

    p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(
            "Static analyzer failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )

    if output_dir is not None and not full_output:
        return load_package_shards(output_dir)
    return json.loads(output_path.read_text(encoding="utf-8"))


def run_incremental_ast_analysis(
    project_root: Path,
    output_path: Path,
    base_analysis: Path,
    changed_files: Path,
    deleted_files: Optional[Path] = None,
    analyzer_jar: Optional[Path] = None,
    classpath: Optional[str] = None,
    output_dir: Optional[Path] = None,
    threads: Optional[int] = None,
    batch_size: Optional[int] = None,
    ast_tree: Optional[str] = None,
    commit: Optional[str] = None,
    full_output: bool = True,
) -> Dict:
    project_root = project_root.resolve()
    output_path = output_path.resolve()
    base_analysis = base_analysis.resolve()
    changed_files = changed_files.resolve()
    deleted_files = deleted_files.resolve() if deleted_files is not None else None
    output_dir = output_dir.resolve() if output_dir is not None else None
    jar = (analyzer_jar or default_analyzer_jar()).resolve()
    if not jar.exists():
        raise RuntimeError(
            f"Analyzer JAR not found: {jar}. Put `testnexus-analyzer-1.0.0.jar` "
            "in the project root, or pass --analyzer-jar."
        )
    if not (base_analysis.is_file() or base_analysis.is_dir()):
        raise RuntimeError(f"Base AST analysis file or shard directory not found: {base_analysis}")
    if not changed_files.is_file():
        raise RuntimeError(f"Changed-files list not found: {changed_files}")
    if deleted_files is not None and not deleted_files.is_file():
        raise RuntimeError(f"Deleted-files list not found: {deleted_files}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "java",
        "-jar",
        str(jar),
        "--mode",
        "incremental",
        "--project-root",
        str(project_root),
        "--base-analysis",
        str(base_analysis),
        "--changed-files",
        str(changed_files),
    ]
    if deleted_files is not None:
        cmd.extend(["--deleted-files", str(deleted_files)])
    if full_output or output_dir is None:
        cmd.extend(["--output", str(output_path)])
    cp = classpath or infer_project_classpath(project_root)
    if cp:
        cmd.extend(["--classpath", cp])
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--output-dir", str(output_dir)])
    if threads is not None and threads > 0:
        cmd.extend(["--threads", str(threads)])
    if batch_size is not None and batch_size > 0:
        cmd.extend(["--batch-size", str(batch_size)])
    if ast_tree:
        cmd.extend(["--ast-tree", ast_tree])
    if commit:
        cmd.extend(["--commit", commit])

    p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(
            "Incremental static analyzer failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )

    if output_dir is not None and not full_output:
        return load_package_shards(output_dir)
    return json.loads(output_path.read_text(encoding="utf-8"))


def load_package_shards(output_dir: Path) -> Dict:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Package shard manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shards: List[Dict] = []
    for item in manifest.get("packages") or []:
        shard_file = output_dir / item.get("file", "")
        if not shard_file.is_file():
            continue
        shard = json.loads(shard_file.read_text(encoding="utf-8"))
        shards.append(
            {
                "packageName": item.get("packageName") or "",
                "file": str(shard_file),
                "analysis": shard,
            }
        )
    call_graph_path = output_dir / (manifest.get("callGraphFile") or "call-graph.json")
    call_graph = {}
    if call_graph_path.is_file():
        call_graph = json.loads(call_graph_path.read_text(encoding="utf-8"))
    return {
        "__sharded__": True,
        "projectRoot": manifest.get("projectRoot"),
        "manifest": manifest,
        "callGraph": call_graph,
        "packageShards": shards,
    }


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

    for fqcn, class_info, shard_file in iter_analysis_classes(analysis):
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
                    "analysis_shard_file": shard_file,
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
                    shard_file=shard_file,
                )
                if target is None:
                    continue
                targets.append(target)
                if len(targets) >= max_targets:
                    return targets

        if len(targets) >= max_targets:
            break

    return targets


def iter_analysis_classes(analysis: Dict) -> Iterable[Tuple[str, Dict, str]]:
    if analysis.get("__sharded__"):
        for shard in analysis.get("packageShards") or []:
            shard_file = shard.get("file") or ""
            for fqcn, class_info in ((shard.get("analysis") or {}).get("classes") or {}).items():
                yield fqcn, class_info, shard_file
        return
    for fqcn, class_info in (analysis.get("classes") or {}).items():
        yield fqcn, class_info, ""


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
    shard_file: str = "",
) -> Dict | None:
    package, class_name = split_fqcn(fqcn)
    modifiers = (method_info.get("ast") or {}).get("modifiers") or []
    if "private" in modifiers:
        return None

    params = method_info.get("parameters") or []
    param_text = ", ".join(
        f"{p.get('type', 'Object')} {p.get('name', 'arg')}" for p in params
    )
    method_name = method_info.get("name") or signature.split("(", 1)[0]
    return_type = method_info.get("returnType") or "void"
    modifier_prefix = " ".join(m for m in ("public", "protected", "static") if m in modifiers).strip()
    if not modifier_prefix:
        modifier_prefix = "public"
    java_signature = f"{modifier_prefix} {return_type} {method_name}({param_text})".strip()
    ast = method_info.get("ast") or {}
    return {
        "package": package,
        "class_name": class_name,
        "method_name": method_name,
        "signature_key": signature,
        "signature": java_signature,
        "snippet": method_ast_summary(fqcn, signature, method_info),
        "source_file": source_file,
        "package_line": f"package {package};" if package else "",
        "start_line": method_info.get("startLine"),
        "end_line": method_info.get("endLine"),
        "ast": ast,
        "analysis_source": "ast",
        "analysis_shard_file": shard_file,
        "dependencies": (ast.get("dependencies") or {}),
    }


# ---------------------------------------------------------------------------
# Javadoc extraction helpers
# ---------------------------------------------------------------------------

def _tag_text(tag: Dict) -> str:
    """Return the plain-text content of a single Javadoc tag dict."""
    # JavaParser serialises tag content as either a plain string or a list of
    # inline elements, each of which may carry a ``text`` field.
    content = tag.get("content") or tag.get("description") or tag.get("text") or ""
    if isinstance(content, list):
        return " ".join(
            (part.get("text") or part.get("content") or "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return str(content).strip()


def _collect_tags(javadoc: Dict, *tag_names: str) -> List[Dict]:
    """Return all tag dicts whose ``name`` field matches any of *tag_names*."""
    tags = javadoc.get("tags") or []
    lower_names = {n.lower().lstrip("@") for n in tag_names}
    return [t for t in tags if (t.get("tagName") or t.get("name") or "").lower().lstrip("@") in lower_names]


def _first_tag_text(javadoc: Dict, *tag_names: str) -> str:
    """Return the text of the first matching tag, or an empty string."""
    matches = _collect_tags(javadoc, *tag_names)
    return _tag_text(matches[0]) if matches else ""


def _inline_links(javadoc: Dict) -> List[str]:
    """Extract {@link …} targets from the comment body."""
    body = javadoc.get("description") or javadoc.get("comment") or ""
    if isinstance(body, list):
        # Inline element list
        links: List[str] = []
        for part in body:
            if isinstance(part, dict) and part.get("type") in ("INLINE_TAG", "link"):
                ref = part.get("reference") or part.get("text") or ""
                if ref:
                    links.append(ref)
        return links
    # Plain-string body – mine {@link …} with regex as a fallback
    return re.findall(r"\{@link\s+([^}]+)\}", str(body))

def _javadoc_summary_and_description(javadoc: Dict) -> Tuple[str, str]:
    """Return (summary, full_description) from a Javadoc dict.

    JavaParser may expose the first sentence as ``firstSentence`` / ``summary``
    and the remainder as ``description`` or ``comment``.  We normalise both
    representations into a consistent (summary, description) pair.
    """
    # Prefer a dedicated summary / firstSentence field when present.
    summary_raw = (
        javadoc.get("firstSentence")
        or javadoc.get("summary")
        or ""
    )
    if isinstance(summary_raw, list):
        summary = " ".join(
            (p.get("text") or "") if isinstance(p, dict) else str(p) for p in summary_raw
        ).strip()
    else:
        summary = str(summary_raw).strip()

    desc_raw = javadoc.get("description") or javadoc.get("comment") or ""
    if isinstance(desc_raw, list):
        description = " ".join(
            (p.get("text") or "") if isinstance(p, dict) else str(p) for p in desc_raw
        ).strip()
    else:
        description = str(desc_raw).strip()

    # If there was no dedicated summary field, derive it from the description.
    if not summary and description:
        summary = description.split(".")[0].strip()

    # Remove the summary sentence from the description to avoid duplication.
    if summary and description.startswith(summary):
        description = description[len(summary):].lstrip(". \t").strip()

    return summary, description

def extract_class_javadoc(class_info: Dict) -> Optional[JavadocInfo]:
    """Extract class-level Javadoc from *class_info*, or return ``None``."""
    javadoc = class_info.get("javadoc")
    if not javadoc or not isinstance(javadoc, dict):
        return None

    summary, description = _javadoc_summary_and_description(javadoc)
    authors = [_tag_text(t) for t in _collect_tags(javadoc, "author")]
    see_refs = [_tag_text(t) for t in _collect_tags(javadoc, "see")]

    return JavadocInfo(
        summary=summary,
        description=description,
        author=authors,
        version=_first_tag_text(javadoc, "version"),
        since=_first_tag_text(javadoc, "since"),
        deprecated=_first_tag_text(javadoc, "deprecated"),
        see=see_refs,
    )


def extract_method_javadoc(method_info: Dict) -> Optional[JavadocMethodInfo]:
    """Extract method-level Javadoc from *method_info*, or return ``None``."""
    javadoc = method_info.get("javadoc")
    if not javadoc or not isinstance(javadoc, dict):
        return None

    summary, description = _javadoc_summary_and_description(javadoc)

    params: List[JavadocParamInfo] = []
    for tag in _collect_tags(javadoc, "param"):
        param_name = tag.get("parameterName") or tag.get("name") or ""
        # For @param the tag text often excludes the parameter name itself.
        param_desc = tag.get("description") or _tag_text(tag)
        params.append(JavadocParamInfo(name=param_name, description=param_desc))

    throws: List[JavadocThrowsInfo] = []
    for tag in _collect_tags(javadoc, "throws", "exception"):
        exc_type = tag.get("exceptionName") or tag.get("type") or ""
        exc_desc = tag.get("description") or _tag_text(tag)
        throws.append(JavadocThrowsInfo(exception_type=exc_type, description=exc_desc))

    see_refs = [_tag_text(t) for t in _collect_tags(javadoc, "see")]
    links = _inline_links(javadoc)

    returns_tag = _collect_tags(javadoc, "return", "returns")
    returns = _tag_text(returns_tag[0]) if returns_tag else ""

    return JavadocMethodInfo(
        summary=summary,
        description=description,
        params=params,
        returns=returns,
        throws=throws,
        deprecated=_first_tag_text(javadoc, "deprecated"),
        see=see_refs,
        links=links,
    )


def extract_constructor_javadoc(ctor_info: Dict) -> Optional[JavadocMethodInfo]:
    """Extract constructor Javadoc from *ctor_info*, or return ``None``.

    Constructor Javadoc is a subset of method Javadoc (no ``@return`` tag).
    """
    javadoc = ctor_info.get("javadoc")
    if not javadoc or not isinstance(javadoc, dict):
        return None

    summary, description = _javadoc_summary_and_description(javadoc)

    params: List[JavadocParamInfo] = []
    for tag in _collect_tags(javadoc, "param"):
        param_name = tag.get("parameterName") or tag.get("name") or ""
        param_desc = tag.get("description") or _tag_text(tag)
        params.append(JavadocParamInfo(name=param_name, description=param_desc))

    throws: List[JavadocThrowsInfo] = []
    for tag in _collect_tags(javadoc, "throws", "exception"):
        exc_type = tag.get("exceptionName") or tag.get("type") or ""
        exc_desc = tag.get("description") or _tag_text(tag)
        throws.append(JavadocThrowsInfo(exception_type=exc_type, description=exc_desc))

    return JavadocMethodInfo(
        summary=summary,
        description=description,
        params=params,
        returns="",
        throws=throws,
        deprecated=_first_tag_text(javadoc, "deprecated"),
        see=[],
        links=[],
    )


def extract_field_javadoc(field_info: Dict) -> Optional[JavadocFieldInfo]:
    """Extract field-level Javadoc from *field_info*, or return ``None``."""
    javadoc = field_info.get("javadoc")
    if not javadoc or not isinstance(javadoc, dict):
        return None

    summary, description = _javadoc_summary_and_description(javadoc)
    return JavadocFieldInfo(
        summary=summary,
        description=description,
        deprecated=_first_tag_text(javadoc, "deprecated"),
    )

# ---------------------------------------------------------------------------
# Javadoc rendering helpers
# ---------------------------------------------------------------------------


def _render_javadoc_info(lines: List[str], jdoc: JavadocInfo) -> None:
    """Append a human-readable rendering of *jdoc* to *lines*."""
    if jdoc.summary:
        lines.append(f"  doc: {jdoc.summary}")
    if jdoc.description:
        lines.append(f"  description: {jdoc.description}")
    if jdoc.deprecated:
        lines.append(f"  @deprecated: {jdoc.deprecated}")
    if jdoc.author:
        lines.append(f"  @author: {', '.join(jdoc.author)}")
    if jdoc.version:
        lines.append(f"  @version: {jdoc.version}")
    if jdoc.since:
        lines.append(f"  @since: {jdoc.since}")
    if jdoc.see:
        lines.append(f"  @see: {', '.join(jdoc.see)}")


def _render_method_javadoc(lines: List[str], jdoc: JavadocMethodInfo, indent: str = "  ") -> None:
    """Append a human-readable rendering of *jdoc* to *lines*."""
    if jdoc.summary:
        lines.append(f"{indent}doc: {jdoc.summary}")
    if jdoc.description:
        lines.append(f"{indent}description: {jdoc.description}")
    if jdoc.deprecated:
        lines.append(f"{indent}@deprecated: {jdoc.deprecated}")
    for param in jdoc.params:
        if param.name or param.description:
            lines.append(f"{indent}@param {param.name}: {param.description}".rstrip(": "))
    if jdoc.returns:
        lines.append(f"{indent}@return: {jdoc.returns}")
    for throws in jdoc.throws:
        exc = throws.exception_type or "Exception"
        lines.append(f"{indent}@throws {exc}: {throws.description}".rstrip(": "))
    if jdoc.see:
        lines.append(f"{indent}@see: {', '.join(jdoc.see)}")
    if jdoc.links:
        lines.append(f"{indent}links: {', '.join(jdoc.links)}")

# ---------------------------------------------------------------------------
# edit the class_ast_summary
# ---------------------------------------------------------------------------


def class_ast_summary(fqcn: str, class_info: Dict) -> str:
    lines = [
        "STATIC ANALYSIS SUMMARY (not raw source)",
        f"class: {fqcn}",
        f"kind: {class_info.get('kind', 'class')}",
    ]

    class_jdoc = extract_class_javadoc(class_info)
    if class_jdoc:
        lines.append("javadoc:")
        _render_javadoc_info(lines, class_jdoc)
    else:
        if class_info.get("domainKind"):
            lines.append(f"domainKind: {class_info['domainKind']}")
        if class_info.get("annotations"):
            lines.append(f"annotations: {', '.join(class_info['annotations'])}")
        if class_info.get("extendsClass"):
            lines.append(f"extends: {class_info['extendsClass']}")
        if class_info.get("implementsList"):
            lines.append(f"implements: {', '.join(class_info['implementsList'])}")

    if class_info.get("autowiredComponents"):
        lines.append("autowiredComponents:")
        lines.extend(f"- {item}" for item in class_info["autowiredComponents"][:20])

    fields = class_info.get("fields") or []
    if fields:
        lines.append("fields:")
        for fld in fields[:30]:
            field_jdoc = extract_field_javadoc(fld)
            if field_jdoc:
                field_type = fld.get("resolvedType") or fld.get("type") or "Object"
                lines.append(f"- {fld.get('name', '')} ({field_type})")
                if field_jdoc.summary:
                    lines.append(f"  doc: {field_jdoc.summary}")
                if field_jdoc.description:
                    lines.append(f"  description: {field_jdoc.description}")
                if field_jdoc.deprecated:
                    lines.append(f"  @deprecated: {field_jdoc.deprecated}")
            else:
                mods = " ".join(fld.get("modifiers") or [])
                field_type = fld.get("resolvedType") or fld.get("type") or "Object"
                anns = fld.get("annotations") or []
                ann_text = f" @{','.join(anns)}" if anns else ""
                lines.append(f"- {mods} {field_type} {fld.get('name', '')}{ann_text}".strip())

    constructors = class_info.get("constructors") or []
    if constructors:
        lines.append("constructors:")
        for ctor in constructors[:12]:
            lines.append(f"- {ctor.get('signature', 'constructor')}")
            ctor_jdoc = extract_constructor_javadoc(ctor)
            if ctor_jdoc:
                _render_method_javadoc(lines, ctor_jdoc, indent="  ")
            else:
                snippet = (ctor.get("sourceSnippet") or "").strip()
                if snippet:
                    lines.append(f"  snippet: {snippet[:240]}")

    lines.append("methods:")
    for sig, method in (class_info.get("methods") or {}).items():
        method_jdoc = extract_method_javadoc(method)
        if method_jdoc:
            lines.append(f"- {sig}")
            _render_method_javadoc(lines, method_jdoc, indent="  ")
        else:
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


def project_type_context_from_analysis(analysis: Dict, target: Optional[Dict] = None) -> List[str]:
    context: List[str] = []
    for fqcn, class_info in context_classes_for_target(analysis, target):
        if (class_info.get("filePath") or "").replace("\\", "/").startswith(JAVA_TEST_PREFIXES):
            continue
        _, name = split_fqcn(fqcn)
        detail = f"{class_info.get('kind', 'class')} {name}"
        if class_info.get("domainKind"):
            detail += f" [{class_info['domainKind']}]"
        if class_info.get("extendsClass"):
            detail += f" extends {class_info['extendsClass']}"
        if class_info.get("implementsList"):
            detail += f" implements {', '.join(class_info['implementsList'])}"
        methods = []
        for sig, method in (class_info.get("methods") or {}).items():
            methods.append(f"{method.get('returnType', 'void')} {sig}")
        if methods:
            detail += " api: " + "; ".join(methods[:12])
        ctor_params = []
        for ctor in class_info.get("constructors") or []:
            params = ctor.get("parameters") or []
            ctor_params.append(
                ", ".join(f"{p.get('type', 'Object')} {p.get('name', 'arg')}" for p in params)
            )
        if ctor_params:
            detail += f" constructors: {'; '.join(ctor_params[:6])}"
        field_names = []
        for field in class_info.get("fields") or []:
            if "public" in (field.get("modifiers") or []):
                field_names.append(
                    f"{field.get('resolvedType') or field.get('type', 'Object')} {field.get('name', '')}"
                )
        if field_names:
            detail += f" fields: {'; '.join(field_names[:8])}"
        context.append(detail)
    return sorted(context)


def read_class_source_snippet(class_info: Dict, project_root: Path, max_lines: int = 120) -> str:
    file_path = class_info.get("filePath")
    if not file_path:
        return ""
    source_path = project_root / file_path
    try:
        text = source_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + "\n// ... truncated ..."


def _domain_kind_label(class_info: Dict) -> str:
    return (class_info.get("domainKind") or "general").lower()


def related_type_sources_from_analysis(
    analysis: Dict,
    target: Dict,
    project_root: Path | None = None,
) -> str:
    deps = target.get("dependencies") or {}
    names = set(simple_names(type_names_from_target(target)))
    uses = set(simple_names(deps.get("usesTypes") or []))
    if uses:
        filtered = {n for n in names if n in uses}
        if filtered:
            names = filtered
    if not names:
        return ""

    root = project_root
    if root is None:
        root_path = analysis.get("projectRoot")
        if root_path:
            root = Path(root_path)

    chunks: List[str] = []
    for fqcn, class_info in context_classes_for_target(analysis, target):
        _, class_name = split_fqcn(fqcn)
        if class_name not in names or class_name == target.get("class_name"):
            continue
        domain = _domain_kind_label(class_info)
        if root and domain in ("entity", "dto"):
            source = read_class_source_snippet(class_info, root)
            if source.strip():
                chunks.append(f"// source: {class_info.get('filePath', fqcn)}\n{source}")
                continue
        chunks.append(class_ast_summary(fqcn, class_info))
    return "\n\n".join(chunks)


def context_classes_for_target(analysis: Dict, target: Optional[Dict]) -> Iterable[Tuple[str, Dict]]:
    if not analysis.get("__sharded__"):
        for fqcn, class_info in (analysis.get("classes") or {}).items():
            yield fqcn, class_info
        return

    target_package = (target or {}).get("package")
    target_shard = (target or {}).get("analysis_shard_file")
    referenced_simple_names = set(simple_names(type_names_from_target(target or {})))

    for shard in analysis.get("packageShards") or []:
        package_name = shard.get("packageName")
        shard_file = shard.get("file") or ""
        classes = (shard.get("analysis") or {}).get("classes") or {}
        include_package = package_name == target_package or shard_file == target_shard
        for fqcn, class_info in classes.items():
            _, class_name = split_fqcn(fqcn)
            if include_package or class_name in referenced_simple_names:
                yield fqcn, class_info


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
