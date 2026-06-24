from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Tuple

from demo.config import (
    DEFAULT_DOCKER_MAVEN_CACHE_VOLUME,
    FALLBACK_JAVA_VERSION,
)

OFFICIAL_MAVEN_IMAGE_PREFIX = "maven:3.9-eclipse-temurin-"


@dataclass(frozen=True)
class MavenRunnerConfig:
    use_docker: bool = False
    java_version: str = FALLBACK_JAVA_VERSION
    docker_image: str | None = None
    maven_cache_volume: str = DEFAULT_DOCKER_MAVEN_CACHE_VOLUME
    compiler_java_version: str | None = None


_config = MavenRunnerConfig()


def configure_maven_runner(
    *,
    use_docker: bool | None = None,
    java_version: str | None = None,
    docker_image: str | None = None,
    maven_cache_volume: str | None = None,
    compiler_java_version: str | None = None,
) -> MavenRunnerConfig:
    global _config
    updates = {}
    if use_docker is not None:
        updates["use_docker"] = use_docker
    if java_version is not None:
        updates["java_version"] = java_version
    if docker_image is not None:
        updates["docker_image"] = docker_image
    if maven_cache_volume is not None:
        updates["maven_cache_volume"] = maven_cache_volume
    if compiler_java_version is not None:
        updates["compiler_java_version"] = compiler_java_version
    _config = replace(_config, **updates)
    return _config


def get_maven_runner_config() -> MavenRunnerConfig:
    return _config


def default_docker_image_for_java(version: str) -> str:
    return f"{OFFICIAL_MAVEN_IMAGE_PREFIX}{version}"


def docker_image_name(config: MavenRunnerConfig | None = None) -> str:
    cfg = config or _config
    if cfg.docker_image:
        return cfg.docker_image
    return default_docker_image_for_java(cfg.java_version)


def ensure_docker_available() -> None:
    if not shutil.which("docker"):
        raise RuntimeError(
            "Docker was not found on PATH. Install Docker or run without --docker-maven."
        )


def run_maven_on_host(maven_args: List[str], project_root: Path) -> Tuple[str, int]:
    from demo.coverage.maven import maven_executable

    cmd = [maven_executable(), *maven_args]
    p = subprocess.run(cmd, cwd=str(project_root), text=True, capture_output=True)
    log = (p.stdout or "") + "\n" + (p.stderr or "")
    return log, p.returncode


def run_maven_in_docker(maven_args: List[str], project_root: Path) -> Tuple[str, int]:
    cfg = _config
    workspace = project_root.resolve()
    image = docker_image_name(cfg)
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{workspace}:/workspace",
        "-v",
        f"{cfg.maven_cache_volume}:/root/.m2",
        "-w",
        "/workspace",
        image,
        "mvn",
        *maven_args,
    ]
    p = subprocess.run(docker_cmd, text=True, capture_output=True)
    log = (p.stdout or "") + "\n" + (p.stderr or "")
    return log, p.returncode


def run_maven(maven_args: List[str], project_root: Path) -> Tuple[str, int]:
    if _config.use_docker:
        return run_maven_in_docker(maven_args, project_root)
    return run_maven_on_host(maven_args, project_root)
