from __future__ import annotations

from typing import Dict

from demo.class_mode.naming import apply_slice_test_class_name
from demo.class_mode.slicing import slice_focus_instruction
from demo.config import GENERATED_PREFIX
from demo.coverage.java_version import java_version_guidance
from demo.llm.prompt_writer import _junit_prompt_rules, ollama_generate_json
from demo.targets import extract_imports_context


def ollama_write_class_slice_prompt(
    model: str,
    target: Dict,
    project_types_text: str,
    java_version: str,
) -> Dict:
    pkg = target["package"] or "(default)"
    cls = target["class_name"]
    slice_kind = target.get("class_prompt_slice") or "behavior"
    sig = target.get("signature") or f"(class slice) {cls}"
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

    suggested_name = apply_slice_test_class_name(target, f"{GENERATED_PREFIX}{cls}Test")
    focus_instruction = slice_focus_instruction(slice_kind)
    allowed_methods = target.get("class_prompt_slice_allowed_methods") or []
    allowed_methods_block = ""
    if allowed_methods:
        allowed_methods_block = (
            "Methods in scope for this slice (each @Test should target a distinct one where possible):\n"
            + ", ".join(allowed_methods)
        )

    class_snippet_guidance = (
        "Class-mode slice snippet is a self-contained Javadoc-aware static analysis summary (not raw Java source). "
        "It includes full class, field, constructor, and in-scope method documentation for this slice. "
        "When Javadoc exists for a member, prefer doc:, description:, @param, @return, @throws, links, and @deprecated. "
        "When Javadoc is absent, use the AST-derived metadata shown (controlFlow, metrics, testabilityHints, calls, etc.). "
        "Do not invent methods or types beyond the snippet, imports, and allowlist."
    )

    sys = (
        "You are a senior Java testing expert. "
        f"You write STRICT prompts for a code-generation model to produce JUnit {junit_version} unit tests."
    )
    user = f"""
Return ONLY valid JSON with keys:
- "test_class_name": string (MUST start with "{GENERATED_PREFIX}" and end with "Test")
- "prompt": string

Suggested test class name for this slice: {suggested_name}

Slice focus ({slice_kind}):
- {focus_instruction}

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
- Use ONLY types already imported by the target class, plus {allowed_test_tools}. Do not introduce new application types unless they appear in the target source or imports.
- Do NOT reference services/repositories unless they are imported in the target source OR appear in the snippet.
- If the target is a Controller class and service types are not imported, do NOT create mocks for them; test only pure logic.
- You may ONLY reference application types whose SIMPLE class name appears in the allowlist below.
- Do not invent packages or class names (e.g., Entity vs Entities).
- If a dependency type is not in imports/snippet/allowlist, avoid that test idea and write a simpler test.
- {dependency_rule}
- Do NOT mock the class or method under test. Do NOT write tests that only assert Mockito stubs.
- Every @Test method must execute at least one real production method from the target class.
- Prefer simple real objects and constructors from the source code over Mockito. Use exact constructor signatures.
- Use these imports exactly; do not use javax.* if project uses jakarta.*; do not invent missing dependencies.
- Never use raw Object in Mockito stubbing; always return the exact declared return type.
- Prefer real values for enums (e.g., Role.ADMIN) rather than mocks.
- When dealing with Spring Security authorities, use Collection<? extends GrantedAuthority> (not List<GrantedAuthority>) unless the target method explicitly returns List.
- Avoid overly specific generics; use Collection where appropriate.
- If this is a framework wiring/config/filter class, produce minimal unit tests focusing only on pure methods.
- At least 3 @Test methods with concrete assertions.
- Name the test class exactly as you output in test_class_name.
- The Java code inside the "prompt" field MUST declare: public class <test_class_name> using the exact test_class_name value from this JSON.
- Only call methods and access fields that appear in the source snippet, related type sources, or allowlist.
- Do NOT call private methods from tests; use only public or protected entry points visible in the snippet.
- For concrete classes, prefer constructing real instances (using constructors from the source) instead of mocking domain types.
- When a method parameter is an interface and a concrete implementation exists in the allowlist, pass real instances — never mock interface methods with Mockito.
- Access public fields directly when no getter exists.
- Place it in package: "{pkg}" if not default.

Target:
- package: {pkg}
- class: {cls}
- target: {sig}
- slice: {slice_kind}

{allowed_methods_block}

Package/imports context:
{imports_context}

{class_snippet_guidance}

Source snippet:
{target["snippet"]}

Allowlist (project types):
{project_types_text}
""".strip()

    return ollama_generate_json(model, user, sys)
