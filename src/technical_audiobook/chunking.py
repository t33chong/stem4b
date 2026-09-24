import re
from dataclasses import dataclass

from .config import NarrationConfig
from .models import Book, SourceUnit


@dataclass
class Chunk:
    id: str
    units: list[SourceUnit]
    before: SourceUnit | None = None
    after: SourceUnit | None = None


def plan_chunks(book: Book, config: NarrationConfig) -> list[Chunk]:
    groups: list[list[SourceUnit]] = []
    current: list[SourceUnit] = []
    chars = images = 0
    # Reserve two image slots for the preceding/following source units.
    image_budget = config.max_images - 2
    for unit in book.units:
        if len(unit.text) > config.max_source_chars:
            raise ValueError(
                f"{unit.location} has {len(unit.text)} characters, exceeding narration.max_source_chars. "
                "Raise that limit for this book; source text is never silently truncated."
            )
        if len(unit.images) > image_budget:
            raise ValueError(
                f"{unit.location} exceeds the image budget; raise narration.max_images"
            )
        full = current and (
            chars + len(unit.text) > config.max_source_chars
            or images + len(unit.images) > image_budget
            or (book.format == "pdf" and len(current) >= config.max_pdf_pages)
            or (unit.heading and unit.heading_level <= 2)
        )
        if full:
            groups.append(current)
            current, chars, images = [], 0, 0
        current.append(unit)
        chars += len(unit.text)
        images += len(unit.images)
    if current:
        groups.append(current)
    positions = {unit.id: index for index, unit in enumerate(book.units)}
    chunks = []
    for index, group in enumerate(groups):
        first, last = positions[group[0].id], positions[group[-1].id]
        chunks.append(
            Chunk(
                id=f"{index + 1:05d}",
                units=group,
                before=book.units[first - 1] if first else None,
                after=book.units[last + 1] if last + 1 < len(book.units) else None,
            )
        )
    return chunks


def split_speech(text: str, limit: int) -> list[str]:
    """Prefer paragraphs, then sentences, then words; always enforce the API limit."""
    remaining = text.strip()
    chunks = []
    while len(remaining) > limit:
        prefix = remaining[: limit + 1]
        boundaries = [m.start() for m in re.finditer(r"\n\s*\n", prefix)]
        if not boundaries:
            boundaries = [m.end() for m in re.finditer(r"[.!?][\"'’”)]*\s+", prefix)]
        boundaries = [n for n in boundaries if 0 < n <= limit]
        if not boundaries:
            boundaries = [m.start() for m in re.finditer(r"\s+", prefix) if m.start() > 0]
        cut = max(boundaries, default=limit)
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks
