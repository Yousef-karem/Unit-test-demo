from __future__ import annotations

from typing import Protocol

from demo.prompt_generation.models import PromptGenerationContext, PromptResult


class PromptGenerator(Protocol):
    def generate(self, context: PromptGenerationContext) -> PromptResult:
        ...
