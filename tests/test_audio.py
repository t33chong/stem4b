import json
import shutil

import pytest
from conftest import Reply, wave_bytes
from openai import OpenAI

from stem4b.api import client_options
from stem4b.audio import (
    ffmetadata,
    package,
    pronounce,
    run_media,
    speech_plan,
    synthesize,
)
from stem4b.chunking import split_speech
from stem4b.config import AudioConfig, Config, TTSConfig
from stem4b.models import Segment, Transcript


def transcript():
    return Transcript(
        title="Test #1; A=B",
        author="An Author",
        source_sha256="abc",
        coverage=[],
        segments=[
            Segment(
                kind="heading",
                text="Chapter one. Signals.",
                display_title="1. Signals",
                heading_level=1,
                source_ids=["one"],
            ),
            Segment(
                kind="paragraph",
                text="NumPy processes signals. A signal carries information.",
                source_ids=["one"],
            ),
            Segment(
                kind="heading",
                text="Chapter two. Information.",
                display_title="2. Information",
                heading_level=1,
                source_ids=["two"],
            ),
            Segment(kind="paragraph", text="Entropy measures uncertainty.", source_ids=["two"]),
        ],
    )


@pytest.mark.parametrize(
    "text",
    [
        "First sentence. Second sentence is longer.\n\nAnother paragraph.",
        "z" * 201,
        "αβγ δέζη. " * 40,
        "no punctuation " * 30,
    ],
)
def test_speech_chunking_hard_limit_without_losing_nonwhitespace(text):
    chunks = split_speech(text, 32)
    assert all(0 < len(chunk) <= 32 for chunk in chunks)
    assert "".join("".join(chunks).split()) == "".join(text.split())


def test_pronunciations_and_chapter_speech():
    assert pronounce("SQL SQLAlchemy", {"SQL": "sequel"}) == "sequel SQLAlchemy"
    config = Config(pronunciations={"NumPy": "numb pie"}, tts=TTSConfig(max_chars=32))
    chapters = speech_plan(transcript(), config)
    assert [c.title for c in chapters] == ["1. Signals", "2. Information"]
    assert any("numb pie" in p.text for c in chapters for p in c.parts)
    assert all(len(p.text) <= 32 for c in chapters for p in c.parts)
    assert chapters[0].parts[0].pause_ms == config.audio.heading_pause_ms


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_real_m4b_chapters_cover_duration_resume_and_cache_invalidation(
    workspace, tmp_path, sdk_server
):
    from PIL import Image

    cover = tmp_path / "cover.png"
    Image.new("RGB", (64, 64), "red").save(cover)
    config = Config(
        tts=TTSConfig(model="test-tts", workers=2),
        audio=AudioConfig(heading_pause_ms=80, chapter_pause_ms=150),
    )
    config.book.cover = str(cover)
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return Reply(wave_bytes())

    config.tts.base_url = sdk_server(handle)
    client = OpenAI(**client_options(config.tts))
    book = transcript()
    plan = speech_plan(book, config)
    try:
        synthesize(plan, client, config, workspace)
        assert len(calls) == 4
        expected_frames = sum(p.frames for c in plan for p in c.parts)
        output = tmp_path / "Reader's finished book.m4b"
        package(book, plan, config, workspace, output)
        details = json.loads(
            run_media(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_chapters",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(output),
                ]
            )
        )
        assert details["format"]["tags"]["title"] == book.title
        assert [c["tags"]["title"] for c in details["chapters"]] == ["1. Signals", "2. Information"]
        assert any(s.get("disposition", {}).get("attached_pic") for s in details["streams"])
        assert (
            abs(float(details["format"]["duration"]) - expected_frames / config.audio.sample_rate)
            < 0.1
        )
        original_mtime = output.stat().st_mtime_ns
        resumed = speech_plan(book, config)
        synthesize(resumed, client, config, workspace)
        package(book, resumed, config, workspace, output)
        assert len(calls) == 4
        assert output.stat().st_mtime_ns == original_mtime
        # A changed paragraph only invalidates its own clip, not an entire chapter.
        book.segments[1].text += " This is additional detail."
        changed = speech_plan(book, config)
        synthesize(changed, client, config, workspace)
        assert len(calls) == 5
        with pytest.raises(ValueError, match="already exists"):
            package(book, changed, config, workspace, output)
        package(book, changed, config, workspace, output, force=True)
    finally:
        client.close()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_invalid_audio_never_becomes_silence(workspace, sdk_server):
    config = Config(tts=TTSConfig(model="tts", workers=1))
    config.tts.base_url = sdk_server(lambda _: Reply(b"this is not audio"))
    client = OpenAI(**client_options(config.tts))
    plan = speech_plan(transcript(), config)
    try:
        with pytest.raises(ValueError, match="Invalid speech audio"):
            synthesize(plan, client, config, workspace)
    finally:
        client.close()
    assert not list((workspace / "audio").glob("*.wav"))
    assert list((workspace / "audio").glob("*.invalid"))


def test_metadata_sample_timebase_and_escaping():
    config = Config()
    plan = speech_plan(transcript(), config)
    for chapter in plan:
        for part in chapter.parts:
            part.frames = 1001
    metadata = ffmetadata(transcript(), plan, 24000)
    assert "TIMEBASE=1/24000" in metadata
    assert "title=Test \\#1\\; A\\=B" in metadata
    assert "START=2002" in metadata
