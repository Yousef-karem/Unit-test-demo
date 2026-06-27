from __future__ import annotations

from demo.prompter.builder import build_static_prompt
from demo.prompt_generation.helpers import resolve_related_sources
from demo.prompt_generation.models import PromptGenerationContext, PromptResult
from demo.semantic.extractor import SemanticExtractor


class StaticPromptGenerator:
    def generate(self, context: PromptGenerationContext) -> PromptResult:
        if context.ast_analysis is None:
            raise RuntimeError("Static prompt mode requires AST analysis (--analysis-mode ast)")

        related = resolve_related_sources(
            context.project_root,
            context.ast_analysis,
            context.target,
        )
        spec = SemanticExtractor(context.ast_analysis).extract_spec(
            context.target,
            java_version=context.project_java_version,
            junit_version=context.junit_version,
            has_mockito=context.has_mockito,
            related_sources=related,
        )
        prompt = build_static_prompt(spec)
        return PromptResult(
            test_class_name=spec.test_class_name,
            prompt=prompt,
            metadata={"mode": "static", "domain_kind": spec.domain_kind},
        )
