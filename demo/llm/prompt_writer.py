from __future__ import annotations

import json
import requests
from typing import Dict

from demo.config import OLLAMA_URL, GENERATED_PREFIX
from demo.targets import extract_imports_context
from demo.utils import sanitize_java_output

# Timeout for Ollama requests (seconds). Repair prompts can be large,
# so a generous timeout is needed for smaller local models.
OLLAMA_TIMEOUT = 600


def _check_ollama_response(r: requests.Response, model: str) -> None:
    """Raise a clear error when the Ollama model is not found."""
    if r.status_code == 404:
        raise RuntimeError(
            f"Ollama model '{model}' not found (404). "
            f"Make sure the model is pulled locally with: ollama pull {model}\n"
            f"Available models can be listed with: ollama list"
        )
    r.raise_for_status()


def _extract_first_json_object(text: str) -> Dict:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model output.")

    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i+1])
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON object found: {e}")
    raise ValueError("Unterminated JSON object in model output.")


def ollama_generate_json(model: str, prompt: str, system: str | None = None) -> Dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 2200},
    }
    if system:
        payload["system"] = system
    r = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
    _check_ollama_response(r, model)
    resp_text = (r.json().get("response") or "").strip()
    return _extract_first_json_object(resp_text)


def ollama_generate_text(model: str, prompt: str, system: str | None = None) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 2200},
    }
    if system:
        payload["system"] = system
    r = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
    _check_ollama_response(r, model)
    return (r.json().get("response") or "").strip()


def ollama_write_prompt(model: str, target: Dict, project_types_text: str) -> Dict:
    pkg = target["package"] or "(default)"
    cls = target["class_name"]
    sig = target["signature"] or f"(entire class) {cls}"
    imports_context = extract_imports_context(target)

    class_mode_note = ""
    if target.get("method_name") is None:
        class_mode_note = (
            "If this is a framework wiring/config/filter class (e.g., Security filter, config), "
            "produce minimal unit tests focusing only on pure methods; do not reference servlet API types "
            "unless they appear in imports/snippet."
        )

    sys = (
        "You are a senior Java testing expert. "
        "You write STRICT prompts for a code-generation model to produce JUnit5 unit tests."
    )
    user = f"""
Return ONLY valid JSON with keys:
- "test_class_name": string (MUST start with "{GENERATED_PREFIX}" and end with "Test")
- "prompt": string

Constraints for the generated test class:
- Output MUST be ONLY Java code (no markdown, no explanations).
- Start directly with the Java package/import/class declarations. Never include prose before or after the class.
- Use ONLY JUnit 5 and Mockito (no Spring test framework).
- Do NOT use @SpringBootTest, MockMvc, WebMvcTest, SecurityMockMvcRequestPostProcessors, SpringExtension, or any Spring test utilities.
- Do NOT use Spring test annotations (@SpringBootTest, etc.).
- Do NOT use any libraries beyond JUnit 5 + Mockito.
- Only use types that appear in the provided snippet/imports (plus JDK/JUnit/Mockito).
- Do not use Spring/Spring Security test utilities or types unless they already appear in the imports.
- Use ONLY types already imported by the target class, plus JDK/JUnit/Mockito. Do not introduce new application types (repositories/entities) unless they appear in the target source or imports.
- Do NOT reference services/repositories unless they are imported in the target source OR appear in the snippet.
- If the target is a Controller class and service types are not imported, do NOT create mocks for them; test only pure logic.
- You may ONLY reference application types whose SIMPLE class name appears in the allowlist below.
- Do not invent packages or class names (e.g., Entity vs Entities).
- If a dependency type is not in imports/snippet/allowlist, avoid that test idea and write a simpler test.
- Create the class under test manually; mock only its dependencies.
- Do NOT mock the class or method under test. Do NOT write tests that only assert Mockito stubs.
- Every @Test method must execute at least one real production method from the target class or a concrete implementation of the target interface.
- If the target type is an interface or abstract type, test a real concrete implementation from the allowlist/source context when available; otherwise do not use a mocked interface as the subject under test.
- Prefer simple real objects and constructors from the source code over Mockito. Use exact constructor signatures.
- Use these imports exactly; do not use javax.* if project uses jakarta.*; do not invent missing dependencies.
- Never use raw Object in Mockito stubbing; always return the exact declared return type.
- Prefer real values for enums (e.g., Role.ADMIN) rather than mocks.
- When dealing with Spring Security authorities, use Collection<? extends GrantedAuthority> (not List<GrantedAuthority>) unless the target method explicitly returns List.
- Avoid overly specific generics; use Collection where appropriate.
- {class_mode_note}
- At least 3 @Test methods with concrete assertions.
- Name the test class exactly as you output in test_class_name.
- The Java code inside the "prompt" field MUST declare: public class <test_class_name> using the exact test_class_name value from this JSON.
- Only call methods and access fields that appear in the source snippet, related type sources, or allowlist. Do not invent getters/setters (e.g. getKey()) unless they exist in the provided source.
- For concrete classes, prefer constructing real instances (using constructors from the source) instead of mocking domain types.
- When a method parameter is an interface and a concrete implementation exists in the allowlist (e.g. Item -> MyItem), pass real instances like `new MyItem(5)` — never mock interface methods with Mockito.
- Access public fields directly when no getter exists (e.g. `item.key`, not `item.getKey()`).
- Cover all branches: equal, less-than, and greater-than paths where the source has conditionals.
- Place it in package: "{pkg}" if not default.

Target:
- package: {pkg}
- class: {cls}
- target: {sig}

Package/imports context:
{imports_context}

Source snippet:
{target["snippet"]}

Allowlist (project types):
{project_types_text}
""".strip()

    return ollama_generate_json(model, user, sys)


def ollama_repair_test(
    model: str,
    compiler_errors: str,
    file_content: str,
    source_text: str,
    package_imports: str,
    constructor_info: str,
    repository_types: str,
    related_type_sources: str = "",
) -> str:
    sys = (
        "You are a senior Java testing expert. "
        "You fix compilation errors in JUnit5 + Mockito tests."
    )
    user = f"""
Compiler errors:
{compiler_errors}

File content:
{file_content}

Class under test source:
{source_text}

Package/import lines:
{package_imports}

Constructor signature info:
{constructor_info}

Repository-like types in project:
{repository_types}

Related type sources (only use APIs shown here):
{related_type_sources or "(none)"}

Instruction: Return corrected Java test file ONLY, keep class name and package, fix typing/import issues, don’t introduce new libraries.
Only call methods and access fields that exist in the related type sources or class under test source. Replace invented methods (e.g. getKey()) with real constructors/fields/APIs from the source.
Start directly with package/import/class declarations. Do not include markdown fences, headings, bullet lists, or explanations.
Keep or add JUnit 5 imports for every annotation/assertion used.
Do not mock the class or method under test. Each test must call real production code; replace mock-only assertions with real object calls when constructors/source context allow it.
Do not invent dependency types. If a referenced type (e.g., UserRepository) does not exist in the project, replace it with the closest matching real type from the repository list or remove that dependency and adjust the test accordingly.
""".strip()

    return sanitize_java_output(ollama_generate_text(model, user, sys))


def ollama_runtime_repair_test(
    model: str,
    stack_trace: str,
    file_content: str,
) -> str:
    sys = (
        "You are a senior Java testing expert. "
        "You fix runtime errors in JUnit5 + Mockito tests."
    )
    user = f"""
Stack trace:
{stack_trace}

File content:
{file_content}

Instruction: Return corrected Java test file ONLY, keep class name and package, fix runtime errors, don’t introduce new libraries.
""".strip()

    return sanitize_java_output(ollama_generate_text(model, user, sys))
