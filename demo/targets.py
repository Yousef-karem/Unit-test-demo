from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from demo.packages import PACKAGE_RE


CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b")
METHOD_RE = re.compile(
    r"\bpublic\s+(static\s+)?([\w\<\>\[\]]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*(?:throws\s+[^{]+)?\{",
    re.MULTILINE,
)


def extract_targets(java_path: Path, mode: str) -> List[Dict]:
    txt = java_path.read_text(encoding="utf-8", errors="ignore")
    pkg_m = PACKAGE_RE.search(txt)
    pkg = pkg_m.group(1) if pkg_m else ""
    package_line = pkg_m.group(0).strip() if pkg_m else ""

    cls_m = CLASS_RE.search(txt)
    cls = cls_m.group(1) if cls_m else java_path.stem

    targets: List[Dict] = []
    if mode == "class":
        snippet = txt[:2400]
        targets.append({
            "package": pkg,
            "class_name": cls,
            "method_name": None,
            "signature": None,
            "snippet": snippet,
            "source_file": str(java_path),
            "package_line": package_line,
        })
        return targets

    for m in METHOD_RE.finditer(txt):
        ret = m.group(2)
        name = m.group(3)
        params = (m.group(4) or "").strip()
        snippet = txt[m.start(): m.start() + 2400]
        targets.append({
            "package": pkg,
            "class_name": cls,
            "method_name": name,
            "signature": f"public {ret} {name}({params})",
            "snippet": snippet,
            "source_file": str(java_path),
            "package_line": package_line,
        })
    return targets


def _extract_imports_context_from_text(text: str) -> List[str]:
    lines: List[str] = []
    for line in text.splitlines():
        if re.match(r"^\s*package\s+[^;]+;", line):
            lines.append(line.strip())
        elif re.match(r"^\s*import\s+[^;]+;", line):
            lines.append(line.strip())
    return lines


def extract_imports_context(target: Dict) -> str:
    package_line = (target.get("package_line") or "").strip()
    imports: List[str] = []
    src = target.get("source_file")
    if src:
        try:
            full_text = Path(src).read_text(encoding="utf-8", errors="ignore")
            imports = _extract_imports_context_from_text(full_text)
        except OSError:
            imports = []
    lines = [package_line] if package_line else []
    lines.extend(imports)
    return "\n".join([ln for ln in lines if ln])
