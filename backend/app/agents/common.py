import json
import os
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError


DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_REPAIRS = 2

Model = TypeVar("Model", bound=BaseModel)


def get_client():
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to backend/.env or use --dry-run.")
    from anthropic import Anthropic

    return Anthropic()


def cite(citations: list[str]) -> str:
    return " ".join(f"[{c}]" for c in citations)


def with_feedback(prompt: str, feedback: list[str] | None) -> str:
    """Append reviewer feedback from the orchestrator to a prompt."""
    if not feedback:
        return prompt
    bullets = "\n".join(f"- {f}" for f in feedback)
    return f"{prompt}\n\nReviewer feedback to address in this revision:\n{bullets}"


def parse_tool_input(model_cls: type[Model], data: dict) -> Model:
    """Validate a tool call's input; Claude sometimes sends nested objects as JSON strings, so decode those."""
    try:
        return model_cls.model_validate(data)
    except ValidationError:
        decoded = {}
        for key, value in data.items():
            if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            decoded[key] = value
        return model_cls.model_validate(decoded)


def call_tool(client, system: str, tool_name: str, description: str, model_cls: type[Model], prompt: str) -> Model:
    """Ask Claude to answer by calling one tool whose input schema is a Pydantic model."""
    error_note = ""
    for attempt in range(2):
        response = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
            max_tokens=16000,
            system=system,
            tools=[{
                "name": tool_name,
                "description": description,
                "input_schema": model_cls.model_json_schema(),
            }],
            tool_choice={"type": "auto"},  # this model rejects forced tool use; the prompt asks for the call
            messages=[{"role": "user", "content": prompt + error_note}],
        )
        usage = getattr(response, "usage", None)
        if usage:
            print(f"Tokens: {usage.input_tokens} in, {usage.output_tokens} out")

        block = next((b for b in response.content if b.type == "tool_use" and b.name == tool_name), None)
        if block is None:
            raise SystemExit(f"Claude did not call {tool_name}.")
        try:
            return parse_tool_input(model_cls, block.input)
        except ValidationError as error:
            if attempt == 1:
                raise
            print("Tool input did not match the schema; retrying once.")
            error_note = (
                f"\n\nYour previous {tool_name} call did not match the schema:\n{error}\n"
                "Pass nested objects and arrays as JSON values, never as quoted strings."
            )


def generate_with_repair(
    client,
    system: str,
    tool_name: str,
    description: str,
    model_cls: type[Model],
    prompt: str,
    verify: Callable[[Model], list[str]],
) -> tuple[Model, list[str]]:
    """Draft, verify in code, and send problems back to Claude for up to MAX_REPAIRS rounds."""
    doc = call_tool(client, system, tool_name, description, model_cls, prompt)
    problems = verify(doc)

    for attempt in range(1, MAX_REPAIRS + 1):
        if not problems:
            break
        print(f"Repair round {attempt}: {len(problems)} problem(s)")
        repair = (
            f"{prompt}\n\nYour previous draft:\n{doc.model_dump_json(indent=2)}\n\n"
            "Verification found these problems:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n\nResubmit the complete corrected document."
        )
        doc = call_tool(client, system, tool_name, description, model_cls, repair)
        problems = verify(doc)

    return doc, problems
