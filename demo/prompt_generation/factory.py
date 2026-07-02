from __future__ import annotations

from typing import Callable, Dict

from demo.prompt_generation.llm import LlmPromptGenerator
from demo.prompt_generation.class_slice_llm import ClassSliceLlmPromptGenerator
from demo.prompt_generation.protocol import PromptGenerator
from demo.prompt_generation.static import StaticPromptGenerator

_REGISTRY: Dict[str, Callable[..., PromptGenerator]] = {
    "llm": lambda args: LlmPromptGenerator(gpt_model=args.gpt_model),
    "class-slice-llm": lambda args: ClassSliceLlmPromptGenerator(gpt_model=args.gpt_model),
    "static": lambda args: StaticPromptGenerator(),
}


def create_prompt_generator(args) -> PromptGenerator:
    mode = getattr(args, "prompt_mode", "llm")
    if (
        mode == "llm"
        and getattr(args, "mode", "method") == "class"
        and getattr(args, "class_prompt_slices", 1) > 1
    ):
        return ClassSliceLlmPromptGenerator(gpt_model=args.gpt_model)
    try:
        return _REGISTRY[mode](args)
    except KeyError as exc:
        raise ValueError(f"Unknown prompt mode: {mode}") from exc
