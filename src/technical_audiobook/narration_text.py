"""Editable speech scripts, separate from the source-grounded narration baseline."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Asset, Transcript
from .storage import atomic_text, read_json, write_json

log = logging.getLogger(__name__)
FORMAT = "Format: audiobook-text-v1"
HEADING = re.compile(r"^(#{1,6}) (\S.*)$")


@dataclass
class SpokenSegment:
    # Deliberately has no source_ids: edited text has not passed source review.
    kind: str
    text: str
    display_title: str = ""
    heading_level: int = 0
    continues_previous: bool = False


@dataclass
class SpeechScript:
    title: str
    author: str
    segments: list[SpokenSegment]
    cover: Asset | None = None


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _unescape(value: str) -> str:
    result = []
    index = 0
    escapes = {"\\": "\\", "r": "\r", "n": "\n", "#": "#"}
    while index < len(value):
        character = value[index]
        if character == "\\":
            index += 1
            if index == len(value) or value[index] not in escapes:
                raise ValueError("Invalid text escape; write a literal backslash as \\\\")
            character = escapes[value[index]]
        result.append(character)
        index += 1
    return "".join(result)


def _spoken_lines(value: str, heading: bool = False) -> str:
    lines = []
    for line in value.split("\n"):
        escaped = _escape(line)
        if escaped.startswith("#") or (heading and not line.strip()):
            escaped = "\\" + escaped
        lines.append(escaped)
    return "\n".join(lines)


def legacy_text(transcript: Transcript) -> str:
    """The original read-only export format, for migration without losing edits."""
    parts = [f"Title: {transcript.title}\nAuthor: {transcript.author}"]
    for segment in transcript.segments:
        if segment.kind == "heading":
            parts.append(f"{'#' * segment.heading_level} {segment.display_title}\n{segment.text}")
        else:
            parts.append(segment.text)
    return "\n\n".join(parts) + "\n"


def render_text(transcript: Transcript) -> str:
    parts = [f"Title: {_escape(transcript.title)}\nAuthor: {_escape(transcript.author)}\n{FORMAT}"]
    for segment in transcript.segments:
        if segment.continues_previous:
            raise ValueError("Cannot export an unresolved paragraph continuation")
        if segment.kind == "heading":
            parts.append(
                f"{'#' * segment.heading_level} {_escape(segment.display_title)}\n"
                + _spoken_lines(segment.text, heading=True)
            )
        else:
            parts.append(_spoken_lines(segment.text))
    return "\n\n".join(parts) + "\n"


def _normalize(text: str) -> str:
    # Editor BOM/line-ending changes do not count as content edits.
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def parse_text(text: str) -> SpeechScript:
    """Parse heading blocks and prose without rewriting, reviewing or attributing it."""
    lines = _normalize(text).split("\n")
    if len(lines) < 3 or not lines[0].startswith("Title: ") or not lines[1].startswith("Author: "):
        raise ValueError("Narration text must start with Title: and Author: metadata lines")
    escaped = lines[2] == FORMAT
    start = 3 if escaped else 2
    if len(lines) <= start or lines[start].strip():
        raise ValueError("Expected a blank line after narration metadata (or unsupported Format)")

    def decode(value: str, line_number: int) -> str:
        try:
            return _unescape(value) if escaped else value
        except ValueError as exc:
            raise ValueError(f"Line {line_number}: {exc}") from exc

    title, author = decode(lines[0][7:], 1), decode(lines[1][8:], 2)
    if not title.strip() or not author.strip():
        raise ValueError("Narration title and author must not be empty")
    segments = []
    prose = []

    def spoken(line: str, line_number: int) -> str:
        if escaped and line.startswith("\\") and not line[1:].strip():
            return line[1:]  # An escaped blank line inside a spoken heading.
        return decode(line, line_number)

    def flush():
        text = "\n".join(prose).strip()
        if text:
            segments.append(SpokenSegment("paragraph", text))
        prose.clear()

    index = start + 1
    while index < len(lines):
        line = lines[index]
        match = HEADING.fullmatch(line)
        if line.startswith("#") and not match:
            raise ValueError(
                f"Line {index + 1}: headings need 1–6 # characters, a space and a title"
            )
        if match:
            flush()
            heading_line = index + 1
            heading_text = []
            index += 1
            while index < len(lines) and lines[index].strip():
                if lines[index].startswith("#"):
                    raise ValueError(f"Line {index + 1}: missing blank line between heading blocks")
                heading_text.append(spoken(lines[index], index + 1))
                index += 1
            text = "\n".join(heading_text).strip()
            if not text:
                raise ValueError(f"Line {heading_line}: heading needs spoken text on the next line")
            title_text = decode(match[2], heading_line)
            if not title_text.strip():
                raise ValueError(f"Line {heading_line}: heading display title must not be empty")
            segments.append(SpokenSegment("heading", text, title_text, len(match[1])))
        else:
            prose.append(spoken(line, index + 1))
            index += 1
    flush()
    if not segments:
        raise ValueError("Narration text contains no speech")
    return SpeechScript(title, author, segments)


def load_script(path: Path) -> SpeechScript | Transcript:
    if path.suffix.lower() == ".json":
        log.info("Using explicit JSON speech input; sibling text edits are not applied")
        return Transcript.model_validate(read_json(path))
    if path.suffix.lower() != ".txt":
        raise ValueError("Speech input must be a narration .txt or .json file")
    text = path.read_text(encoding="utf-8-sig")
    baseline_path = path.with_suffix(".json")
    baseline = (
        Transcript.model_validate(read_json(baseline_path)) if baseline_path.exists() else None
    )
    if baseline and _normalize(text) == _normalize(legacy_text(baseline)):
        # Old exports had no escaping. An exact match is safe even if their heading
        # or paragraph text contains literal '#' lines or embedded blank lines.
        script = SpeechScript(
            baseline.title,
            baseline.author,
            [
                SpokenSegment(
                    s.kind, s.text, s.display_title, s.heading_level, s.continues_previous
                )
                for s in baseline.segments
            ],
        )
    else:
        script = parse_text(text)
    if baseline:
        script.cover = baseline.cover
    log.info("Using editable speech text from %s (manual edits are not source-reviewed)", path)
    return script


def save_narration(transcript: Transcript, work: Path, *, stem: str = "narration"):
    """Publish a generated baseline without overwriting the user's editable script."""
    if Path(stem).name != stem or stem in {"", ".", ".."}:
        raise ValueError("Narration filename must be a simple stem inside the workspace")
    json_path, text_path = work / f"{stem}.json", work / f"{stem}.txt"
    generated = render_text(transcript)
    baseline = Transcript.model_validate(read_json(json_path)) if json_path.exists() else None
    if text_path.exists():
        existing = _normalize(text_path.read_text(encoding="utf-8-sig"))
        unedited = existing in {_normalize(generated), _normalize(legacy_text(transcript))}
        if baseline:
            unedited = unedited or existing in {
                _normalize(render_text(baseline)),
                _normalize(legacy_text(baseline)),
            }
        if not unedited:
            if baseline == transcript:
                log.info("Preserving edited %s; generated baseline is unchanged", text_path.name)
                return
            if baseline and baseline.model_dump(exclude={"cover"}) == transcript.model_dump(
                exclude={"cover"}
            ):
                log.info("Updating cover metadata while preserving edited %s", text_path.name)
                write_json(json_path, transcript)
                return
            raise ValueError(
                f"{text_path.name} contains edits and the generated baseline has changed or is missing. "
                f"Neither {text_path.name} nor {json_path.name} was overwritten. To keep your text, use "
                f"synthesize {text_path.name}. To use the new narration, move the edited text aside "
                "and rerun convert; accepted narration checkpoints remain cached."
            )
    # Text first: after an interruption it can be recognized as this generated export,
    # even if the old baseline JSON is still present. --force never bypasses edit protection.
    atomic_text(text_path, generated)
    write_json(json_path, transcript)
