"""Request configuration, audit records and source-output validation for the SDK."""

import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from openai import Omit, OpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ValidationError

from .config import Endpoint, LLMConfig

T = TypeVar("T", bound=BaseModel)
_record_lock = threading.Lock()


class NarrationError(ValueError):
    """A provider completed a request but did not return usable narration."""


class TruncatedResponse(NarrationError):
    pass


def request_headers(config: Endpoint) -> dict:
    # Explicitly isolate providers from global SDK credentials/custom headers.
    headers = {
        line.split(":", 1)[0].strip(): Omit()
        for line in os.getenv("OPENAI_CUSTOM_HEADERS", "").splitlines()
        if ":" in line
    }
    key = config.api_key()
    headers.update(
        {
            "Authorization": f"Bearer {key}" if key else Omit(),
            "OpenAI-Organization": Omit(),
            "OpenAI-Project": Omit(),
        }
    )
    return headers


def client_options(config: Endpoint) -> dict:
    """Constructor options for an ordinary OpenAI client, never a client wrapper."""
    return {
        "api_key": config.api_key() or "not-needed",
        "base_url": config.base_url,
        "organization": "",
        "project": "",
        "default_headers": request_headers(config),
        "timeout": config.timeout_seconds,
        "max_retries": config.retries,
    }


def record_request(
    work: Path,
    config: Endpoint,
    purpose: str,
    usage: dict | None = None,
    service_tier: str | None = None,
):
    record = {
        "time": datetime.now(UTC).isoformat(),
        "purpose": purpose,
        "model": config.model,
        "usage": usage or {},
    }
    if isinstance(config, LLMConfig):
        requested = config.service_tier or config.extra_body.get("service_tier")
        if requested is not None:
            record["requested_service_tier"] = requested
        if service_tier is not None:
            record["service_tier"] = service_tier
    work.mkdir(parents=True, exist_ok=True)
    with _record_lock, (work / "requests.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def generate_json(
    client: OpenAI,
    config: LLMConfig,
    work: Path,
    messages: list[dict],
    schema: type[T],
    purpose: str,
    validate: Callable[[T], None] | None = None,
) -> T:
    """Book-specific schema/coverage repair; request retries belong to the SDK."""
    config.require_model()
    instructions = "\nReturn JSON only, conforming to this schema:\n" + json.dumps(
        schema.model_json_schema()
    )
    conversation = [dict(message) for message in messages]
    conversation[0] = {**conversation[0], "content": conversation[0]["content"] + instructions}
    # Two bounded schema/coverage repair attempts, separate from network retries.
    for attempt in range(3):
        options = {
            config.token_parameter: config.max_output_tokens,
            "extra_body": config.extra_body,
            "extra_headers": request_headers(config),
        }
        if config.json_mode == "json_object":
            options["response_format"] = {"type": "json_object"}
        if config.temperature is not None:
            options["temperature"] = config.temperature
        if config.service_tier is not None:
            options["service_tier"] = config.service_tier
        try:
            response = client.chat.completions.create(
                model=config.model, messages=conversation, **options
            )
        except (ValueError, UnicodeDecodeError):
            raise NarrationError("Chat endpoint did not return a valid completion object") from None
        if not isinstance(response, ChatCompletion):
            raise NarrationError("Chat endpoint did not return a completion object")
        record_request(
            work,
            config,
            purpose,
            response.usage.model_dump(exclude_none=True) if response.usage else None,
            response.service_tier,
        )
        try:
            choice = response.choices[0]
            message = choice.message
            content = message.content
        except (AttributeError, IndexError, TypeError):
            raise NarrationError("Chat endpoint returned no usable completion choice") from None
        if choice.finish_reason == "length":
            raise TruncatedResponse("Model output reached the token limit")
        if message.refusal or choice.finish_reason not in {None, "stop"}:
            raise NarrationError(
                "Model refused or did not complete the narration; no audio was substituted"
            )
        if not isinstance(content, str) or not content.strip():
            raise NarrationError("Chat endpoint returned an empty or non-text completion")
        cleaned = content.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            result = schema.model_validate_json(cleaned)
            if validate:
                validate(result)
            return result
        except (ValidationError, ValueError) as exc:
            if attempt == 2:
                raise NarrationError(
                    f"Invalid {schema.__name__} after three attempts: {exc}"
                ) from exc
            conversation.extend(
                [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "Repair the JSON and coverage errors below. Return the complete corrected object, without dropping content:\n"
                        + str(exc),
                    },
                ]
            )
    raise NarrationError("Model response validation failed")
