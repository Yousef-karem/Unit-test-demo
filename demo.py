import json
import re
from pathlib import Path

import requests
import torch
from transformers import AutoTokenizer, AutoModel

# Where outputs will be saved
OUT_DIR = Path("demo_out")
OUT_DIR.mkdir(exist_ok=True)

# Ollama local server and model (you installed this)
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen2.5-coder:7b"

# CodeBERT model for validation
CODEBERT_MODEL = "microsoft/codebert-base"

# -----------------------------
# LLM generation (Ollama)
# -----------------------------
def ollama_generate(prompt: str) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 1400,
        }
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=180)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()

# -----------------------------
# CodeBERT embeddings
# -----------------------------
class CodeBertEmbedder:
    def __init__(self, model_name: str = CODEBERT_MODEL):
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModel.from_pretrained(model_name)
        self.model.eval()

    @torch.no_grad()
    def embed(self, text: str) -> torch.Tensor:
        tokens = self.tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
            padding=False
        )
        out = self.model(**tokens)
        vec = out.last_hidden_state.mean(dim=1).squeeze(0)
        vec = vec / (vec.norm() + 1e-9)
        return vec

def cosine(a: torch.Tensor, b: torch.Tensor) -> float:
    return float((a * b).sum().item())

# -----------------------------
# Heuristic validation
# -----------------------------
def has_junit5(code: str) -> bool:
    return ("org.junit.jupiter" in code) or ("@Test" in code)

def has_mockito(code: str) -> bool:
    pats = [r"\bMockito\.", r"\bwhen\s*\(", r"@Mock\b", r"MockitoExtension"]
    return any(re.search(p, code) for p in pats)

def count_asserts(code: str) -> int:
    return len(re.findall(r"\bassert[A-Za-z]*\s*\(", code))

def extract_class_name(code: str) -> str:
    m = re.search(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", code)
    return m.group(1) if m else "GeneratedTest"
def mocks_class_under_test(code: str, class_name: str) -> bool:
    pattern = rf"@Mock\s+private\s+{class_name}\b"
    return re.search(pattern, code) is not None


def validate(method_snippet: str, test_code: str, embedder: CodeBertEmbedder) -> dict:
    sim = cosine(embedder.embed(method_snippet), embedder.embed(test_code))
    junit = has_junit5(test_code)
    mockito = has_mockito(test_code)
    asserts = count_asserts(test_code)

    # Simple demo score (0..10-ish)
    score = 0.0
    score += 5.0 * sim
    score += 2.0 * (1.0 if junit else 0.0)
    score += 2.0 * (1.0 if mockito else 0.0)
    score += 1.0 * min(1.0, asserts / 6.0)
    if mocks_class_under_test(test_code, "Calculator"):
        score -= 2.0

    return {
        "codebert_similarity": sim,
        "has_junit5": junit,
        "has_mockito": mockito,
        "assert_count": asserts,
        "score": score,
        "test_class_name": extract_class_name(test_code),
        "model_used": OLLAMA_MODEL
    }

# -----------------------------
# Prompt
# -----------------------------
def build_prompt(method_snippet: str) -> str:
    return f"""
You are an expert Java developer and QA engineer.
Generate a COMPLETE runnable JUnit 5 test class (Java code only) using Mockito for the method below.

STRICT RULES:
- Output ONLY Java code. No markdown. No explanations.
- Use JUnit 5 imports and annotations.
- Use Mockito (MockitoExtension OR manual mocks).
- Include @BeforeEach setup.
- Write at least 3 @Test methods:
  1) normal case
  2) boundary/null/edge case
  3) exception/invalid case if applicable
- Use concrete assertions (assertEquals/assertTrue/assertThrows...).

Method snippet:
{method_snippet}
""".strip()

def main():
    # Replace this snippet later with any method from any project
    method_snippet = """
public class Calculator {
  public int divide(int a, int b) {
    if (b == 0) throw new IllegalArgumentException("b cannot be 0");
    return a / b;
  }
}
""".strip()

    print("Loading CodeBERT...")
    embedder = CodeBertEmbedder()

    print("Generating test with Ollama model:", OLLAMA_MODEL)
    prompt = build_prompt(method_snippet)
    test_code = ollama_generate(prompt)

    report = validate(method_snippet, test_code, embedder)

    # Save outputs
    (OUT_DIR / "method_snippet.java").write_text(method_snippet, encoding="utf-8")
    (OUT_DIR / "generated_test.java").write_text(test_code, encoding="utf-8")
    (OUT_DIR / "validation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== DONE ===")
    print("Saved:")
    print(" - demo_out/method_snippet.java")
    print(" - demo_out/generated_test.java")
    print(" - demo_out/validation_report.json")
    print("\nValidation summary:")
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()

