import io
import json
import shutil
from zipfile import ZipFile

import httpx
import pytest
from conftest import wave_bytes
from PIL import Image

from technical_audiobook.api import SpeechClient
from technical_audiobook.audio import package, speech_plan, synthesize
from technical_audiobook.cli import main
from technical_audiobook.config import Config, ExtractionConfig, TTSConfig
from technical_audiobook.covers import audio_hash, probe_media, repair_cover, verify_cover_repair
from technical_audiobook.extract import EXTRACT_VERSION, extract_book, extract_cover
from technical_audiobook.models import Coverage, Segment, Transcript
from technical_audiobook.narration_text import load_script, save_narration
from technical_audiobook.storage import digest, file_digest, read_json, write_json


def cover_epub(tmp_path, declaration):
    target = tmp_path / "cover book.epub"
    metadata, guide, properties = "", "", ""
    if declaration == "properties":
        properties = ' properties="cover-image"'
    elif declaration in {"id", "path", "page-id", "broken-id", "external", "traversal"}:
        reference = {
            "id": "art",
            "path": "Images/cover%20art.png",
            "page-id": "cover-page",
            "broken-id": "nonexistent",
            "external": "https://invalid.example/cover.png",
            "traversal": "../../outside.png",
        }[declaration]
        metadata = f'<meta name="cover" content="{reference}"/>'
    if declaration in {"guide", "svg-guide", "broken-id", "external", "traversal"}:
        guide = '<guide><reference type="cover" href="cover.xhtml#artwork"/></guide>'
    image = io.BytesIO()
    Image.new("RGB", (80, 120), "red").save(image, "PNG")
    visual = '<img id="artwork" src="Images/cover%20art.png"/>'
    if declaration == "svg-guide":
        visual = """<svg id="artwork" xmlns="http://www.w3.org/2000/svg"
          width="80" height="120" viewBox="0 0 80 120">
          <image href="Images/cover%20art.png" width="80" height="120"/>
          <rect width="20" height="20" fill="blue"/></svg>"""
    with ZipFile(target, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OPS/book.opf",
            f'''<package xmlns="http://www.idpf.org/2007/opf">
          <metadata><title>Cover test</title>{metadata}</metadata><manifest>
          <item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>
          <item id="art" href="Images/cover%20art.png" media-type="image/png"{properties}/>
          <item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine><itemref idref="cover-page" linear="no"/><itemref idref="body"/></spine>
          {guide}</package>''',
        )
        archive.writestr("OPS/cover.xhtml", f"<html><body>{visual}</body></html>")
        archive.writestr(
            "OPS/body.xhtml", "<html><body><h1>Signals</h1><p>Useful content.</p></body></html>"
        )
        archive.writestr("OPS/Images/cover art.png", image.getvalue())
    return target


@pytest.mark.parametrize(
    "declaration",
    [
        "properties",
        "id",
        "path",
        "page-id",
        "guide",
        "svg-guide",
        "broken-id",
        "external",
        "traversal",
    ],
)
def test_epub_cover_declarations_and_safe_fallbacks(tmp_path, workspace, declaration):
    source = cover_epub(tmp_path, declaration)
    book = extract_book(source, workspace, ExtractionConfig())
    assert book.cover == extract_cover(source, workspace, ExtractionConfig())
    assert all("cover.xhtml" not in unit.location for unit in book.units)
    with Image.open(workspace / book.cover.path) as artwork:
        assert artwork.getpixel((40, 60)) == (255, 0, 0)
        if declaration == "svg-guide":
            assert artwork.getpixel((5, 5)) == (0, 0, 255)


def test_epub_without_a_cover_declaration_does_not_guess(tmp_path, workspace):
    source = cover_epub(tmp_path, "none")
    assert extract_cover(source, workspace, ExtractionConfig()) is None


@pytest.mark.parametrize("kind", ["pdf", "epub"])
def test_missing_cover_cache_migrates_without_reextracting_units(
    kind, pdf_book, tmp_path, workspace, monkeypatch
):
    source = pdf_book if kind == "pdf" else cover_epub(tmp_path, "path")
    config = ExtractionConfig(start_page=2, end_page=2) if kind == "pdf" else ExtractionConfig()
    original = extract_book(source, workspace, config)
    key = digest([EXTRACT_VERSION, file_digest(source), config.model_dump()])
    cache = workspace / "source" / f"{key}.json"
    legacy = original.model_copy(deep=True)
    legacy.cover = None
    write_json(cache, legacy)

    def fail(*args):
        raise AssertionError("Cover migration must not re-extract the narration source")

    monkeypatch.setattr(f"technical_audiobook.extract.extract_{kind}", fail)
    restored = extract_book(source, workspace, config)
    assert restored == original
    assert read_json(cache)["cover"] == original.cover.model_dump()
    # A missing separate cover asset also should not rerender selected PDF pages.
    (workspace / restored.cover.path).unlink()
    assert extract_book(source, workspace, config) == original


def test_cover_only_metadata_refresh_preserves_edited_speech(pdf_book, workspace):
    book = extract_book(pdf_book, workspace, ExtractionConfig(start_page=2, end_page=2))
    transcript = Transcript(
        title=book.title,
        author=book.author,
        source_sha256=book.source_sha256,
        segments=[Segment(kind="paragraph", text="Generated speech.", source_ids=["p00002"])],
        coverage=[Coverage(source_id="p00002", disposition="narrated")],
    )
    save_narration(transcript, workspace)
    text = workspace / "narration.txt"
    edited = text.read_text().replace("Generated speech.", "My edited speech.")
    text.write_text(edited)
    transcript.cover = book.cover
    save_narration(transcript, workspace)
    assert text.read_text() == edited
    assert load_script(text).cover == book.cover
    assert read_json(workspace / "narration.json")["segments"] == [
        s.model_dump() for s in transcript.segments
    ]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_offline_cover_repair_preserves_audio_chapters_edits_and_resume(
    pdf_book, workspace, tmp_path, monkeypatch
):
    config = Config(tts=TTSConfig(model="test", workers=1))
    book = extract_book(pdf_book, workspace, ExtractionConfig(start_page=2, end_page=2))
    book.cover = None  # Legacy selected-page extraction.
    write_json(workspace / "source.json", book)
    transcript = Transcript(
        title=book.title,
        author=book.author,
        source_sha256=book.source_sha256,
        segments=[
            Segment(
                kind="heading",
                text="Chapter one.",
                source_ids=["p00002"],
                display_title="Chapter one",
                heading_level=1,
            ),
            Segment(kind="paragraph", text="Useful content.", source_ids=["p00002"]),
        ],
        coverage=[Coverage(source_id="p00002", disposition="narrated")],
    )
    save_narration(transcript, workspace)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=wave_bytes(), headers={"content-type": "audio/wav"})

    client = SpeechClient(config.tts, workspace, httpx.MockTransport(handle))
    plan = speech_plan(transcript, config)
    try:
        synthesize(plan, client, config, workspace)
    finally:
        client.close()
    output = tmp_path / "book.m4b"
    package(transcript, plan, config, workspace, output)
    original_bytes = output.read_bytes()
    original_probe = probe_media(output)
    original_receipt = read_json(workspace / "output.json")
    original_audio = audio_hash(output)
    with monkeypatch.context() as patch:
        patch.setattr(
            "technical_audiobook.covers.audio_hash",
            lambda path: original_audio if path.resolve() == output.resolve() else "changed audio",
        )
        with pytest.raises(ValueError, match="changed encoded audio"):
            repair_cover(pdf_book, output, workspace, config)
    assert output.read_bytes() == original_bytes
    assert read_json(workspace / "output.json") == original_receipt
    assert not output.with_name("book.before-cover.m4b").exists()
    assert not list(tmp_path.glob(".book.cover-*.m4b"))
    text_path = workspace / "narration.txt"
    text_path.write_text(text_path.read_text().replace("Useful content.", "User's future edit."))
    edited = text_path.read_bytes()
    source_units = read_json(workspace / "source.json")["units"]

    # The command neither loads model credentials nor creates a speech/model client.
    def unexpected(*args, **kwargs):
        raise AssertionError("Cover repair must remain offline")

    monkeypatch.setattr("technical_audiobook.pipeline.LLMClient", unexpected)
    monkeypatch.setattr("technical_audiobook.pipeline.SpeechClient", unexpected)
    monkeypatch.chdir(tmp_path)
    assert (
        main(["repair-cover", str(pdf_book), "--work-dir", str(workspace), "-o", str(output)]) == 0
    )
    assert output.with_name("book.before-cover.m4b").read_bytes() == original_bytes
    assert read_json(output.with_name("book.before-cover.output.json")) == original_receipt
    verify_cover_repair(original_probe, probe_media(output))
    assert audio_hash(output) == original_audio
    assert text_path.read_bytes() == edited
    assert read_json(workspace / "source.json")["units"] == source_units
    assert load_script(text_path).cover is not None
    saved_mtime = output.stat().st_mtime_ns
    repair_cover(pdf_book, output, workspace, config)
    assert output.stat().st_mtime_ns == saved_mtime
    # Cover-only repair updates the packaging signature, preserving ordinary resume.
    transcript.cover = load_script(text_path).cover
    package(transcript, plan, config, workspace, output)
    assert output.stat().st_mtime_ns == saved_mtime
    # Unrelated files or source books must not be overwritten.
    write_json(
        workspace / "output.json", {**read_json(workspace / "output.json"), "sha256": "wrong"}
    )
    with pytest.raises(ValueError, match="unrelated"):
        repair_cover(pdf_book, output, workspace, config)
    assert output.stat().st_mtime_ns == saved_mtime


@pytest.mark.parametrize("failure", ["cover", "duration", "chapter", "metadata", "audio"])
def test_cover_repair_validation_detects_lost_content(failure):
    before = {
        "streams": [
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "24000", "channels": 1}
        ],
        "format": {"duration": "10.0", "tags": {"title": "Book"}},
        "chapters": [{"start_time": "0", "end_time": "10", "tags": {"title": "Chapter 1"}}],
    }
    after = json.loads(json.dumps(before))
    after["streams"].append({"disposition": {"attached_pic": 1}})
    if failure == "cover":
        after["streams"].pop()
    elif failure == "duration":
        after["format"]["duration"] = "9.0"
    elif failure == "chapter":
        after["chapters"][0]["tags"]["title"] = "Wrong"
    elif failure == "metadata":
        after["format"]["tags"]["title"] = "Wrong"
    else:
        after["streams"][0]["sample_rate"] = "48000"
    with pytest.raises(ValueError):
        verify_cover_repair(before, after)
