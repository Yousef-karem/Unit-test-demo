from __future__ import annotations

import json
import requests
from typing import Dict, Tuple

from demo.config import OLLAMA_URL, GENERATED_PREFIX, DEFAULT_OLLAMA_REPAIR_TIMEOUT
from demo.coverage.java_version import java_version_guidance
from demo.targets import extract_imports_context
from demo.utils import sanitize_java_output

# Timeout for Ollama requests (seconds). Repair prompts can be large,
# so a generous timeout is needed for smaller local models.
OLLAMA_TIMEOUT = 600
OLLAMA_REPAIR_TIMEOUT = DEFAULT_OLLAMA_REPAIR_TIMEOUT


class OllamaRepairTimeout(RuntimeError):
    """Raised when an Ollama repair request exceeds its timeout."""


def _clip_repair_context(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    keep_head = max_chars // 2
    keep_tail = max_chars - keep_head
    return (
        text[:keep_head].rstrip()
        + "\n\n// ... repair context truncated ...\n\n"
        + text[-keep_tail:].lstrip()
    )


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


def ollama_generate_text(
    model: str,
    prompt: str,
    system: str | None = None,
    *,
    timeout: int | None = None,
) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 2200},
    }
    if system:
        payload["system"] = system
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=timeout or OLLAMA_TIMEOUT)
    except requests.Timeout as exc:
        raise OllamaRepairTimeout(
            f"Ollama request timed out after {timeout or OLLAMA_TIMEOUT}s"
        ) from exc
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


def ollama_write_prompt(model: str, target: Dict, project_types_text: str, java_version: str) -> Dict:
    pkg = target["package"] or "(default)"
    cls = target["class_name"]
    sig = target["signature"] or f"(entire class) {cls}"
    imports_context = (
        target.get("package_line") or "Imports unavailable from AST analyzer; use allowlist only."
        if target.get("analysis_source") == "ast"
        else extract_imports_context(target)
    )
    test_libraries = target.get("test_libraries") or {}
    junit_version = str(test_libraries.get("junit", "5"))
    has_mockito = bool(test_libraries.get("mockito", True))
    (
        framework_rule,
        allowed_test_tools,
        library_limit,
        junit_imports,
        junit_visibility,
        junit_forbidden,
    ) = _junit_prompt_rules(junit_version, has_mockito)
    dependency_rule = (
        "Create the class under test manually; mock only its dependencies."
        if has_mockito
        else "Create or call the class under test directly using real constructors/static methods and simple values."
    )

    class_mode_note = ""
    if target.get("method_name") is None:
        class_mode_note = (
            "If this is a framework wiring/config/filter class (e.g., Security filter, config), "
            "produce minimal unit tests focusing only on pure methods; do not reference servlet API types "
            "unless they appear in imports/snippet."
        )

    sys = (
        "You are a senior Java testing expert. "
        f"You write STRICT prompts for a code-generation model to produce JUnit {junit_version} unit tests."
    )
    user = f"""
Return ONLY valid JSON with keys:
- "test_class_name": string (MUST start with "{GENERATED_PREFIX}" and end with "Test")
- "prompt": string

Constraints for the generated test class:
- Output MUST be ONLY Java code (no markdown, no explanations).
- Start directly with the Java package/import/class declarations. Never include prose before or after the class.
- Target Java language level: {java_version}. {java_version_guidance(java_version)}
- Target test framework: JUnit {junit_version}. {junit_imports}
- {junit_visibility}
- {junit_forbidden}
- {framework_rule}
- Do NOT use @SpringBootTest, MockMvc, WebMvcTest, SecurityMockMvcRequestPostProcessors, SpringExtension, or any Spring test utilities.
- Do NOT use Spring test annotations (@SpringBootTest, etc.).
- {library_limit}
- Only use types that appear in the provided snippet/imports (plus {allowed_test_tools}).
- Do not use Spring/Spring Security test utilities or types unless they already appear in the imports.
- Use ONLY types already imported by the target class, plus {allowed_test_tools}. Do not introduce new application types (repositories/entities) unless they appear in the target source or imports.
- Do NOT reference services/repositories unless they are imported in the target source OR appear in the snippet.
- If the target is a Controller class and service types are not imported, do NOT create mocks for them; test only pure logic.
- You may ONLY reference application types whose SIMPLE class name appears in the allowlist below.
- Do not invent packages or class names (e.g., Entity vs Entities).
- If a dependency type is not in imports/snippet/allowlist, avoid that test idea and write a simpler test.
- {dependency_rule}
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
- Do NOT call private methods from tests; use only public or protected entry points visible in the snippet.
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
    compiler_errors = _clip_repair_context(compiler_errors, 5000)
    file_content = _clip_repair_context(file_content, 14000)
    source_text = _clip_repair_context(source_text, 16000)
    related_type_sources = _clip_repair_context(related_type_sources, 18000)
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
If compiler errors say a field is missing on an interface/base type but related sources show one concrete implementation with that public field, cast before reading it (for example `((MeuItem) result).chave`).
If compiler errors say an invented getter method is missing but the source shows a public field with the same property name, read the field directly.
If compiler errors say a class symbol is missing, either add the correct import from the related sources/package lines or remove that test idea; do not invent a class.
Fix the existing test minimally. Preserve tests that already compile and keep every assertion tied to real production behavior.
Start directly with package/import/class declarations. Do not include markdown fences, headings, bullet lists, or explanations.
{junit_import_note}
Do not mock the class or method under test. Each test must call real production code; replace mock-only assertions with real object calls when constructors/source context allow it.
Do not invent dependency types. If a referenced type (e.g., UserRepository) does not exist in the project, replace it with the closest matching real type from the repository list or remove that dependency and adjust the test accordingly.
""".strip()

    return sanitize_java_output(
        ollama_generate_text(model, user, sys, timeout=OLLAMA_REPAIR_TIMEOUT)
    )


def _assertion_error_repair_guidance(stack_trace: str, source_text: str) -> str:
    if "AssertionError" not in stack_trace:
        return ""
    lines = [
        "ASSERTION FAILURE GUIDANCE:",
        "- Fix incorrect expected values; match assertions to observable behavior in the source code.",
        "- Do not assert on exact stdout line counts when the method prints once per match in a loop.",
        "- Prefer partial output checks (contains) over brittle exact string equality when appropriate.",
    ]
    if "System.out" in source_text or "println" in source_text:
        lines.extend(
            [
                "- For stdout side effects: capture with ByteArrayOutputStream and System.setOut in @Before; "
                "restore in @After. Assert out.toString().contains(\"expected fragment\").",
                "- NEVER use System.out.toString() to assert printed output.",
            ]
        )
    if "static" in source_text and "void" in source_text:
        lines.append(
            "- For static void methods: call ClassName.methodName(...) directly; "
            "assert on captured IO or side effects, not return values."
        )
    return "\n".join(lines)


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
    assertion_guidance = _assertion_error_repair_guidance(stack_trace, source_text)
    stack_trace = _clip_repair_context(stack_trace, 7000)
    file_content = _clip_repair_context(file_content, 14000)
    source_text = _clip_repair_context(source_text, 16000)
    related_type_sources = _clip_repair_context(related_type_sources, 18000)
    user = f"""
Target Java version: {java_version}. {java_version_guidance(java_version)}
Target test framework: JUnit {junit_version}.

Failing test method:
{failing_method or "(not provided)"}

Stack trace:
{stack_trace}

{assertion_guidance}

File content:
{file_content}

Class under test source:
{source_text or "(not provided)"}

Related type sources / AST summaries:
{related_type_sources or "(not provided)"}

Instruction: Return corrected Java test file ONLY, keep class name and package, fix runtime errors, don’t introduce new libraries.
If the stack trace shows a NullPointerException from an array element or interface value, replace null/mocked domain values with real concrete instances from related type sources.
For interface parameters, use concrete implementations and constructors shown above. Do not mock domain/value interfaces when concrete implementations exist.
If JUnit reports "Expected exception" but the source code does not throw that exception, remove the expected-exception annotation and assert normal behavior instead.
If a string-search algorithm crashes on an empty pattern (m == 0 / P == ""), change that test to a valid non-empty pattern, preferably a no-match pattern when the test intended no output.
If a test captures System.out, save `PrintStream originalOut = System.out` before replacing it and restore with `System.setOut(originalOut)`.
""".strip()

    return sanitize_java_output(
        ollama_generate_text(model, user, sys, timeout=OLLAMA_REPAIR_TIMEOUT)
    )
