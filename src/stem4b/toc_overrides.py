"""Book-bound navigation choices and offline final-pass inputs, not narration caches."""

from pathlib import Path
from shlex import join as shell_join
from typing import Literal

from pydantic import Field, model_validator

from .config import Config
from .models import Book, Model, Transcript
from .storage import atomic_bytes, digest, file_digest, read_json, write_json


class TocCorrection(Model):
    entry: int = Field(ge=1, description="One-based index in the original source TOC")
    action: Literal["omit", "destination", "match"]
    source_id: str | None = None
    heading_id: str | None = None
    level: int | None = Field(default=None, ge=1, le=6)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "omit":
            if self.source_id or self.heading_id or self.level:
                raise ValueError("An omitted navigation entry cannot also specify a destination")
        elif not self.source_id:
            raise ValueError("A corrected destination needs a source_id")
        if self.action == "match" and not self.heading_id:
            raise ValueError("Matching a heading needs its saved heading_id")
        if self.action != "match" and (self.heading_id or self.level):
            raise ValueError("Heading/level choices require action=match")
        return self


class TocCorrections(Model):
    version: Literal[1] = 1
    source_sha256: str
    source_toc_sha256: str
    entries: list[TocCorrection] = Field(default_factory=list)


def corrections_for(book: Book) -> TocCorrections:
    return TocCorrections(source_sha256=book.source_sha256, source_toc_sha256=digest(book.toc))


def load_corrections(work: Path, book: Book) -> dict[int, TocCorrection]:
    path = work / "toc-overrides.json"
    if not path.exists():
        return {}
    try:
        saved = TocCorrections.model_validate(read_json(path))
        if saved.source_sha256 != book.source_sha256 or saved.source_toc_sha256 != digest(book.toc):
            raise ValueError("Corrections belong to a different source or source TOC")
        result = {}
        ids = {u.id for u in book.units}
        for item in saved.entries:
            index = item.entry - 1
            if index >= len(book.toc.entries) or index in result:
                raise ValueError("Corrections contain an unknown or duplicate TOC entry")
            if item.action != "omit" and item.source_id not in ids:
                raise ValueError(f"Correction {item.entry} points to an unknown source unit")
            result[index] = item
        return result
    except ValueError as exc:
        raise ValueError(
            f"Invalid {path}: {exc}. Use {shell_join(['stem4b', 'repair-toc', str(work), '--reset'])} "
            "to back up and clear these corrections, then review again. Narration is unaffected."
        ) from exc


def save_corrections(work: Path, book: Book, choices: dict[int, TocCorrection]):
    path = work / "toc-overrides.json"
    if path.exists():
        atomic_bytes(work / "toc-override-history" / f"{file_digest(path)}.json", path.read_bytes())
    saved = corrections_for(book)
    saved.entries = [choices[index] for index in sorted(choices)]
    write_json(path, saved)


def save_toc_input(
    work: Path,
    book: Book,
    config: Config,
    transcript: Transcript | None = None,
    omitted_front_ids: set[str] | None = None,
):
    # Only final-pass settings are needed. Never serialize credentials or provider settings.
    write_json(
        work / "toc-input.json",
        {
            "version": 1,
            "source_sha256": book.source_sha256,
            "source_toc_sha256": digest(book.toc),
            "config": {
                "navigation": config.navigation.model_dump(),
                "narration": {
                    "document_type": config.narration.document_type,
                    "include_exercises": config.narration.include_exercises,
                },
                "audio": config.audio.model_dump(),
                "tts": {"max_chars": config.tts.max_chars},
                "pronunciations": config.pronunciations,
            },
            "transcript": transcript.model_dump() if transcript else None,
            "omitted_front_source_ids": sorted(omitted_front_ids or set()),
        },
    )
