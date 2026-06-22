from __future__ import annotations

SYSTEM_INSTRUCTION = """
You are a senior Java developer and software testing expert.
You write high-quality, complete, and compileable unit tests using JUnit {junit_version}.
""".strip()

USER_PROMPT_TEMPLATE = """
Generate a COMPLETE runnable JUnit {junit_version} test class named exactly `{test_class_name}`.
Target Java version: {java_version}. {java_version_guidance}

STRICT CONSTRAINTS & RULES:
- Output MUST be ONLY Java code (no markdown code blocks, no explanations, no HTML).
- Start directly with the package declaration, imports, and class definition.
- Place it in package: "{package_name}" (declare this package at the top of the file).
- Do NOT use @SpringBootTest, MockMvc, WebMvcTest, SecurityMockMvcRequestPostProcessors, SpringExtension, or any Spring test utilities.
- Do NOT use Spring test annotations.
- {library_limit}
- Only use types that appear in the target class snippet, related type sources, or allowlist. Do not invent classes/packages.
- {dependency_rule}
- {framework_rule}
- {junit_imports}
- {junit_visibility}
- {junit_forbidden}
- Do NOT mock the class under test or methods under test. Every test must execute the real production method.
- Prefer real value objects and constructors over Mockito mocks where possible.
- At least 3 @Test methods with concrete assertions.
- Access public fields directly when no getter exists (e.g. `item.key`, not `item.getKey()`).
- Do NOT call private methods from tests; test them indirectly by calling public/protected entry points.

TARGET UNDER TEST:
- Package: {package_name}
- Class: {class_name}
- Domain Role: {domain_kind}
- Target: {signature}

SOURCE CODE SNIPPET:
{snippet}

{constructor_guidance}

{branch_guidance}

{private_method_guidance}

{collaborator_guidance}

{related_sources_guidance}
""".strip()
