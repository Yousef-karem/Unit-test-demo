from __future__ import annotations

import json
from typing import Dict

from openai import OpenAI

from demo.config import GENERATED_PREFIX
from demo.targets import extract_imports_context
from demo.utils import sanitize_java_output


def _extract_first_json_object(text: str) -> Dict:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)

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
                return json.loads(text[start:i+1])
    raise ValueError("Unterminated JSON object in model output.")


def gpt_write_prompt(client: OpenAI, gpt_model: str, target: Dict, project_types_text: str) -> Dict:
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
- Use these imports exactly; do not use javax.* if project uses jakarta.*; do not invent missing dependencies.
- Never use raw Object in Mockito stubbing; always return the exact declared return type.
- Prefer real values for enums (e.g., Role.ADMIN) rather than mocks.
- When dealing with Spring Security authorities, use Collection<? extends GrantedAuthority> (not List<GrantedAuthority>) unless the target method explicitly returns List.
- Avoid overly specific generics; use Collection where appropriate.
- {class_mode_note}
- At least 3 @Test methods with concrete assertions.
- Name the test class exactly as you output in test_class_name.
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

    resp = client.responses.create(
        model=gpt_model,
        input=[
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
    )
    out_text = resp.output_text.strip()
    return _extract_first_json_object(out_text)


def gpt_repair_test(
    client: OpenAI,
    gpt_model: str,
    compiler_errors: str,
    file_content: str,
    source_text: str,
    package_imports: str,
    constructor_info: str,
    repository_types: str,
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

Instruction: Return corrected Java test file ONLY, keep class name and package, fix typing/import issues, don’t introduce new libraries.
Do not invent dependency types. If a referenced type (e.g., UserRepository) does not exist in the project, replace it with the closest matching real type from the repository list or remove that dependency and adjust the test accordingly.
""".strip()

    resp = client.responses.create(
        model=gpt_model,
        input=[
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
    )
    return sanitize_java_output(resp.output_text.strip())


def gpt_runtime_repair_test(client: OpenAI, gpt_model: str, stack_trace: str, file_content: str) -> str:
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

    resp = client.responses.create(
        model=gpt_model,
        input=[
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
    )
    return sanitize_java_output(resp.output_text.strip())
