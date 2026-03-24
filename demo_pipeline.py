from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import torch
from dotenv import load_dotenv
from openai import OpenAI
from transformers import AutoModel, AutoTokenizer

from demo.packages import PACKAGE_RE, list_java_files

load_dotenv()

# -------------------------
# Config
# -------------------------
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"
DEFAULT_GPT_MODEL = "gpt-5.2"  # update if your account uses a different id
GENERATED_PREFIX = "LLM_Generated"

CODEBERT_MODEL = "microsoft/codebert-base"
MOD = 998244353

OUT_DIR = Path("demo_out")
OUT_DIR.mkdir(exist_ok=True)
(OUT_DIR / "prompts").mkdir(exist_ok=True)
(OUT_DIR / "generated").mkdir(exist_ok=True)
(OUT_DIR / "jacoco").mkdir(exist_ok=True)

# -------------------------
# Java scanning (simple method extraction)
# -------------------------
CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b")
METHOD_RE = re.compile(
    r"\bpublic\s+(static\s+)?([\w\<\>\[\]]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*(?:throws\s+[^{]+)?\{",
    re.MULTILINE,
)
TYPE_DECL_RE = re.compile(r"\b(class|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b")

def stable_suffix_for_target(t: Dict) -> str:
    key = "|".join([
        str(t.get("source_file", "")),
        str(t.get("class_name", "")),
        str(t.get("method_name", "")),
        str(t.get("params", "")),
    ])
    return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:8]

def ensure_unique_test_class_name(base: str, t: Dict) -> str:
    if not (base.startswith(GENERATED_PREFIX) and base.endswith("Test")):
        suffix = (t.get("class_name", "") + (t.get("method_name") or ""))
        suffix = re.sub(r"[^A-Za-z0-9]+", "", suffix)
        base = f"{GENERATED_PREFIX}{suffix}Test"

    suffix = stable_suffix_for_target(t)
    if base.endswith("Test"):
        base = base[:-4] + "_" + suffix + "Test"
    return base

def extract_methods(java_path: Path, max_methods: int) -> List[Dict]:
    text = java_path.read_text(encoding="utf-8", errors="ignore")
    pkg = (PACKAGE_RE.search(text).group(1) if PACKAGE_RE.search(text) else "")
    cls = (CLASS_RE.search(text).group(1) if CLASS_RE.search(text) else java_path.stem)

    methods = []
    for m in METHOD_RE.finditer(text):
        ret = m.group(2)
        name = m.group(3)
        params = (m.group(4) or "").strip()
        snippet = text[m.start(): m.start() + 1600]  # simple window
        methods.append({
            "package": pkg,
            "class_name": cls,
            "method_name": name,
            "return_type": ret,
            "params": params,
            "snippet": snippet,
            "source_file": str(java_path),
        })
        if len(methods) >= max_methods:
            break
    return methods

def list_project_types(project_root: Path) -> List[str]:
    types = set()
    for f in list_java_files(project_root):
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in TYPE_DECL_RE.finditer(txt):
            types.add(m.group(2))
    return sorted(types)

# -------------------------
# GPT prompt-writer (OpenAI Responses API)
# -------------------------
def gpt_make_prompt(client: OpenAI, gpt_model: str, method: Dict, project_types_text: str) -> Dict:
    """
    GPT writes a strict generation prompt for the code model.
    We require JSON output to keep it deterministic.
    """
    sys = (
        "You are a senior Java developer and software testing expert. "
        "Your job is to write a strict prompt for a code-generation model to produce JUnit 5 unit tests."
    )
    user = f"""
Return ONLY valid JSON with fields:
- "prompt": string (the strict instruction for the code generator)
- "test_class_name": string (suggested test class name)
- "notes": string (short notes like dependencies or pitfalls)

Rules for the prompt you write:
- It must instruct: output ONLY Java code (no markdown/explanations).
- It must instruct: do NOT mock the class under test; only mock external dependencies.
- It must require: JUnit5 + Mockito, @BeforeEach, at least 3 tests (normal, boundary, exception).
- It must require: concrete assertions, no vague phrases.
- It must include: package name if present.
- It must require: test_class_name MUST start with "{GENERATED_PREFIX}" and end with "Test".
- It must require: name the test class exactly as test_class_name.
- It must forbid: application types not in allowlist/imports/snippet.
- It must state: You may ONLY reference application types whose SIMPLE class name appears in the allowlist below.
- It must state: Do not invent packages or class names (e.g., Entity vs Entities).
- It must state: If a dependency type is not in imports/snippet/allowlist, avoid that test idea and write a simpler test.

Target info:
package = {method['package'] or '(default)'}
class = {method['class_name']}
method signature = public {method['return_type']} {method['method_name']}({method['params']})

Method snippet:
{method['snippet']}

Allowlist (project types):
{project_types_text}
""".strip()

    # OpenAI recommends Responses API as the modern interface. :contentReference[oaicite:1]{index=1}
    resp = client.responses.create(
        model=gpt_model,
        input=[
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
        # Ask for JSON only; we'll parse strictly.
        response_format={"type": "json_object"},
    )
    text = resp.output_text.strip()
    return json.loads(text)

# -------------------------
# Ollama generation (LLaMA/Qwen local)
# -------------------------
def ollama_generate(model: str, prompt: str, max_tokens: int = 1800, temperature: float = 0.2) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=240)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()

# -------------------------
# CodeBERT validator (semantic similarity + heuristics)
# -------------------------
class CodeBertEmbedder:
    def __init__(self, model_name: str = CODEBERT_MODEL):
        self.tok = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModel.from_pretrained(model_name)
        self.model.eval()

    @torch.no_grad()
    def embed(self, text: str) -> torch.Tensor:
        tokens = self.tok(text, return_tensors="pt", truncation=True, max_length=512)
        out = self.model(**tokens)
        v = out.last_hidden_state.mean(dim=1).squeeze(0)
        v = v / (v.norm() + 1e-9)
        return v

def cosine(a: torch.Tensor, b: torch.Tensor) -> float:
    return float((a * b).sum().item())

def heuristics(method: Dict, test_code: str) -> Dict:
    def has(pat: str) -> bool:
        return re.search(pat, test_code) is not None

    junit = ("org.junit.jupiter" in test_code) or has(r"@Test\b")
    mockito = ("org.mockito" in test_code) or has(r"MockitoExtension") or has(r"\bwhen\s*\(")
    asserts = len(re.findall(r"\bassert[A-Za-z]*\s*\(", test_code))
    code_only = "```" not in test_code and "Explanation:" not in test_code

    # penalty: mocking class under test (important!)
    cut = method["class_name"]
    mocks_cut = has(rf"@Mock\s+private\s+{re.escape(cut)}\b") or has(rf"Mockito\.mock\(\s*{re.escape(cut)}\.class")

    return {
        "has_junit5": junit,
        "has_mockito": mockito,
        "assert_count": asserts,
        "code_only": code_only,
        "mocks_class_under_test": mocks_cut,
    }

def validate_with_codebert(embedder: CodeBertEmbedder, method: Dict, test_code: str) -> Dict:
    sim = cosine(embedder.embed(method["snippet"]), embedder.embed(test_code))
    h = heuristics(method, test_code)

    score = 0.0
    score += 5.0 * sim
    score += 2.0 * (1.0 if h["has_junit5"] else 0.0)
    score += 1.5 * (1.0 if h["has_mockito"] else 0.0)
    score += 1.5 * min(1.0, h["assert_count"] / 6.0)
    score += 0.5 * (1.0 if h["code_only"] else 0.0)
    if h["mocks_class_under_test"]:
        score -= 2.0

    return {"similarity": sim, "score": score, **h}

# -------------------------
# JaCoCo: ensure plugin + run + parse coverage
# -------------------------
JACOCO_PLUGIN_XML = """
<plugin>
  <groupId>org.jacoco</groupId>
  <artifactId>jacoco-maven-plugin</artifactId>
  <version>0.8.12</version>
  <executions>
    <execution>
      <goals>
        <goal>prepare-agent</goal>
      </goals>
    </execution>
    <execution>
      <id>report</id>
      <phase>test</phase>
      <goals>
        <goal>report</goal>
      </goals>
    </execution>
  </executions>
</plugin>
""".strip()

def ensure_jacoco(project_root: Path) -> Path:
    pom = project_root / "pom.xml"
    if not pom.exists():
        raise RuntimeError("No pom.xml found. This script currently supports Maven projects.")

    txt = pom.read_text(encoding="utf-8", errors="ignore")
    if "jacoco-maven-plugin" in txt:
        return pom

    # backup
    backup = project_root / "pom.xml.bak_llm_demo"
    if not backup.exists():
        shutil.copy2(pom, backup)

    # naive insertion: inside <plugins> ... </plugins>
    if "<plugins>" in txt:
        txt2 = txt.replace("<plugins>", "<plugins>\n" + JACOCO_PLUGIN_XML + "\n", 1)
    else:
        # insert build/plugins skeleton before </project>
        insert = f"""
<build>
  <plugins>
{JACOCO_PLUGIN_XML}
  </plugins>
</build>
"""
        txt2 = txt.replace("</project>", insert + "\n</project>", 1)

    pom.write_text(txt2, encoding="utf-8")
    return pom

def run_maven_tests(project_root: Path) -> None:
    # run tests + produce jacoco report
    cmd = [
        "mvn",
        "-Drat.skip=true",
        "-Dcheckstyle.skip=true",
        "-Denforcer.skip=true",
        "-q",
        "test",
    ]
    subprocess.run(cmd, cwd=str(project_root), check=False)

def find_jacoco_xml(project_root: Path) -> Optional[Path]:
    p = project_root / "target" / "site" / "jacoco" / "jacoco.xml"
    return p if p.exists() else None

def parse_jacoco(xml_path: Path) -> Dict[str, float]:
    # parse a few headline counters from jacoco.xml (LINE, INSTRUCTION)
    import xml.etree.ElementTree as ET
    tree = ET.parse(str(xml_path))
    root = tree.getroot()

    def pct(covered: int, missed: int) -> float:
        denom = covered + missed
        return (covered / denom) if denom > 0 else 0.0

    totals = {}
    for c in root.findall("counter"):
        typ = c.attrib.get("type")
        covered = int(c.attrib.get("covered", "0"))
        missed = int(c.attrib.get("missed", "0"))
        totals[typ] = {"covered": covered, "missed": missed}

    line = totals.get("LINE", {"covered": 0, "missed": 0})
    instr = totals.get("INSTRUCTION", {"covered": 0, "missed": 0})

    return {
        "line_coverage": pct(line["covered"], line["missed"]),
        "instruction_coverage": pct(instr["covered"], instr["missed"]),
    }

# -------------------------
# Write generated test into project
# -------------------------
def write_test_to_project(project_root: Path, method: Dict, test_code: str, suggested_name: str) -> Path:
    pkg = method["package"]
    # Put tests into src/test/java/<package>/
    base = project_root / "src" / "test" / "java"
    if pkg:
        base = base / Path(pkg.replace(".", "/"))
    base.mkdir(parents=True, exist_ok=True)

    # filename: suggested or detected
    m = re.search(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", test_code)
    cls = m.group(1) if m else suggested_name
    out = base / f"{cls}.java"
    out.write_text(test_code, encoding="utf-8")
    return out

# -------------------------
# Main
# -------------------------
@dataclass
class ItemResult:
    method: Dict
    gpt: Dict
    test_path: str
    validation: Dict

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", required=True, help="Path to Maven project (has pom.xml)")
    ap.add_argument("--gpt-model", default=DEFAULT_GPT_MODEL)
    ap.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    ap.add_argument("--max-files", type=int, default=5)
    ap.add_argument("--max-methods-per-file", type=int, default=2)
    ap.add_argument("--min-score", type=float, default=3.0)
    ap.add_argument("--run-coverage", action="store_true")
    args = ap.parse_args()

    project_root = Path(args.project_root).resolve()
    project_types = list_project_types(project_root)
    project_types_text = ", ".join(project_types[:800])

    # OpenAI client reads OPENAI_API_KEY from env (recommended) :contentReference[oaicite:2]{index=2}
    client = OpenAI()

    embedder = CodeBertEmbedder()

    java_files = list_java_files(project_root)[: args.max_files]
    if not java_files:
        print("No Java files found under src/main/java")
        return

    results: List[ItemResult] = []

    for jf in java_files:
        methods = extract_methods(jf, args.max_methods_per_file)
        for method in methods:
            # 1) GPT writes the prompt
            gpt_out = gpt_make_prompt(client, args.gpt_model, method, project_types_text)
            gpt_out["test_class_name"] = ensure_unique_test_class_name(gpt_out.get("test_class_name", ""), method)

            prompt_path = OUT_DIR / "prompts" / f"{Path(method['source_file']).stem}_{method['method_name']}.json"
            prompt_path.write_text(json.dumps(gpt_out, indent=2), encoding="utf-8")

            # 2) Ollama generates the test code
            test_code = ollama_generate(args.ollama_model, gpt_out["prompt"])

            # Save raw generated test in demo_out as well
            gen_path = OUT_DIR / "generated" / f"{gpt_out['test_class_name']}.java"
            gen_path.write_text(test_code, encoding="utf-8")

            # 3) Validate
            val = validate_with_codebert(embedder, method, test_code)
            if val["score"] < args.min_score:
                continue

            # 4) Write into project so Maven/JaCoCo can run
            test_path = write_test_to_project(project_root, method, test_code, gpt_out["test_class_name"])

            results.append(ItemResult(method=method, gpt=gpt_out, test_path=str(test_path), validation=val))
            print(f"Generated: {test_path} | score={val['score']:.3f} sim={val['similarity']:.3f}")

    coverage = {}
    if args.run_coverage and results:
        ensure_jacoco(project_root)
        run_maven_tests(project_root)
        jac_xml = find_jacoco_xml(project_root)
        if jac_xml:
            coverage = parse_jacoco(jac_xml)
            # copy jacoco folder to demo_out/jacoco for presentation
            src = project_root / "target" / "site" / "jacoco"
            if src.exists():
                dst = OUT_DIR / "jacoco"
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)

    summary = {
        "project_root": str(project_root),
        "gpt_model": args.gpt_model,
        "ollama_model": args.ollama_model,
        "kept_tests": len(results),
        "items": [
            {
                "source": r.method["source_file"],
                "class": r.method["class_name"],
                "method": r.method["method_name"],
                "test_path": r.test_path,
                "validation": r.validation,
            }
            for r in results
        ],
        "coverage": coverage,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # simple readable report
    lines = []
    lines.append("# LLM Unit Test Demo Report\n")
    lines.append(f"- Project: `{project_root}`")
    lines.append(f"- GPT (prompt writer): `{args.gpt_model}`")
    lines.append(f"- LLM (generator via Ollama): `{args.ollama_model}`")
    lines.append(f"- Kept tests: **{len(results)}**\n")
    if coverage:
        lines.append(f"- Line coverage: **{coverage['line_coverage']*100:.2f}%**")
        lines.append(f"- Instruction coverage: **{coverage['instruction_coverage']*100:.2f}%**\n")

    for i, r in enumerate(results, 1):
        v = r.validation
        lines.append(f"## {i}. {r.method['class_name']}.{r.method['method_name']}")
        lines.append(f"- Test: `{r.test_path}`")
        lines.append(f"- Score: {v['score']:.3f} (sim={v['similarity']:.3f}, asserts={v['assert_count']}, mocksCUT={v['mocks_class_under_test']})\n")

    (OUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")

    print("\nDONE.")
    print("See demo_out/report.md and demo_out/summary.json")
    if args.run_coverage:
        print("Coverage HTML copied to demo_out/jacoco/index.html (open in browser).")

if __name__ == "__main__":
    main()
