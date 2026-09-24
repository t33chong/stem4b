import json
import shutil

import httpx
import pytest
from conftest import wave_bytes

from technical_audiobook.api import LLMClient, SpeechClient
from technical_audiobook.cli import main
from technical_audiobook.config import Config, LLMConfig, TTSConfig
from technical_audiobook.models import Draft, Review
from technical_audiobook.pipeline import convert, preflight_output


@pytest.mark.parametrize("book_fixture", ["pdf_book", "epub_book"])
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_end_to_end_book_to_m4b(request, book_fixture, workspace, tmp_path, monkeypatch):
    book = request.getfixturevalue(book_fixture)
    calls = {"chat": 0, "speech": 0}

    def handle(req):
        payload = json.loads(req.content)
        if req.url.path.endswith("speech"):
            calls["speech"] += 1
            return httpx.Response(200, content=wave_bytes(), headers={"content-type": "audio/wav"})
        calls["chat"] += 1
        if "source-grounded editor" in payload["messages"][0]["content"]:
            result = Review(approved=True).model_dump()
        else:
            units = []
            for part in payload["messages"][1]["content"]:
                if part["type"] == "text" and part["text"].startswith("{"):
                    unit = json.loads(part["text"])
                    if unit["role"] == "PRIMARY":
                        units.append(unit)
            result = {
                "segments": [
                    {
                        "kind": "heading",
                        "text": f"Section {calls['chat']}.",
                        "display_title": f"Section {calls['chat']}",
                        "heading_level": 1,
                        "source_ids": [units[0]["source_id"]],
                    },
                    {
                        "kind": "paragraph",
                        "text": "The signal carries information. The figure illustrates its amplitude.",
                        "source_ids": [u["source_id"] for u in units],
                    },
                ],
                "coverage": [
                    {"source_id": u["source_id"], "disposition": "narrated"} for u in units
                ],
            }
            Draft.model_validate(result)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]
            },
        )

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        "technical_audiobook.pipeline.LLMClient",
        lambda conf, work: LLMClient(conf, work, transport),
    )
    monkeypatch.setattr(
        "technical_audiobook.pipeline.SpeechClient",
        lambda conf, work: SpeechClient(conf, work, transport),
    )
    config = Config(llm=LLMConfig(model="test-vision"), tts=TTSConfig(model="test-tts"))
    output = tmp_path / f"{book_fixture}.m4b"
    assert convert(book, output, workspace, config) == output
    assert output.stat().st_size > 0
    assert calls["chat"] >= 2 and calls["speech"] >= 2
    transcript = json.loads((workspace / "narration.json").read_text())
    source = json.loads((workspace / "source.json").read_text())
    assert len(transcript["coverage"]) == len(source["units"])
    before = calls.copy()
    convert(book, output, workspace, config)
    assert calls == before


def test_offline_cli_extract(pdf_book, tmp_path, capsys):
    output = tmp_path / "book.m4b"
    assert main(["convert", str(pdf_book), "-o", str(output), "--until", "extract"]) == 0
    assert (tmp_path / "book.work" / "plan.json").exists()
    assert not output.exists()
    assert "plan.json" in capsys.readouterr().out


def test_missing_model_fails_before_processing(pdf_book, tmp_path):
    with pytest.raises(ValueError, match="Set a model"):
        convert(pdf_book, tmp_path / "out.m4b", tmp_path / "work", Config())
    assert not (tmp_path / "work").exists()


def test_existing_unrelated_output_is_rejected_before_processing(pdf_book, tmp_path):
    output = tmp_path / "existing.m4b"
    output.write_bytes(b"existing user file")
    with pytest.raises(ValueError, match="already exists"):
        convert(pdf_book, output, tmp_path / "work", Config())
    assert output.read_bytes() == b"existing user file"
    assert not (tmp_path / "work").exists()
    with pytest.raises(ValueError, match="extension"):
        preflight_output(tmp_path / "out.mp3", tmp_path / "work", False)
