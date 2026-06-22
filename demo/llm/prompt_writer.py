from __future__ import annotations

import json
import requests
from typing import Dict, Tuple

from demo.config import OLLAMA_URL, GENERATED_PREFIX
from demo.coverage.java_version import java_version_guidance
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


def _junit_prompt_rules(junit_version: str, has_mockito: bool) -> Tuple[str, str, str, str, str, str]:
    junit_label = f"JUnit {junit_version}"
    if junit_version == "4":
        junit_imports = (
            "Use org.junit.Test, org.junit.Before, org.junit.After, and static org.junit.Assert.*. "
            "Do NOT use org.junit.jupiter.*, @BeforeEach, or @AfterEach."
        )
        junit_visibility = "Test methods should be public void testName()."
        junit_forbidden = "Do NOT import or use org.junit.jupiter.api.*."
    else:
        junit_imports = (
            "Use org.junit.jupiter.api.Test, Assertions, @BeforeEach, and @AfterEach. "
            "Do NOT use org.junit.Test or org.junit.Assert."
        )
        junit_visibility = "Test methods may be package-private void testName()."
        junit_forbidden = "Do NOT import org.junit.Test or static org.junit.Assert.*."

    if has_mockito:
        framework_rule = (
            f"Use ONLY {junit_label} and Mockito (no Spring test framework). "
            "Mock only real external dependencies when needed."
        )
        allowed_test_tools = "JDK/JUnit/Mockito"
        library_limit = f"Do NOT use any libraries beyond {junit_label} + Mockito."
    else:
        framework_rule = (
            f"Use ONLY {junit_label}. Mockito is NOT available in this project: "
            "do not import org.mockito, do not use @Mock/@InjectMocks/when/verify/MockitoAnnotations, "
            "and do not invent implementation classes."
        )
        allowed_test_tools = "JDK/JUnit"
        library_limit = f"Do NOT use any libraries beyond {junit_label}."

    return framework_rule, allowed_test_tools, library_limit, junit_imports, junit_visibility, junit_forbidden


from demo.semantic.models import TestSpec
from demo.prompter.rules import get_junit_prompt_rules, get_java_guidance

def ollama_write_prompt(model: str, spec: TestSpec) -> Dict:
    pkg = spec.package_name or "(default)"
    cls = spec.class_name
    sig = spec.signature or f"(entire class) {cls}"
    
    rules = get_junit_prompt_rules(spec.junit_version, spec.has_mockito)
    java_guidance_text = get_java_guidance(spec.java_version)
    
    col_strategies = []
    for col in spec.collaborator_strategy:
        prefix = "Mock" if col.strategy == "mock" else "Use REAL object"
        col_strategies.append(f"- {prefix} `{col.type_name}`: {col.details}")
    col_strat_text = "\n".join(col_strategies) if col_strategies else "No specific collaborator strategies."
    
    cf = spec.control_flow_characteristics
    cf_lines = []
    if cf.get("has_loops"):
        cf_lines.append("- Target has loops. Focus on testing loop bounds (0, 1, and multiple iterations).")
    if cf.get("has_conditionals"):
        cf_lines.append("- Target has conditional checks. Cover branches.")
    if cf.get("has_exceptions"):
        cf_lines.append("- Target can throw exceptions. Test validation failure and exception paths.")
    cf_text = "\n".join(cf_lines) if cf_lines else "- Test typical cases and bounds."
    
    p_delegation_text = ""
    if spec.private_method_delegation:
        p_delegation_text = f"Target delegates to private methods: {', '.join(spec.private_method_delegation)}. Test these private helper paths indirectly through the public API."
        
    class_mode_note = ""
    if spec.method_name is None:
        class_mode_note = (
            "If this is a framework wiring/config/filter class (e.g., Security filter, config), "
            "produce minimal unit tests focusing only on pure methods; do not reference servlet API types "
            "unless they appear in imports/snippet."
        )

    sys = (
        "You are a senior Java testing expert. "
        f"You write STRICT prompts for a code-generation model to produce JUnit {spec.junit_version} unit tests."
    )
    user = f"""
Return ONLY valid JSON with keys:
- "test_class_name": string (MUST start with "{GENERATED_PREFIX}" and end with "Test")
- "prompt": string

Constraints for the generated test class:
- Output MUST be ONLY Java code (no markdown, no explanations).
- Start directly with the Java package/import/class declarations. Never include prose before or after the class.
- Target Java language level: {spec.java_version}. {java_guidance_text}
- Target test framework: JUnit {spec.junit_version}. {rules['junit_imports']}
- {rules['junit_visibility']}
- {rules['junit_forbidden']}
- {rules['framework_rule']}
- Do NOT use @SpringBootTest, MockMvc, WebMvcTest, SecurityMockMvcRequestPostProcessors, SpringExtension, or any Spring test utilities.
- Do NOT use Spring test annotations (@SpringBootTest, etc.).
- {rules['library_limit']}
- Only use types that appear in the provided snippet/imports.
- Do not use Spring/Spring Security test utilities or types unless they already appear in the imports.
- Use ONLY types already imported by the target class. Do not introduce new application types (repositories/entities) unless they appear in the target source or imports.
- Do NOT reference services/repositories unless they are imported in the target source OR appear in the snippet.
- If the target is a Controller class and service types are not imported, do NOT create mocks for them; test only pure logic.
- Do not invent packages or class names (e.g., Entity vs Entities).
- If a dependency type is not in imports/snippet, avoid that test idea and write a simpler test.
- {rules['dependency_rule']}
- Do NOT mock the class or method under test. Do NOT write tests that only assert Mockito stubs.
- Every @Test method must execute at least one real production method from the target class or a concrete implementation of the target interface.
- If the target type is an interface or abstract type, test a real concrete implementation from the source context when available; otherwise do not use a mocked interface as the subject under test.
- Prefer simple real objects and constructors from the source code over Mockito. Use exact constructor signatures.
- Use these imports exactly; do not use javax.* if project uses jakarta.*; do not invent missing dependencies.
- Never use raw Object in Mockito stubbing; always return the exact declared return type.
- Prefer real values for enums rather than mocks.
- {class_mode_note}
- At least 3 @Test methods with concrete assertions.
- Name the test class exactly as you output in test_class_name.
- The Java code inside the "prompt" field MUST declare: public class <test_class_name> using the exact test_class_name value from this JSON.
- Only call methods and access fields that appear in the source snippet or related type sources. Do not invent getters/setters unless they exist in the provided source.
- Do NOT call private methods from tests; use only public or protected entry points visible in the snippet.
- For concrete classes, prefer constructing real instances (using constructors from the source) instead of mocking domain types.
- Access public fields directly when no getter exists (e.g. `item.key`, not `item.getKey()`).
- Place it in package: "{pkg}" if not default.

Domain Kind classification: {spec.domain_kind}
Control Flow characteristics:
{cf_text}
Private Method Delegation: {p_delegation_text or 'None.'}
Collaborator Strategies:
{col_strat_text}

Target:
- package: {pkg}
- class: {cls}
- target: {sig}

Package/imports context:
{spec.imports_context}

Source snippet:
{spec.snippet}

Related type sources:
{spec.related_sources}
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
    java_version: str = "17",
    junit_version: str = "5",
) -> str:
    junit_import_note = (
        "Keep or add org.junit.Test and static org.junit.Assert imports for every annotation/assertion used."
        if junit_version == "4"
        else "Keep or add JUnit 5 imports for every annotation/assertion used."
    )
    plain_junit_note = (
        f"rewrite the test as plain JUnit {junit_version} using real objects or static method calls"
    )
    sys = (
        "You are a senior Java testing expert. "
        f"You fix compilation errors in JUnit {junit_version} tests."
    )
    user = f"""
Target Java version: {java_version}. {java_version_guidance(java_version)}
Target test framework: JUnit {junit_version}.

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
If compiler errors say org.mockito does not exist, remove every Mockito import/annotation/call and {plain_junit_note}.
Only call methods and access fields that exist in the related type sources or class under test source. Replace invented methods (e.g. getKey()) with real constructors/fields/APIs from the source.
Start directly with package/import/class declarations. Do not include markdown fences, headings, bullet lists, or explanations.
{junit_import_note}
Do not mock the class or method under test. Each test must call real production code; replace mock-only assertions with real object calls when constructors/source context allow it.
Do not invent dependency types. If a referenced type (e.g., UserRepository) does not exist in the project, replace it with the closest matching real type from the repository list or remove that dependency and adjust the test accordingly.
""".strip()

    return sanitize_java_output(ollama_generate_text(model, user, sys))


def ollama_runtime_repair_test(
    model: str,
    stack_trace: str,
    file_content: str,
    failing_method: str = "",
    source_text: str = "",
    related_type_sources: str = "",
    java_version: str = "17",
    junit_version: str = "5",
) -> str:
    sys = (
        "You are a senior Java testing expert. "
        f"You fix runtime errors in JUnit {junit_version} tests."
    )
    user = f"""
Target Java version: {java_version}. {java_version_guidance(java_version)}
Target test framework: JUnit {junit_version}.

Failing test method:
{failing_method or "(not provided)"}

Stack trace:
{stack_trace}

File content:
{file_content}

Class under test source:
{source_text or "(not provided)"}

Related type sources / AST summaries:
{related_type_sources or "(not provided)"}

Instruction: Return corrected Java test file ONLY, keep class name and package, fix runtime errors, don’t introduce new libraries.
If the stack trace shows a NullPointerException from an array element or interface value, replace null/mocked domain values with real concrete instances from related type sources.
For interface parameters, use concrete implementations and constructors shown above. Do not mock domain/value interfaces when concrete implementations exist.
""".strip()

    return sanitize_java_output(ollama_generate_text(model, user, sys))
