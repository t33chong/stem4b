"""Offline artwork repair: keep encoded audio, chapters, and narration intact."""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from filelock import FileLock, Timeout

from .audio import AudioChapter, SpeechPart, package_signature, require_ffmpeg, run_media
from .config import Config
from .extract import extract_cover, save_image
from .storage import asset_path, file_digest, read_json, write_json

log = logging.getLogger(__name__)


def probe_media(path: Path) -> dict:
    return json.loads(
        run_media(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-show_chapters",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def audio_hash(path: Path) -> str:
    # Hash encoded packet payloads, not decoded PCM: -c copy guarantees no re-encode.
    return run_media(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:a",
            "-c",
            "copy",
            "-f",
            "streamhash",
            "-hash",
            "sha256",
            "-",
        ]
    ).strip()


def verify_cover_repair(before: dict, after: dict):
    if not any(s.get("disposition", {}).get("attached_pic") for s in after["streams"]):
        raise ValueError("Repaired audiobook is missing cover artwork")

    def audio_streams(probe):
        return [
            tuple(s.get(key) for key in ("codec_name", "sample_rate", "channels", "start_time"))
            for s in probe["streams"]
            if s.get("codec_type") == "audio"
        ]

    if not audio_streams(before) or audio_streams(before) != audio_streams(after):
        raise ValueError("Cover repair changed the audio stream properties")
    if abs(float(before["format"]["duration"]) - float(after["format"]["duration"])) > 0.01:
        raise ValueError("Cover repair changed the audiobook duration")
    old_chapters, new_chapters = before.get("chapters", []), after.get("chapters", [])
    if len(old_chapters) != len(new_chapters):
        raise ValueError("Cover repair changed the chapter count")
    for old, new in zip(old_chapters, new_chapters, strict=True):
        if old.get("tags", {}) != new.get("tags", {}) or any(
            abs(float(old[key]) - float(new[key])) > 0.002 for key in ("start_time", "end_time")
        ):
            raise ValueError("Cover repair changed chapter titles or timing")
    for key, value in before["format"].get("tags", {}).items():
        if key not in {"major_brand", "minor_version", "compatible_brands", "encoder"}:
            if after["format"].get("tags", {}).get(key) != value:
                raise ValueError(f"Cover repair changed audiobook metadata: {key}")


def repair_cover(source: Path, output: Path, work: Path, config: Config) -> Path:
    """Repair a workspace's completed M4B, without invoking any LLM or TTS client."""
    source, output, work = source.resolve(), output.resolve(), work.resolve()
    if not source.is_file() or not output.is_file() or output.suffix.lower() != ".m4b":
        raise ValueError("Cover repair requires an existing source book and .m4b output")
    if not work.is_dir():
        raise ValueError("Cover repair requires the original conversion workspace")
    require_ffmpeg()
    try:
        with FileLock(work / ".pipeline.lock", timeout=0):
            return _repair_cover(source, output, work, config)
    except Timeout as exc:
        raise ValueError(f"Another process is using this workspace: {work}") from exc


def _repair_cover(source: Path, output: Path, work: Path, config: Config) -> Path:
    receipt_file = work / "output.json"
    source_file = work / "source.json"
    if not receipt_file.is_file() or not source_file.is_file():
        raise ValueError("Cover repair requires source.json and output.json from the original run")
    receipt, book = read_json(receipt_file), read_json(source_file)
    source_sha256 = file_digest(source)
    if book.get("source_sha256") != source_sha256:
        raise ValueError("Source book does not match the conversion workspace")
    old_sha256 = file_digest(output)
    if receipt.get("path") != str(output) or receipt.get("sha256") != old_sha256:
        raise ValueError(
            "Audiobook does not match output.json; refusing to replace an unrelated file"
        )
    metadata = [(source_file, book)]
    narration_file = work / "narration.json"
    if narration_file.is_file():
        narration = read_json(narration_file)
        if narration.get("source_sha256") != source_sha256:
            raise ValueError("Narration does not match the source book")
        metadata.append((narration_file, narration))
    old_cover = metadata[-1][1].get("cover")
    if config.book.cover:
        override = Path(config.book.cover)
        cover = save_image(
            override.read_bytes(),
            "Book cover",
            work,
            config.extraction.image_max_dimension,
            override.suffix.lower() == ".svg",
        )
    else:
        cover = extract_cover(source, work, config.extraction)
    if cover is None:
        raise ValueError("No declared cover found in the source; set book.cover to an image file")
    if receipt.get("cover_sha256") == cover.sha256:
        # Also finish a metadata update interrupted after installing the repaired M4B.
        for path, data in metadata:
            if data.get("cover") != cover.model_dump():
                write_json(path, {**data, "cover": cover.model_dump()})
        log.info("Reusing audiobook with verified cover: %s", output)
        return output
    backup = output.with_name(f"{output.stem}.before-cover.m4b")
    if backup.exists() or backup.with_suffix(".output.json").exists():
        raise ValueError(
            f"Cover backup already exists: {backup}. Move it aside before another repair."
        )
    before = probe_media(output)
    original_audio = audio_hash(output)
    signature = None
    audio_file = work / "audio.json"
    if audio_file.is_file():
        chapters = [
            AudioChapter(
                c["title"],
                [SpeechPart(text="", key=p["key"], frames=p["frames"]) for p in c["parts"]],
            )
            for c in read_json(audio_file)
        ]
        tags = before["format"].get("tags", {})
        title, author = tags.get("title", ""), tags.get("artist", "")
        prior_signature = package_signature(
            title, author, chapters, config, old_cover["sha256"] if old_cover else None
        )
        if prior_signature == receipt.get("signature"):
            signature = package_signature(title, author, chapters, config, cover.sha256)
    if signature is None:
        log.warning(
            "Packaging settings differ from the saved run; invalidating only the M4B receipt signature"
        )
    fd, name = tempfile.mkstemp(prefix=f".{output.stem}.cover-", suffix=".m4b", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        log.info("Attaching cover without re-encoding audio: %s", output)
        run_media(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(output),
                "-i",
                str(asset_path(work, cover.path)),
                "-map",
                "0:a",
                "-map",
                "1:v:0",
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-c:a",
                "copy",
                "-c:v",
                "png",
                "-disposition:v:0",
                "attached_pic",
                "-movflags",
                "+faststart",
                "-f",
                "ipod",
                str(temporary),
            ]
        )
        after = probe_media(temporary)
        verify_cover_repair(before, after)
        if audio_hash(temporary) != original_audio:
            raise ValueError("Cover repair changed encoded audio; original audiobook was retained")
        new_sha256 = file_digest(temporary)
        shutil.copy2(output, backup)
        # Preserve the old receipt alongside the original file for manual recovery.
        write_json(backup.with_suffix(".output.json"), receipt)
        os.replace(temporary, output)
        write_json(
            receipt_file,
            {
                **receipt,
                "signature": signature,
                "sha256": new_sha256,
                "cover_sha256": cover.sha256,
                "chapters": after.get("chapters", []),
                "duration_seconds": float(after["format"]["duration"]),
                "cover_repair": {
                    "backup": str(backup),
                    "previous_sha256": old_sha256,
                    "encoded_audio_hash": original_audio,
                },
            },
        )
        for path, data in metadata:
            write_json(path, {**data, "cover": cover.model_dump()})
        log.info("Cover repaired; original audiobook retained at %s", backup)
        return output
    finally:
        temporary.unlink(missing_ok=True)
