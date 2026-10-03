import json
import shutil

import pytest
from conftest import Reply, wave_bytes

from technical_audiobook.chapters import BoundaryDecision
from technical_audiobook.cli import main
from technical_audiobook.config import Config, LLMConfig, TTSConfig
from technical_audiobook.models import Draft, Review
from technical_audiobook.pipeline import convert, preflight_output, synthesize_transcript


@pytest.mark.parametrize("book_fixture", ["pdf_book", "epub_book"])
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("document_type", ["book", "paper"])
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_end_to_end_book_to_m4b(
    request, book_fixture, workers, document_type, workspace, tmp_path, sdk_server
):
    book = request.getfixturevalue(book_fixture)
    calls = {"chat": 0, "speech": 0}
    speech_inputs = []

    def handle(req):
        payload = json.loads(req.content)
        if req.path.endswith("speech"):
            calls["speech"] += 1
            speech_inputs.append(payload["input"])
            return Reply(wave_bytes())
        calls["chat"] += 1
        if "You assess a proposed chapter boundary" in payload["messages"][0]["content"]:
            evidence = [
                json.loads(p["text"])
                for p in payload["messages"][1]["content"]
                if p["type"] == "text" and p["text"].startswith("{")
            ]
            candidate = next(item for item in evidence if item["role"].startswith("CANDIDATE"))
            result = BoundaryDecision(
                source_id=candidate["source_id"],
                independent=True,
                title="Second chapter",
                reason="New chapter begins here.",
            ).model_dump()
        elif "source-grounded editor" in payload["messages"][0]["content"]:
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
        return Reply(
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]},
        )

    base_url = sdk_server(handle)
    config = Config(
        llm=LLMConfig(model="test-vision", base_url=base_url),
        tts=TTSConfig(model="test-tts", base_url=base_url),
    )
    config.narration.workers = workers
    config.narration.document_type = document_type
    output = tmp_path / f"{book_fixture}.m4b"
    assert convert(book, output, workspace, config) == output
    assert output.stat().st_size > 0
    assert calls["chat"] >= 2 and calls["speech"] >= 2
    transcript = json.loads((workspace / "narration.json").read_text())
    source = json.loads((workspace / "source.json").read_text())
    assert len(transcript["coverage"]) == len(source["units"])
    if workers == 2:
        chapter_plan = json.loads((workspace / "chapter-plan.json").read_text())
        assert chapter_plan["workers"] == 2
        assert len(chapter_plan["jobs"]) == 2
    before = calls.copy()
    config.narration.workers = 1
    config.llm.service_tier = "flex"
    convert(book, output, workspace, config)
    assert calls == before
    text_path = workspace / "narration.txt"
    original_json = (workspace / "narration.json").read_bytes()
    edited = text_path.read_text().replace(
        "The signal carries information.", "This is my corrected explanation.", 1
    )
    text_path.write_text(edited)
    assert convert(book, output, workspace, config, until="narrate") == text_path
    assert text_path.read_text() == edited
    assert (workspace / "narration.json").read_bytes() == original_json
    assert calls == before
    # Both convert and synthesize use the edited text, without any new LLM calls.
    convert(book, output, workspace, config, force=True)
    assert calls == {"chat": before["chat"], "speech": before["speech"] + 1}
    assert "This is my corrected explanation." in speech_inputs[-1]
    after_edit = calls.copy()
    synthesize_transcript(text_path, output, config)
    assert calls == after_edit
    # An explicitly chosen JSON input retains the old behavior and old audio cache.
    synthesize_transcript(workspace / "narration.json", output, config, force=True)
    assert calls == after_edit
    assert text_path.read_text() == edited
    config.book.title = "A different generated baseline"
    with pytest.raises(
        ValueError, match="Neither narration.txt nor narration.json was overwritten"
    ):
        convert(book, output, workspace, config, force=True)
    assert text_path.read_text() == edited
    assert (workspace / "narration.json").read_bytes() == original_json
    assert calls == after_edit


def test_offline_cli_extract(pdf_book, tmp_path, capsys, monkeypatch):
    # The CLI discovers audiobook.toml and .env in its working directory. Do not
    # apply a user's real book/page selection to this synthetic three-page PDF.
    monkeypatch.chdir(tmp_path)
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
