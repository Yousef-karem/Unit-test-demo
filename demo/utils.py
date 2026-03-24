from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional


def run(cmd: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True, capture_output=True)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def safe_repo_dirname(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def repo_name_from_arg(repo: str) -> str:
    from demo.repo import is_github_url

    if is_github_url(repo):
        return safe_repo_dirname(repo)
    p = Path(repo).expanduser().resolve()
    return safe_name(p.name)


def sanitize_java_output(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```java"):
        cleaned = cleaned[len("```java"):].lstrip()
    if cleaned.startswith("```"):
        cleaned = cleaned[len("```"):].lstrip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()

    start_re = re.compile(r"\b(package|import|public\s+class|class)\b")
    m = start_re.search(cleaned)
    if m:
        cleaned = cleaned[m.start():]
    return cleaned


def ensure_unique_run_class_name(base: str, used: set[str], index: int) -> str:
    if base not in used:
        return base

    if base.endswith("Test"):
        prefix = base[:-4]
        suffix = "Test"
    else:
        prefix = base
        suffix = ""

    candidate = f"{prefix}_M{index}{suffix}"
    counter = 1
    while candidate in used:
        candidate = f"{prefix}_M{index}_{counter}{suffix}"
        counter += 1
    return candidate
