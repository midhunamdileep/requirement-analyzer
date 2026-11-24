import contextvars
import json
import os
import time
from datetime import datetime
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError


DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_REPAIRS = 2

Model = TypeVar("Model", bound=BaseModel)

# Event log. The orchestrator sets these per agent call; with nothing set, events are just printed (CLI).
_sink: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar("log_sink", default=None)
_collector: contextvars.ContextVar[list | None] = contextvars.ContextVar("log_collector", default=None)
_agent: contextvars.ContextVar[str] = contextvars.ContextVar("log_agent", default="")


def start_logging(agent: str, sink: Callable[[str], None] | None = None) -> list[dict]:
    """Begin collecting events for one agent call; returns the list the events are appended to."""
    events: list[dict] = []
    _agent.set(agent)
    _collector.set(events)
    _sink.set(sink)
    return events


def _describe(kind: str, data: dict) -> str:
    if kind == "llm_call":
        return f"calling Claude (attempt {data['attempt']}, prompt {data['prompt_chars']:,} chars)"
    if kind == "llm_done":
        return f"Claude replied in {data['seconds']:.1f}s ({data['tokens_in']:,} tokens in, {data['tokens_out']:,} out)"
    if kind == "schema_retry":
        return "tool input did not match the schema; retrying once"
    if kind == "verify":
        n = len(data["problems"])
        return "verification passed" if not n else f"verification found {n} problem(s)"
    if kind == "repair_round":
        return f"repair round {data['round']}: sending {data['problems']} problem(s) back to Claude"
    return kind + (" " + json.dumps(data, default=str) if data else "")


def log_event(kind: str, **data) -> None:
    agent = _agent.get()
    event = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": agent, "kind": kind, **data}
    collector = _collector.get()
    if collector is not None:
        collector.append(event)
    line = (f"    [{agent}] " if agent else "") + _describe(kind, data)
    (_sink.get() or print)(line)


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
        log_event("llm_call", attempt=attempt + 1, prompt_chars=len(prompt) + len(error_note))
        started = time.monotonic()
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
        log_event("llm_done", seconds=time.monotonic() - started,
                  tokens_in=usage.input_tokens if usage else 0, tokens_out=usage.output_tokens if usage else 0)

        block = next((b for b in response.content if b.type == "tool_use" and b.name == tool_name), None)
        if block is None:
            raise SystemExit(f"Claude did not call {tool_name}.")
        try:
            return parse_tool_input(model_cls, block.input)
        except ValidationError as error:
            if attempt == 1:
                raise
            log_event("schema_retry")
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
    log_event("verify", problems=problems)

    for attempt in range(1, MAX_REPAIRS + 1):
        if not problems:
            break
        log_event("repair_round", round=attempt, problems=len(problems))
        repair = (
            f"{prompt}\n\nYour previous draft:\n{doc.model_dump_json(indent=2)}\n\n"
            "Verification found these problems:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n\nResubmit the complete corrected document."
        )
        doc = call_tool(client, system, tool_name, description, model_cls, repair)
        problems = verify(doc)
        log_event("verify", problems=problems)

    return doc, problems
