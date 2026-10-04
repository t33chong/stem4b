"""Offline first-run setup and diagnostics; never contact model providers."""

import os
import shutil
from importlib.resources import files
from pathlib import Path

from .config import Config


def initialize(directory: Path) -> list[Path]:
    directory = directory.resolve()
    templates = {"stem4b.toml": "stem4b.toml", ".env": "example.env"}
    targets = [directory / name for name in templates]
    existing = [target for target in targets if target.exists() or target.is_symlink()]
    if existing:
        raise ValueError(
            "Nothing written. Setup files already exist: "
            + ", ".join(str(target) for target in existing)
            + ". Edit them, or run 'stem4b init NEW_DIRECTORY' to create fresh examples."
        )
    directory.mkdir(parents=True, exist_ok=True)
    for target, template in zip(targets, templates.values(), strict=True):
        content = files("stem4b").joinpath("templates", template).read_text(encoding="utf-8")
        # Exclusive creation protects an existing file even if another process just created it.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
    return targets


def diagnose(config: Config, stage: str = "m4b") -> tuple[list[str], bool]:
    """Return human-readable checks and success, without printing secrets or making requests."""
    messages = ["Offline checks only; model availability, credentials and quotas are not verified."]
    valid = True
    endpoints = []
    if stage != "extract":
        endpoints.append(("llm", config.llm))
    if stage == "m4b":
        endpoints.append(("tts", config.tts))
    for name, endpoint in endpoints:
        try:
            endpoint.require_model()
        except ValueError as exc:
            messages.append(f"FAIL {name}: {exc}")
            valid = False
        else:
            messages.append(f"OK   {name}: model configured")
        if endpoint.api_key():
            messages.append(f"OK   {name}: {endpoint.api_key_env} is set (value hidden)")
        else:
            messages.append(
                f"WARN {name}: {endpoint.api_key_env} is unset; this works only for an endpoint "
                "that does not require authentication."
            )
    if stage == "m4b":
        for executable in ("ffmpeg", "ffprobe"):
            if shutil.which(executable):
                messages.append(f"OK   {executable}: found on PATH")
            else:
                messages.append(f"FAIL {executable}: install FFmpeg or use the Docker image")
                valid = False
    required_files = [("book.cover", config.book.cover)]
    if stage != "extract":
        required_files.append(("narration.instructions_file", config.narration.instructions_file))
    for label, filename in required_files:
        if filename and not Path(filename).is_file():
            messages.append(f"FAIL {label}: file not found: {filename}")
            valid = False
    messages.append(
        "Ready for a trial conversion." if valid else "Fix the FAIL items and rerun doctor."
    )
    return messages, valid
