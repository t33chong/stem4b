"""Request configuration, audit records and source-output validation for the SDK."""

import json
import logging
import math
import os
import random
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep, time
from typing import TypeVar

from openai import APIConnectionError, APIStatusError, Omit, OpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ValidationError

from .config import Endpoint, LLMConfig

T = TypeVar("T", bound=BaseModel)
_record_lock = threading.Lock()
log = logging.getLogger(__name__)


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
    request_service_tier: str | None = None,
):
    record = {
        "time": datetime.now(UTC).isoformat(),
        "purpose": purpose,
        "model": config.model,
        "usage": usage or {},
    }
    if isinstance(config, LLMConfig):
        configured = config.service_tier or config.extra_body.get("service_tier")
        requested = request_service_tier if request_service_tier is not None else configured
        if requested is not None:
            record["requested_service_tier"] = requested
        if configured != requested:
            record["configured_service_tier"] = configured
        if service_tier is not None:
            record["service_tier"] = service_tier
    work.mkdir(parents=True, exist_ok=True)
    with _record_lock, (work / "requests.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except ValueError:
            if name != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(value)
                seconds = (date if date.tzinfo else date.replace(tzinfo=UTC)).timestamp() - time()
                seconds = max(0, seconds)
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def _flex_retryable(error: APIStatusError) -> bool:
    # Quota/billing problems cannot be fixed by waiting or paying for another tier.
    if (
        str(error.code)
        in {
            "insufficient_quota",
            "billing_hard_limit_reached",
            "billing_not_active",
            "quota_exceeded",
            "usage_limit_reached",
        }
        or error.response.headers.get("x-should-retry", "").lower() == "false"
    ):
        return False
    return error.status_code in {408, 409, 429} or error.status_code >= 500


def _flex_completion(
    client: OpenAI, config: LLMConfig, messages: list[dict], options: dict, purpose: str
) -> tuple[ChatCompletion, str]:
    """Budget actual Flex attempts; keep the SDK as the only provider client.

    with_options shares the original transport. Do not close or mutate that view:
    other chapter workers may be using the same ordinary OpenAI client.
    """
    flex_client = client.with_options(max_retries=0)
    delay = config.flex_initial_backoff_seconds
    for attempt in range(1, config.flex_max_attempts + 1):
        try:
            return flex_client.chat.completions.create(
                model=config.model, messages=messages, **options
            ), "flex"
        except (APIStatusError, APIConnectionError) as exc:
            if isinstance(exc, APIStatusError) and not _flex_retryable(exc):
                raise
            exhausted = attempt == config.flex_max_attempts
            fallback = (
                exhausted
                and config.flex_fallback_to_standard
                and isinstance(exc, APIStatusError)
                and exc.status_code in {429, 503}
            )
            if exhausted and not fallback:
                raise
            wait = delay * random.uniform(0.75, 1.0)
            if isinstance(exc, APIStatusError):
                hint = _retry_after_seconds(exc.response.headers)
                if hint is not None and hint > config.flex_max_backoff_seconds:
                    log.warning(
                        "Flex %s requested Retry-After %.3fs, beyond flex_max_backoff_seconds; "
                        "stopping without another request or tier fallback.",
                        purpose,
                        hint,
                    )
                    raise
                wait = max(wait, hint or 0)
                detail = (
                    f"HTTP {exc.status_code} (request ID: {exc.request_id or 'unavailable'}).\n"
                    f"Provider response body:\n{exc.response.text or '<empty body>'}"
                )
            else:
                detail = f"{type(exc).__name__}: {exc}"
                if exc.__cause__ is not None:
                    detail += f"; {type(exc.__cause__).__name__}: {exc.__cause__}"
            log.warning(
                "Flex %s attempt %s/%s failed: %s",
                purpose,
                attempt,
                config.flex_max_attempts,
                detail,
            )
            if fallback:
                tier = config.flex_fallback_service_tier
                log.warning(
                    "Flex exhausted for %s; standard fallback is explicitly enabled. "
                    "Retrying with service_tier=%s in %.3fs (standard/project pricing may apply).",
                    purpose,
                    tier,
                    wait,
                )
                sleep(wait)
                # extra_body wins over named SDK fields, so remove a legacy Flex value.
                fallback_options = {
                    **options,
                    "service_tier": tier,
                    "extra_body": {
                        k: v for k, v in config.extra_body.items() if k != "service_tier"
                    },
                }
                return client.chat.completions.create(
                    model=config.model, messages=messages, **fallback_options
                ), tier
            log.warning("Retrying %s on Flex in %.3fs", purpose, wait)
            sleep(wait)
            delay = min(config.flex_max_backoff_seconds, delay * 2)
    raise AssertionError("Flex attempt budget must be positive")


def generate_json(
    client: OpenAI,
    config: LLMConfig,
    work: Path,
    messages: list[dict],
    schema: type[T],
    purpose: str,
    validate: Callable[[T], None] | None = None,
) -> T:
    """Book-specific schema/coverage repair with an optional Flex scheduling policy."""
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
        requested_tier = config.service_tier or config.extra_body.get("service_tier")
        try:
            if requested_tier == "flex":
                response, requested_tier = _flex_completion(
                    client, config, conversation, options, purpose
                )
            else:
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
            request_service_tier=requested_tier,
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
