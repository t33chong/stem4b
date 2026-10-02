"""Cached speech, uniform PCM clips, and one AAC encode with sample-based chapters."""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .api import SpeechClient
from .chunking import split_speech
from .config import Config
from .models import Transcript
from .narration_text import SpeechScript
from .storage import asset_path, atomic_text, digest, file_digest, read_json, write_json

log = logging.getLogger(__name__)
AUDIO_VERSION = 1


@dataclass
class SpeechPart:
    text: str
    pause_ms: int = 0
    key: str = ""
    path: Path | None = None
    frames: int = 0


@dataclass
class AudioChapter:
    title: str
    parts: list[SpeechPart] = field(default_factory=list)


def require_ffmpeg():
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise ValueError(
                f"{binary} is required for audio output. Install FFmpeg and add it to PATH."
            )


def run_media(command: list[str]):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"{command[0]} failed: {result.stderr[-2500:]}")
    return result.stdout


def pronounce(text: str, glossary: dict[str, str]) -> str:
    if not glossary:
        return text
    # Simultaneous, case-sensitive substitution avoids replacing inside identifiers
    # or recursively rewriting a replacement that contains another glossary entry.
    pattern = (
        r"(?<!\w)(?:"
        + "|".join(re.escape(key) for key in sorted(glossary, key=len, reverse=True))
        + r")(?!\w)"
    )
    return re.sub(pattern, lambda match: glossary[match.group()], text)


def speech_plan(transcript: Transcript | SpeechScript, config: Config) -> list[AudioChapter]:
    chapters: list[AudioChapter] = []
    current = AudioChapter(transcript.title)
    paragraphs: list[str] = []

    def flush():
        if paragraphs:
            combined = pronounce("\n\n".join(paragraphs), config.pronunciations)
            current.parts.extend(
                SpeechPart(text) for text in split_speech(combined, config.tts.max_chars)
            )
            paragraphs.clear()

    def finish_chapter():
        flush()
        if current.parts:
            current.parts[-1].pause_ms += config.audio.chapter_pause_ms
            chapters.append(current)

    for segment in transcript.segments:
        if segment.continues_previous:
            raise ValueError("Transcript still contains an unresolved paragraph continuation")
        if segment.kind == "heading":
            flush()
            if segment.heading_level <= config.audio.toc_depth:
                finish_chapter()
                current = AudioChapter(segment.display_title)
            heading = pronounce(segment.text, config.pronunciations)
            pieces = [SpeechPart(text) for text in split_speech(heading, config.tts.max_chars)]
            pieces[-1].pause_ms = config.audio.heading_pause_ms
            current.parts.extend(pieces)
        else:
            paragraphs.append(segment.text)
    finish_chapter()
    if not chapters:
        raise ValueError("No speech could be produced from this transcript")
    for chapter in chapters:
        for part in chapter.parts:
            part.key = digest(
                [
                    AUDIO_VERSION,
                    part.text,
                    part.pause_ms,
                    config.tts.model_dump(
                        exclude={"workers", "timeout_seconds", "retries", "api_key_env"}
                    ),
                    config.audio.sample_rate,
                ]
            )
    return chapters


def wav_frames(path: Path, sample_rate: int) -> int:
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or audio.getframerate() != sample_rate
        ):
            raise ValueError("Cached audio does not have the expected PCM format")
        frames = audio.getnframes()
        if frames <= 0:
            raise ValueError("Speech clip contains no audio frames")
        # Validate the data chunk too; a truncated WAV header can advertise missing samples.
        read_frames = 0
        while data := audio.readframes(65536):
            read_frames += len(data) // 2
        if read_frames != frames:
            raise ValueError("Speech clip is truncated")
        return frames


def synthesize_part(
    part: SpeechPart, client: SpeechClient, config: Config, work: Path
) -> tuple[Path, int]:
    directory = work / "audio"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{part.key}.wav"
    receipt = directory / f"{part.key}.json"
    if target.exists() and receipt.exists():
        try:
            cached = read_json(receipt)
            if cached["sha256"] == file_digest(target):
                return target, wav_frames(target, config.audio.sample_rate)
        except (ValueError, OSError, KeyError, EOFError, wave.Error):
            pass
    # Save successful API responses before decoding. A packaging/normalization failure
    # should not require another paid speech request.
    raw = directory / f"{part.key}.source.{config.tts.response_format}"
    if not raw.exists() or raw.stat().st_size == 0:
        client.synthesize(part.text, raw)
    with tempfile.TemporaryDirectory(prefix=".normalize-", dir=directory) as temporary:
        normalized = Path(temporary) / "clip.wav"
        command = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
        if config.tts.response_format == "pcm":
            command += ["-f", "s16le", "-ar", str(config.tts.pcm_sample_rate), "-ac", "1"]
        command += [
            "-i",
            str(raw),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(config.audio.sample_rate),
            "-c:a",
            "pcm_s16le",
            str(normalized),
        ]
        try:
            run_media(command)
            frames = wav_frames(normalized, config.audio.sample_rate)
        except (ValueError, OSError, EOFError, wave.Error) as exc:
            # Retain bad payload for inspection but allow the next run to request a replacement.
            os.replace(raw, raw.with_suffix(raw.suffix + ".invalid"))
            raise ValueError(
                f"Invalid speech audio for clip {part.key[:12]}; response saved as .invalid"
            ) from exc
        pause_frames = round(part.pause_ms * config.audio.sample_rate / 1000)
        if pause_frames:
            padded = Path(temporary) / "padded.wav"
            with wave.open(str(normalized), "rb") as source, wave.open(str(padded), "wb") as dest:
                dest.setparams(source.getparams())
                while data := source.readframes(65536):
                    dest.writeframesraw(data)
                dest.writeframesraw(b"\0\0" * pause_frames)
            os.replace(padded, normalized)
            frames += pause_frames
        os.replace(normalized, target)
    write_json(
        receipt,
        {"sha256": file_digest(target), "frames": frames, "sample_rate": config.audio.sample_rate},
    )
    # The normalized, verified clip is sufficient for resume. Avoid storing audio twice.
    raw.unlink(missing_ok=True)
    return target, frames


def synthesize(chapters: list[AudioChapter], client: SpeechClient, config: Config, work: Path):
    require_ffmpeg()
    unique = {part.key: part for chapter in chapters for part in chapter.parts}
    completed = {}
    with ThreadPoolExecutor(max_workers=config.tts.workers) as executor:
        futures = {
            executor.submit(synthesize_part, part, client, config, work): key
            for key, part in unique.items()
        }
        try:
            for index, future in enumerate(as_completed(futures), 1):
                completed[futures[future]] = future.result()
                log.info("Speech clip %s/%s ready", index, len(unique))
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    for chapter in chapters:
        for part in chapter.parts:
            part.path, part.frames = completed[part.key]
    write_json(
        work / "audio.json",
        [
            {
                "title": chapter.title,
                "parts": [
                    {
                        "key": part.key,
                        "path": str(part.path.relative_to(work)),
                        "frames": part.frames,
                    }
                    for part in chapter.parts
                ],
            }
            for chapter in chapters
        ],
    )


def escape_metadata(value: str) -> str:
    value = value.replace("\r", "").replace("\\", "\\\\")
    for character in ("=", ";", "#", "\n"):
        value = value.replace(character, "\\" + character)
    return value


def ffmetadata(
    transcript: Transcript | SpeechScript, chapters: list[AudioChapter], sample_rate: int
) -> str:
    lines = [
        ";FFMETADATA1",
        f"title={escape_metadata(transcript.title)}",
        f"album={escape_metadata(transcript.title)}",
        f"artist={escape_metadata(transcript.author)}",
        "genre=Audiobook",
        "media_type=2",
        "comment=AI-generated narration",
    ]
    cursor = 0
    for chapter in chapters:
        end = cursor + sum(part.frames for part in chapter.parts)
        if end <= cursor:
            raise ValueError("A chapter has no audio duration")
        lines += [
            "[CHAPTER]",
            f"TIMEBASE=1/{sample_rate}",
            f"START={cursor}",
            f"END={end}",
            f"title={escape_metadata(chapter.title)}",
        ]
        cursor = end
    return "\n".join(lines) + "\n"


def package_signature(
    title: str, author: str, chapters: list[AudioChapter], config: Config, cover_sha256: str | None
) -> str:
    return digest(
        [
            AUDIO_VERSION,
            title,
            author,
            config.audio.model_dump(),
            [(c.title, [(p.key, p.frames) for p in c.parts]) for c in chapters],
            cover_sha256,
        ]
    )


def package(
    transcript: Transcript | SpeechScript,
    chapters: list[AudioChapter],
    config: Config,
    work: Path,
    output: Path,
    force: bool = False,
) -> Path:
    require_ffmpeg()
    if output.suffix.lower() != ".m4b":
        raise ValueError("Audiobook output must have the .m4b extension")
    cover = (
        Path(config.book.cover)
        if config.book.cover
        else (asset_path(work, transcript.cover.path) if transcript.cover else None)
    )
    if cover and not cover.is_file():
        raise ValueError(f"Cover image not found: {cover}")
    signature = package_signature(
        transcript.title,
        transcript.author,
        chapters,
        config,
        file_digest(cover) if cover else None,
    )
    receipt = work / "output.json"
    if output.exists():
        if receipt.exists():
            cached = read_json(receipt)
            if cached.get("signature") == signature and cached.get("sha256") == file_digest(output):
                log.info("Reusing completed audiobook %s", output)
                return output
        if not force:
            raise ValueError(
                f"Output already exists: {output}. Use --force to replace it, or choose a new path."
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    # Put the concat list beside clips: its entries contain only generated hex filenames,
    # so quotes, Unicode and spaces in the user's workspace never enter the concat grammar.
    listing = work / "audio" / "concat.txt"
    atomic_text(
        listing, "\n".join(f"file '{part.path.name}'" for c in chapters for part in c.parts) + "\n"
    )
    metadata = work / "chapters.ffmeta"
    atomic_text(metadata, ffmetadata(transcript, chapters, config.audio.sample_rate))
    fd, name = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=".m4b", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(listing),
            "-f",
            "ffmetadata",
            "-i",
            str(metadata),
        ]
        if cover:
            command += ["-i", str(cover)]
        command += [
            "-map",
            "0:a:0",
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            config.audio.bitrate,
        ]
        if cover:
            command += ["-map", "2:v:0", "-c:v", "png", "-disposition:v:0", "attached_pic"]
        command += ["-movflags", "+faststart", "-f", "ipod", str(temporary)]
        log.info("Encoding %s chapters to %s", len(chapters), output)
        run_media(command)
        probe = json.loads(
            run_media(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-show_chapters",
                    "-of",
                    "json",
                    str(temporary),
                ]
            )
        )
        if not any(s.get("codec_name") == "aac" for s in probe.get("streams", [])):
            raise ValueError("Final audiobook has no AAC audio stream")
        if cover and not any(
            s.get("disposition", {}).get("attached_pic") for s in probe.get("streams", [])
        ):
            raise ValueError("Final audiobook is missing its requested cover artwork")
        expected_duration = (
            sum(p.frames for c in chapters for p in c.parts) / config.audio.sample_rate
        )
        actual_duration = float(probe["format"]["duration"])
        if abs(actual_duration - expected_duration) > 0.25:
            raise ValueError("Final audiobook duration does not match the complete speech plan")
        found_chapters = probe.get("chapters", [])
        if len(found_chapters) != len(chapters):
            raise ValueError("Final audiobook is missing chapter metadata")
        cursor = 0
        for expected, actual in zip(chapters, found_chapters, strict=True):
            if abs(float(actual["start_time"]) - cursor / config.audio.sample_rate) > 0.02:
                raise ValueError("Final audiobook chapter timing is inconsistent")
            cursor += sum(p.frames for p in expected.parts)
        os.replace(temporary, output)
        write_json(
            receipt,
            {
                "signature": signature,
                "sha256": file_digest(output),
                "path": str(output),
                "duration_seconds": actual_duration,
                "chapters": found_chapters,
            },
        )
    finally:
        temporary.unlink(missing_ok=True)
    return output
