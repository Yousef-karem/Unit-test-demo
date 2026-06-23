from __future__ import annotations

import requests

from demo.config import OLLAMA_URL


def ollama_generate(model: str, prompt: str) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 2200},
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=300)
    try:
        r.raise_for_status()
    except requests.HTTPError as exc:
        body = (r.text or "").strip()
        raise RuntimeError(
            f"Ollama generation failed with HTTP {r.status_code} for model {model!r}. "
            f"Response body: {body}"
        ) from exc
    return (r.json().get("response") or "").strip()
