from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class PromptGenerationContext:
    target: Dict[str, Any]
    project_root: Path
    ast_analysis: Optional[Dict[str, Any]]
    project_java_version: str
    junit_version: str
    has_mockito: bool
    project_types_text: str
    target_mode: str  # existing --mode method|class


@dataclass(frozen=True)
class PromptResult:
    test_class_name: str
    prompt: str
    metadata: Dict[str, Any] = field(default_factory=dict)
