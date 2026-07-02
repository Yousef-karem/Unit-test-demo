from __future__ import annotations

import os
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
DEFAULT_GPT_MODEL = os.getenv("GPT_MODEL", "qwen2.5-coder:7b")  # change if your account uses a different id
DEFAULT_GENERATION_THREADS = int(os.getenv("GENERATION_THREADS", "3"))
DEFAULT_PROMPT_MODE = os.getenv("PROMPT_MODE", "llm")

GENERATED_PREFIX = "LLM_Generated"
GENERATED_PATTERN = f"{GENERATED_PREFIX}*Test"

DEFAULT_COVERAGE_THRESHOLD = 0.8
DEFAULT_COVERAGE_REFINEMENT_METRICS = ("line", "instruction", "branch")
DEFAULT_MAX_ITERATION_REFINEMENTS = 1
DEFAULT_MAX_STAGNATION_ITERATIONS = 3
DEFAULT_MAX_RUNTIME_REPAIR_ATTEMPTS = 1
DEFAULT_RUNTIME_REPAIR_THREADS = int(os.getenv("RUNTIME_REPAIR_THREADS", "3"))
DEFAULT_MAX_COMPILE_REPAIR_ATTEMPTS = 1
DEFAULT_COMPILE_REPAIR_THREADS = int(os.getenv("COMPILE_REPAIR_THREADS", "3"))
DEFAULT_OLLAMA_REPAIR_TIMEOUT = int(os.getenv("OLLAMA_REPAIR_TIMEOUT", "300"))
DEFAULT_OLLAMA_REPAIR_CONCURRENCY = int(os.getenv("OLLAMA_REPAIR_CONCURRENCY", "1"))
DEFAULT_SKIP_GENERATION_COMPILE_GATE = True

# Backward-compatible aliases used by existing pipeline code.
COVERAGE_THRESHOLD = DEFAULT_COVERAGE_THRESHOLD
COVERAGE_REFINEMENT_METRICS = DEFAULT_COVERAGE_REFINEMENT_METRICS
MAX_ITERATION_REFINEMENTS = DEFAULT_MAX_ITERATION_REFINEMENTS
MAX_STAGNATION_ITERATIONS = DEFAULT_MAX_STAGNATION_ITERATIONS

DEMO_OUT = Path("demo_out")


def resolve_output_dir(output_dir: str | None) -> Path:
    """Resolve the root directory for generated artifacts (runs, coverage, summary.json, logs, ...).

    This is the single source of truth for the tool's output location; every
    module should derive its paths from this function's result instead of
    referencing DEMO_OUT or a hardcoded "demo_out" directly.

    If `output_dir` is falsy (the --output-dir CLI flag was omitted), this
    returns DEMO_OUT unchanged, which preserves the tool's original behavior
    exactly: a "demo_out" directory created relative to the current working
    directory. This keeps existing callers (including the current VS Code
    extension, which sets its cwd to the tool's directory) working with no
    changes required.
    """
    if output_dir:
        return Path(output_dir).expanduser().resolve()
    return DEMO_OUT


FALLBACK_JAVA_VERSION = "17"
DEFAULT_DOCKER_MAVEN_IMAGE = os.getenv("DOCKER_MAVEN_IMAGE")
DEFAULT_DOCKER_MAVEN_CACHE_VOLUME = os.getenv("DOCKER_MAVEN_CACHE_VOLUME", "llm-coverage-maven-cache")
