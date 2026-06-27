from __future__ import annotations

from typing import Callable, Dict

from demo.prompt_generation.llm import LlmPromptGenerator
from demo.prompt_generation.protocol import PromptGenerator
from demo.prompt_generation.static import StaticPromptGenerator

_REGISTRY: Dict[str, Callable[..., PromptGenerator]] = {
    "llm": lambda args: LlmPromptGenerator(gpt_model=args.gpt_model),
    "static": lambda args: StaticPromptGenerator(),
}


def create_prompt_generator(args) -> PromptGenerator:
    mode = getattr(args, "prompt_mode", "llm")
    try:
        return _REGISTRY[mode](args)
    except KeyError as exc:
        raise ValueError(f"Unknown prompt mode: {mode}") from exc
