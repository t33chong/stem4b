"""Minimal, provider-neutral Chat Completions and speech transports."""

import json
import logging
import os
import random
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Endpoint, LLMConfig, TTSConfig

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class APIError(RuntimeError):
    pass


class TruncatedResponse(APIError):
    pass


class APIClient:
    def __init__(self, config: Endpoint, work: Path, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.work = work
        headers = {"Authorization": f"Bearer {config.api_key()}"} if config.api_key() else {}
        self.http = httpx.Client(
            base_url=config.base_url + "/",
            headers=headers,
            timeout=config.timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )
        self._log_lock = threading.Lock()

    def close(self):
        self.http.close()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None and (header := response.headers.get("retry-after")):
            try:
                seconds = float(header)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError):
                    seconds = 0
            if seconds > 0:
                return min(seconds, 60)
        return min(2**attempt + random.random(), 30)

    def _request(self, path: str, body: dict, destination: Path | None = None):
        for attempt in range(self.config.retries + 1):
            response = None
            try:
                with self.http.stream("POST", path, json=body) as response:
                    if response.status_code in {408, 409, 429} or response.status_code >= 500:
                        if attempt < self.config.retries:
                            log.warning(
                                "API returned HTTP %s; retry %s/%s",
                                response.status_code,
                                attempt + 1,
                                self.config.retries,
                            )
                            time.sleep(self._retry_delay(response, attempt))
                            continue
                    if not response.is_success:
                        # Provider error bodies can echo private content or credentials.
                        raise APIError(
                            f"{path}: HTTP {response.status_code}. Check the endpoint, model, credentials "
                            "and supported request parameters. Provider response body was not logged."
                        )
                    if destination is None:
                        try:
                            return json.loads(response.read())
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise APIError(f"{path}: endpoint did not return JSON") from exc
                    content_type = response.headers.get("content-type", "").lower()
                    if any(kind in content_type for kind in ("json", "text/", "html")):
                        raise APIError("Speech endpoint returned text/JSON instead of audio bytes")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, name = tempfile.mkstemp(prefix=".speech-", dir=destination.parent)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            for data in response.iter_bytes():
                                stream.write(data)
                        if Path(name).stat().st_size == 0:
                            raise APIError("Speech endpoint returned empty audio")
                        os.replace(name, destination)
                    finally:
                        Path(name).unlink(missing_ok=True)
                    return None
            except httpx.TransportError as exc:
                if attempt == self.config.retries:
                    raise APIError(
                        f"{path}: transport failed after {attempt + 1} attempts ({type(exc).__name__})"
                    ) from exc
                log.warning("API transport failure; retry %s/%s", attempt + 1, self.config.retries)
                time.sleep(self._retry_delay(response, attempt))
        raise APIError("API retries exhausted")

    def record(self, purpose: str, usage: dict | None = None):
        record = {
            "time": datetime.now(UTC).isoformat(),
            "purpose": purpose,
            "model": self.config.model,
            "usage": usage or {},
        }
        self.work.mkdir(parents=True, exist_ok=True)
        with self._log_lock, (self.work / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")


class LLMClient(APIClient):
    config: LLMConfig

    def generate(
        self,
        messages: list[dict],
        schema: type[T],
        purpose: str,
        validate: Callable[[T], None] | None = None,
    ) -> T:
        self.config.require_model()
        instructions = "\nReturn JSON only, conforming to this schema:\n" + json.dumps(
            schema.model_json_schema()
        )
        conversation = [dict(message) for message in messages]
        conversation[0] = {**conversation[0], "content": conversation[0]["content"] + instructions}
        # Two bounded schema/coverage repair attempts; HTTP retries are separate.
        for attempt in range(3):
            body = {
                "model": self.config.model,
                "messages": conversation,
                self.config.token_parameter: self.config.max_output_tokens,
                **self.config.extra_body,
            }
            if self.config.json_mode == "json_object":
                body["response_format"] = {"type": "json_object"}
            if self.config.temperature is not None:
                body["temperature"] = self.config.temperature
            response = self._request("chat/completions", body)
            if not isinstance(response, dict):
                raise APIError("Chat endpoint returned a JSON value instead of a completion object")
            self.record(purpose, response.get("usage"))
            try:
                choice = response["choices"][0]
                message = choice["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise APIError("Chat endpoint returned no completion choice") from exc
            if choice.get("finish_reason") == "length":
                raise TruncatedResponse("Model output reached the token limit")
            if message.get("refusal") or choice.get("finish_reason") not in {None, "stop"}:
                raise APIError(
                    "Model refused or did not complete the narration; no audio was substituted"
                )
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise APIError("Chat endpoint returned an empty or non-text completion")
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
                    raise APIError(
                        f"Invalid {schema.__name__} after three attempts: {exc}"
                    ) from exc
                conversation.extend(
                    [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": f"Repair the JSON and coverage errors below. Return the complete corrected object, without dropping content:\n{exc}",
                        },
                    ]
                )
        raise APIError("Model response validation failed")


class SpeechClient(APIClient):
    config: TTSConfig

    def synthesize(self, text: str, destination: Path):
        self.config.require_model()
        if not text.strip() or len(text) > self.config.max_chars:
            raise ValueError("Speech input is empty or exceeds tts.max_chars")
        body = {
            "model": self.config.model,
            "voice": self.config.voice,
            "input": text,
            "response_format": self.config.response_format,
            **self.config.extra_body,
        }
        if self.config.instructions:
            body["instructions"] = self.config.instructions
        self._request("audio/speech", body, destination)
        self.record("speech", {"characters": len(text)})
