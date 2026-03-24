from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from openai import OpenAI

# ----------------------------
# Defaults
# ----------------------------
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
DEFAULT_GPT_MODEL = os.getenv("GPT_MODEL", "gpt-5.2")  # change if your account uses a different id

GENERATED_PREFIX = "LLM_Generated"
GENERATED_PATTERN = f"{GENERATED_PREFIX}*Test"

DEMO_OUT = Path("demo_out")
DEMO_OUT.mkdir(exist_ok=True)
(DEMO_OUT / "repos").mkdir(exist_ok=True)
(DEMO_OUT / "prompts").mkdir(exist_ok=True)
(DEMO_OUT / "generated").mkdir(exist_ok=True)
(DEMO_OUT / "coverage").mkdir(exist_ok=True)

# ----------------------------
# Utilities
# ----------------------------
def run(cmd: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True, capture_output=True)

def is_github_url(s: str) -> bool:
    return s.startswith("https://github.com/") or s.startswith("git@github.com:")

def safe_repo_dirname(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)

def clone_or_update(repo: str, dest_root: Path, branch: Optional[str]) -> Path:
    if not is_github_url(repo):
        p = Path(repo).expanduser().resolve()
        if not p.exists():
            raise RuntimeError(f"Local path not found: {p}")
        return p

    dest = dest_root / safe_repo_dirname(repo)
    if dest.exists() and (dest / ".git").exists():
        # update
        run(["git", "fetch", "--all"], cwd=dest, check=False)
        if branch:
            run(["git", "checkout", branch], cwd=dest, check=False)
            run(["git", "pull", "--rebase"], cwd=dest, check=False)
        else:
            run(["git", "pull", "--rebase"], cwd=dest, check=False)
    else:
        run(["git", "clone", repo, str(dest)], check=True)
        if branch:
            run(["git", "checkout", branch], cwd=dest, check=True)
    return dest

def detect_build_system(project_root: Path) -> str:
    if (project_root / "pom.xml").exists():
        return "maven"
    if (project_root / "build.gradle").exists() or (project_root / "build.gradle.kts").exists():
        return "gradle"
    raise RuntimeError("Could not detect Maven or Gradle (no pom.xml / build.gradle / build.gradle.kts).")

# ----------------------------
# Package discovery
# ----------------------------
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
    # sort by count desc then name
    return dict
