from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import unquote


def _run(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True, capture_output=True)


def is_github_url(s: str) -> bool:
    return s.startswith("https://github.com/") or s.startswith("git@github.com:")


def parse_github_web_url(repo: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Parse a GitHub URL into (clone_url, branch, subpath).

    Supports:
    - https://github.com/owner/repo
    - https://github.com/owner/repo/tree/branch
    - https://github.com/owner/repo/tree/branch/sub/dir
    - https://github.com/owner/repo/blob/branch/path/to/file.java
    - git@github.com:owner/repo.git
    """
    repo = repo.strip().rstrip("/")

    if repo.startswith("git@github.com:"):
        return repo, None, None

    if not repo.startswith("https://github.com/"):
        return repo, None, None

    blob_m = re.match(
        r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/blob/([^/]+)/(.+)$",
        repo,
    )
    if blob_m:
        owner, name, branch, file_path = blob_m.groups()
        parent = str(Path(unquote(file_path)).parent)
        subpath = None if parent in (".", "") else parent
        return f"https://github.com/{owner}/{name}.git", branch, subpath

    tree_m = re.match(
        r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/tree/([^/]+)(?:/(.+))?$",
        repo,
    )
    if tree_m:
        owner, name, branch, subpath = tree_m.groups()
        return f"https://github.com/{owner}/{name}.git", branch, unquote(subpath) if subpath else None

    plain_m = re.match(
        r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
        repo,
    )
    if plain_m:
        owner, name = plain_m.groups()
        return f"https://github.com/{owner}/{name}.git", None, None

    return repo, None, None


def github_repo_label(repo: str) -> str:
    """Derive a stable folder label from a GitHub URL or local path."""
    if is_github_url(repo):
        clone_url, _, subpath = parse_github_web_url(repo)
        if subpath:
            return Path(subpath).name
        plain = re.sub(r"\.git$", "", clone_url.rstrip("/"))
        return plain.rsplit("/", 1)[-1]
    return Path(repo).expanduser().resolve().name


def _resolve_project_root(dest: Path, subpath: Optional[str]) -> Path:
    if not subpath:
        return dest
    project = dest / subpath
    if not project.exists():
        raise RuntimeError(
            f"Subpath not found after clone: {subpath}\n"
            f"Expected: {project}"
        )
    return project


def clone_or_update(repo: str, dest_repo: Path, branch: Optional[str]) -> Path:
    subpath: Optional[str] = None
    clone_url = repo

    if is_github_url(repo):
        clone_url, url_branch, subpath = parse_github_web_url(repo)
        if branch is None and url_branch:
            branch = url_branch

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
        if dest.exists():
            shutil.rmtree(dest)
        result = _run(["git", "clone", clone_url, str(dest)], check=False)
        if result.returncode != 0:
            err = ((result.stderr or "") + (result.stdout or "")).strip()
            raise RuntimeError(
                f"git clone failed for {clone_url}\n{err or 'unknown git error'}"
            )
        if branch:
            checkout = _run(["git", "checkout", branch], cwd=dest, check=False)
            if checkout.returncode != 0:
                err = ((checkout.stderr or "") + (checkout.stdout or "")).strip()
                raise RuntimeError(
                    f"git checkout {branch} failed after clone\n{err or 'unknown git error'}"
                )
    return _resolve_project_root(dest, subpath)


def detect_build_system(project_root: Path) -> str:
    if (project_root / "pom.xml").exists():
        return "maven"
    if (project_root / "build.gradle").exists() or (project_root / "build.gradle.kts").exists():
        return "gradle"
    raise RuntimeError("Could not detect Maven or Gradle (no pom.xml / build.gradle / build.gradle.kts).")
