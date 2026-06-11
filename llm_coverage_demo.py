from __future__ import annotations

import argparse

from demo.config import DEFAULT_GPT_MODEL, DEFAULT_OLLAMA_MODEL


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="GitHub URL or local project path")
    ap.add_argument("--branch", default=None)
    ap.add_argument("--mode", choices=["method", "class"], default="method", help="Generate tests per method or per class")
    ap.add_argument("--build", choices=["auto", "maven", "gradle"], default="auto")
    ap.add_argument("--packages", default=None, help='Comma-separated packages, or "ALL" (default). Example: com.app.service,com.app.util')
    ap.add_argument("--select-packages", action="store_true", help="Interactive multi-select packages")
    ap.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    ap.add_argument("--gpt-model", default=DEFAULT_GPT_MODEL, help="Local Ollama model to write prompts and perform repairs (defaults to qwen2.5-coder:7b)")
    ap.add_argument("--max-files", type=int, default=10, help="Safety limit for demo; increase if you want")
    ap.add_argument("--max-targets", type=int, default=50, help="Safety limit for demo; increase if you want")
    ap.add_argument(
        "--skip-framework-classes",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Skip classes likely to be framework wiring (default: enabled)",
    )
    args = ap.parse_args()
    from demo.pipeline import run_pipeline

    run_pipeline(args)


if __name__ == "__main__":
    main()
