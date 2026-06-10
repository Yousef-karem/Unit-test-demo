from __future__ import annotations

import argparse

from demo.config import DEFAULT_GPT_MODEL, DEFAULT_OLLAMA_MODEL


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=False, help="GitHub URL or local project path")
    ap.add_argument(
        "--generate-from-prompts",
        default=None,
        help="Path to an existing DemoTestCases folder; reads prompts/*.json and writes generated tests with Ollama",
    )
    ap.add_argument("--branch", default=None)
    ap.add_argument("--mode", choices=["method", "class"], default="method", help="Generate tests per method or per class")
    ap.add_argument("--build", choices=["auto", "maven", "gradle"], default="auto")
    ap.add_argument("--packages", default=None, help='Comma-separated packages, or "ALL" (default). Example: com.app.service,com.app.util')
    ap.add_argument("--select-packages", action="store_true", help="Interactive multi-select packages")
    ap.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    ap.add_argument("--gpt-model", default=DEFAULT_GPT_MODEL)
    ap.add_argument("--max-files", type=int, default=10, help="Safety limit for demo; increase if you want")
    ap.add_argument("--max-targets", type=int, default=50, help="Safety limit for demo; increase if you want")
    ap.add_argument(
        "--skip-framework-classes",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Skip classes likely to be framework wiring (default: enabled)",
    )
    args = ap.parse_args()
    if not args.repo and not args.generate_from_prompts:
        ap.error("--repo is required unless --generate-from-prompts is provided")

    from demo.pipeline import run_pipeline

    run_pipeline(args)


if __name__ == "__main__":
    main()
