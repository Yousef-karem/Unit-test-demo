from __future__ import annotations

from demo.llm.class_slice_prompt_writer import ollama_write_class_slice_prompt
from demo.prompt_generation.helpers import (
    append_related_sources,
    build_direct_generation_prompt,
    looks_like_java_test_file,
    resolve_related_sources,
)
from demo.prompt_generation.models import PromptGenerationContext, PromptResult


class ClassSliceLlmPromptGenerator:
    def __init__(self, gpt_model: str) -> None:
        self._gpt_model = gpt_model

    def generate(self, context: PromptGenerationContext) -> PromptResult:
        g = ollama_write_class_slice_prompt(
            self._gpt_model,
            context.target,
            context.project_types_text,
            java_version=context.project_java_version,
        )
        test_class = g.get("test_class_name", "")
        prompt = g.get("prompt", "")

        if looks_like_java_test_file(prompt):
            prompt = build_direct_generation_prompt(
                target=context.target,
                test_class=test_class,
                project_types_text=context.project_types_text,
                java_version=context.project_java_version,
                junit_version=context.junit_version,
                has_mockito=context.has_mockito,
            )

        related_sources = resolve_related_sources(
            context.project_root,
            context.ast_analysis,
            context.target,
        )
        prompt = append_related_sources(prompt, related_sources, context.target)

        return PromptResult(
            test_class_name=test_class,
            prompt=prompt,
            metadata={
                "mode": "class-slice-llm",
                "class_prompt_slice": context.target.get("class_prompt_slice"),
                "class_prompt_slice_index": context.target.get("class_prompt_slice_index"),
                "class_prompt_slice_total": context.target.get("class_prompt_slice_total"),
            },
        )
