from __future__ import annotations

from typing import Dict

from demo.coverage.java_version import java_version_guidance


def get_junit_prompt_rules(junit_version: str, has_mockito: bool) -> Dict[str, str]:
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
        dependency_rule = "Create the class under test manually; mock only its dependencies."
    else:
        framework_rule = (
            f"Use ONLY {junit_label}. Mockito is NOT available in this project: "
            "do not import org.mockito, do not use @Mock/@InjectMocks/when/verify/MockitoAnnotations, "
            "and do not invent implementation classes."
        )
        allowed_test_tools = "JDK/JUnit"
        library_limit = f"Do NOT use any libraries beyond {junit_label}."
        dependency_rule = (
            "Create or call the class under test directly using real constructors/static methods "
            "and simple values."
        )

    return {
        "junit_label": junit_label,
        "junit_imports": junit_imports,
        "junit_visibility": junit_visibility,
        "junit_forbidden": junit_forbidden,
        "framework_rule": framework_rule,
        "allowed_test_tools": allowed_test_tools,
        "library_limit": library_limit,
        "dependency_rule": dependency_rule,
    }


def get_java_guidance(java_version: str) -> str:
    return java_version_guidance(java_version)
