from __future__ import annotations

import os
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
DEFAULT_GPT_MODEL = os.getenv("GPT_MODEL", "qwen2.5-coder:7b")  # change if your account uses a different id

GENERATED_PREFIX = "LLM_Generated"
GENERATED_PATTERN = f"{GENERATED_PREFIX}*Test"

DEMO_OUT = Path("demo_out")
