import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .models import Model


class Endpoint(Model):
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key_env: str
    timeout_seconds: float = Field(default=300, gt=0)
    retries: int = Field(default=3, ge=0, le=10)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an http(s) API root, usually ending in /v1")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Keep credentials in the configured environment variable, not the URL")
        return value.rstrip("/")

    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")

    def require_model(self):
        if not self.model.strip() or self.model.startswith("your-"):
            raise ValueError(
                "Set a model in [llm] / [tts] in your configuration "
                "(or LLM_MODEL / TTS_MODEL). Run 'stem4b init' for an example configuration."
            )


class LLMConfig(Endpoint):
    api_key_env: str = "LLM_API_KEY"
    service_tier: str | None = Field(default=None, min_length=1)
    flex_max_attempts: int = Field(default=6, ge=1, le=1000)
    flex_initial_backoff_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    flex_max_backoff_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    flex_fallback_to_standard: bool = False
    flex_fallback_service_tier: Literal["auto", "default"] = "auto"
    max_output_tokens: int = Field(default=16000, ge=256)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    json_mode: Literal["json_object", "prompt"] = "json_object"
    temperature: float | None = Field(default=None, ge=0, le=2)

    def cache_options(self) -> dict:
        # Processing tier affects scheduling/pricing, not accepted narration content.
        # Omitting it also preserves the exact keys of pre-SDK checkpoints.
        return self.model_dump(
            exclude={
                "service_tier",
                "flex_max_attempts",
                "flex_initial_backoff_seconds",
                "flex_max_backoff_seconds",
                "flex_fallback_to_standard",
                "flex_fallback_service_tier",
            }
        )

    @model_validator(mode="after")
    def reserved_options(self):
        reserved = {
            "model",
            "messages",
            "stream",
            "n",
            "response_format",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
        }
        if reserved.intersection(self.extra_body):
            raise ValueError("llm.extra_body cannot override core request fields")
        if self.service_tier is not None and "service_tier" in self.extra_body:
            raise ValueError("Set service_tier in llm or llm.extra_body, not both")
        if self.flex_max_backoff_seconds < self.flex_initial_backoff_seconds:
            raise ValueError(
                "flex_max_backoff_seconds must be at least flex_initial_backoff_seconds"
            )
        return self


class TTSConfig(Endpoint):
    api_key_env: str = "TTS_API_KEY"
    voice: str = "alloy"
    response_format: Literal["wav", "mp3", "flac", "opus", "aac", "pcm"] = "wav"
    pcm_sample_rate: int = Field(default=24000, ge=8000, le=192000)
    instructions: str | None = None
    max_chars: int = Field(default=2500, ge=32)
    workers: int = Field(default=4, ge=1, le=32)

    def require_model(self):
        super().require_model()
        if not self.voice.strip() or self.voice.startswith("your-"):
            raise ValueError(
                "Set tts.voice in your configuration (or TTS_VOICE) to a supported voice."
            )

    @model_validator(mode="after")
    def reserved_options(self):
        reserved = {
            "model",
            "input",
            "voice",
            "response_format",
            "instructions",
            "stream",
            "stream_format",
        }
        if reserved.intersection(self.extra_body):
            raise ValueError("tts.extra_body cannot override core request fields")
        return self


class ExtractionConfig(Model):
    pdf_dpi: int = Field(default=144, ge=72, le=400)
    image_max_dimension: int = Field(default=2000, ge=512, le=4096)
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    include_nonlinear_epub: bool = False

    @model_validator(mode="after")
    def page_order(self):
        if self.start_page and self.end_page and self.end_page < self.start_page:
            raise ValueError("end_page must be at least start_page")
        return self


class NarrationConfig(Model):
    document_type: Literal["book", "paper"] = "book"
    workers: int = Field(default=1, ge=1, le=32)
    max_pdf_pages: int = Field(default=3, ge=1, le=20)
    max_source_chars: int = Field(default=18000, ge=1000)
    max_images: int = Field(default=10, ge=3, le=100)
    context_chars: int = Field(default=4000, ge=0)
    review: bool = True
    max_revisions: int = Field(default=2, ge=0, le=50)
    include_exercises: bool = False
    instructions_file: str | None = None

    def cache_options(self) -> dict:
        excluded = {"max_revisions", "workers"}
        if self.document_type == "book":
            # Keep existing book checkpoint identities byte-for-byte compatible.
            excluded.add("document_type")
        return self.model_dump(exclude=excluded)


class NavigationConfig(Model):
    reconcile: bool = True
    # PDF navigation labels omit "CHAPTER"; EPUB labels retain it when supplied.
    chapter_prefix: Literal["auto", "keep", "omit"] = "auto"
    omit_front_matter: bool = True


class AudioConfig(Model):
    sample_rate: int = Field(default=24000, ge=8000, le=48000)
    bitrate: str = "64k"
    heading_pause_ms: int = Field(default=800, ge=0, le=10000)
    chapter_pause_ms: int = Field(default=1500, ge=0, le=10000)
    toc_depth: int = Field(default=3, ge=1, le=6)

    @field_validator("bitrate")
    @classmethod
    def valid_bitrate(cls, value):
        if not re.fullmatch(r"[1-9]\d*k?", value):
            raise ValueError("Use an ffmpeg bitrate such as 64k or 96000")
        return value


class BookConfig(Model):
    title: str | None = None
    author: str | None = None
    cover: str | None = None


class Config(Model):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    narration: NarrationConfig = Field(default_factory=NarrationConfig)
    navigation: NavigationConfig = Field(default_factory=NavigationConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    book: BookConfig = Field(default_factory=BookConfig)
    pronunciations: dict[str, str] = Field(default_factory=dict)

    @field_validator("pronunciations")
    @classmethod
    def nonempty_pronunciations(cls, value):
        if any(not key.strip() or not val.strip() for key, val in value.items()):
            raise ValueError("Pronunciation spellings and replacements must not be empty")
        return value


def load_config(path: Path | None = None) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path else {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML configuration in {path}: {exc}") from exc
    for section in ("llm", "tts"):
        if section in data and not isinstance(data[section], dict):
            raise ValueError(f"{section} must be a TOML table, written as [{section}]")
        for name in (
            ("base_url", "model", "voice")
            if section == "tts"
            else ("base_url", "model", "service_tier")
        ):
            value = os.getenv(f"{section}_{name}".upper())
            if value:
                data.setdefault(section, {})[name] = value
    config = Config.model_validate(data)
    # Relative file references are relative to the config, not the shell's working directory.
    root = path.resolve().parent if path else Path.cwd()
    if config.narration.instructions_file:
        config.narration.instructions_file = str(
            (root / config.narration.instructions_file).resolve()
        )
    if config.book.cover:
        config.book.cover = str((root / config.book.cover).resolve())
    return config
