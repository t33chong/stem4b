import logging
from pathlib import Path

from filelock import FileLock, Timeout

from .api import LLMClient, SpeechClient
from .audio import package, require_ffmpeg, speech_plan, synthesize
from .chapters import chapter_candidates
from .chunking import plan_chunks
from .config import Config
from .extract import extract_book, save_image
from .models import Transcript
from .narration import Narrator
from .storage import read_json, write_json

log = logging.getLogger(__name__)


def default_work(output: Path) -> Path:
    return output.with_suffix(".work")


def preflight_output(output: Path, work: Path, force: bool):
    if output.suffix.lower() != ".m4b":
        raise ValueError("Output must have the .m4b extension")
    if output.is_dir():
        raise ValueError(f"Output is a directory: {output}")
    if output.exists() and not force:
        receipt = work / "output.json"
        if not receipt.exists() or read_json(receipt).get("path") != str(output):
            raise ValueError(f"Output already exists: {output}. Use --force or choose a new path.")


def render_transcript(
    transcript: Transcript,
    config: Config,
    work: Path,
    output: Path,
    until: str = "m4b",
    force: bool = False,
) -> Path:
    preflight_output(output, work, force)
    config.tts.require_model()
    require_ffmpeg()
    if config.book.cover and not Path(config.book.cover).is_file():
        raise ValueError(f"Cover image not found: {config.book.cover}")
    if config.book.title:
        transcript.title = config.book.title
    if config.book.author:
        transcript.author = config.book.author
    plan = speech_plan(transcript, config)
    client = SpeechClient(config.tts, work)
    try:
        synthesize(plan, client, config, work)
    finally:
        client.close()
    if until == "audio":
        return work / "audio.json"
    return package(transcript, plan, config, work, output, force)


def convert(
    source: Path, output: Path, work: Path, config: Config, until: str = "m4b", force: bool = False
) -> Path:
    source, output, work = source.resolve(), output.resolve(), work.resolve()
    if not source.is_file():
        raise ValueError(f"Book not found: {source}")
    if until not in {"extract", "narrate", "audio", "m4b"}:
        raise ValueError("Unknown pipeline stage")
    preflight_output(output, work, force)
    if until != "extract":
        config.llm.require_model()
    if until in {"audio", "m4b"}:
        config.tts.require_model()
        require_ffmpeg()
    if config.book.cover and not Path(config.book.cover).is_file():
        raise ValueError(f"Cover image not found: {config.book.cover}")
    work.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(work / ".pipeline.lock", timeout=0):
            log.info("Extracting %s", source.name)
            book = extract_book(source, work, config.extraction)
            book.title = config.book.title or book.title
            book.author = config.book.author or book.author
            if config.book.cover:
                cover_path = Path(config.book.cover)
                book.cover = save_image(
                    cover_path.read_bytes(),
                    "Book cover",
                    work,
                    config.extraction.image_max_dimension,
                    cover_path.suffix.lower() == ".svg",
                )
            chunks = plan_chunks(book, config.narration)
            candidates = chapter_candidates(chunks)
            write_json(
                work / "plan.json",
                {
                    "title": book.title,
                    "author": book.author,
                    "source_units": len(book.units),
                    "initial_chunks": len(chunks),
                    "source_characters": sum(len(u.text) for u in book.units),
                    "source_images": sum(len(u.images) for u in book.units),
                    "baseline_llm_requests": len(chunks) * (2 if config.narration.review else 1),
                    "narration_workers": config.narration.workers,
                    "chapter_boundary_requests_if_uncached": (
                        len(candidates) if config.narration.workers > 1 else 0
                    ),
                    "chapter_candidates": [
                        {"chunk_id": c.id, "source_id": c.units[0].id, "title": c.units[0].heading}
                        for c in candidates
                    ],
                    "warnings": book.warnings,
                    "chunks": [{"id": c.id, "source_ids": [u.id for u in c.units]} for c in chunks],
                },
            )
            log.info("Prepared %s source units in %s sections", len(book.units), len(chunks))
            for warning in book.warnings:
                log.warning("%s", warning)
            if until == "extract":
                return work / "plan.json"
            client = LLMClient(config.llm, work)
            try:
                transcript = Narrator(client, config, work).narrate(book, chunks)
            finally:
                client.close()
            if until == "narrate":
                return work / "narration.json"
            return render_transcript(transcript, config, work, output, until, force)
    except Timeout as exc:
        raise ValueError(f"Another process is using this workspace: {work}") from exc


def synthesize_transcript(
    transcript_file: Path, output: Path, config: Config, force: bool = False
) -> Path:
    transcript_file, output = transcript_file.resolve(), output.resolve()
    work = transcript_file.parent
    try:
        with FileLock(work / ".pipeline.lock", timeout=0):
            transcript = Transcript.model_validate(read_json(transcript_file))
            return render_transcript(transcript, config, work, output, force=force)
    except Timeout as exc:
        raise ValueError(f"Another process is using this workspace: {work}") from exc
