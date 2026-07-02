from __future__ import annotations

import argparse

from demo.config import (
    DEFAULT_DOCKER_MAVEN_CACHE_VOLUME,
    DEFAULT_DOCKER_MAVEN_IMAGE,
    DEFAULT_GENERATION_THREADS,
    DEFAULT_GPT_MODEL,
    DEFAULT_PROMPT_MODE,
    DEFAULT_MAX_COMPILE_REPAIR_ATTEMPTS,
    DEFAULT_MAX_ITERATION_REFINEMENTS,
    DEFAULT_MAX_RUNTIME_REPAIR_ATTEMPTS,
    DEFAULT_MAX_STAGNATION_ITERATIONS,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_COMPILE_REPAIR_THREADS,
    DEFAULT_OLLAMA_REPAIR_CONCURRENCY,
    DEFAULT_OLLAMA_REPAIR_TIMEOUT,
    DEFAULT_RUNTIME_REPAIR_THREADS,
    DEFAULT_SKIP_GENERATION_COMPILE_GATE,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None, help="GitHub URL or local project path")
    ap.add_argument(
        "--from-run",
        default=None,
        help="Re-run JaCoCo coverage for an existing run without regenerating tests "
        "(example: demo_out/09Ordenacao/runs/20260617_071517)",
    )
    ap.add_argument("--branch", default=None)
    ap.add_argument("--mode", choices=["method", "class"], default="method", help="Generate tests per method or per class")
    ap.add_argument(
        "--prompt-mode",
        choices=["llm", "class-slice-llm", "static"],
        default=DEFAULT_PROMPT_MODE,
        help="Prompt generation strategy: llm (LLM meta-prompt), class-slice-llm (multi-prompt class mode), "
        "or static (AST semantic prompt builder)",
    )
    ap.add_argument("--build", choices=["auto", "maven", "gradle"], default="auto")
    ap.add_argument(
        "--analysis-mode",
        choices=["ast", "source"],
        default="ast",
        help="Use static analyzer AST JSON for targets/context, or legacy source regex extraction",
    )
    ap.add_argument(
        "--analyzer-jar",
        default=None,
        help="Path to testnexus-analyzer fat JAR; defaults to ./testnexus-analyzer-1.0.0.jar",
    )
    ap.add_argument(
        "--analysis-classpath",
        default=None,
        help="Optional classpath for AST symbol solving; if omitted, common target/build/lib jars are inferred",
    )
    ap.add_argument(
        "--analysis-threads",
        type=int,
        default=None,
        help="Worker threads for AST analysis; defaults to available processors",
    )
    ap.add_argument(
        "--analysis-shards-dir",
        default=None,
        help="Optional directory for package-sharded AST JSON output; defaults to DemoTestCases/<repo>-shards for each run",
    )
    ap.add_argument(
        "--analysis-full-output",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also write/load one full analysis.json beside package shards (default: disabled)",
    )
    ap.add_argument(
        "--analysis-batch-size",
        type=int,
        default=50,
        help="Java files per AST worker batch inside each package (default: 50)",
    )
    ap.add_argument(
        "--analysis-ast-tree",
        choices=["none", "summary", "full"],
        default="summary",
        help="AST tree detail stored per method; summary is recommended for large projects",
    )
    ap.add_argument(
        "--analysis-incremental",
        action="store_true",
        help="Run AST analysis incrementally from a previous analysis file and PR changed-file list",
    )
    ap.add_argument(
        "--analysis-base",
        default=None,
        help="Base analysis JSON, manifest.json, or package-shard directory from a previous analyzer run",
    )
    ap.add_argument(
        "--analysis-changed-files",
        default=None,
        help="Text file with changed/added .java paths, one per line, relative to the target project root",
    )
    ap.add_argument(
        "--analysis-deleted-files",
        default=None,
        help="Optional text file with deleted .java paths, one per line, relative to the target project root",
    )
    ap.add_argument(
        "--analysis-diff-base",
        default=None,
        help="Optional Git base ref/commit for automatic incremental changed/deleted file detection",
    )
    ap.add_argument("--packages", default=None, help='Comma-separated packages, or "ALL" (default). Example: com.app.service,com.app.util')
    ap.add_argument("--select-packages", action="store_true", help="Interactive multi-select packages")
    ap.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    ap.add_argument("--gpt-model", default=DEFAULT_GPT_MODEL, help="Local Ollama model to write prompts and perform repairs (defaults to qwen2.5-coder:7b)")
    ap.add_argument(
        "--generation-threads",
        type=int,
        default=DEFAULT_GENERATION_THREADS,
        help="Worker threads for parallel prompt/test generation (default: GENERATION_THREADS env or 3)",
    )
    ap.add_argument("--max-files", type=int, default=10, help="Safety limit for demo; increase if you want")
    ap.add_argument("--max-targets", type=int, default=50, help="Safety limit for demo; increase if you want")
    ap.add_argument(
        "--class-prompt-slices",
        type=int,
        default=1,
        choices=[1, 2, 3, 4],
        help="In class mode with AST analysis, expand each class into N focused prompt slices (default: 1 = current behavior)",
    )
    ap.add_argument(
        "--max-refinement-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATION_REFINEMENTS,
        help="Maximum coverage-refinement LLM iterations per survivor test (default: 5)",
    )
    ap.add_argument(
        "--max-stagnation-iterations",
        type=int,
        default=DEFAULT_MAX_STAGNATION_ITERATIONS,
        help="Maximum consecutive coverage-refinement iterations with no improvement (default: 3)",
    )
    ap.add_argument(
        "--max-runtime-repair-attempts",
        type=int,
        default=DEFAULT_MAX_RUNTIME_REPAIR_ATTEMPTS,
        help="Maximum LLM repair attempts per failing test file during runtime refinement (default: 1)",
    )
    ap.add_argument(
        "--runtime-repair-threads",
        type=int,
        default=DEFAULT_RUNTIME_REPAIR_THREADS,
        help="Worker threads for parallel runtime repair (default: RUNTIME_REPAIR_THREADS env or 3)",
    )
    ap.add_argument(
        "--max-compile-repair-attempts",
        type=int,
        default=DEFAULT_MAX_COMPILE_REPAIR_ATTEMPTS,
        help="Maximum LLM repair attempts per failing test file during compile refinement (default: 1)",
    )
    ap.add_argument(
        "--compile-repair-threads",
        type=int,
        default=DEFAULT_COMPILE_REPAIR_THREADS,
        help="Worker threads for parallel compile repair (default: COMPILE_REPAIR_THREADS env or 3)",
    )
    ap.add_argument(
        "--ollama-repair-timeout",
        type=int,
        default=DEFAULT_OLLAMA_REPAIR_TIMEOUT,
        help="Ollama timeout in seconds per compile/runtime repair call (default: OLLAMA_REPAIR_TIMEOUT env or 300)",
    )
    ap.add_argument(
        "--ollama-repair-concurrency",
        type=int,
        default=DEFAULT_OLLAMA_REPAIR_CONCURRENCY,
        help="Max concurrent Ollama repair calls (default: OLLAMA_REPAIR_CONCURRENCY env or 1; use 1 on single GPU)",
    )
    ap.add_argument(
        "--skip-generation-compile-gate",
        default=DEFAULT_SKIP_GENERATION_COMPILE_GATE,
        action=argparse.BooleanOptionalAction,
        help="Skip Maven test-compile during generation; compile stage runs next (default: skip enabled)",
    )
    ap.add_argument(
        "--skip-framework-classes",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Skip classes likely to be framework wiring (default: enabled)",
    )
    ap.add_argument(
        "--docker-maven",
        action="store_true",
        help="Run Maven compile/test/coverage inside a Docker container (pinned JDK/Maven)",
    )
    ap.add_argument(
        "--docker-maven-image",
        default=DEFAULT_DOCKER_MAVEN_IMAGE,
        help="Override Docker image for Maven (default: maven:3.9-eclipse-temurin-<java-version>)",
    )
    ap.add_argument(
        "--docker-maven-cache-volume",
        default=DEFAULT_DOCKER_MAVEN_CACHE_VOLUME,
        help="Named Docker volume for Maven .m2 cache (default: llm-coverage-maven-cache)",
    )
    args = ap.parse_args()
    if args.from_run:
        if args.repo:
            ap.error("Use either --from-run or --repo, not both.")
        from demo.coverage_rerun import run_coverage_from_run

        run_coverage_from_run(args)
        return
    if not args.repo:
        ap.error("--repo is required unless --from-run is provided.")
    if args.prompt_mode == "static" and args.analysis_mode != "ast":
        ap.error("Static prompt mode requires --analysis-mode ast")
    if args.class_prompt_slices > 1 and args.mode != "class":
        ap.error("--class-prompt-slices requires --mode class")
    if args.class_prompt_slices > 1 and args.analysis_mode != "ast":
        ap.error("--class-prompt-slices requires --analysis-mode ast")
    from demo.prompt_generation.factory import create_prompt_generator
    from demo.pipeline import Pipeline

    Pipeline(create_prompt_generator(args)).run(args)


if __name__ == "__main__":
    main()
