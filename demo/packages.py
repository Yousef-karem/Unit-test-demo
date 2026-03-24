from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List


PACKAGE_RE = re.compile(r"^\s*package\s+([a-zA-Z0-9_.]+)\s*;", re.MULTILINE)


def list_java_files(project_root: Path) -> List[Path]:
    src = project_root / "src" / "main" / "java"
    return list(src.rglob("*.java")) if src.exists() else []


def discover_packages(project_root: Path) -> Dict[str, int]:
    pkgs: Dict[str, int] = {}
    for f in list_java_files(project_root):
        txt = f.read_text(encoding="utf-8", errors="ignore")
        m = PACKAGE_RE.search(txt)
        pkg = m.group(1) if m else ""
        pkgs[pkg] = pkgs.get(pkg, 0) + 1
    return dict(sorted(pkgs.items(), key=lambda kv: (-kv[1], kv[0])))


def choose_packages_interactive(pkgs: Dict[str, int]) -> List[str]:
    items = list(pkgs.items())
    print("\nDiscovered packages (with file counts):")
    for i, (p, c) in enumerate(items, 1):
        label = p if p else "(default package)"
        print(f"{i:>3}. {label}  [{c}]")

    print("\nEnter comma-separated numbers to select multiple packages.")
    print("Or press Enter to select ALL packages.")
    s = input("Selection: ").strip()
    if not s:
        return ["*"]
    idxs = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        idxs.append(int(part))
    chosen = []
    for i in idxs:
        if 1 <= i <= len(items):
            chosen.append(items[i - 1][0])
    return chosen if chosen else ["*"]


def file_in_selected_packages(java_file: Path, project_root: Path, selected: List[str]) -> bool:
    if selected == ["*"]:
        return True
    txt = java_file.read_text(encoding="utf-8", errors="ignore")
    m = PACKAGE_RE.search(txt)
    pkg = m.group(1) if m else ""
    return pkg in selected
