from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def _run(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True, capture_output=True)


def is_github_url(s: str) -> bool:
    return s.startswith("https://github.com/") or s.startswith("git@github.com:")


def clone_or_update(repo: str, dest_repo: Path, branch: Optional[str]) -> Path:
    if not is_github_url(repo):
        p = Path(repo).expanduser().resolve()
        if not p.exists():
            raise RuntimeError(f"Local path not found: {p}")
        if dest_repo.exists():
            shutil.rmtree(dest_repo)
        shutil.copytree(p, dest_repo)
        return dest_repo

    dest = dest_repo
    if dest.exists() and (dest / ".git").exists():
        _run(["git", "fetch", "--all"], cwd=dest, check=False)
        if branch:
            _run(["git", "checkout", branch], cwd=dest, check=False)
            _run(["git", "pull", "--rebase"], cwd=dest, check=False)
        else:
            _run(["git", "pull", "--rebase"], cwd=dest, check=False)
    else:
        _run(["git", "clone", repo, str(dest)], check=True)
        if branch:
            _run(["git", "checkout", branch], cwd=dest, check=True)
    return dest


def detect_build_system(project_root: Path) -> str:
    if (project_root / "pom.xml").exists():
        return "maven"
    if (project_root / "build.gradle").exists() or (project_root / "build.gradle.kts").exists():
        return "gradle"
    raise RuntimeError("Could not detect Maven or Gradle (no pom.xml / build.gradle / build.gradle.kts).")
